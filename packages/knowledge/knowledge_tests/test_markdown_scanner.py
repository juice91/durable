"""The Markdown block scanner: golden outline for the CommonMark classes it follows, and a seeded
differential test of top-level headings against markdown-it's full parser."""

from __future__ import annotations

import hashlib
import random

import pytest

from markdown_it import MarkdownIt

from eap_knowledge import CHUNKER_ID, ChunkingConfig, chunk, extract

SCANNER_DOC = """# Scanner classes

[ref]: https://example.com/ref
---

Intro with a [ref] link.

## A reference definition before an underline

[docs]: /docs
===

## B lazy continuation

> quoted paragraph
lazy line that continues it
> # heading inside a quote

- item text
lazy item line
- # heading inside an item

## C empty list item

- 

  # after an empty item and a blank line

## D nested list re-entry

- outer
  - inner

  # back in the outer item

- a

      indented code in an item
Setext after the list
---

## E [ref] in a heading

1. ordered
   ## inside ordered item
10) paren marker

    # indented code at top level

``` md
# fenced
```

Closing paragraph.
"""

SCANNER_OUTLINE = [
    (1, "Scanner classes"),
    (2, "A reference definition before an underline"),
    (2, "B lazy continuation"),
    (2, "C empty list item"),
    (1, "after an empty item and a blank line"),
    (2, "D nested list re-entry"),
    (2, "Setext after the list"),
    (2, "E ref in a heading"),
]
# Pins the scanner's output: a scanner change that moves a heading or a line shows up here.
# 2026-11-14: a merged section keeps the first section's title (one merged chunk changed).
SCANNER_GOLDEN = (4, "3cd6cc4da20f9306d81c329b5bb5f3b0f23b3d99bf76bc15e7901bac739c088a")


def _markdown_it_headings(text: str) -> list[tuple[int, str]]:
    tokens = MarkdownIt("commonmark").parse(text)
    out: list[tuple[int, str]] = []
    for i, tok in enumerate(tokens):
        if tok.type == "heading_open" and tok.level == 0:
            parts: list[str] = []
            for child in tokens[i + 1].children or ():
                if child.type in ("text", "code_inline", "image"):
                    parts.append(child.content)
                elif child.type in ("softbreak", "hardbreak"):
                    parts.append(" ")
            out.append((int(tok.tag[1]), " ".join("".join(parts).split())))
    return out


def _our_headings(text: str) -> list[tuple[int, str]]:
    return [(s.level, s.heading) for s in extract(text, "text/markdown").sections if s.level]


def test_scanner_golden_outline_matches_markdown_it() -> None:
    assert _our_headings(SCANNER_DOC) == SCANNER_OUTLINE == _markdown_it_headings(SCANNER_DOC)
    chunks = chunk(extract(SCANNER_DOC, "text/markdown", title="Doc", source_url="https://x/s"),
                   ChunkingConfig(max_tokens=128, overlap_tokens=16, min_tokens=20))
    digest = hashlib.sha256("\n".join(c.embed_text_hash for c in chunks).encode()).hexdigest()
    assert (len(chunks), digest) == SCANNER_GOLDEN
    assert CHUNKER_ID == "1/unicode-15.1.0"


FRAGMENTS = [
    "# H{n}", "## Sub {n} ##", "### *emph* {n}", "#### `code` {n}", "Setext {n}\n===", "Setext two {n}\n---",
    "para {n} text", "para {n}\nsecond line", "- item {n}", "- item\n  # nested {n}", "1. one {n}\n2. two",
    "> quote {n}", "> # quoted {n}\nlazy", "```\n# fenced {n}\n```", "~~~\n## tilde {n}\n~~~", "    # indented {n}",
    "<div>\n# html {n}\n</div>", "<!-- c {n} -->", "[ref{n}]: /url", "[ref]: /u\n---", "---", "***", "",
    "- \n\n  # after empty item {n}", "- a\n  - b\n\n  # reentry {n}", "  - x\n\n    para", "+ plus {n}",
    "<script>\n# s {n}\n</script>", "text with <!-- inline --> comment {n}", "1) paren {n}", "10. ten {n}",
    "> a\n> - b\n>   # c{n}", "\\# not heading {n}", "#5 not heading", "#\tTab {n}",
    "## [ref] link {n}", "# a [b](c) ![d](e) {n}",
    # reference definitions without a destination, or with it on the next line
    "[r]:", "[r]:\n---", "[r]:\n[r]:", "[r]:\n/next-line {n}", "[r{n}]: /u\n---", "[r]: /u garbage {n}\n---",
]
# Container leftovers: code (a fence or indented code) inside a list item or a quote, followed by
# the kind of line that can end the container (HTML, setext text, a lazy line).
CONTAINER_CODE_FRAGMENTS = [
    "- item\n  ```\n  # in fence {n}\n  ```", "> ```\n> # q fence {n}\n> ```",
    "- item\n\n  ```\n  x\n  ```\n<div>{n}</div>", "- a\n\n      code {n}", "- a\n\n      code\n<div>\n# h {n}\n</div>",
    ">     code {n}", "> ```\n> x\nSetext {n}\n===", "- a\n  ```\n  x\nSetext {n}\n===",
]

# Known divergence classes and how many of the seeded documents fall in each. Pinned on purpose: a
# scanner change that adds or removes a divergence in a class changes a count and fails here.
#   front_matter: a document starting with "---" and YAML-like lines is front matter here (stripped),
#     while markdown-it reads it as Markdown.
#   container_leftover: a line after code inside a list item or quote (see above).
EXPECTED_DIVERGENCES = {"front_matter": 0, "container_leftover": 0}


def _divergence_class(doc: str, used: set[int]) -> str | None:
    if doc.startswith("---\n"):
        return "front_matter"
    if used & set(range(len(FRAGMENTS), len(FRAGMENTS) + len(CONTAINER_CODE_FRAGMENTS))):
        return "container_leftover"
    return None


def test_top_level_headings_match_markdown_it_on_random_documents() -> None:
    pool = FRAGMENTS + CONTAINER_CODE_FRAGMENTS
    counts = dict.fromkeys(EXPECTED_DIVERGENCES, 0)
    for seed in range(2000):
        rng = random.Random(seed)
        picks = [rng.randrange(len(pool)) for _ in range(rng.randint(1, 14))]
        doc = "\n".join(pool[p].format(n=k) + ("\n" if rng.random() < 0.6 else "") for k, p in enumerate(picks))
        if _our_headings(doc) == _markdown_it_headings(doc):
            continue
        cls = _divergence_class(doc, set(picks))
        assert cls is not None, ("unexplained divergence", seed, doc, _our_headings(doc), _markdown_it_headings(doc))
        counts[cls] += 1
    assert counts == EXPECTED_DIVERGENCES


@pytest.mark.parametrize(
    ("doc", "headings"),
    [
        ("[r]:\n---", [(2, "[r]:")]),  # no destination: a paragraph and a setext underline
        ("[r]:\n/url\n---", []),  # destination on the next line: a definition, then a rule
        ("[r]: /u garbage\n---", [(2, "[r]: /u garbage")]),  # not one destination token
        ('[r]: /u "title"\n---\n\n# see [r]', [(1, "see r")]),  # a definition, used in a heading
    ],
)
def test_link_reference_definition_rules(doc: str, headings: list[tuple[int, str]]) -> None:
    assert _our_headings(doc) == headings == _markdown_it_headings(doc)
