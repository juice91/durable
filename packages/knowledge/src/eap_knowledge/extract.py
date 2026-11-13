"""Extraction of Markdown, HTML and plain text into heading sections."""

from __future__ import annotations

import re
import unicodedata
import html.parser as _stdlib_html_parser
from html.parser import HTMLParser

from markdown_it import MarkdownIt

from eap_knowledge.errors import ExtractionFailed, UnsupportedMimeType
from eap_knowledge.model import Document, Section
from eap_knowledge.slug import SlugRegistry, slugify

_FORMATS = {
    "text/markdown": "markdown",
    "text/x-markdown": "markdown",
    "text/md": "markdown",
    "text/html": "html",
    "application/xhtml+xml": "html",
    "text/plain": "text",
}

# A line of YAML front matter: a comment, a list item, an indented continuation, a blank line or a
# `key:` (plain or quoted key, no spaces in a plain key).
_YAML_LINE = re.compile(r"""(?:|#.*|-(?:[ \t].*)?|[ \t]+.*|(?:[A-Za-z0-9_][\w.-]*|"[^"]*"|'[^']*')[ \t]*:(?:[ \t].*)?)""")
_MD_DROPPED_BLOCK = re.compile(r"[ \t]*<(?:script|style|noscript|template)\b", re.I)

# html.parser builds a list entry per attribute, so the memory a start tag costs grows with its
# attribute count (not with value length): a tag with more attributes than this is rejected.
MAX_HTML_ATTRIBUTES = 1024
_BLANK_RUNS = re.compile(r"\n{3,}")
_WS = re.compile(r"\s+")


def document_format(mime_type: str) -> str:
    """``"markdown"``, ``"html"`` or ``"text"`` for a mime type (parameters ignored)."""
    base = mime_type.split(";", 1)[0].strip().lower()
    try:
        return _FORMATS[base]
    except KeyError:
        raise UnsupportedMimeType(mime_type) from None


def extract(content: str, mime_type: str, *, title: str = "", source_url: str | None = None) -> Document:
    """Split ``content`` into sections at headings, keeping each section's heading path.

    The content is NFC-normalised and its newlines unified first, so the same document saved by
    different editors chunks identically. HTML without a ``title`` argument takes its ``<title>``.
    """
    fmt = document_format(mime_type)
    content = _normalize(content)
    title = _collapse(_normalize(title))
    if fmt == "markdown":
        raw = _markdown_sections(content)
    elif fmt == "html":
        raw, html_title = _html_sections(content)
        title = title or html_title
    else:
        raw = [(0, "", content, None)]
    sections = _build(raw)
    extracted = sum(_non_ws_len(s.heading) + _non_ws_len(s.text) for s in sections)
    return Document(
        title=title, source_url=source_url or None, format=fmt, sections=sections, extracted_chars=extracted
    )


def _non_ws_len(text: str) -> int:
    return len(text) - sum(1 for ch in text if ch.isspace())


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    return text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")


def _collapse(text: str) -> str:
    return _WS.sub(" ", text).strip()


def _clean_block(text: str) -> str:
    """Trailing spaces off every line, at most one blank line in a row, no outer blank lines.

    Leading indentation is kept (code blocks).
    """
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return _BLANK_RUNS.sub("\n\n", text).strip("\n")


# A raw section: (level, heading text, body text, explicit anchor id or None).
_Raw = tuple[int, str, str, str | None]


def _build(raw: list[_Raw]) -> tuple[Section, ...]:
    stack: list[tuple[int, str]] = []
    slugs = SlugRegistry()
    # Explicit HTML ids are used as-is and claimed first, so a generated slug never takes one.
    for level, _, _, explicit_id in raw:
        if level and explicit_id:
            slugs.reserve(explicit_id)
    sections: list[Section] = []
    for level, heading, body, explicit_id in raw:
        anchor: str | None = None
        if level:
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, heading))
            if explicit_id:
                anchor = explicit_id
            else:
                base = slugify(heading)
                anchor = slugs.claim(base) if base else None
        sections.append(
            Section(
                heading_path=tuple(h for _, h in stack),
                heading=heading,
                level=level,
                text=_clean_block(body),
                anchor=anchor,
            )
        )
    return tuple(sections)


# --- Markdown ------------------------------------------------------------------------------------


def _strip_front_matter(content: str) -> str:
    """Remove YAML front matter, only when it unmistakably is some.

    The first line is exactly ``---``, the next is not blank, every line up to a closing ``---`` or
    ``...`` looks like YAML, and the closing line exists. Anything else (a leading horizontal rule,
    for instance) is left alone.
    """
    if not content.startswith("---\n"):
        return content
    lines = content.split("\n", 2)
    if len(lines) < 3 or not lines[1].strip():
        return content
    pos = 4  # after "---\n"
    while True:
        end = content.find("\n", pos)
        line = content[pos:] if end < 0 else content[pos:end]
        if line in ("---", "..."):
            return "" if end < 0 else content[end + 1 :]
        if end < 0 or not _YAML_LINE.fullmatch(line):
            return content
        pos = end + 1


def _markdown_sections(content: str) -> list[_Raw]:
    content = _strip_front_matter(content)
    lines = content.split("\n")
    tokens = MarkdownIt("commonmark").parse(content)
    # Raw HTML script/style/noscript/template blocks are dropped, as in HTML uploads.
    for tok in tokens:
        if tok.type == "html_block" and tok.map is not None and _MD_DROPPED_BLOCK.match(tok.content):
            for k in range(tok.map[0], min(tok.map[1], len(lines))):
                lines[k] = ""
    # Top-level headings only (a heading inside a list item or blockquote stays body text), with
    # their source line span [start, end).
    heads: list[tuple[int, int, int, str]] = []
    for i, tok in enumerate(tokens):
        if tok.type == "heading_open" and tok.level == 0 and tok.map is not None:
            heads.append((tok.map[0], tok.map[1], int(tok.tag[1]), _inline_text(tokens[i + 1])))
    first = heads[0][0] if heads else len(lines)
    raw: list[_Raw] = [(0, "", "\n".join(lines[:first]), None)]
    for idx, (_, end, level, text) in enumerate(heads):
        stop = heads[idx + 1][0] if idx + 1 < len(heads) else len(lines)
        raw.append((level, text, "\n".join(lines[end:stop]), None))
    return raw


def _inline_text(inline) -> str:  # type: ignore[no-untyped-def]  # markdown_it Token
    parts: list[str] = []
    for child in inline.children or ():
        if child.type in ("text", "code_inline"):
            parts.append(child.content)
        elif child.type in ("softbreak", "hardbreak"):
            parts.append(" ")
        elif child.type == "image":
            parts.append(child.content)
    return _collapse("".join(parts))


# --- HTML ----------------------------------------------------------------------------------------

_DROP = frozenset({"script", "style", "nav", "noscript", "template", "svg"})
_HEADINGS = {f"h{n}": n for n in range(1, 7)}
_BLOCKS = frozenset(
    {
        "p", "li", "pre", "blockquote", "div", "section", "article", "main", "header", "footer",
        "aside", "ul", "ol", "dl", "dt", "dd", "table", "thead", "tbody", "tr", "figure",
        "figcaption", "form", "fieldset", "details", "summary", "address", "body", "html",
    }
)
_CELLS = frozenset({"td", "th"})


class _HTMLSections(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.raw: list[_Raw] = []
        self.title = ""
        self._level = 0
        self._heading = ""
        self._heading_id: str | None = None
        self._paragraphs: list[str] = []
        self._buf: list[str] = []
        self._drop = 0
        self._pre = 0
        self._in_title = False
        self._title_buf: list[str] = []
        self._heading_buf: list[str] | None = None

    def _cap_attributes(self, name_start: int) -> None:
        """Count a tag's attributes with the parser's own grammar, before the parser scans the tag.

        html.parser locates a tag's end with ``locatetagend``, whose repeated group costs the regex
        engine about 250 bytes per attribute (an 830k-attribute tag peaks at 620 MB), so that regex
        must not run on an oversized tag. This walks ``tagfind_tolerant`` then ``attrfind_tolerant``
        back to back, exactly as ``parse_starttag`` does, and stops after MAX_HTML_ATTRIBUTES + 1
        matches: bounded work, nothing built. These are private stdlib names, guarded by a test;
        CHUNKER_ID pins the Unicode version and this ties extraction to the Python version as well.
        """
        rawdata = self.rawdata
        match = _stdlib_html_parser.tagfind_tolerant.match(rawdata, name_start)
        if match is None:
            return
        attrfind = _stdlib_html_parser.attrfind_tolerant
        k, count = match.end(), 0
        while (m := attrfind.match(rawdata, k)) is not None and m.end() > k:
            count += 1
            if count > MAX_HTML_ATTRIBUTES:
                raise ExtractionFailed("too_many_attributes", f"a tag has over {MAX_HTML_ATTRIBUTES} attributes")
            k = m.end()

    def parse_starttag(self, i: int) -> int:
        self._cap_attributes(i + 1)
        return super().parse_starttag(i)

    def parse_endtag(self, i: int) -> int:
        self._cap_attributes(i + 2)  # end tags go through the same locatetagend scan
        return super().parse_endtag(i)

    # Text --------------------------------------------------------------------------------------

    def _flush(self) -> None:
        text = "".join(self._buf)
        self._buf = []
        text = text.strip("\n") if self._pre else _collapse(text)
        if text.strip():
            self._paragraphs.append(text)

    def _close_heading(self) -> None:
        if self._heading_buf is not None:
            self._heading = _collapse("".join(self._heading_buf))
            self._heading_buf = None

    def _end_section(self) -> None:
        self._flush()
        self.raw.append((self._level, self._heading, "\n\n".join(self._paragraphs), self._heading_id))
        self._paragraphs = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DROP:
            self._drop += 1
            return
        if self._drop:
            return
        if tag == "title":
            self._in_title = True
        elif tag in _HEADINGS:
            self._close_heading()  # malformed: a heading opened inside an unclosed one
            self._end_section()
            self._level = _HEADINGS[tag]
            self._heading_id = next((v.strip() for k, v in attrs if k == "id" and v and v.strip()), None)
            self._heading_buf = []
        elif self._heading_buf is not None:
            if tag == "br" or tag in _BLOCKS or tag in _CELLS:
                self._heading_buf.append(" ")
            return
        elif tag == "br":
            self._buf.append("\n" if self._pre else " ")
            if not self._pre:
                self._flush()
        elif tag in _BLOCKS:
            self._flush()
            if tag == "pre":
                self._pre += 1
            elif tag == "li":
                self._buf.append("- ")
        elif tag in _CELLS:
            self._buf.append(" ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DROP:
            return  # a self-closed <svg/> has no end tag to balance
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP:
            self._drop = max(0, self._drop - 1)
            return
        if self._drop:
            return
        if tag == "title":
            self._in_title = False
        elif tag in _HEADINGS:
            self._close_heading()
        elif self._heading_buf is not None:
            if tag in _BLOCKS or tag in _CELLS:
                self._heading_buf.append(" ")
            return
        elif tag in _BLOCKS:
            self._flush()
            if tag == "pre":
                self._pre = max(0, self._pre - 1)
        elif tag in _CELLS:
            self._buf.append(" ")

    def handle_data(self, data: str) -> None:
        if self._drop:
            return
        if self._in_title:
            self._title_buf.append(data)
        elif self._heading_buf is not None:
            self._heading_buf.append(data)
        else:
            self._buf.append(data)

    def close(self) -> None:
        super().close()
        self._close_heading()  # unclosed heading at end of input
        self._end_section()
        self.title = _collapse("".join(self._title_buf))


def _html_sections(content: str) -> tuple[list[_Raw], str]:
    parser = _HTMLSections()
    parser.feed(content)
    parser.close()
    return parser.raw, parser.title
