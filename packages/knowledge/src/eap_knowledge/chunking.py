"""Heading-aware chunking: merge tiny sections, split oversized ones with overlap, build embed text.

Every step is a pure function of the document and the config, so identical input gives identical
chunks, texts and hashes on every host (the hash-based embedding reuse depends on it).

Two algorithm versions, chosen by ``config.chunker_version``; version 1's output never changes.

- **v1**: a section under ``min_tokens`` merges into the NEXT section (the last one into the
  previous).
- **v2** (RAG P1, the default for new knowledge bases): a short section merges into its PARENT
  heading's section when it has one, else into its previous same-level sibling, only while no text
  has followed that group (document order is kept; otherwise it stays on its own), and never into
  the next unrelated section, so a heading with one command keeps its context. A code-only group (at least
  80 % of its non-space characters in fenced code) gets the nearest preceding prose paragraph
  (at most 300 characters) as a ``context:`` line in its embed text, never in its stored text.
  Splitting, overlap and the embed-text prefix are v1's.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import replace

from eap_knowledge.config import DEFAULT_CHUNKING, SUPPORTED_CHUNKER_VERSIONS, ChunkingConfigLike
from eap_knowledge.errors import InvalidChunkingConfig
from eap_knowledge.model import Chunk, Document, Section, approx_tokens, sha256_hex
from eap_knowledge.slug import anchor_fragment

CHARS_PER_TOKEN = 4
# Each heading and the document title are clipped to this many characters in the embed prefix,
# the chunk title and the chunk heading path (a clipped heading keeps its full text in the body).
MAX_HEADING_CHARS = 128
# The embed prefix ("path: ...\n\ntitle: ...\n\n") never exceeds max_chars // PREFIX_SHARE, so
# len(embed_text) <= max_tokens * 4 always holds and at least three quarters of each chunk is text.
PREFIX_SHARE = 4
_ELLIPSIS = "\u2026"

# Split levels for an oversized section, coarsest first; each pattern has one capturing group, the
# separator, so the pieces and separators rejoin to the exact original text. After the last level
# the text is cut into fixed-size runs of characters.
_LEVELS = (
    re.compile(r"(\n{2,})"),  # paragraphs
    re.compile(r"(\n)"),  # lines
    re.compile(r"((?<=[.!?;])[ \t]+|(?<=[。！？；])[ \t]*)"),  # sentences, CJK ones without spaces
    re.compile(r"([ \t]+)"),  # words
)


def chunk(document: Document, config: ChunkingConfigLike = DEFAULT_CHUNKING) -> list[Chunk]:
    """Chunk ``document``; an empty document (or one with only headings) gives ``[]``."""
    max_tokens, overlap_tokens, min_tokens, version = _validate(config)
    max_chars = max_tokens * CHARS_PER_TOKEN
    doc_title = _clip(document.title, MAX_HEADING_CHARS)
    groups: list[tuple[Section, str]]
    if document.format == "code":
        # Source code: the extractor already cut one section per symbol or line window, sized for
        # max_tokens; merging neighbours would blur their line ranges.
        groups = [(section, "") for section in document.sections if section.text.strip()]
    elif version == 1:
        # A section with no body is dropped, unless its heading was clipped: then its full heading
        # line is the body, so the clipped-away text still reaches the chunks.
        sections = [s for s in document.sections if _has_body(s)]
        groups = [(section, "") for section in _merge_small(sections, min_tokens)]
    else:
        groups = _merge_small_v2(list(document.sections), min_tokens)
    chunks: list[Chunk] = []
    for section, context in groups:
        path = tuple(_clip(h, MAX_HEADING_CHARS) for h in section.heading_path)
        title = _clip(section.heading, MAX_HEADING_CHARS) if section.heading else doc_title
        prefix = _embed_prefix(doc_title, path, title, max_chars // PREFIX_SHARE)
        context_line = _context_line(context, max_chars // PREFIX_SHARE)
        anchor = _anchor_url(document.source_url, section.anchor)
        for text, overlap in _split(section.text, max_chars - len(prefix) - len(context_line), overlap_tokens):
            embed_text = prefix + context_line + text
            chunks.append(
                Chunk(
                    ordinal=len(chunks),
                    heading_path=path,
                    title=title,
                    text=text,
                    embed_text=embed_text,
                    embed_text_hash=sha256_hex(embed_text),
                    approx_tokens=approx_tokens(embed_text),
                    anchor=anchor,
                    overlap=overlap,
                    page=section.page,
                    page_end=section.page_end,
                    condition=section.condition,
                    code=section.code,
                )
            )
    return chunks


def _has_body(section: Section) -> bool:
    return bool(section.text.strip()) or len(section.heading) > MAX_HEADING_CHARS


def _validate(config: ChunkingConfigLike) -> tuple[int, int, int, int]:
    values = {}
    for field in ("max_tokens", "overlap_tokens", "min_tokens", "chunker_version"):
        value = getattr(config, field, None)
        if not isinstance(value, int) or isinstance(value, bool):
            raise InvalidChunkingConfig(field, value, "must be an integer")
        values[field] = value
    max_tokens, overlap, min_tokens = values["max_tokens"], values["overlap_tokens"], values["min_tokens"]
    if values["chunker_version"] not in SUPPORTED_CHUNKER_VERSIONS:
        supported = ", ".join(map(str, sorted(SUPPORTED_CHUNKER_VERSIONS)))
        raise InvalidChunkingConfig("chunker_version", values["chunker_version"], f"supported: {supported}")
    if max_tokens < 1:
        raise InvalidChunkingConfig("max_tokens", max_tokens, "must be at least 1")
    if not 0 <= overlap < max_tokens:
        raise InvalidChunkingConfig("overlap_tokens", overlap, "must be at least 0 and below max_tokens")
    if not 0 <= min_tokens <= max_tokens:
        raise InvalidChunkingConfig("min_tokens", min_tokens, "must be between 0 and max_tokens")
    return max_tokens, overlap, min_tokens, values["chunker_version"]


def _join(*parts: str) -> str:
    return "\n\n".join(p for p in parts if p)


def _heading_line(section: Section) -> str:
    """A merged-away section keeps its heading in the text, as a Markdown heading line."""
    return f"{'#' * section.level} {section.heading}" if section.level and section.heading else ""


def _merge_small(sections: list[Section], min_tokens: int) -> list[Section]:
    """Merge each section under ``min_tokens`` into the next one; the last one into the previous.

    A merged section keeps the FIRST merged section's heading path, title and anchor (its text
    starts there, and the anchor link lands there); every later section's heading stays in the text
    as a Markdown heading line, so nothing is lost. The first section's own heading is the chunk
    title, so its line is not repeated in the text, unless it is too long for the prefix (then its
    full line is the start of the text, as for any section). A tiny last section (with anything
    carried into it) joins the previous one with its heading line.
    """
    out: list[Section] = []
    out_members: list[list[Section]] = []
    first: Section | None = None
    members: list[Section] = []
    as_head = ""  # the group's text with `first` as the title
    as_tail = ""  # the group's text when it joins the previous section (first heading line kept)
    last = len(sections) - 1
    for i, section in enumerate(sections):
        piece = _join(_heading_line(section), section.text)
        if first is None:
            first = section
            long_line = _heading_line(section) if len(section.heading) > MAX_HEADING_CHARS else ""
            as_head, as_tail = _join(long_line, section.text), piece
        else:
            as_head, as_tail = _join(as_head, piece), _join(as_tail, piece)
        members.append(section)
        if approx_tokens(as_head) >= min_tokens or (i == last and not out):
            out.append(_with_meta(replace(first, text=as_head), members))
            out_members.append(members)
        elif i < last:
            continue
        else:  # a tiny last group joins the previous section
            out_members[-1] = out_members[-1] + members
            out[-1] = _with_meta(replace(out[-1], text=_join(out[-1].text, as_tail)), out_members[-1])
        first, members, as_head, as_tail = None, [], "", ""
    return out


def _with_meta(section: Section, members: list[Section]) -> Section:
    """``section`` with the pages and conditions of every merged member (text untouched).

    ``page`` is the first page, ``page_end`` the last one when it differs, and ``condition`` the
    members' distinct conditions in order, joined with ``"; "`` and clipped to MAX_CONDITION_CHARS.
    """
    if len(members) == 1:
        only = members[0]
        return replace(section, page=only.page, page_end=only.page_end, condition=only.condition)
    pages = [m.page for m in members if m.page is not None]
    ends = [m.page_end if m.page_end is not None else m.page for m in members if m.page is not None]
    page = min(pages) if pages else None
    page_end = max(ends) if ends else None
    conditions = list(dict.fromkeys(m.condition for m in members if m.condition))
    condition = _clip("; ".join(conditions), MAX_CONDITION_CHARS) if conditions else None
    return replace(
        section, page=page, page_end=page_end if page_end != page else None, condition=condition
    )


# --- chunker v2 ----------------------------------------------------------------------------------

# A condition recorded on a chunk is at most this long (eap_schemas.knowledge.MAX_CONDITION_CHARS).
MAX_CONDITION_CHARS = 200
# v2's code context: a group whose non-space characters are at least this share fenced code gets the
# nearest preceding prose paragraph, clipped to CODE_CONTEXT_CHARS, in its embed text.
CODE_SHARE = 0.8
CODE_CONTEXT_CHARS = 300
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


class _Group:
    __slots__ = ("first", "parts", "members", "index", "position")

    def __init__(self, first: Section, index: int, position: int) -> None:
        self.first = first
        long_line = _heading_line(first) if len(first.heading) > MAX_HEADING_CHARS else ""
        self.parts = [long_line, first.text]
        self.members = [first]
        self.index = index  # the position of `first` in the document's sections
        self.position = position  # this group's position in the output order

    def text(self) -> str:
        return _join(*self.parts)


def _merge_small_v2(sections: list[Section], min_tokens: int) -> list[tuple[Section, str]]:
    """v2 grouping: ``(section with the group's text, code context or "")`` per group, in order.

    Every section with a heading takes a place in the heading tree (a stack of levels). A section
    whose body is under ``min_tokens`` is appended, with its heading line, to the group that holds
    its parent heading's content, else to the group of its previous sibling (the last section at
    the same level under the same parent), but only to a group that no text has followed yet, so
    the text stays in document order: once a longer sibling (or anything else) has started a group
    with text, a short section joins that sibling's group when it is the latest one, and otherwise
    stays on its own. It never joins the next section. A heading with no body still opens a group,
    so its short children gather under its title; a group left without text is dropped. The
    preamble (level 0) is never a parent.
    """
    groups: list[_Group] = []
    stack: list[tuple[int, _Group]] = []  # (heading level, group holding that section's content)
    last_text = -1  # position in `groups` of the latest group holding text
    for index, section in enumerate(sections):
        level = section.level
        popped: list[tuple[int, _Group]] = []
        if level:
            while stack and stack[-1][0] >= level:
                popped.append(stack.pop())
        parent = stack[-1][1] if level and stack else None
        sibling = next((group for lv, group in popped if lv == level), None)
        body = section.text.strip()
        target: _Group | None = None
        if body and approx_tokens(section.text) < min_tokens and len(section.heading) <= MAX_HEADING_CHARS:
            target = next((g for g in (parent, sibling) if g is not None and g.position >= last_text), None)
        if target is not None:
            target.parts.append(_join(_heading_line(section), section.text))
            target.members.append(section)
            home = target
            last_text = target.position
        else:
            home = _Group(section, index, len(groups))
            groups.append(home)
            if home.text().strip():
                last_text = home.position
        if level:
            stack.append((level, home))
    prose_before = _prose_before(sections)
    out: list[tuple[Section, str]] = []
    for group in groups:
        text = group.text()
        if not text.strip():
            continue
        context = prose_before[group.index] if _is_code_only(text) else ""
        out.append((_with_meta(replace(group.first, text=text), group.members), context))
    return out


def _prose_before(sections: list[Section]) -> list[str]:
    """For each section, the last prose paragraph (outside fenced code) of the sections before it."""
    out: list[str] = []
    last = ""
    for section in sections:
        out.append(last)
        paragraph = _last_prose_paragraph(section.text)
        if paragraph:
            last = paragraph
    return out


def _fenced_lines(text: str) -> list[tuple[str, bool]]:
    """Each line of ``text`` with whether it belongs to fenced code (fence lines included)."""
    out: list[tuple[str, bool]] = []
    fence = ""
    for line in text.split("\n"):
        match = _FENCE.match(line)
        if fence:
            out.append((line, True))
            if match and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence) and not line.strip()[len(match.group(1)):].strip():
                fence = ""
        elif match:
            fence = match.group(1)
            out.append((line, True))
        else:
            out.append((line, False))
    return out


def _is_code_only(text: str) -> bool:
    code = total = 0
    for line, in_code in _fenced_lines(text):
        n = len("".join(line.split()))
        total += n
        if in_code:
            code += n
    return total > 0 and code >= CODE_SHARE * total


def _last_prose_paragraph(text: str) -> str:
    paragraph: list[str] = []
    last = ""
    for line, in_code in _fenced_lines(text) + [("", False)]:
        if in_code or not line.strip():
            if paragraph:
                last = " ".join(paragraph)
                paragraph = []
            continue
        stripped = line.strip()
        if stripped.startswith("#"):  # a merged heading line is not prose
            continue
        paragraph.append(stripped)
    return _WS.sub(" ", last).strip()


_WS = re.compile(r"\s+")


def _context_line(context: str, cap: int) -> str:
    """``"context: <paragraph>\\n\\n"``, at most ``cap`` characters, or ``""``."""
    if not context:
        return ""
    overhead = len("context: \n\n")
    room = min(CODE_CONTEXT_CHARS, cap - overhead)
    if room < 16:
        return ""
    return f"context: {_clip(context, room)}\n\n"


def _clip(text: str, limit: int) -> str:
    """``text`` cut to at most ``limit`` characters at a grapheme boundary, ending in an ellipsis."""
    if len(text) <= limit:
        return text
    if limit <= 1:
        return _ELLIPSIS[:limit]
    k = limit - 1
    while k > 1 and _unsafe_cut(text, k):
        k -= 1
    return text[:k] + _ELLIPSIS


def _render_prefix(path: list[str], title: str) -> str:
    lines = []
    if path:
        lines.append("path: " + " > ".join(path))
    if title:
        lines.append("title: " + title)
    return "".join(line + "\n\n" for line in lines)


def _embed_prefix(doc_title: str, heading_path: tuple[str, ...], title: str, cap: int) -> str:
    """``"path: <doc title> > <h1> > <h2>\\n\\ntitle: <heading>\\n\\n"``, at most ``cap`` characters.

    Empty lines are left out. Over the cap, the middle of the path is replaced by an ellipsis, one
    element at a time; if that is not enough, the title line is clipped to half the cap and the
    path line to the rest; if even that does not fit, there is no prefix.
    """
    path = [p for p in (doc_title, *heading_path) if p]
    prefix = _render_prefix(path, title)
    if len(prefix) <= cap:
        return prefix
    for keep in range(len(path) - 1, 1, -1):
        head, tail = keep // 2, keep - keep // 2
        shortened = [*path[:head], _ELLIPSIS, *path[len(path) - tail :]]
        prefix = _render_prefix(shortened, title)
        if len(prefix) <= cap:
            return prefix
    overhead = len("title: \n\n")
    title_room = cap // 2 - overhead if title else 0
    short_title = _clip(title, title_room) if title_room > 0 else ""
    title_len = len(short_title) + overhead if short_title else 0
    path_room = cap - title_len - len("path: \n\n")
    if len(path) > 2:
        path = [path[0], _ELLIPSIS, path[-1]]
    short_path = _clip(" > ".join(path), path_room) if path and path_room > 0 else ""
    prefix = _render_prefix([short_path] if short_path else [], short_title)
    return prefix if len(prefix) <= cap else ""


def _anchor_url(source_url: str | None, anchor: str | None) -> str | None:
    if not source_url:
        return None
    base = source_url.split("#", 1)[0]
    return f"{base}#{anchor_fragment(anchor)}" if anchor else base


def _split(text: str, budget: int, overlap_tokens: int) -> list[tuple[str, int]]:
    """``(text, overlap)`` pieces of at most ``budget`` characters (the cap minus the prefix)."""
    if len(text) <= budget:
        return [(text, 0)]
    overlap_chars = min(overlap_tokens * CHARS_PER_TOKEN, budget // 4)
    atoms = _atomize(text, max(1, budget - overlap_chars - 2), 0)
    return _pack(atoms, budget, overlap_chars)


def _split_keep(text: str, pattern: re.Pattern[str]) -> list[tuple[str, str]]:
    """``(separator before, piece)`` pairs; empty pieces fold their separator into the next one."""
    out: list[tuple[str, str]] = []
    sep = ""
    for idx, item in enumerate(pattern.split(text)):
        if idx % 2:
            sep += item
        elif item:
            out.append((sep if out else "", item))
            sep = ""
    return out


def _atomize(text: str, limit: int, level: int) -> list[tuple[str, str]]:
    """Recursively split ``text`` into ``(separator, atom)`` pairs with every atom ``<= limit``."""
    if len(text) <= limit:
        return [("", text)]
    if level == len(_LEVELS):
        return [("", piece) for piece in _cut_chars(text, limit)]
    parts = _split_keep(text, _LEVELS[level])
    if len(parts) <= 1:
        return _atomize(text, limit, level + 1)
    out: list[tuple[str, str]] = []
    for sep, piece in parts:
        sub = _atomize(piece, limit, level + 1)
        out.append((sep, sub[0][1]))
        out.extend(sub[1:])
    return out


_ZWJ = "\u200d"
_MARKS = frozenset({"Mn", "Mc", "Me"})


def _joins_previous(ch: str) -> bool:
    """A code point that extends the grapheme before it.

    ZWJ, variation selectors, emoji skin-tone modifiers, tag characters (subdivision flags), Hangul
    vowel and trailing jamo, and every mark (Mn, Mc, Me: combining marks, Indic and Thai vowel
    signs, the keycap U+20E3).
    """
    cp = ord(ch)
    return (
        ch == _ZWJ
        or 0xFE00 <= cp <= 0xFE0F
        or 0x1F3FB <= cp <= 0x1F3FF
        or 0xE0020 <= cp <= 0xE007F
        or 0x1160 <= cp <= 0x11FF
        or 0xD7B0 <= cp <= 0xD7FB
        or unicodedata.category(ch) in _MARKS
    )


def _is_regional_indicator(ch: str) -> bool:
    return 0x1F1E6 <= ord(ch) <= 0x1F1FF


def _unsafe_cut(text: str, k: int, lo: int = 0) -> bool:
    """Cutting before ``text[k]`` would split a grapheme.

    Also unsafe: after a ZWJ, and between the two regional indicators of a flag (an odd number of
    indicators before ``k``, counted back no further than ``lo``, which is a known boundary).
    """
    if not 0 < k < len(text):
        return False
    if _joins_previous(text[k]) or text[k - 1] == _ZWJ:
        return True
    if _is_regional_indicator(text[k]) and _is_regional_indicator(text[k - 1]):
        run, j = 0, k - 1
        while j >= lo and _is_regional_indicator(text[j]):
            run += 1
            j -= 1
        return run % 2 == 1
    return False


def _cut_chars(text: str, limit: int) -> list[str]:
    """Fixed-size runs of at most ``limit`` code points, each cut backed off to a grapheme boundary.

    A cut never backs off to an empty piece: a single cluster longer than ``limit`` is cut at the
    limit.
    """
    out: list[str] = []
    i = 0
    while i < len(text):
        j = min(i + limit, len(text))
        k = j
        while k > i + 1 and _unsafe_cut(text, k, i):
            k -= 1
        if _unsafe_cut(text, k, i):
            k = j
        out.append(text[i:k])
        i = k
    return out


def _pack(atoms: list[tuple[str, str]], budget: int, overlap_chars: int) -> list[tuple[str, int]]:
    """Greedily fill chunks up to ``budget``; each new chunk starts with the previous one's tail."""
    out: list[tuple[str, int]] = []
    current, current_overlap = "", 0
    for sep, atom in atoms:
        if not current:
            current = atom
        elif len(current) + len(sep) + len(atom) <= budget:
            current += sep + atom
        else:
            out.append((current, current_overlap))
            tail = _overlap_tail(current, overlap_chars)
            if tail and len(tail) + len(sep) + len(atom) <= budget:
                current, current_overlap = tail + sep + atom, len(tail)
            else:
                current, current_overlap = atom, 0
    if current:
        out.append((current, current_overlap))
    return out


def _overlap_tail(text: str, overlap_chars: int) -> str:
    """A suffix of ``text`` of at most ``overlap_chars`` and at most half of it.

    It starts at a word boundary when the window has whitespace (CJK text without spaces is cut
    mid-run), and never has leading whitespace.
    """
    n = min(overlap_chars, len(text) // 2)
    if n <= 0:
        return ""
    start = len(text) - n
    if not text[start - 1].isspace() and not text[start].isspace():
        match = re.search(r"\s", text[start:])
        if match:
            start += match.start()
    while _unsafe_cut(text, start):  # never start an overlap inside a grapheme (shrinks it)
        start += 1
    return text[start:].lstrip()
