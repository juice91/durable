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
class Section:
    """A heading and the text under it, up to the next heading.

    ``heading_path`` is the heading texts from the outermost ancestor down to and including this
    section's own heading; the preamble before the first heading has ``()``, ``heading == ""`` and
    ``level == 0``. ``anchor`` is the fragment for this heading (the HTML ``id`` when it has one,
    otherwise its slug, de-duplicated across the document), or ``None`` without a heading.
    """

    heading_path: tuple[str, ...]
    heading: str
    level: int
    text: str
    anchor: str | None


@dataclass(frozen=True, slots=True)
class Document:
    """``extracted_chars`` counts the non-whitespace characters of all headings and section texts,
    so a caller can fail "non-empty input, empty extraction"."""

    title: str
    source_url: str | None
    format: str  # "markdown" | "html" | "text"
    sections: tuple[Section, ...]
    extracted_chars: int = 0


@dataclass(frozen=True, slots=True)
class Chunk:
    """One embeddable unit.

    ``text`` is the section text (merged or split); its first ``overlap`` characters repeat the end
    of the previous chunk of the same section (0 for the first chunk of every section).
    ``approx_tokens`` is measured on ``embed_text``, the string sent to the embedder.
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
