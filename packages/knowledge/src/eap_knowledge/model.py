"""Value types: an extracted ``Document`` of ``Section``s, and the ``Chunk``s cut from it."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


def approx_tokens(text: str) -> int:
    """``ceil(len(text) / 4)``, and never 0 (CJK text included)."""
    return max(1, (len(text) + 3) // 4)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CodeRef:
    """Where a source-code chunk comes from (the ``code`` extractor): the language, the repository
    path, the qualified symbol when known, the 1-based inclusive line range, and the kind (``code``,
    or what the document's metadata says, e.g. ``test``). Carried to ``metadata.eap``."""

    language: str
    path: str
    start_line: int
    end_line: int
    symbol: str | None = None
    kind: str = "code"


@dataclass(frozen=True, slots=True)
class Section:
    """A heading and the text under it, up to the next heading.

    ``heading_path`` is the heading texts from the outermost ancestor down to and including this
    section's own heading; the preamble before the first heading has ``()``, ``heading == ""`` and
    ``level == 0``. ``anchor`` is the fragment for this heading (the HTML ``id`` when it has one,
    otherwise its slug, de-duplicated across the document), or ``None`` without a heading.

    The worker extractors (RAG P1) may also set ``page``/``page_end`` (1-based PDF pages) and
    ``condition`` (an unevaluated rst ``ifconfig``/``only`` condition, at most 200 characters); the
    chunker carries them to every chunk cut from the section (``metadata.eap``). They never change
    a chunk's text or hash.
    """

    heading_path: tuple[str, ...]
    heading: str
    level: int
    text: str
    anchor: str | None
    page: int | None = None
    page_end: int | None = None
    condition: str | None = None
    code: CodeRef | None = None


@dataclass(frozen=True, slots=True)
class Document:
    """``extracted_chars`` counts the non-whitespace characters of all headings and section texts,
    so a caller can fail "non-empty input, empty extraction".

    ``format`` is ``markdown``, ``html`` or ``text`` from ``extract()``, and the extractor id
    (``rst``, ``yaml``, ``json``, ``markdown-doxygen``, ``pdf``, ``code``) from a worker extractor.
    ``warnings`` are stable codes for content an extractor dropped or could not honour (one entry
    per occurrence, e.g. ``include_dropped``); the ``extracted`` event counts them.
    """

    title: str
    source_url: str | None
    format: str
    sections: tuple[Section, ...]
    extracted_chars: int = 0
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Chunk:
    """One embeddable unit.

    ``text`` is the section text (merged or split); its first ``overlap`` characters repeat the end
    of the previous chunk of the same section (0 for the first chunk of every section).
    ``approx_tokens`` is measured on ``embed_text``, the string sent to the embedder.
    ``page``/``page_end``/``condition`` come from the chunk's section(s) (merged sections: the first
    page, the last page, and their distinct conditions joined with ``"; "``).
    """

    ordinal: int
    heading_path: tuple[str, ...]
    title: str
    text: str
    embed_text: str
    embed_text_hash: str
    approx_tokens: int
    anchor: str | None
    overlap: int = 0
    page: int | None = None
    page_end: int | None = None
    condition: str | None = None
    code: CodeRef | None = None
