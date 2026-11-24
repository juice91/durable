"""Structured formats keep the header comment with its facts and get a title (yaml extractor 3;
json stays 1, since its title fallback never reaches a stored document), and every other format chunks byte-identically.

The fix is extractor-only: ``chunk()`` is untouched, so markdown, html, text, rst, pdf and doxygen
markdown must chunk exactly as before it. The GOLDEN digests were recorded on d085138 (before the
change) with chunker v1 and v2; each covers every field of every chunk.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from eap_knowledge import EXTRACTOR_VERSIONS, ChunkingConfig, chunk, extract
from eap_knowledge.formats.doxygen import extract_doxygen_markdown
from eap_knowledge.formats.pdf import extract_pdf
from eap_knowledge.formats.rst import extract_rst
from eap_knowledge.formats.structured import PREAMBLE_SHARE, TITLE_MAX_CHARS, extract_json, extract_yaml
from eap_knowledge.model import Document
from knowledge_tests.pdf_fixtures import manual_pdf

MARKDOWN = """# Guide

Short intro.

## Install

Run `pip install eap`.

```sh
pip install eap
```

## Configure

A longer paragraph about configuration. """ + "Settings matter. " * 60 + """

### Advanced

Tiny.
"""

HTML = """<html><head><title>Board page</title></head><body>
<h1>Board</h1><p>The board has two UARTs.</p>
<h2>Pins</h2><p>Pin 4 is TX.</p><h2>Power</h2><p>""" + "Use 3.3 V only. " * 50 + """</p>
</body></html>"""

TEXT = "Plain notes.\n\n" + "A line of plain text. " * 120

RST = """Device guide
============

Intro paragraph.

Setup
-----

.. code-block:: sh

   make flash

Pins
----

""" + "Pin text. " * 80 + "\n"

DOXYGEN = """# UART driver {#uart}

@brief The UART API.

## Functions

`uart_init()` starts the port.
"""


def _documents() -> dict[str, Document]:
    return {
        "markdown": extract(MARKDOWN, "text/markdown", title="Guide", source_url="https://d.example/guide"),
        "html": extract(HTML, "text/html", source_url="https://d.example/board"),
        "text": extract(TEXT, "text/plain", title="notes.txt"),
        "rst": extract_rst(RST, title="device.rst", source_url="https://d.example/device"),
        "pdf": extract_pdf(manual_pdf(), title="trm.pdf").document,
        "doxygen": extract_doxygen_markdown(DOXYGEN, title="uart.md"),
    }


def _digest(document: Document, version: int) -> str:
    config = ChunkingConfig(max_tokens=128, min_tokens=32, overlap_tokens=16, chunker_version=version)
    rows = [
        [c.ordinal, list(c.heading_path), c.title, c.text, c.embed_text, c.embed_text_hash, c.approx_tokens,
         c.anchor, c.overlap, c.page, c.page_end, c.condition]
        for c in chunk(document, config)
    ]
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()


GOLDEN: dict[tuple[str, int], str] = {
    ("markdown", 1): "e6f918d820b7272555a4d144bcd3c3b154a9cdd1dd1019085701d2fb8dc2f730",
    ("markdown", 2): "f0495ff173b62a52cd4b5e2c3cf6b233a4af5120adc841d79917319df0b3edb2",
    ("html", 1): "f75078f473b3fd6d7b3f63bdc45a70e93cf4005b4f7c5b4ae992e0a7edbd734d",
    ("html", 2): "63316bec809235a6ab888249815d7437090da107401cd74c76ddfb275d5c5360",
    ("text", 1): "162c1ff31182012567dfd2e5a9c58e07069023f84fd9ab675874daa02dad2732",
    ("text", 2): "162c1ff31182012567dfd2e5a9c58e07069023f84fd9ab675874daa02dad2732",
    ("rst", 1): "bb1f522b891cbd7910ff447078cc45a3badb3d32b8a45027adb03ccddb928267",
    ("rst", 2): "ff0b44f8072897d95b528a8eb536e32339db3e3f61c6b41dfe40af4d6c8b8a11",
    ("pdf", 1): "e34dd3d5d35805773552515d4db644c408636f9c1248f1b719cb212301563959",
    ("pdf", 2): "e34dd3d5d35805773552515d4db644c408636f9c1248f1b719cb212301563959",
    ("doxygen", 1): "1264b7a58e6391d8e200de319f2396f36db84ac3c07b79db93c12bfefea4b4a7",
    ("doxygen", 2): "1264b7a58e6391d8e200de319f2396f36db84ac3c07b79db93c12bfefea4b4a7",
}


@pytest.mark.parametrize("fmt", ["markdown", "html", "text", "rst", "pdf", "doxygen"])
@pytest.mark.parametrize("version", [1, 2])
def test_other_formats_chunk_byte_identically(fmt: str, version: int) -> None:
    assert _digest(_documents()[fmt], version) == GOLDEN[(fmt, version)]


# --- yaml / json ------------------------------------------------------------------------------------

TI_ADS1X1X_BASE = Path("~/ti-rag/repos/simplelink-zephyr/dts/bindings/adc/ti,ads1x1x-base.yaml").expanduser()
ADS_URL = "https://github.com/TexasInstruments/simplelink-zephyr/blob/main/dts/bindings/adc/ti,ads1x1x-base.yaml"
ADS_SNAPSHOT = """\
# Common fields for Acme Semiconductor ADS1x1x

include: [adc-controller.yaml, i2c-device.yaml]

properties:
  "#io-channel-cells":
    const: 1

io-channel-cells:
  - input
"""
# The TI quality run's chunking config (chunker v2), and v1.
TI_V2 = ChunkingConfig(max_tokens=512, min_tokens=64, overlap_tokens=64, chunker_version=2)
TI_V1 = ChunkingConfig(max_tokens=512, min_tokens=64, overlap_tokens=64, chunker_version=1)


def test_versions_bump_yaml_only():
    assert EXTRACTOR_VERSIONS == {"rst": 1, "pdf": 1, "yaml": 3, "json": 1, "markdown-doxygen": 1, "code": 1}


@pytest.mark.parametrize("config", [TI_V2, TI_V1], ids=["v2", "v1"])
@pytest.mark.parametrize("source", ["snapshot", "ti-repo"])
def test_ads1x1x_base_is_one_chunk_with_comment_and_facts(source: str, config: ChunkingConfig) -> None:
    if source == "ti-repo":
        if not TI_ADS1X1X_BASE.is_file():
            pytest.skip("the TI checkout is not on this host")
        text = TI_ADS1X1X_BASE.read_text()
    else:
        text = ADS_SNAPSHOT
    doc = extract_yaml(text, source_url=ADS_URL, max_tokens=config.max_tokens)
    assert doc.title == "ti,ads1x1x-base.yaml"
    chunks = chunk(doc, config)
    assert len(chunks) == 1
    only = chunks[0]
    assert only.text.startswith("Common fields for Acme Semiconductor ADS1x1x\n\ninclude:\n- adc-controller.yaml")
    assert "properties:\n  '#io-channel-cells':\n    const: 1" in only.text
    assert "io-channel-cells:\n- input" in only.text
    assert only.embed_text.startswith("path: ti,ads1x1x-base.yaml > include\n\ntitle: include\n\nCommon fields")


def test_a_long_comment_stays_its_own_bounded_section():
    sentence = "This binding describes a long list of register fields and their behaviour. "
    long_head = "".join(f"# {sentence}{i}\n" for i in range(60))  # ~4.7k chars: over max_tokens // 4
    doc = extract_yaml(long_head + "\ninclude: base.yaml\nreg:\n  maxItems: 1\n", max_tokens=128)
    assert doc.sections[0].level == 0 and doc.sections[0].text.startswith(sentence)
    assert doc.sections[1].text == "include: base.yaml"  # the facts do not carry the long comment
    for version in (1, 2):
        config = ChunkingConfig(max_tokens=128, min_tokens=32, overlap_tokens=16, chunker_version=version)
        chunks = chunk(doc, config)
        assert len(chunks) > 1
        assert all(len(c.embed_text) <= config.max_tokens * 4 for c in chunks)


def test_the_short_threshold_is_max_tokens_over_preamble_share():
    max_tokens = 128
    limit = max_tokens // PREAMBLE_SHARE * 4  # characters at exactly max_tokens // 4 tokens
    at = "# " + "x" * limit + "\na: 1\n"
    over = "# " + "x" * (limit + 1) + "\na: 1\n"
    assert extract_yaml(at, max_tokens=max_tokens).sections[0].text == ""
    assert extract_yaml(at, max_tokens=max_tokens).sections[1].text == "x" * limit + "\n\na: 1"
    assert extract_yaml(over, max_tokens=max_tokens).sections[0].text == "x" * (limit + 1)
    assert extract_yaml(over, max_tokens=max_tokens).sections[1].text == "a: 1"


def test_the_preamble_never_makes_a_section_split():
    # `b` fits max_tokens alone; with the preamble it would not. It still stays one section.
    body = "b:\n" + "".join(f"  k{i}: v\n" for i in range(20))
    doc = extract_yaml("# head note\n" + body, max_tokens=64)
    assert [(s.level, s.heading) for s in doc.sections] == [(0, ""), (1, "b")]
    assert doc.sections[1].text.startswith("head note\n\nb:\n  k0: v")


def test_a_split_first_section_carries_the_preamble_into_its_first_child():
    body = "props:\n" + "".join(f"  p{i}: {'word ' * 20}\n" for i in range(6))
    doc = extract_yaml("# Common fields\n" + body, max_tokens=64)
    assert (doc.sections[1].heading, doc.sections[1].text) == ("props", "")  # heading only
    assert doc.sections[2].heading == "p0"
    assert doc.sections[2].text.startswith("Common fields\n\np0: word")
    assert sum("Common fields" in s.text for s in doc.sections) == 1
    for version in (1, 2):  # even with a small min_tokens, the comment is never a chunk on its own
        config = ChunkingConfig(max_tokens=64, min_tokens=4, overlap_tokens=0, chunker_version=version)
        hit = [c for c in chunk(doc, config) if "Common fields" in c.text]
        assert len(hit) == 1 and "p0: word" in hit[0].text


def test_multi_document_stream_short_preamble_opens_the_first_key_of_document_1():
    doc = extract_yaml("# top\n---\na: 1\n---\nb: 2\n")
    assert [(s.level, s.heading, s.text) for s in doc.sections] == [
        (0, "", ""), (1, "Document 1", ""), (2, "a", "top\n\na: 1"), (1, "Document 2", ""), (2, "b", "b: 2"),
    ]
    for version in (1, 2):
        config = ChunkingConfig(max_tokens=512, min_tokens=1, overlap_tokens=0, chunker_version=version)
        hit = [c for c in chunk(doc, config) if "top" in c.text]
        assert len(hit) == 1 and "a: 1" in hit[0].text


def test_multi_document_stream_with_a_list_first_document_opens_document_1():
    doc = extract_yaml("# top\n---\n- x\n---\nb: 2\n")
    assert [(s.level, s.heading, s.text) for s in doc.sections] == [
        (0, "", ""), (1, "Document 1", "top\n\n- x"), (1, "Document 2", ""), (2, "b", "b: 2"),
    ]


def test_multi_document_stream_with_a_split_first_key_opens_its_first_child():
    big = "props:\n" + "".join(f"  p{i}: {'word ' * 20}\n" for i in range(6))
    doc = extract_yaml("# top\n---\n" + big + "---\nb: 2\n", max_tokens=64)
    got = [(s.level, s.heading) for s in doc.sections[:4]]
    assert got == [(0, ""), (1, "Document 1"), (2, "props"), (3, "p0")]
    assert [s.text for s in doc.sections[:3]] == ["", "", ""]
    assert doc.sections[3].text.startswith("top\n\np0: word")
    assert sum("top" in s.text for s in doc.sections) == 1


def test_a_long_file_name_is_clipped_like_other_titles():
    url = "https://example.test/dir/" + "n" * 5000 + "%00%1b.yaml"
    title = extract_yaml("a: 1\n", source_url=url).title
    assert title == "n" * TITLE_MAX_CHARS
    assert extract_json('{"a": 1}', source_url="u/" + "j" * 300).title == "j" * TITLE_MAX_CHARS
    assert extract_yaml("a: 1\n", source_url="u/" + "x" * (TITLE_MAX_CHARS - 1) + "%20%20y").title == "x" * (TITLE_MAX_CHARS - 1)


def test_a_comment_only_file_still_extracts_its_text():
    doc = extract_yaml("# only a comment\n")
    assert [(s.level, s.text) for s in doc.sections] == [(0, "only a comment")]


@pytest.mark.parametrize(
    ("text", "title"),
    [
        ("title: UART binding\ndescription: d\n", "UART binding"),
        ("name: my-package\ndescription: d\n", "my-package"),
        ("description: Texas Instrument ADS1115 I2C ADC\ncompatible: x\n", "Texas Instrument ADS1115 I2C ADC"),
        ("description: |\n  line one\n  line two\n", "ti,ads1x1x-base.yaml"),  # multi-line: the file name
        (f"title: {'t' * (TITLE_MAX_CHARS + 1)}\nname: short\n", "short"),  # too long: the next key
        ("title: 7\nname: [a]\n", "ti,ads1x1x-base.yaml"),  # not strings
        ("title: '  spaced   out  '\n", "spaced out"),
        ("- a\n- b\n", "ti,ads1x1x-base.yaml"),  # not a mapping
    ],
)
def test_yaml_title_fallback_order(text: str, title: str) -> None:
    assert extract_yaml(text, source_url=ADS_URL).title == title


def test_the_caller_title_wins_and_no_url_means_no_file_name():
    assert extract_yaml("title: In file\n", title="dts/bindings/x.yaml", source_url=ADS_URL).title == "dts/bindings/x.yaml"
    assert extract_yaml("a: 1\n", title="   ", source_url=ADS_URL).title == "ti,ads1x1x-base.yaml"
    assert extract_yaml("a: 1\n").title == ""
    assert extract_yaml("a: 1\n", source_url="https://example.test/").title == ""


@pytest.mark.parametrize(
    ("url", "name"),
    [
        ("https://example.test/dir/my%20file.json?raw=1#frag", "my file.json"),
        ("https://example.test/dir/cfg.json/", "cfg.json"),
        ("docs/config/app.json", "app.json"),
        ("https://[bad/", ""),  # invalid URL: no file name, no error
    ],
)
def test_file_name_from_source_url(url: str, name: str) -> None:
    assert extract_json('{"a": 1}', source_url=url).title == name


def test_json_without_comments_is_unchanged_apart_from_the_title():
    text = '{"compatible": "ti,x", "properties": {"reg": {"maxItems": 1}}}'
    doc = extract_json(text, source_url="https://example.test/bindings/x.json")
    assert doc.title == "x.json"
    assert [(s.level, s.heading, s.text, s.anchor) for s in doc.sections] == [
        (0, "", "", None),
        (1, "compatible", "compatible: ti,x", "compatible"),
        (1, "properties", "properties:\n  reg:\n    maxItems: 1", "properties"),
    ]
    # Chunk texts are the same as with no title; only the embed prefix gains the title.
    for config in (TI_V1, TI_V2):
        titled, untitled = chunk(doc, config), chunk(replace(doc, title=""), config)
        assert [c.text for c in titled] == [c.text for c in untitled]
        assert all(c.embed_text.startswith("path: x.json") for c in titled)
    assert extract_json('{"title": "Cfg", "a": 1}').title == "Cfg"
