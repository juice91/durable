"""RM.3 acceptance: realistic docs chunk into stable, readable sections (see also test_knowledge.py)."""

from __future__ import annotations

import html as html_lib
import re
import unicodedata
from dataclasses import dataclass

import pytest

from eap_knowledge import CHUNKER_ID, ChunkingConfig, chunk, extract

URL = "https://docs.example.com/guide"

_PARA = (
    "The scheduler picks the next ready run, leases it to a worker and records the lease in the "
    "durable event log. Leases expire after thirty seconds unless the worker renews them. "
)
LONG_SECTION = "\n\n".join(_PARA * 3 + f"Paragraph {i} ends here." for i in range(8))

README = f"""# Orca Runtime

A small runtime for durable agent runs.

## Install

```bash
# not a heading
pip install orca-runtime
## also not a heading
```

Then run `orca --help` to check the install worked correctly on your machine today.

## Usage

### Configuration

| Key | Default | Meaning |
| --- | ------- | ------- |
| `workers` | 4 | Number of worker processes |
| `lease_s` | 30 | Lease length in seconds |

Set the keys in `orca.toml` before starting the scheduler for the first time.

### Scheduling

{LONG_SECTION}

#### Retries

Failed runs are retried with exponential backoff, up to five attempts per run, and then parked.

## Install

A second install section, whose anchor must be de-duplicated against the first one.

## License

MIT
"""

HTML = """<!doctype html><html><head><title>Billing FAQ</title>
<style>body { color: red; } .secret-css { x: 1 }</style>
<script>var secretScript = "do not index"; document.write('<h1>no</h1>');</script></head>
<body><nav><a href="/">Home</a><a href="/pricing">Pricing</a></nav>
<h1 id="faq">Billing FAQ</h1>
<p>Answers to the most common questions about invoices, plans and payment methods for teams.</p>
<h2>Invoices</h2>
<p>Invoices are issued on the first day of each month and sent to the billing contact. You can
download every past invoice as a PDF from the billing page at any time you like.</p>
<h2 id="refunds">Refunds &amp; credits</h2>
<p>Refunds are processed within five business days. Credits never expire and apply automatically.</p>
<ul><li>Annual plans: pro-rated refund</li><li>Monthly plans: credit only</li></ul>
<script>track("pageview")</script></body></html>
"""

PLAIN = (
    "Release notes for version 2.4\n\n"
    + "\n\n".join(f"Item {i}: " + "fixed a bug in the retry loop of the scheduler. " * 6 for i in range(10))
    + "\n"
)

CJK_MD = """# 快速入门

这是一个用于持久化代理运行的小型运行时。它记录每一次租约，并在崩溃后恢复运行。

## 安装步骤

请先安装依赖，然后运行命令检查安装是否成功。如果失败，请查看日志文件并重试一次。

## 配置说明

""" + "".join(f"第{i}条：调度器会选择下一个就绪的运行，并把它租给工作进程。" for i in range(40)) + "\n"

SMALL = ChunkingConfig(max_tokens=128, overlap_tokens=16, min_tokens=20)


def _docs() -> dict[str, tuple[str, str]]:
    return {
        "readme": (README, "text/markdown"),
        "html": (HTML, "text/html"),
        "plain": (PLAIN, "text/plain"),
        "cjk": (CJK_MD, "text/markdown"),
    }


def _chunks(name: str, config: ChunkingConfig = SMALL):
    content, mime = _docs()[name]
    return chunk(extract(content, mime, title="Doc", source_url=URL), config)


def _non_ws(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _body(chunks) -> str:
    return "".join(c.text[c.overlap :] for c in chunks)


def _source_body(content: str, mime: str) -> tuple[str, str, int]:
    """(non-whitespace body text, non-whitespace heading text, list bullets added) from the source."""
    content = unicodedata.normalize("NFC", content)
    if mime == "text/html":
        content = re.sub(r"(?is)<(script|style|nav|title)\b.*?</\1>", "", content)
        headings = re.findall(r"(?is)<h[1-6][^>]*>(.*?)</h[1-6]>", content)
        content = re.sub(r"(?is)<h[1-6][^>]*>.*?</h[1-6]>", "", content)
        bullets = len(re.findall(r"(?i)<li\b", content))
        text = html_lib.unescape(re.sub(r"<[^>]*>", " ", content))
        return _non_ws(text), _non_ws(html_lib.unescape("".join(headings)) + "#" * 6 * len(headings)), bullets
    if mime == "text/markdown":
        body, heads, fenced = [], [], False
        for line in content.split("\n"):
            if line.startswith("```"):
                fenced = not fenced
            (heads if not fenced and re.match(r"#{1,6} ", line) else body).append(line)
        return _non_ws("\n".join(body)), _non_ws("".join(heads)), 0
    return _non_ws(content), "", 0


# --- section paths, anchors, caps ------------------------------------------------------------------


def _outline(name: str) -> list[tuple[tuple[str, ...], str | None]]:
    out: list[tuple[tuple[str, ...], str | None]] = []
    for c in _chunks(name):
        if not out or out[-1] != (c.heading_path, c.anchor):
            out.append((c.heading_path, c.anchor))
    return out


def test_readme_outline_paths_and_anchors() -> None:
    base = URL + "#"
    assert _outline("readme") == [
        (("Orca Runtime", "Install"), base + "install"),  # the tiny H1 preamble merged into Install
        (("Orca Runtime", "Usage", "Configuration"), base + "configuration"),
        (("Orca Runtime", "Usage", "Scheduling"), base + "scheduling"),
        (("Orca Runtime", "Usage", "Scheduling", "Retries"), base + "retries"),
        (("Orca Runtime", "Install"), base + "install-1"),  # duplicate heading de-duplicated
    ]
    # the tiny trailing License section joined the previous chunk, keeping its heading line
    assert "## License\n\nMIT" in _chunks("readme")[-1].text


def test_readme_code_fence_and_table_stay_in_body_text() -> None:
    chunks = _chunks("readme")
    assert "## also not a heading" in chunks[0].text and "pip install orca-runtime" in chunks[0].text
    assert "| `workers` | 4 |" in chunks[1].text
    assert all("also not a heading" not in c.heading_path for c in chunks)


def test_readme_long_section_is_split_in_order_under_the_cap() -> None:
    sched = [c for c in _chunks("readme") if c.heading_path[-1] == "Scheduling"]
    assert len(sched) >= 5
    assert all(c.approx_tokens <= SMALL.max_tokens for c in sched)
    assert all(c.anchor == URL + "#scheduling" for c in sched)
    assert [c.overlap for c in sched][0] == 0 and all(c.overlap > 0 for c in sched[1:])
    text = _body(sched)
    assert text.index("Paragraph 0 ends") < text.index("Paragraph 7 ends")


@pytest.mark.parametrize("name", ["readme", "html", "plain", "cjk"])
def test_every_chunk_respects_the_cap_and_ordinals_are_dense(name: str) -> None:
    chunks = _chunks(name)
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
    assert all(1 <= c.approx_tokens <= SMALL.max_tokens for c in chunks)
    assert all(c.text.strip() and c.text == c.text.strip() for c in chunks)
    assert all(0 <= c.overlap < len(c.text) for c in chunks)


def test_html_outline_drops_script_style_nav_and_honours_ids() -> None:
    chunks = _chunks("html")
    joined = "\n".join(c.embed_text for c in chunks)
    for junk in ("secret", "do not index", "Pricing", "track(", "pageview", "color: red"):
        assert junk not in joined
    assert [(c.heading_path, c.anchor) for c in chunks] == [
        (("Billing FAQ",), URL + "#faq"),
        (("Billing FAQ", "Invoices"), URL + "#invoices"),
        (("Billing FAQ", "Refunds & credits"), URL + "#refunds"),
    ]
    assert "- Annual plans: pro-rated refund" in chunks[2].text
    assert chunks[0].embed_text.startswith("path: Doc > Billing FAQ\n\ntitle: Billing FAQ\n\n")


def test_html_takes_its_title_tag_when_none_is_given() -> None:
    doc = extract(HTML, "text/html")
    assert doc.title == "Billing FAQ"
    assert chunk(doc, SMALL)[1].embed_text.startswith("path: Billing FAQ > Billing FAQ > Invoices\n\n")


def test_plain_text_has_no_heading_and_anchors_to_the_page() -> None:
    chunks = _chunks("plain")
    assert {c.heading_path for c in chunks} == {()}
    assert {c.anchor for c in chunks} == {URL}
    assert chunks[0].embed_text.startswith("path: Doc\n\ntitle: Doc\n\n")


def test_cjk_outline_chunks_and_unicode_anchors() -> None:
    chunks = _chunks("cjk")
    assert [(c.heading_path, c.anchor) for c in chunks[:2]] == [
        (("快速入门", "安装步骤"), URL + "#安装步骤"),
        (("快速入门", "配置说明"), URL + "#配置说明"),
    ]
    assert len(chunks) >= 3 and all(c.approx_tokens <= SMALL.max_tokens for c in chunks)
    assert len({c.embed_text_hash for c in chunks}) == len(chunks)
    assert "第39条" in chunks[-1].text


# --- coverage and overlap --------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["readme", "html", "plain", "cjk"])
@pytest.mark.parametrize("config", [SMALL, ChunkingConfig(max_tokens=64, overlap_tokens=8, min_tokens=0), ChunkingConfig()])
def test_overlap_removed_chunks_cover_every_non_whitespace_char(name: str, config: ChunkingConfig) -> None:
    content, mime = _docs()[name]
    chunks = chunk(extract(content, mime, title="Doc", source_url=URL), config)
    covered = _non_ws(_body(chunks))
    # Compared with the normalised SOURCE minus the markup that is dropped on purpose; headings live
    # in heading_path/title, and only heading lines kept in bodies (merges) and list bullets are extra.
    expected, headings, bullets = _source_body(content, mime)
    it = iter(covered)
    assert all(ch in it for ch in expected), "source text lost by extraction or chunking"
    assert len(covered) - len(expected) <= len(headings) + bullets


@pytest.mark.parametrize("name", ["readme", "plain", "cjk"])
def test_overlap_is_a_suffix_of_the_previous_chunk_and_never_a_whole_chunk(name: str) -> None:
    chunks = _chunks(name)
    seen: set[str] = set()
    for prev, cur in zip(chunks, chunks[1:]):
        if cur.overlap:
            assert cur.overlap < len(cur.text)
            assert cur.overlap <= len(prev.text) // 2
            assert prev.text.endswith(cur.text[: cur.overlap])
            assert prev.text != cur.text[: cur.overlap]
    for c in chunks:
        assert c.embed_text not in seen
        seen.add(c.embed_text)
        assert c.text[c.overlap :].strip(), "a chunk consisting only of overlap"


def test_overlap_zero_config_has_no_overlap_and_exact_partition() -> None:
    config = ChunkingConfig(max_tokens=64, overlap_tokens=0, min_tokens=0)
    chunks = _chunks("plain", config)
    assert all(c.overlap == 0 for c in chunks)
    assert _non_ws(_body(chunks)) == _non_ws(PLAIN)


# --- edge inputs -----------------------------------------------------------------------------------

@dataclass(frozen=True)
class _TinyConfig:
    # Below the RM.2 contract's max_tokens floor (32), so a plain attribute object: chunk() accepts
    # any object with the four attributes.
    max_tokens: int = 16
    overlap_tokens: int = 2
    min_tokens: int = 0
    chunker_version: int = 1


TINY = _TinyConfig()


@pytest.mark.parametrize("mime", ["text/markdown", "text/html", "text/plain"])
@pytest.mark.parametrize("content", ["", " ", "\n\n\t \n", "\r\n\r\n", "  "])
def test_whitespace_only_documents_give_no_chunks(content: str, mime: str) -> None:
    assert chunk(extract(content, mime), SMALL) == []


def test_only_headings_give_no_chunks() -> None:
    assert chunk(extract("# A\n\n## B\n\n### C\n", "text/markdown"), SMALL) == []
    assert chunk(extract("<h1>A</h1><h2>B</h2>", "text/html"), SMALL) == []


def test_heading_at_end_of_file_without_newline_is_harmless() -> None:
    for content in ("# Top\n\nSome body text here.\n\n## Trailing", "# Top\n\nSome body text here.\n## Trailing\n"):
        chunks = chunk(extract(content, "text/markdown"), ChunkingConfig(max_tokens=64, overlap_tokens=0, min_tokens=0))
        assert [c.heading_path for c in chunks] == [("Top",)]
        assert "Some body text here." in chunks[0].text
    doc = extract("# Top\n\nbody\n\n## Trailing", "text/markdown")
    assert doc.sections[-1].heading_path == ("Top", "Trailing") and doc.sections[-1].text == ""


def test_very_long_single_line_is_cut_under_the_cap_and_loses_nothing() -> None:
    line = "x" * 10_000
    chunks = chunk(extract("# T\n\n" + line, "text/markdown"), SMALL)
    assert len(chunks) >= 20 and all(c.approx_tokens <= SMALL.max_tokens for c in chunks)
    assert _body(chunks) == line
    spaced = " ".join(f"w{i}" for i in range(5000))  # one 'line', many words
    chunks = chunk(extract(spaced, "text/plain"), SMALL)
    assert all(c.approx_tokens <= SMALL.max_tokens for c in chunks)
    assert _body(chunks).replace(" ", "") == spaced.replace(" ", "")


def test_mixed_crlf_cr_and_lf_chunk_like_lf() -> None:
    lf = "# Title\n\nFirst paragraph of text.\n\n## Sub\n\nSecond paragraph\nwith two lines.\n"
    mixed = "# Title\r\n\r\nFirst paragraph of text.\r\r## Sub\n\nSecond paragraph\r\nwith two lines.\n"
    a = chunk(extract(lf, "text/markdown"), SMALL)
    b = chunk(extract(mixed, "text/markdown"), SMALL)
    assert [c.embed_text_hash for c in a] == [c.embed_text_hash for c in b]
    assert all("\r" not in c.embed_text for c in b)


def test_bom_and_nfd_input_chunk_like_clean_nfc() -> None:
    clean = "# Café\n\nCrème brûlée recipe text goes here.\n"
    dirty = "﻿" + "# Café\n\nCrème brûlée recipe text goes here.\n"
    assert [c.embed_text_hash for c in chunk(extract(clean, "text/markdown"), SMALL)] == [
        c.embed_text_hash for c in chunk(extract(dirty, "text/markdown"), SMALL)
    ]


FAMILY = "\U0001f468‍\U0001f469‍\U0001f467"  # ZWJ family: 5 code points, 1 grapheme


def test_emoji_runs_are_cut_on_codepoint_boundaries_only() -> None:
    text = "\U0001f600" * 3000
    chunks = chunk(extract(text, "text/plain"), TINY)
    cap = TINY.max_tokens * 4
    assert _body(chunks) == text  # lossless once overlaps are removed
    assert len(chunks) >= len(text) // cap  # really cut, not one oversized chunk
    for c in chunks:
        assert set(c.text) == {"\U0001f600"}  # only whole emoji, nothing else
        assert len(c.embed_text) <= cap and c.approx_tokens <= TINY.max_tokens


def test_zwj_run_is_cut_by_codepoints_and_loses_nothing() -> None:
    # Character cuts back off to grapheme boundaries: every chunk holds whole ZWJ families, and no
    # joiner is dropped or left dangling.
    text = FAMILY * 400
    chunks = chunk(extract(text, "text/plain"), TINY)
    assert _body(chunks) == text
    assert all(c.approx_tokens <= TINY.max_tokens for c in chunks)
    for c in chunks:
        c.embed_text.encode("utf-8")  # raises on a lone surrogate
        assert c.text.replace(FAMILY, "") == ""
        assert not c.text.startswith("\u200d") and not c.text.endswith("\u200d")


# --- golden hashes (chunker_version 1; a change here means every knowledge base re-embeds) -----------

GOLDEN = {
    # readme and cjk changed deliberately before v1 was frozen (review item 7): a tiny section merged
    # into the next one now has the receiving section's heading line between the carried text and
    # its body. html and plain merge nothing at this config and are unchanged.
    "readme": (16, "5ffa12873539f730063f50a5afeaf61019ed95df17b6c5374eb09b8ad63cfa9f", "09dfdfb48a26", "a63b1e15dcc8"),
    "html": (3, "d47215b364655bd12f44354a09fc8e872b9778a13e7de8eccc2b7cc539cdf7dd", "36cce6f814bf", "384d5a6db34a"),
    "plain": (10, "34a47e10fb6c464b2cbe78c7c5bdedc44af083b50da10f9f6582fed354df4249", "ffb5dd8aacd9", "6748c83d5f7b"),
    "cjk": (4, "9f5d1b0fedd16dc765cd42162edef67ab23ac8de546959aae1540930e65deaa9", "ff1239c61977", "267d2664f341"),
}


@pytest.mark.parametrize("name", sorted(GOLDEN))
def test_golden_hashes_for_the_sample_docs(name: str) -> None:
    import hashlib

    chunks = _chunks(name)
    count, aggregate, first, second = GOLDEN[name]
    assert len(chunks) == count
    assert hashlib.sha256("\n".join(c.embed_text_hash for c in chunks).encode()).hexdigest() == aggregate
    assert [c.embed_text_hash[:12] for c in chunks[:2]] == [first, second]
    # The goldens hold for this chunker identity; a Python upgrade with a new Unicode database shows up here.
    assert CHUNKER_ID == "1/unicode-15.1.0"


@pytest.mark.parametrize("name", sorted(GOLDEN))
def test_sample_docs_are_stable_across_repeated_extraction(name: str) -> None:
    assert _chunks(name) == _chunks(name)
    content, mime = _docs()[name]
    crlf = content.replace("\n", "\r\n")
    a = chunk(extract(content, mime, title="Doc", source_url=URL), SMALL)
    b = chunk(extract(crlf, mime, title="Doc", source_url=URL), SMALL)
    assert [c.embed_text_hash for c in a] == [c.embed_text_hash for c in b]
