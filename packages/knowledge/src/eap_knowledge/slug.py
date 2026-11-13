"""Heading slugs, compatible with GitHub's Markdown anchors."""

from __future__ import annotations

import unicodedata
from urllib.parse import quote


def slugify(heading: str) -> str:
    """GitHub-style: lower-case, keep letters, digits, marks, ``-`` and ``_``, spaces become ``-``.

    Punctuation is removed, not replaced, and runs of hyphens are not collapsed, exactly as GitHub
    does: ``"What's new?"`` -> ``"whats-new"``, ``"A -- B"`` -> ``"a----b"``, ``"日本語 見出し"`` ->
    ``"日本語-見出し"``.
    """
    out: list[str] = []
    for ch in heading.strip().lower():
        if ch == " ":
            out.append("-")
        elif ch in "-_" or unicodedata.category(ch)[0] in "LNM":
            out.append(ch)
    return "".join(out)


class SlugRegistry:
    """Hands out unique anchors in document order: ``foo``, ``foo-1``, ``foo-2``, ..."""

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}

    def reserve(self, anchor: str) -> None:
        """Register an explicit anchor (an HTML ``id``), used as-is; later slugs avoid it."""
        self._counts.setdefault(anchor, 1)

    def claim(self, base: str) -> str:
        if base not in self._counts:
            self._counts[base] = 1
            return base
        n = self._counts[base]
        while f"{base}-{n}" in self._counts:
            n += 1
        self._counts[base] = n + 1
        candidate = f"{base}-{n}"
        self._counts[candidate] = 1
        return candidate


def anchor_fragment(anchor: str) -> str:
    """Percent-encode an anchor for a URL fragment.

    Non-ASCII letters, marks and digits stay as they are (an IRI, as GitHub links them); ASCII
    characters outside the unreserved/sub-delim set (spaces, quotes, ``<``, ``>``, ``%`` ...) and
    any other non-ASCII character (spaces, controls, symbols) are percent-encoded.
    """
    out: list[str] = []
    for ch in anchor:
        if ord(ch) < 128:
            out.append(quote(ch, safe=_FRAGMENT_SAFE))
        elif unicodedata.category(ch)[0] in "LMN":
            out.append(ch)
        else:
            out.append(quote(ch, safe=""))
    return "".join(out)


_FRAGMENT_SAFE = "-._~!$&()*+,;=:@/?"
