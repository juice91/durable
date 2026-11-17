"""eap_knowledge: extraction, chunking, embed text, anchors, hashes, determinism, import rule."""

from __future__ import annotations

import ast
import hashlib
import pickle
import random
import re
import sys
import time
import tracemalloc
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import pytest

from eap_knowledge import (
    CHUNKER_ID,
    DEFAULT_CHUNKING,
    MAX_HEADING_CHARS,
    MAX_HTML_ATTRIBUTES,
    MAX_SECTIONS,
    SUPPORTED_CHUNKER_VERSIONS,
    ExtractionFailed,
    Chunk,
    ChunkingConfig,
    InvalidChunkingConfig,
    UnsupportedMimeType,
    approx_tokens,
    chunk,
    document_format,
    extract,
    slugify,
)

from .timing import cap

WORDS = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu".split()


def _sentences(n: int, seed: int = 0) -> str:
    rng = random.Random(seed)
    return " ".join(" ".join(rng.choice(WORDS) for _ in range(rng.randint(4, 14))).capitalize() + "." for _ in range(n))


@dataclass(frozen=True)
class Cfg:
    """A non-pydantic config: ``chunk`` accepts any object with the four attributes."""

    max_tokens: int = 512
    overlap_tokens: int = 64
    min_tokens: int = 40
    chunker_version: int = 1


def _non_ws(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _strip_overlap(chunks: list[Chunk]) -> str:
    return "".join(c.text[c.overlap :] for c in chunks)


# --- formats and errors --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mime", "fmt"),
    [
        ("text/markdown", "markdown"),
        ("text/x-markdown; charset=utf-8", "markdown"),
        ("TEXT/HTML", "html"),
        ("application/xhtml+xml", "html"),
        ("text/plain", "text"),
    ],
)
def test_document_format(mime: str, fmt: str) -> None:
    assert document_format(mime) == fmt


def test_unsupported_mime_type_is_pickle_safe() -> None:
    with pytest.raises(UnsupportedMimeType) as info:
        extract("x", "application/pdf")
    err = pickle.loads(pickle.dumps(info.value))
    assert isinstance(err, UnsupportedMimeType)
    assert err.mime_type == "application/pdf"
    assert "application/pdf" in str(err)


@pytest.mark.parametrize(
    ("kwargs", "field"),
    [
        ({"max_tokens": 0}, "max_tokens"),
        ({"overlap_tokens": 512}, "overlap_tokens"),
        ({"overlap_tokens": -1}, "overlap_tokens"),
        ({"min_tokens": 513}, "min_tokens"),
        ({"chunker_version": 3}, "chunker_version"),
        ({"max_tokens": True}, "max_tokens"),
    ],
)
def test_invalid_config(kwargs: dict, field: str) -> None:
    with pytest.raises(InvalidChunkingConfig) as info:
        chunk(extract("hello", "text/plain"), Cfg(**kwargs))
    assert info.value.field == field
    err = pickle.loads(pickle.dumps(info.value))
    assert str(err) == str(info.value)


def test_chunking_config_is_the_rm2_contract_type() -> None:
    from eap_schemas.knowledge import ChunkingConfig as SchemaConfig

    assert ChunkingConfig is SchemaConfig
    assert isinstance(DEFAULT_CHUNKING, SchemaConfig)
    assert (DEFAULT_CHUNKING.max_tokens, DEFAULT_CHUNKING.overlap_tokens, DEFAULT_CHUNKING.min_tokens) == (512, 64, 40)
    assert DEFAULT_CHUNKING.chunker_version == 1


def test_the_chunker_supports_exactly_the_schema_versions() -> None:
    # RAG P1: every version the schema accepts (1 and 2) is implemented, and nothing else.
    from eap_schemas.knowledge import CHUNKER_VERSIONS

    assert SUPPORTED_CHUNKER_VERSIONS == frozenset(CHUNKER_VERSIONS) == frozenset({1, 2})
    doc = extract("hello", "text/plain")
    for version in CHUNKER_VERSIONS:
        assert [c.text for c in chunk(doc, ChunkingConfig(chunker_version=version))] == ["hello"]


def test_accepts_any_object_with_the_config_attributes() -> None:
    @dataclass(frozen=True)
    class Other:
        max_tokens: int = 512
        overlap_tokens: int = 64
        min_tokens: int = 40
        chunker_version: int = 1

    doc = extract("# A\n\n" + _sentences(80), "text/markdown")
    assert chunk(doc, Other()) == chunk(doc, DEFAULT_CHUNKING) == chunk(doc)


# --- tokens and slugs ----------------------------------------------------------------------------


@pytest.mark.parametrize(("text", "tokens"), [("", 1), ("a", 1), ("abcd", 1), ("abcde", 2), ("中", 1), ("日本語テキスト", 2)])
def test_approx_tokens_is_ceil_chars_over_4_and_never_zero(text: str, tokens: int) -> None:
    assert approx_tokens(text) == tokens


@pytest.mark.parametrize(
    ("heading", "slug"),
    [
        ("Hello World", "hello-world"),
        ("What's new?", "whats-new"),
        ("  Trim me  ", "trim-me"),
        ("A -- B", "a----b"),
        ("snake_case and-kebab", "snake_case-and-kebab"),
        ("`code` & <b>", "code--b"),
        ("Café Ünïcode", "café-ünïcode"),
        ("日本語 見出し", "日本語-見出し"),
        ("v1.2.3", "v123"),
    ],
)
def test_slugify_matches_github_rules(heading: str, slug: str) -> None:
    assert slugify(heading) == slug


def test_duplicate_headings_get_numbered_anchors() -> None:
    doc = extract("# Setup\n\na\n\n# Setup\n\nb\n\n# Setup\n\nc\n\n# Setup-1\n\nd", "text/markdown")
    assert [s.anchor for s in doc.sections if s.level] == ["setup", "setup-1", "setup-2", "setup-1-1"]


# --- Markdown ------------------------------------------------------------------------------------


def test_markdown_headings_inside_code_fences_are_ignored() -> None:
    md = "# Real\n\n```python\n# not a heading\nx = 1\n```\n\n~~~\n## also not\n~~~\n"
    doc = extract(md, "text/markdown")
    assert [s.heading for s in doc.sections] == ["", "Real"]
    assert "# not a heading" in doc.sections[1].text
    assert "## also not" in doc.sections[1].text


def test_markdown_nested_heading_paths_and_setext() -> None:
    md = "Preamble\n\nTop\n===\n\nt\n\n## Child `code` [link](x)\n\nc\n\n### Grand\n\ng\n\n## Sibling\n\ns\n\n# Next\n\nn"
    doc = extract(md, "text/markdown")
    assert [s.heading_path for s in doc.sections] == [
        (),
        ("Top",),
        ("Top", "Child code link"),
        ("Top", "Child code link", "Grand"),
        ("Top", "Sibling"),
        ("Next",),
    ]
    assert doc.sections[0].text == "Preamble"


def test_markdown_front_matter_is_not_a_heading() -> None:
    doc = extract("---\ntitle: x\ntags: [a]\n---\n# Body\n\ntext", "text/markdown")
    assert [s.heading for s in doc.sections] == ["", "Body"]
    assert "title: x" not in "".join(s.text for s in doc.sections)


def test_newlines_and_unicode_are_normalised() -> None:
    nfd = "# Café\r\n\r\nline\r\n"
    nfc = "# Café\n\nline\n"
    assert extract(nfd, "text/markdown") == extract(nfc, "text/markdown")


# --- HTML ----------------------------------------------------------------------------------------


def test_html_strips_script_style_nav_and_keeps_structure() -> None:
    html = (
        "<html><head><title>Page &amp; Co</title><style>p{color:red}</style>"
        "<script>var heading = '<h1>x</h1>';</script></head><body>"
        "<nav><a>Home</a><h2>Menu</h2></nav>"
        "<p>Intro <b>bold</b>   text</p>"
        "<h1 id='custom-id'>Main</h1><p>One</p><ul><li>A</li><li>B</li></ul>"
        "<h2>Sub <em>part</em></h2><pre>  indented\n  code</pre><blockquote>Quote</blockquote>"
        "<table><tr><td>c1</td><td>c2</td></tr></table><noscript>enable js</noscript>"
        "</body></html>"
    )
    doc = extract(html, "text/html", source_url="https://ex.com/doc")
    assert doc.title == "Page & Co"
    joined = "\n".join(s.text for s in doc.sections)
    for gone in ("color", "var heading", "Home", "Menu", "enable js"):
        assert gone not in joined
    assert [s.heading_path for s in doc.sections] == [(), ("Main",), ("Main", "Sub part")]
    assert doc.sections[0].text == "Intro bold text"
    assert doc.sections[1].text == "One\n\n- A\n\n- B"
    assert "  indented\n  code" in doc.sections[2].text
    assert "Quote" in doc.sections[2].text and "c1 c2" in doc.sections[2].text
    assert [s.anchor for s in doc.sections] == [None, "custom-id", "sub-part"]


def test_html_explicit_title_wins() -> None:
    assert extract("<title>T</title><p>x</p>", "text/html", title="Given").title == "Given"


# --- plain text and empty documents --------------------------------------------------------------


def test_plain_text_is_one_section_split_on_blank_lines() -> None:
    text = "\n\n\n".join(_sentences(30, seed=i) for i in range(6))
    doc = extract(text, "text/plain", title="Notes")
    assert len(doc.sections) == 1 and doc.sections[0].heading_path == ()
    chunks = chunk(doc)
    assert len(chunks) > 1
    assert all(c.title == "Notes" for c in chunks)
    assert chunks[0].embed_text.startswith("path: Notes\n\ntitle: Notes\n\n")


@pytest.mark.parametrize(
    ("content", "mime"),
    [("", "text/plain"), ("   \n\n  ", "text/plain"), ("", "text/markdown"), ("# A\n## B\n", "text/markdown"),
     ("<script>x</script>", "text/html"), ("", "text/html")],
)
def test_empty_documents_give_no_chunks(content: str, mime: str) -> None:
    assert chunk(extract(content, mime)) == []


# --- chunking ------------------------------------------------------------------------------------


def test_embed_text_format_hash_and_anchor() -> None:
    md = "# Guide\n\n## Install\n\n" + _sentences(12)
    doc = extract(md, "text/markdown", title="Product Docs", source_url="https://docs.ex.com/guide#stale")
    [c] = chunk(doc)
    assert c.ordinal == 0
    assert c.heading_path == ("Guide", "Install")
    assert c.title == "Install"
    assert c.embed_text == f"path: Product Docs > Guide > Install\n\ntitle: Install\n\n{c.text}"
    assert c.embed_text_hash == hashlib.sha256(c.embed_text.encode()).hexdigest()
    assert c.anchor == "https://docs.ex.com/guide#install"
    assert c.approx_tokens == approx_tokens(c.embed_text)
    assert c.overlap == 0


def test_no_source_url_means_no_anchor_and_preamble_anchors_to_page() -> None:
    assert all(c.anchor is None for c in chunk(extract("# A\n\n" + _sentences(20), "text/markdown")))
    doc = extract(_sentences(20) + "\n\n# A\n\n" + _sentences(20, 1), "text/markdown", source_url="https://x/p")
    assert [c.anchor for c in chunk(doc)] == ["https://x/p", "https://x/p#a"]


def test_tiny_sections_merge_into_the_next_and_keep_their_heading() -> None:
    md = "# Top\n\n## Small\n\ntiny bit\n\n## Big\n\n" + _sentences(20) + "\n\n## Last\n\nend"
    chunks = chunk(extract(md, "text/markdown", source_url="https://x/doc"))
    assert len(chunks) == 1
    [c] = chunks
    # The merged section is titled by its FIRST section, where its text (and its anchor link) starts.
    assert (c.heading_path, c.title, c.anchor) == (("Top", "Small"), "Small", "https://x/doc#small")
    assert c.text.startswith("tiny bit\n\n## Big\n\n")
    assert c.text.endswith("\n\n## Last\n\nend")
    assert c.embed_text.startswith("path: Top > Small\n\ntitle: Small\n\n")


def test_a_merged_section_never_takes_a_later_heading_as_its_title() -> None:
    """The browser check's case: a tiny "Incident 524" merged with "Incident 525" was titled
    "Incident 525" while its text started with "## Incident 524"."""
    md = ("# Incidents\n\n## Incident 524\n\nDisk full on db-2.\n\n## Incident 525\n\n" + _sentences(20)
          + "\n\n## Incident 526\n\n" + _sentences(20, seed=1))
    chunks = chunk(extract(md, "text/markdown", source_url="https://x/inc"))
    assert [(c.title, c.anchor) for c in chunks] == [
        ("Incident 524", "https://x/inc#incident-524"), ("Incident 526", "https://x/inc#incident-526")
    ]
    first = chunks[0]
    assert first.text.startswith("Disk full on db-2.\n\n## Incident 525\n\n")
    assert "## Incident 524" not in first.text  # the title is in the prefix, never repeated
    assert "title: Incident 524" in first.embed_text and "Incident 525" not in first.embed_text.split("\n\n")[1]


def test_several_tiny_sections_take_the_first_title_and_a_tiny_tail_joins_the_previous() -> None:
    md = "## A\n\na\n\n## B\n\nb\n\n## C\n\n" + _sentences(20) + "\n\n## D\n\nd\n\n## E\n\ne"
    [c] = chunk(extract(md, "text/markdown"))
    assert c.title == "A" and c.heading_path == ("A",)
    assert c.text.startswith("a\n\n## B\n\nb\n\n## C\n\n")
    assert c.text.endswith("## D\n\nd\n\n## E\n\ne")  # the tiny tail group keeps its first heading line


def test_a_long_first_heading_keeps_its_full_line_when_merged() -> None:
    long = "L" * 200
    md = f"## {long}\n\nx\n\n## Next\n\n" + _sentences(20)
    # min_tokens 100: the long heading line (~50 tokens) alone does not reach it, so Next merges in.
    c = chunk(extract(md, "text/markdown"), ChunkingConfig(max_tokens=512, overlap_tokens=64, min_tokens=100))[0]
    assert c.title.startswith("LLL") and len(c.title) == 128
    assert c.text.startswith(f"## {long}\n\nx\n\n## Next\n\n")


def test_single_tiny_section_still_chunks() -> None:
    [c] = chunk(extract("# Only\n\nhi", "text/markdown"))
    assert c.text == "hi" and c.heading_path == ("Only",)


def test_oversized_section_splits_under_the_cap_with_overlap() -> None:
    cfg = ChunkingConfig(max_tokens=128, overlap_tokens=16, min_tokens=10)
    md = "# Long\n\n" + "\n\n".join(_sentences(8, seed=i) for i in range(12))
    chunks = chunk(extract(md, "text/markdown", title="T"), cfg)
    assert len(chunks) > 3
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
    for c in chunks:
        assert c.approx_tokens <= cfg.max_tokens
        assert len(c.embed_text) <= cfg.max_tokens * 4
    for prev, cur in zip(chunks, chunks[1:]):
        assert 0 < cur.overlap < len(cur.text)  # overlap never makes a whole chunk
        assert cur.overlap <= cfg.overlap_tokens * 4
        assert prev.text.endswith(cur.text[: cur.overlap])
        assert cur.text != prev.text and cur.text[cur.overlap :].strip()
        assert not cur.text[0].isspace()
    assert chunks[0].overlap == 0


def test_overlap_never_crosses_sections() -> None:
    cfg = ChunkingConfig(max_tokens=64, overlap_tokens=8, min_tokens=0)
    md = "# A\n\n" + _sentences(20) + "\n\n# B\n\n" + _sentences(20, 1)
    chunks = chunk(extract(md, "text/markdown"), cfg)
    firsts = [c for i, c in enumerate(chunks) if i == 0 or chunks[i - 1].heading_path != c.heading_path]
    assert len(firsts) == 2 and all(c.overlap == 0 for c in firsts)


def test_unbreakable_text_is_cut_by_characters() -> None:
    cfg = ChunkingConfig(max_tokens=32, overlap_tokens=4, min_tokens=0)
    chunks = chunk(extract("x" * 1000, "text/plain"), cfg)
    assert all(len(c.text) <= 128 for c in chunks)
    assert _strip_overlap(chunks) == "x" * 1000


def test_cjk_chunks_have_nonzero_tokens_and_respect_the_cap() -> None:
    text = "日本語の文章です。" * 400 + "\n\n" + "中文没有空格" * 300
    chunks = chunk(extract(text, "text/plain"), ChunkingConfig(max_tokens=64, overlap_tokens=8, min_tokens=4))
    assert len(chunks) > 5
    assert all(1 <= c.approx_tokens <= 64 for c in chunks)
    assert _non_ws(_strip_overlap(chunks)) == _non_ws(text)
    [single] = chunk(extract("字", "text/plain"))
    assert single.approx_tokens >= 1


def _prefix(c: Chunk) -> str:
    return c.embed_text[: len(c.embed_text) - len(c.text)]


@pytest.mark.parametrize("max_tokens", [1, 4, 16, 32, 64, 512])
def test_embed_text_never_exceeds_the_cap_whatever_the_headings(max_tokens: int) -> None:
    long = "Heading " * 40
    md = "".join(f"{'#' * lvl} {long}{lvl}\n\n" for lvl in range(1, 7)) + _sentences(40)
    cfg = Cfg(max_tokens=max_tokens, overlap_tokens=0, min_tokens=0)
    chunks = chunk(extract(md, "text/markdown", title=long), cfg)
    assert chunks
    for c in chunks:
        assert len(c.embed_text) <= max_tokens * 4
        assert len(_prefix(c)) <= max_tokens * 4 // 4
        assert c.embed_text.endswith(c.text)
        assert len(c.title) <= MAX_HEADING_CHARS and all(len(h) <= MAX_HEADING_CHARS for h in c.heading_path)


def test_deep_path_is_shortened_in_the_middle() -> None:
    md = "".join(f"{'#' * lvl} Level{lvl}\n\n" for lvl in range(1, 7)) + _sentences(40)
    cfg = ChunkingConfig(max_tokens=64, overlap_tokens=0, min_tokens=0)  # prefix cap: 64 chars
    [first, *_] = chunk(extract(md, "text/markdown", title="Doc"), cfg)
    assert first.heading_path == tuple(f"Level{n}" for n in range(1, 7))
    assert _prefix(first) == "path: Doc > Level1 > \u2026 > Level5 > Level6\n\ntitle: Level6\n\n"


def test_nested_clipped_headings_without_bodies_lose_nothing() -> None:
    heads = ["".join(chr(0x61 + (i + j) % 26) for j in range(500_000)) for i in range(6)]
    md = "".join(f"{'#' * (n + 1)} {h}\n\n" for n, h in enumerate(heads)) + "final body text"
    chunks = chunk(extract(md, "text/markdown"))
    got = _non_ws(_strip_overlap(chunks))
    expected = _non_ws("".join(heads) + "final body text")
    it = iter(got)
    assert all(ch in it for ch in expected)
    assert all(len(c.embed_text) <= 2048 for c in chunks)


def test_a_clipped_heading_keeps_its_full_text_in_the_body() -> None:
    heading = "word " * 100  # 500 chars
    [c, *_] = chunk(extract(f"# {heading}\n\nbody text here", "text/markdown"))
    assert c.title.endswith("\u2026") and len(c.title) == MAX_HEADING_CHARS
    assert c.text.startswith("# " + heading.strip())


def test_huge_heading_in_a_5mb_document_stays_bounded() -> None:
    heading = "h" * 1_000_000
    body = "\n\n".join(_sentences(20, seed=i) for i in range(4000))[:4_000_000]
    md = f"# {heading}\n\n{body}"
    tracemalloc.start()
    started = time.perf_counter()
    chunks = chunk(extract(md, "text/markdown", title="T" * 1_000_000))
    elapsed = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    total = sum(len(c.embed_text) for c in chunks)
    # Each 2048-char chunk repeats a prefix of at most ~400 chars (title, path and title clipped to
    # 128) plus up to 256 chars of overlap, so ~1.6x here; plain text with no headings is ~1.25x.
    assert total <= 1.75 * len(md), total / len(md)
    assert all(len(c.embed_text) <= 512 * 4 for c in chunks)
    assert peak < 40 * len(md), peak
    assert elapsed < cap(30)


def test_determinism_across_runs_and_copies() -> None:
    md = "# A\n\n" + _sentences(200) + "\n\n## B\n\n```\n# x\n```\n\n" + "汉字" * 900
    first = chunk(extract(md, "text/markdown", title="D", source_url="https://x/y"))
    for _ in range(3):
        again = chunk(extract(str(md), "text/markdown", title="D", source_url="https://x/y"))
        assert again == first
        assert [c.embed_text_hash for c in again] == [c.embed_text_hash for c in first]


# Golden values pin the chunker_version 1 output. A change here means the algorithm changed: that
# needs a chunker_version bump, because stored chunks and cached embeddings depend on it.
def test_golden_hashes_for_chunker_version_1() -> None:
    md = "# Guide\n\n## Install\n\nRun `pip install x`. Then import it.\n\n" + _sentences(120, seed=7)
    chunks = chunk(extract(md, "text/markdown", title="Docs", source_url="https://d/x"))
    digest = hashlib.sha256("|".join(c.embed_text_hash for c in chunks).encode()).hexdigest()
    assert (len(chunks), [c.overlap for c in chunks]) == GOLDEN_SHAPE
    assert digest == GOLDEN_DIGEST
    assert CHUNKER_ID == "1/unicode-15.1.0" == f"1/unicode-{unicodedata.unidata_version}"


GOLDEN_SHAPE = (4, [0, 255, 254, 252])
GOLDEN_DIGEST = "b56701ead11d0b90f715914c67550780d347758ab847bed49cc9d0e3732f5c29"


@pytest.mark.parametrize("seed", range(25))
def test_property_chunks_cover_all_non_whitespace_content(seed: int) -> None:
    rng = random.Random(seed)
    blocks: list[str] = []
    for i in range(rng.randint(1, 12)):
        kind = rng.random()
        if kind < 0.3:
            blocks.append("#" * rng.randint(1, 4) + f" Heading {i}")
        elif kind < 0.4:
            blocks.append("```\n# fenced " + "y" * rng.randint(0, 900) + "\n```")
        elif kind < 0.5:
            blocks.append("字" * rng.randint(1, 3000))
        else:
            blocks.append(_sentences(rng.randint(0, 120), seed=seed * 100 + i))
    md = "\n\n".join(blocks)
    max_tokens = rng.choice([16, 64, 128, 512])
    cfg = Cfg(
        max_tokens=max_tokens, overlap_tokens=rng.choice([0, 4, 8]), min_tokens=min(rng.choice([0, 5, 40]), max_tokens)
    )
    chunks = chunk(extract(md, "text/markdown", title="P"), cfg)
    # Compared with the SOURCE: every non-heading block is in the chunk texts, in order, once the
    # overlaps are removed; the only extra characters are heading lines kept in bodies.
    expected = _non_ws("".join(b for b in blocks if not b.startswith("#")))
    heading_lines = _non_ws("".join(b for b in blocks if b.startswith("#")))
    got = _non_ws(_strip_overlap(chunks))
    it = iter(got)
    assert all(ch in it for ch in expected)  # expected is a subsequence of got
    assert len(got) - len(expected) <= len(heading_lines)
    for c in chunks:
        assert c.text.strip() and c.approx_tokens >= 1


# --- grapheme-safe character cuts ----------------------------------------------------------------

FAMILY = "\U0001f468\u200d\U0001f469\u200d\U0001f467"
JOINERS = re.compile("[\u200d\ufe00-\ufe0f\U0001f3fb-\U0001f3ff\u0300-\u036f]")


@pytest.mark.parametrize(
    "unit",  # combining sequences chosen with no NFC precomposed form, since extract() applies NFC
    [FAMILY, "x\u0301", "\U0001f44d\U0001f3fd", "\u2764\ufe0f", "\U0001f469\U0001f3fd\u200d\U0001f4bb", "q\u0323\u0302"],
)
@pytest.mark.parametrize("overlap", [0, 4])
def test_character_cuts_never_split_a_grapheme(unit: str, overlap: int) -> None:
    text = unit * 300
    chunks = chunk(extract(text, "text/plain"), Cfg(max_tokens=16, overlap_tokens=overlap, min_tokens=0))
    assert len(chunks) > 3
    assert _strip_overlap(chunks) == text
    for c in chunks:
        assert c.text.replace(unit, "") == "", "a chunk holds a partial grapheme"
        assert not JOINERS.match(c.text) and not c.text.endswith("\u200d")


@pytest.mark.parametrize(
    "unit",
    [
        "\u0915\u093f",  # Devanagari ki: a spacing mark (Mc)
        "\u0e01\u0e34",  # Thai: a non-spacing vowel (Mn)
        "\U0001f1ef\U0001f1f5",  # JP flag: a regional indicator pair
        "1\ufe0f\u20e3",  # keycap one: variation selector plus an enclosing mark (Me)
        "\U0001f3f4\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f",  # England: tags
        FAMILY,
    ],
)
@pytest.mark.parametrize("overlap", [0, 4])
def test_cuts_at_a_one_char_offset_never_split_a_grapheme(unit: str, overlap: int) -> None:
    text = "a" + unit * 300
    assert unicodedata.normalize("NFC", text) == text
    chunks = chunk(extract(text, "text/plain"), Cfg(max_tokens=16, overlap_tokens=overlap, min_tokens=0))
    assert len(chunks) > 3
    assert _strip_overlap(chunks) == text
    for c in chunks:
        assert c.text.replace(unit, "") in ("", "a"), "a chunk holds a partial grapheme"


def test_hangul_vowel_and_trailing_jamo_join_the_previous_char() -> None:
    # extract() applies NFC, which composes L V T jamo into syllables, so the cutter is tested directly.
    from eap_knowledge.chunking import _cut_chars

    text = "\u1100\u1161\u11a8" * 50
    for limit in (3, 4, 7, 10):
        pieces = _cut_chars(text, limit)
        assert "".join(pieces) == text
        assert all(p.startswith("\u1100") for p in pieces)


def test_a_single_cluster_longer_than_the_limit_is_still_cut() -> None:
    text = "x" + "\u0301" * 400  # one grapheme of 401 code points (no NFC precomposition)
    chunks = chunk(extract(text, "text/plain"), Cfg(max_tokens=16, overlap_tokens=0, min_tokens=0))
    assert len(chunks) > 1 and all(c.text for c in chunks)
    assert _strip_overlap(chunks) == text


# --- front matter, HTML safety, anchors ------------------------------------------------------------


@pytest.mark.parametrize("close", ["---", "..."])
def test_yaml_front_matter_is_stripped(close: str) -> None:
    md = f"---\ntitle: Guide\n# a comment\ntags:\n  - a\n- b\n\"quoted key\": x\n{close}\n# Body\n\ntext"
    doc = extract(md, "text/markdown")
    assert [s.heading for s in doc.sections] == ["", "Body"]
    assert "Guide" not in "".join(s.text for s in doc.sections)


@pytest.mark.parametrize(
    "md",
    [
        "---\n\nIntro paragraph that must survive.\n\n---\n\nMore text.",  # leading rule, blank line
        "---\nIntro paragraph that must survive.\n\nMore text.\n\n---\n\nEnd.",  # not YAML
        "---\nkey: value\nno closing line, so this is not front matter",
        "--- \nkey: value\n---\nnot exactly three dashes",
    ],
)
def test_leading_rules_and_lookalikes_are_not_front_matter(md: str) -> None:
    doc = extract(md, "text/markdown")
    text = "\n".join(f"{s.heading}\n{s.text}" for s in doc.sections)
    for word in re.findall(r"[A-Za-z]{4,}", md):
        assert word in text


def test_markdown_raw_script_and_style_blocks_are_dropped() -> None:
    md = "# A\n\nkeep one\n\n<script>\nsteal()\n</script>\n\n<style>\n.x{}\n</style>\n\nkeep two\n\n<div>kept html</div>\n"
    [_, a] = extract(md, "text/markdown").sections
    assert "steal" not in a.text and ".x{}" not in a.text
    assert "keep one" in a.text and "keep two" in a.text and "kept html" in a.text


@pytest.mark.parametrize("md", ["<!-- only a comment -->", "<!-- a -->\n\n<!--\nmulti\nline\n-->\n", "<!-- unterminated"])
def test_markdown_comment_only_files_extract_nothing(md: str) -> None:
    doc = extract(md, "text/markdown")
    assert doc.extracted_chars == 0
    assert chunk(doc) == []


def test_markdown_comments_between_paragraphs_are_dropped_and_paragraphs_kept() -> None:
    md = (
        "# A\n\nFirst paragraph.\n\n<!-- block comment -->\n\nSecond <!-- inline\nspanning --> paragraph."
        "\n\n<!-- note -->trailing text\n\n## B <!-- heading comment -->\n\nThird."
    )
    doc = extract(md, "text/markdown")
    joined = "\n".join(f"{s.heading}\n{s.text}" for s in doc.sections)
    assert "<!--" not in joined and "comment" not in joined and "spanning" not in joined
    for kept in ("First paragraph.", "Second", "paragraph.", "trailing text", "Third."):
        assert kept in joined
    assert [s.heading for s in doc.sections] == ["", "A", "B"]
    assert doc.extracted_chars == len(re.sub(r"\s", "", "AFirstparagraph.Secondparagraph.trailingtextBThird."))


def test_comments_inside_code_are_kept_as_code() -> None:
    md = "# A\n\n```html\n<!-- fenced comment -->\n<p>x</p>\n```\n\nUse `<!-- span -->` here.\n\n    <!-- indented code -->\n"
    [_, a] = extract(md, "text/markdown").sections
    assert "<!-- fenced comment -->" in a.text
    assert "`<!-- span -->`" in a.text
    assert "<!-- indented code -->" in a.text


_MIB = 1024 * 1024


def _rep(unit: str, size: int = _MIB) -> str:
    return (unit * (size // len(unit) + 1))[:size]


def _assert_linear(make, *, limit: float = 2.0) -> None:
    """Time extract+chunk at 256 KiB and 1 MiB: 4x the input may take at most ~8x the time (a
    quadratic path would take 16x), and the larger run stays under a generous absolute limit."""

    def run(size: int) -> float:
        text = make(size)
        started = time.perf_counter()
        chunk(extract(text, "text/markdown"))
        return time.perf_counter() - started

    run(_MIB // 64)  # warm up
    # Best of three: a pause (GC, a busy machine) inflates one timing, a quadratic path all of them.
    small, large = min(run(_MIB // 4) for _ in range(3)), min(run(_MIB) for _ in range(3))
    assert large < cap(limit), (small, large)
    assert large <= 8 * small + 0.1, (small, large)


INLINE_UNITS = ["[", "*a", "_a ", "`a", "<a", "!["]


@pytest.mark.parametrize("unit", INLINE_UNITS)
@pytest.mark.parametrize("shape", ["paragraph", "headings", "with_comment"])
def test_markdown_inline_pathologies_are_linear(unit: str, shape: str) -> None:
    # markdown-it's inline pass took 9-33 s on 5 MiB of these.
    makers = {
        "paragraph": lambda n: _rep(unit, n),
        "headings": lambda n: _rep("# " + unit * 2000 + "\n", n),
        "with_comment": lambda n: "<!-- c --> " + _rep(unit, n) + " <!-- d -->",
    }
    _assert_linear(makers[shape])


@pytest.mark.parametrize(
    "make",
    [
        pytest.param(lambda n: "# a" + " " * n + "#b", id="atx-1mb-spaces"),
        pytest.param(lambda n: "# a" + " \t" * (n // 2) + "#b", id="atx-tab-space-mix"),
        pytest.param(lambda n: _rep("# a" + " " * 2000 + "#b\n", n), id="atx-lines-of-2000-spaces"),
        pytest.param(lambda n: _rep("# a" + " " * 2000 + "##\n", n), id="atx-closing-after-spaces"),
        pytest.param(lambda n: _rep("> a\n" + "lazy line\n" * 4, n), id="lazy-quote"),
        pytest.param(lambda n: _rep("[a]: /u\n", n), id="refdefs"),
        pytest.param(lambda n: _rep("[" + "a" * 2000 + "\n", n), id="unclosed-refdef-lines"),
        pytest.param(lambda n: _rep("- \n\n  x\n", n), id="empty-items"),
        pytest.param(lambda n: _rep("- a\n  - b\n\n  c\n", n), id="nested-reentry"),
    ]
    + [pytest.param(lambda n, u=u: _rep(u, n), id=repr(u)) for u in
       ["a\n", "a\n\n", "# a\n", "# *a\n", "a\n=\n", "- a\n", "1. a\n", "> a\n", "```\nx\n```\n", "<div>\n"]],
)
def test_markdown_block_pathologies_are_linear(make) -> None:
    _assert_linear(make)


def test_atx_closing_sequence_rules() -> None:
    from eap_knowledge.markdown import atx_content

    cases = {"foo ##": "foo", "foo#": "foo#", "#": "", "foo \\#": "foo \\#", "  foo  #  ": "foo", "a" + " " * 9 + "#b": "a" + " " * 9 + "#b"}
    for raw, want in cases.items():
        assert atx_content(raw) == want, raw


def test_heading_text_does_not_depend_on_position() -> None:
    heading = "# *x* " + "`a` " * 6 + "\n\nbody\n\n"
    doc = extract(heading * 6000, "text/markdown")
    texts = {s.heading for s in doc.sections if s.level}
    assert texts == {"x " + "a " * 5 + "a"}


def test_headings_past_the_section_cap_stay_as_body_text() -> None:
    md = "".join(f"# H{i}\n\nbody {i}\n\n" for i in range(MAX_SECTIONS + 50))
    doc = extract(md, "text/markdown")
    assert len(doc.sections) == MAX_SECTIONS
    last = doc.sections[-1]
    assert last.heading == f"H{MAX_SECTIONS - 2}"
    assert f"# H{MAX_SECTIONS + 49}" in last.text and f"body {MAX_SECTIONS + 49}" in last.text
    html = "".join(f"<h2>H{i}</h2><p>body {i}</p>" for i in range(MAX_SECTIONS + 5))
    hdoc = extract(html, "text/html")
    assert len(hdoc.sections) == MAX_SECTIONS and f"## H{MAX_SECTIONS + 4}" in hdoc.sections[-1].text


def test_headings_inside_lists_and_quotes_are_not_top_level() -> None:
    md = "# Top\n\n- item\n\n  # in list\n\n> # in quote\n> more\nlazy line\n\n1. x\n   ## in ordered\n\n## Real\n\nend"
    assert [s.heading for s in extract(md, "text/markdown").sections] == ["", "Top", "Real"]


def test_heading_inline_syntax_matches_markdown_it() -> None:
    md = "# *Emph* and `code` [link](x) ![alt](i) \\# &amp; <b>x</b> ##\n\nSetext *two*\nlines\n---\n\nbody"
    doc = extract(md, "text/markdown")
    assert [s.heading for s in doc.sections] == ["", "Emph and code link alt # & x", "Setext two lines"]


def _bounded(fn, seconds: float, peak_bytes: int):
    tracemalloc.start()
    started = time.perf_counter()
    try:
        return fn()
    finally:
        elapsed = time.perf_counter() - started
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert elapsed < cap(seconds) and peak < peak_bytes, (elapsed, peak)


@pytest.mark.parametrize(
    "html",
    [
        '<a b=\f"x>y" ' + "c " * 2_490_000 + ">",  # \f around '=' (the parser treats it as space)
        "<p " + "a=1 " * 830_000 + ">x</p>",
        "<p " + "a " * (MAX_HTML_ATTRIBUTES + 1) + ">x</p>",
        "<p>x</p " + "a " * 2_490_000 + ">",  # end tags run the same scan
    ],
)
def test_tags_with_too_many_attributes_are_rejected_in_bounded_time_and_memory(html: str) -> None:
    def run() -> None:
        with pytest.raises(ExtractionFailed) as info:
            extract(html, "text/html")
        assert info.value.reason == "too_many_attributes"
        assert pickle.loads(pickle.dumps(info.value)).reason == "too_many_attributes"

    _bounded(run, seconds=3, peak_bytes=50_000_000)


def test_a_tag_at_the_attribute_cap_is_fine() -> None:
    html = "<p " + " ".join(f"a{i}=1" for i in range(MAX_HTML_ATTRIBUTES)) + ">kept</p>"
    assert extract(html, "text/html").sections[0].text == "kept"


@pytest.mark.parametrize(
    ("html", "kept"),
    [
        ('<p>before</p><img src="data:image/png;base64,' + "QUJD" * 50_000 + '"><p>after</p>', "after"),
        ("<p>before</p><!-- " + "c" * 5_000_000 + " --><p>after</p>", "after"),
        ('<script type="application/json">{"q": "a<b", "pad": "' + "x" * 70_000 + '"}</script><p>after</p>', "after"),
        ("<p>before</p><![CDATA[" + "d" * 70_000 + "]]><p>after</p>", "after"),
        ("<p>a < b " + "word " * 14_000 + "</p><p>after</p>", "after"),  # spaced: not a tag
        ("<p>a<b " + "word " * 1_000 + "</p><p>after</p>", "after"),  # a tag (as browsers see it), under the cap
        ("<p>prose a<b then " + "z" * 70_000 + "</p><p>after</p>", "after"),
    ],
)
def test_legitimate_large_markup_ingests_with_bounded_memory(html: str, kept: str) -> None:
    doc = _bounded(lambda: extract(html, "text/html"), seconds=3, peak_bytes=30 * len(html) + 20_000_000)
    assert kept in "\n".join(s.text for s in doc.sections)


def test_private_html_parser_names_still_exist() -> None:
    # The attribute cap reuses html.parser's own grammar through private names; if a Python upgrade
    # removes or renames them, this fails loudly instead of silently dropping the cap.
    import html.parser as hp

    assert hasattr(hp, "attrfind_tolerant") and hasattr(hp, "tagfind_tolerant")
    assert callable(hp.HTMLParser.parse_starttag) and callable(hp.HTMLParser.parse_endtag)
    assert hp.attrfind_tolerant.match('<a b="x">', 3) is not None



def test_html_parser_grammar_is_the_one_the_cap_mirrors() -> None:
    # The attribute walk mirrors these three regexes (Python 3.13.15). Patch releases have rewritten
    # them for CVE fixes; if any pattern text changes, re-check the walk against the parser, then
    # update these digests (and bump CHUNKER_ID if extraction output can change).
    import hashlib
    import html.parser as hp

    digests = {
        name: hashlib.sha256(getattr(hp, name).pattern.encode()).hexdigest()[:16]
        for name in ("locatetagend", "attrfind_tolerant", "tagfind_tolerant")
    }
    assert digests == {
        "locatetagend": "3cf729cae302b017",
        "attrfind_tolerant": "a4fea92ca3dcfd15",
        "tagfind_tolerant": "d5c5cc6b0a05e4a6",
    }

def test_malformed_html_headings() -> None:
    html = "<h1>One<h2>Two<br>lines</h2><p>body</p><h3>Three<p>para</p>more</h3><p>tail</p>"
    doc = extract(html, "text/html")
    assert [s.heading for s in doc.sections] == ["", "One", "Two lines", "Three para more"]
    assert [s.text for s in doc.sections] == ["", "", "body", "tail"]


def test_extracted_chars_lets_callers_spot_empty_extractions() -> None:
    assert extract("<script>x()</script><style>p{}</style>", "text/html").extracted_chars == 0
    assert extract("# Hi\n\nab c", "text/markdown").extracted_chars == 5


def test_explicit_ids_are_used_as_is_and_claimed_before_slugs() -> None:
    html = "<h2>Intro</h2><p>a</p><h2 id='intro'>Other</h2><p>b</p><h2 id='intro'>Again</h2><p>c</p>"
    assert [s.anchor for s in extract(html, "text/html").sections if s.level] == ["intro-1", "intro", "intro"]


def test_anchor_fragments_are_percent_encoded_but_keep_unicode_letters() -> None:
    html = "<h1 id='a b\"<x>%'>One</h1><p>" + _sentences(10) + "</p><h1>日本語 見出し</h1><p>" + _sentences(10, 1) + "</p>"
    chunks = chunk(extract(html, "text/html", source_url="https://x/p"), Cfg(min_tokens=0))
    assert [c.anchor for c in chunks] == ["https://x/p#a%20b%22%3Cx%3E%25", "https://x/p#日本語-見出し"]


# --- layering ------------------------------------------------------------------------------------


# RAG P1: the worker-side extractors (formats/*.py except the registry) may also import their
# parsers; the rest of the package, which the API imports, may not.
WORKER_PARSERS = {"docutils", "yaml", "pypdf"}  # pypdf: formats/pdf.py (P1.A3)


def test_package_imports_only_stdlib_eap_schemas_and_markdown_it() -> None:
    allowed = {"eap_schemas", "eap_knowledge", "markdown_it", "__future__"}
    package = Path(__file__).resolve().parents[1] / "src" / "eap_knowledge"
    for path in package.rglob("*.py"):
        worker_side = path.parent.name == "formats" and path.name != "__init__.py"
        for node in ast.walk(ast.parse(path.read_text())):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for name in names:
                top = name.split(".")[0]
                ok = top in allowed or top in sys.stdlib_module_names or (worker_side and top in WORKER_PARSERS)
                assert ok, f"{path.name} imports {name}"
                # sandbox.py's probe test hooks import socket, subprocess and importlib (a fresh load
                # of _posixsubprocess) only to prove that its audit hook refuses them (P1.A3);
                # nothing else may.
                probes = ("socket", "subprocess", "importlib") if path.name == "sandbox.py" else ()
                assert top in probes or top not in ("socket", "http", "subprocess", "importlib"), (
                    f"{path.name} imports {name}"
                )
                assert top != "urllib" or name == "urllib.parse", f"{path.name} imports {name}"
