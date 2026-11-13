"""Markdown block scanning: headings, code, HTML blocks and comments, in linear time.

markdown-it's block parser costs several microseconds per line (5 MiB of short lines or list items
takes 5 to 15 s) and its inline parser is quadratic on inputs like 5 MiB of "[". Chunking needs
far less: the top-level headings with their source line spans, which lines are code, and which
HTML to drop. This scanner finds exactly that, following CommonMark's block rules for headings
(ATX and setext), fences, indented code, HTML blocks (types 1 to 7), block quotes and lists, in
one pass over the lines. markdown-it is still used for heading text that has inline syntax, under
a fixed per-document budget (see ``_InlineBudget``), so the cost stays linear in the input.
"""

from __future__ import annotations

import bisect
import html
import re
from dataclasses import dataclass

from markdown_it import MarkdownIt
from markdown_it.common.utils import normalizeReference

# --- line patterns (all anchored at the line start, possessive where they repeat) -----------------

_ATX = re.compile(r" {0,3}(#{1,6})(?:[ \t]++(.*+))?$")
_FENCE_OPEN = re.compile(r"( {0,3})(`{3,}+|~{3,}+)(.*+)$")
_SETEXT = re.compile(r" {0,3}(=++|-++)[ \t]*+$")
_HR = re.compile(r" {0,3}(?:(?:\*[ \t]*+){3,}+|(?:-[ \t]*+){3,}+|(?:_[ \t]*+){3,}+)$")
_LIST = re.compile(r"( {0,3})([-+*]|(\d{1,9})[.)])([ \t]++|$)")
_QUOTE = re.compile(r" {0,3}>")
# A link reference definition; it never opens a paragraph (so it cannot become setext text). It
# needs a destination: on the same line, or on the next line when that line is not blank and does
# not start a block (as markdown-it: `[r]:` then `---` is a paragraph and a setext underline).
_REFDEF = re.compile(r" {0,3}\[([^\]]++)\]:[ \t]*+(.*+)$")
# First characters of a line that may carry container markers or indentation.
_PREFIX_START = frozenset(" \t>-+*0123456789")
_EMPTY_MARKER = re.compile(r"(?:[-+*]|\d{1,9}[.)])$")
_CONTAINER_PREFIX = re.compile(r"(?:[ \t]*+(?:>|[-+*](?=[ \t])|\d{1,9}[.)](?=[ \t])))*+[ \t]*+")

_HTML_1 = re.compile(r" {0,3}<(script|pre|style|textarea)(?=[\s>]|$)", re.I)
_HTML_1_END = re.compile(r"</(?:script|pre|style|textarea)>", re.I)
_HTML_6 = re.compile(
    r" {0,3}</?(?:address|article|aside|base|basefont|blockquote|body|caption|center|col|colgroup|dd|"
    r"details|dialog|dir|div|dl|dt|fieldset|figcaption|figure|footer|form|frame|frameset|h[1-6]|head|"
    r"header|hr|html|iframe|legend|li|link|main|menu|menuitem|nav|noframes|ol|optgroup|option|p|param|"
    r"search|section|summary|table|tbody|td|tfoot|th|thead|title|tr|track|ul)(?=[\s/>]|$)",
    re.I,
)
_HTML_7 = re.compile(
    r" {0,3}(?:<[A-Za-z][A-Za-z0-9-]*+(?:[ \t]++[A-Za-z_:][\w.:-]*+(?:[ \t]*+=[ \t]*+"
    r"""(?:[^\s"'=<>`]++|'[^']*+'|"[^"]*+"))?+)*+[ \t]*+/?>|</[A-Za-z][A-Za-z0-9-]*+[ \t]*+>)[ \t]*+$"""
)
# HTML blocks dropped entirely, as in HTML uploads.
_DROPPED_HTML = re.compile(r" {0,3}<(?:script|style|noscript|template)\b", re.I)
_BLOCK_COMMENT = re.compile(r"<!--.*?(?:-->|\Z)", re.S)

# First characters after which a line can only be paragraph text (the fast path).
_MAYBE_SPECIAL = frozenset(" \t#`~<>-+*_=0123456789[")


def _indent(line: str) -> int:
    width = 0
    for ch in line:
        if ch == " ":
            width += 1
        elif ch == "\t":
            width += 4 - width % 4
        else:
            break
    return width


def _html_start(line: str, para_open: bool) -> int:
    """The CommonMark HTML block type (1 to 7) this line starts, or 0."""
    if "<" not in line[:4]:
        return 0
    if _HTML_1.match(line):
        return 1
    stripped = line.lstrip(" ")
    if stripped.startswith("<!--"):
        return 2
    if stripped.startswith("<?"):
        return 3
    if stripped.startswith("<![CDATA["):
        return 5
    if len(stripped) > 2 and stripped.startswith("<!") and stripped[2].isalpha():
        return 4
    if _HTML_6.match(line):
        return 6
    if not para_open and _HTML_7.match(line):
        return 7
    return 0


def _html_ends(kind: int, line: str) -> bool:
    if kind == 1:
        return _HTML_1_END.search(line) is not None
    return {2: "-->", 3: "?>", 4: ">", 5: "]]>"}[kind] in line


def _fence_closes(line: str, char: str, length: int) -> bool:
    stripped = line.lstrip(" ")
    if len(line) - len(stripped) > 3:
        return False
    run = len(stripped) - len(stripped.lstrip(char))
    return run >= length and not stripped[run:].strip()


def _fence_open(line: str) -> tuple[str, int] | None:
    m = _FENCE_OPEN.match(line)
    if m is None or (m.group(2)[0] == "`" and "`" in m.group(3)):
        return None
    return m.group(2)[0], len(m.group(2))


def atx_content(raw: str) -> str:
    """An ATX heading's text without its optional closing sequence (``# foo ##`` -> ``foo``).

    Plain string operations: a regex search for the closing sequence is quadratic on long runs of
    spaces. The sequence counts only when it is the whole content or follows a space or tab.
    """
    s = raw.strip()
    t = s.rstrip("#")
    if t != s and (not t or t[-1] in " \t"):
        s = t
    return s.strip()


def _interrupts(line: str) -> bool:
    """A line that starts a block of its own (so it is never a lazy continuation)."""
    if line.lstrip(" ")[:1] not in _MAYBE_SPECIAL:
        return False
    return bool(
        _ATX.match(line) or _fence_open(line) or _HR.match(line) or _LIST.match(line) or _QUOTE.match(line)
        or _html_start(line, para_open=True)
    )


# --- inline heading text ---------------------------------------------------------------------------

_INLINE_SYNTAX = re.compile(r"[\\`*_\[\]!<&]")
_SYNTAX_CHARS = "\\`*_[]!<&"
_ESCAPE = re.compile(r"\\([!-/:-@\[-`{-~])")
_INLINE_MD = MarkdownIt("commonmark")

# markdown-it's inline parser costs about 6 microseconds per syntax character (brackets scan ahead,
# so more), so it runs only on headings that are small by their own content: at most this many
# characters, syntax characters and brackets. The worst such heading costs ~0.1 ms, and there are at
# most MAX_SECTIONS of them. Every other heading takes the linear plain-text path. The decision
# depends on the heading alone, never on its position in the document.
MAX_INLINE_HEADING_CHARS = 256
MAX_INLINE_SYNTAX_CHARS = 16
MAX_INLINE_BRACKETS = 8


def _inline_parse_ok(raw: str) -> bool:
    return (
        len(raw) <= MAX_INLINE_HEADING_CHARS
        and raw.count("[") + raw.count("]") <= MAX_INLINE_BRACKETS
        and sum(raw.count(c) for c in _SYNTAX_CHARS) <= MAX_INLINE_SYNTAX_CHARS
    )


def _collapse(text: str) -> str:
    return " ".join(text.split())


def reference_env(labels: list[str]) -> dict:
    """markdown-it's ``env`` with the document's reference labels, so ``[label]`` in a heading
    renders as link text as it would in a full parse (only the labels matter for plain text)."""
    return {"references": {normalizeReference(label): {"href": "", "title": ""} for label in labels}}


def heading_text(raw: str, env: dict | None = None) -> str:
    """A heading's plain text, as markdown-it renders it (text, code spans, image alt text)."""
    if not _INLINE_SYNTAX.search(raw):
        return _collapse(raw)
    if not _inline_parse_ok(raw):
        # Linear fallback: comments out, code-span backticks out, escapes and entities decoded.
        text = _strip_complete_comments(raw).replace("`", "")
        return _collapse(html.unescape(_ESCAPE.sub(r"\1", text)))
    tokens = _INLINE_MD.parseInline(raw, env if env is not None else {})
    parts: list[str] = []
    for child in (tokens[0].children or ()) if tokens else ():
        if child.type in ("text", "code_inline", "image"):
            parts.append(child.content)
        elif child.type in ("softbreak", "hardbreak"):
            parts.append(" ")
    return _collapse("".join(parts))


def _strip_complete_comments(text: str) -> str:
    out: list[str] = []
    pos = 0
    while (start := text.find("<!--", pos)) >= 0:
        end = text.find("-->", start + 4)
        if end < 0:
            break
        out.append(text[pos:start])
        pos = end + 3
    out.append(text[pos:])
    return "".join(out)


# --- comments in paragraphs --------------------------------------------------------------------------

_TICKS_OR_COMMENT = re.compile(r"`+|<!--")


def strip_comments_outside_code(block: str) -> str:
    """Drop complete ``<!--…-->`` comments outside backtick code spans, in linear time.

    A backtick run opens a code span closed by the next run of the same length (found by bisecting
    a per-length index). An unterminated ``<!--`` is literal text, as in CommonMark inlines.
    Newlines inside a comment are kept, so line numbers do not move.
    """
    runs: dict[int, list[int]] = {}
    for m in re.finditer(r"`+", block):
        runs.setdefault(m.end() - m.start(), []).append(m.start())
    out: list[str] = []
    last = cursor = 0
    while (m := _TICKS_OR_COMMENT.search(block, cursor)) is not None:
        if m.group(0) == "<!--":
            close = block.find("-->", m.end())
            if close < 0:
                break
            end = close + 3
            out.append(block[last : m.start()])
            out.append("\n" * block.count("\n", m.start(), end))
            last = cursor = end
        else:
            length = m.end() - m.start()
            starts = runs[length]
            j = bisect.bisect_left(starts, m.end())
            cursor = starts[j] + length if j < len(starts) else m.end()
    out.append(block[last:])
    return "".join(out)


# --- the scanner ---------------------------------------------------------------------------------

_TEXT, _CODE, _HTML, _DROP = 0, 1, 2, 3


@dataclass
class Scan:
    """``kinds[i]`` classifies line i (text, code, html, dropped); ``headings`` are top-level
    ``(start, end, level, raw text)`` with ``[start, end)`` the heading's own lines (an ATX raw text
    still has its closing ``#`` sequence; see ``atx_content``)."""

    kinds: list[int]
    headings: list[tuple[int, int, int, str]]
    ref_labels: list[str]  # link reference definition labels, as written


def scan(lines: list[str]) -> Scan:
    n = len(lines)
    kinds = [_TEXT] * n
    headings: list[tuple[int, int, int, str]] = []
    para: int | None = None  # first line of the open top-level paragraph
    ref_labels: list[str] = []
    i = 0
    while i < n:
        line = lines[i]
        first = line[:1]
        if first and first not in _MAYBE_SPECIAL:
            if para is None:  # fast path: paragraph text (no block can start with this char)
                para = i
            i += 1
            continue
        if first == "[":  # only a paragraph line or a link reference definition
            if para is None:
                if consumed := _refdef(lines, i, ref_labels):
                    i += consumed
                    continue
                para = i
            i += 1
            continue
        if not line or line.isspace():
            para = None
            i += 1
            continue
        if first == "#" and (m := _ATX.match(line)):
            para = None
            headings.append((i, i + 1, len(m.group(1)), m.group(2) or ""))
            i += 1
            continue
        if first in " \t" and _indent(line) >= 4:
            if para is None:  # indented code, through blank and indented lines
                while i < n and (not lines[i].strip() or _indent(lines[i]) >= 4):
                    kinds[i] = _CODE
                    i += 1
                continue
            i += 1
            continue
        if para is not None and (m := _SETEXT.match(line)):
            raw = "\n".join(lines[k].strip() for k in range(para, i))
            headings.append((para, i + 1, 1 if m.group(1)[0] == "=" else 2, raw))
            para = None
            i += 1
            continue
        if (fence := _fence_open(line)) is not None:
            para = None
            kinds[i] = _CODE
            i += 1
            while i < n and not _fence_closes(lines[i], *fence):
                kinds[i] = _CODE
                i += 1
            if i < n:
                kinds[i] = _CODE
                i += 1
            continue
        if m := _ATX.match(line):
            para = None
            headings.append((i, i + 1, len(m.group(1)), m.group(2) or ""))
            i += 1
            continue
        if kind := _html_start(line, para is not None):
            para = None
            kind_mark = _DROP if _DROPPED_HTML.match(line) else _HTML
            if kind <= 5:
                while i < n:
                    kinds[i] = kind_mark
                    i += 1
                    if _html_ends(kind, lines[i - 1]):
                        break
            else:
                while i < n and lines[i].strip():
                    kinds[i] = kind_mark
                    i += 1
            continue
        if _HR.match(line):
            para = None
            i += 1
            continue
        if _QUOTE.match(line):
            para = None
            i = _container(lines, i, kinds, quote=True)
            continue
        if (m := _LIST.match(line)) and (
            para is None or (m.group(4) and line[m.end() :].strip() and m.group(3) in (None, "1"))
        ):
            para = None
            i = _container(lines, i, kinds, quote=False)
            continue
        if para is None:
            if consumed := _refdef(lines, i, ref_labels):
                i += consumed
                continue
            para = i
        i += 1
    return Scan(kinds, headings, ref_labels)


def _refdef(lines: list[str], i: int, labels: list[str]) -> int:
    """Lines taken by a link reference definition starting at ``i`` (0 if there is none).

    Known divergences: a title on a following line is not consumed (it reads as paragraph text), and
    a title's closing quote is not checked.
    """
    ref = _REFDEF.match(lines[i])
    if ref is None:
        return 0
    if ref.group(2):
        taken = 1 if _destination_and_title(ref.group(2)) else 0
    elif i + 1 < len(lines) and not _interrupts(lines[i + 1]) and _destination_and_title(lines[i + 1].strip()):
        taken = 2
    else:
        taken = 0
    if taken:
        labels.append(ref.group(1))
    return taken


def _destination_and_title(text: str) -> bool:
    """One destination token, then nothing or the start of a title (a quote or a parenthesis)."""
    parts = text.split(None, 1)
    return bool(parts) and (len(parts) == 1 or parts[1][:1] in "\"'(")


def _container(lines: list[str], i: int, kinds: list[int], *, quote: bool) -> int:
    """Consume a block quote or list starting at ``i``; returns the first line after it.

    Nothing inside is a top-level heading. Fences inside are found on the line content (container
    markers stripped) and marked as code, so their comments are kept. A list keeps the content
    widths of its open items: after a blank line, a line belongs to the list when it is indented to
    at least the outermost item's width (CommonMark nests it in the deepest item it reaches). An
    empty item followed by a blank line is closed. Known divergence: nested block quotes inside list
    items, and list items inside block quotes, are followed only through their markers.
    """
    n = len(lines)
    widths: list[int] = []  # content widths of the open list items, outermost first
    lazy_ok = False  # the previous line was paragraph text, so a lazy continuation may follow
    saw_blank = False
    empty_item = False  # the previous line was an item marker with no content
    fence: tuple[str, int] | None = None
    item_code = False
    while i < n:
        line = lines[i]
        blank = not line.strip()
        lazy = False  # a lazy continuation line: paragraph text by definition
        if quote:
            if blank:
                break
            marker = _QUOTE.match(line)
            lazy = marker is None and lazy_ok and fence is None and not _interrupts(line)
            inside = marker is not None or lazy
            if marker is not None:  # indented code inside the quote (after "> ")
                after = line[marker.end() :]
                after = after[1:] if after[:1] == " " else after
                item_code = after.strip() != "" and _indent(after) >= 4 and not lazy_ok
        else:
            if blank:
                if empty_item and widths:
                    widths.pop()  # an item may begin with at most one blank line
                saw_blank, lazy_ok, empty_item = True, False, False
                i += 1
                continue
            item = _LIST.match(line)
            empty_item = False
            item_code = False
            indent = _indent(line)
            if item is not None and not (line.lstrip(" ")[:1] in "-*_" and _HR.match(line)) and (not widths or indent < widths[-1] or saw_blank
                                                              or not lazy_ok or line[item.end() :].strip()):
                while widths and widths[-1] > indent:
                    widths.pop()
                spaces = len(item.group(4).expandtabs(4))
                empty_item = not line[item.end() :].strip()
                widths.append(item.end(2) + (spaces if 1 <= spaces <= 4 and not empty_item else 1))
                inside = True
            elif widths and indent >= widths[0]:
                if saw_blank:  # the line belongs to the deepest item it reaches
                    while widths[-1] > indent:
                        widths.pop()
                deepest = next(w for w in reversed(widths) if w <= indent)
                item_code = indent - deepest >= 4 and not lazy_ok  # indented code inside the item
                inside = True
            else:
                lazy = not saw_blank and lazy_ok and fence is None and not _interrupts(line)
                inside = lazy
            saw_blank = False
        if not inside:
            break
        content = _CONTAINER_PREFIX.sub("", line, count=1) if line[:1] in _PREFIX_START else line
        if fence is not None:
            kinds[i] = _CODE
            if _fence_closes(content, *fence):
                fence = None
        elif content[:1] in "`~" and (opened := _fence_open(content)) is not None:
            kinds[i] = _CODE
            fence = opened
        if item_code and fence is None:
            kinds[i] = _CODE  # no lazy continuation after code, and its comments are kept
        lazy_ok = fence is None and kinds[i] == _TEXT and (lazy or _is_paragraph_text(content))
        i += 1
    return i


def _is_paragraph_text(content: str) -> bool:
    """Container content that is paragraph text (so a lazy continuation line may follow it)."""
    stripped = content.strip()
    if not stripped or (stripped[:1] in "-+*0123456789" and _EMPTY_MARKER.match(stripped)):
        return False
    if content[:1] not in _MAYBE_SPECIAL:
        return True
    return not (_ATX.match(content) or _HR.match(content) or _html_start(content, para_open=True))


def apply_html_rules(lines: list[str], kinds: list[int]) -> None:
    """Drop script/style/noscript/template blocks and strip HTML comments, in place.

    Comments in HTML blocks go like ``<!--…-->`` up to the block end (CommonMark type 2); comments
    in text runs (paragraphs, also inside lists and quotes) go only when complete and outside code
    spans. Line counts never change.
    """
    n = len(lines)
    i = 0
    while i < n:
        kind = kinds[i]
        j = i
        if kind == _CODE:
            i += 1
            continue
        while j < n and kinds[j] == kind and (kind != _TEXT or lines[j].strip()):
            j += 1
        if j == i:
            i += 1
            continue
        if kind == _DROP:
            for k in range(i, j):
                lines[k] = ""
        else:
            block = "\n".join(lines[i:j])
            if "<!--" in block:
                block = _BLOCK_COMMENT.sub(_newlines, block) if kind == _HTML else strip_comments_outside_code(block)
                lines[i:j] = block.split("\n")
        i = j


def _newlines(match: re.Match[str]) -> str:
    return "\n" * match.group(0).count("\n")


__all__ = [
    "MAX_INLINE_BRACKETS",
    "MAX_INLINE_HEADING_CHARS",
    "MAX_INLINE_SYNTAX_CHARS",
    "Scan",
    "apply_html_rules",
    "atx_content",
    "heading_text",
    "scan",
    "strip_comments_outside_code",
]
