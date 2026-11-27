"""RAG P1 quality fix 1: the YAML extractor keeps comments (extractor version 2).

The live Acme check lost zephyr #2 (2 -> 0): ti,ads1x1x-base.yaml keeps its only description in its
header comment, and v1 dropped every comment. The leading comment block is now the document-level
preamble (the level-0 section's body) and every other comment rides with its key's section.
"""

from __future__ import annotations

import pytest
from eap_schemas.knowledge import ChunkingConfig

from eap_knowledge import EXTRACTOR_VERSIONS, ExtractionFailed, chunk
from eap_knowledge.formats.structured import MAX_YAML_ALIASES, extract_json, extract_yaml

URL = "https://example.test/dts/bindings/adc/ti,ads1x1x-base.yaml"

# Snapshot of simplelink-zephyr dts/bindings/adc/ti,ads1x1x-base.yaml (verbatim).
ADS1X1X_BASE_YAML = """\
# Common fields for Acme Semiconductor ADS1x1x

include: [adc-controller.yaml, i2c-device.yaml]

properties:
  "#io-channel-cells":
    const: 1

io-channel-cells:
  - input
"""

# Snapshot of simplelink-zephyr dts/bindings/adc/ti,ads1115.yaml (verbatim): a child binding.
ADS1115_YAML = """\
description: Texas Instrument ADS1115 I2C ADC

compatible: "ti,ads1115"

include: ti,ads1x1x-base.yaml
"""


def _sections(doc):
    return [(s.level, s.heading, s.text) for s in doc.sections]


def test_yaml_extractor_version_is_bumped_for_re_extract():
    assert EXTRACTOR_VERSIONS["yaml"] == 3  # 3: short preamble opens the first section; title fallback
    assert EXTRACTOR_VERSIONS["json"] == 1  # not bumped: the title fallback never changes a stored document


def test_ads1x1x_base_keeps_its_header_comment_with_the_first_section():
    doc = extract_yaml(ADS1X1X_BASE_YAML, source_url=URL)
    assert _sections(doc) == [
        (0, "", ""),
        (1, "include", "Common fields for Acme Semiconductor ADS1x1x\n\ninclude:\n- adc-controller.yaml\n- i2c-device.yaml"),
        (1, "properties", "properties:\n  '#io-channel-cells':\n    const: 1"),
        (1, "io-channel-cells", "io-channel-cells:\n- input"),
    ]
    assert doc.sections[0].anchor is None
    assert [s.anchor for s in doc.sections[1:]] == ["include", "properties", "io-channel-cells"]
    # The preamble counts towards the extracted text ("#io-channel-cells" is a quoted key, not a comment).
    assert doc.extracted_chars == sum(len("".join((s.heading + s.text).split())) for s in doc.sections)
    assert doc.extracted_chars > extract_yaml(ADS1X1X_BASE_YAML.split("\n", 2)[2]).extracted_chars


def test_ads1x1x_base_chunk_carries_the_description():
    doc = extract_yaml(ADS1X1X_BASE_YAML, source_url=URL)
    chunks = chunk(doc, ChunkingConfig())
    assert len(chunks) == 1
    assert chunks[0].text == (
        "Common fields for Acme Semiconductor ADS1x1x\n\n"
        "include:\n- adc-controller.yaml\n- i2c-device.yaml\n\n"
        "# properties\n\nproperties:\n  '#io-channel-cells':\n    const: 1\n\n"
        "# io-channel-cells\n\nio-channel-cells:\n- input"
    )


def test_a_file_without_comments_is_unchanged():
    doc = extract_yaml(ADS1115_YAML)
    assert _sections(doc) == [
        (0, "", ""),
        (1, "description", "description: Texas Instrument ADS1115 I2C ADC"),
        (1, "compatible", "compatible: ti,ads1115"),
        (1, "include", "include: ti,ads1x1x-base.yaml"),
    ]


COMMENTED = """\
# Header line 1
#
# Header line 2

# about a
a: 1  # inline a
# about b
b:
  # about b.x
  x: 1
  y: [1, 2]  # in y
# trailing
"""


def test_comments_attach_to_keys_above_inline_inside_and_trailing():
    doc = extract_yaml(COMMENTED)
    assert _sections(doc) == [
        (0, "", ""),
        # the leading block (the bare "#" line dropped), short, so it opens the first section
        (1, "a", "Header line 1\nHeader line 2\n\n# about a\n# inline a\na: 1"),
        (1, "b", "# about b\n# about b.x\n# in y\n# trailing\nb:\n  x: 1\n  y:\n  - 1\n  - 2"),
    ]


def test_a_split_section_gives_comments_to_its_sub_keys():
    text = COMMENTED.replace("x: 1", "x: " + "word " * 50)
    doc = extract_yaml(text, max_tokens=20)
    by = {s.heading: s.text for s in doc.sections}
    assert by["b"] == "# about b"
    assert by["x"].startswith("# about b.x\nx: word word")
    assert by["y"] == "# in y\n# trailing\ny:\n- 1\n- 2"
    assert [s.anchor for s in doc.sections if s.heading in ("x", "y")] == ["b.x", "b.y"]


def test_a_comment_block_after_a_blank_line_goes_to_the_first_key_not_the_preamble():
    doc = extract_yaml("# file header\n\n# about a\na: 1\n")
    assert _sections(doc)[:2] == [(0, "", ""), (1, "a", "file header\n\n# about a\na: 1")]


def test_a_header_directly_above_the_first_key_is_still_the_preamble():
    doc = extract_yaml("# Common fields\ninclude: base.yaml\n")
    assert _sections(doc) == [(0, "", ""), (1, "include", "Common fields\n\ninclude: base.yaml")]


def test_licence_lines_are_dropped():
    text = "# Copyright (c) 2024 Acme Semiconductor\n# SPDX-License-Identifier: Apache-2.0\n# Real text\n\na: 1\n"
    assert _sections(extract_yaml(text))[:2] == [(0, "", ""), (1, "a", "Real text\n\na: 1")]


def test_hash_inside_strings_and_block_scalars_is_not_a_comment():
    text = 'a: "x # not a comment"\nb: |\n  line\n  # literal, not a comment\nc: \'#\' # real\n'
    doc = extract_yaml(text)
    by = {s.heading: s.text for s in doc.sections}
    assert by["a"] == "a: 'x # not a comment'"
    assert by["b"] == "b: |\n  line\n  # literal, not a comment"  # block scalar content, kept as text
    assert by["c"] == "# real\nc: '#'"
    assert doc.sections[0].text == ""


def test_multi_document_stream_comments():
    doc = extract_yaml("# top\n---\n# d1\na: 1\n---\n# d2\n- 1\n")
    assert _sections(doc) == [
        (0, "", ""),
        (1, "Document 1", "d1"),
        (2, "a", "top\n\na: 1"),  # yaml 3: the short preamble opens the first leaf section
        (1, "Document 2", "d2\n\n- 1"),
    ]


def test_a_comment_only_file_extracts_its_text():
    doc = extract_yaml("# only a comment\n# and another\n")
    assert _sections(doc) == [(0, "", "only a comment\nand another")]
    assert doc.extracted_chars > 0


def test_scalar_document_with_comments():
    assert _sections(extract_yaml("# head\n\n# about\nhello\n")) == [(0, "", "head\n\nabout\n\nhello")]


def test_alias_value_keeps_comments_on_its_key():
    text = "base: &b\n  x: 1\n# about copy\ncopy: *b  # aliased\n"
    by = {s.heading: s.text for s in extract_yaml(text).sections}
    assert by["copy"] == "# about copy\n# aliased\ncopy:\n  x: 1"
    assert by["base"] == "base:\n  x: 1"


def test_merge_key_comments_go_to_a_neighbour_and_nothing_breaks():
    text = "base: &b {x: 1}\nm:\n  # merged in\n  <<: *b\n  y: 2\n"
    by = {s.heading: s.text for s in extract_yaml(text).sections}
    assert by["m"] == "# merged in\nm:\n  x: 1\n  y: 2"


def test_a_raw_control_character_is_invalid_yaml_not_a_crash():
    """The reader refuses it in the loader's constructor: extract_failed (it was a raw ReaderError)."""
    with pytest.raises(ExtractionFailed) as err:
        extract_yaml("# bell\x07 here\na: 1\n")
    assert err.value.code == "extract_failed" and "bell" not in str(err.value)


@pytest.mark.parametrize("n", [MAX_YAML_ALIASES + 1])
def test_the_alias_limit_still_applies_with_comments(n):
    text = "# header\na: &a 1\n" + "".join(f"k{i}: *a  # c\n" for i in range(n))
    with pytest.raises(ExtractionFailed) as err:
        extract_yaml(text)
    assert err.value.code == "extract_limit"


def test_billion_laughs_with_comments_still_fails_the_expansion_budget():
    lines = ["# laughs", 'a: &a ["lol","lol","lol","lol","lol","lol","lol","lol","lol"]  # nine']
    for i, name in enumerate("bcdefghi"):
        prev = "abcdefghi"[i]
        lines.append(f"{name}: &{name} [{', '.join([f'*{prev}'] * 9)}]  # x9")
    with pytest.raises(ExtractionFailed) as err:
        extract_yaml("\n".join(lines) + "\n")
    assert err.value.code == "extract_limit"


def test_invalid_yaml_after_comments_still_reports_a_position_only():
    with pytest.raises(ExtractionFailed) as err:
        extract_yaml("# secret comment text\na: [1, 2\n")
    assert err.value.code == "extract_failed"
    assert "secret" not in str(err.value) and "line" in str(err.value)


def test_json_is_unchanged():
    doc = extract_json('{"title": "Cfg", "a": {"b": 1}}')
    assert _sections(doc) == [(0, "", ""), (1, "title", "title: Cfg"), (1, "a", "a:\n  b: 1")]


# ------------------------------------------------------------------ the copied scanner's guard (review MINOR 4)


def _code_lines(source: str) -> list[str]:
    """The method's code lines: no docstring, no comment-only or blank lines, trailing comments
    cut, indentation normalised and quotes unified (upstream uses single quotes)."""
    import ast
    import textwrap

    tree = ast.parse(textwrap.dedent(source))
    fn = tree.body[0]
    assert isinstance(fn, ast.FunctionDef)
    body = fn.body
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
        body = body[1:]
    return [ast.unparse(stmt) for stmt in body]


def test_the_scanner_copy_matches_pyyaml_except_the_recording():
    """scan_to_next_token is copied from PyYAML: a PyYAML release that changes it must fail here,
    not silently diverge. Removing our recording statements must give upstream's code exactly."""
    import inspect

    import yaml
    from yaml.scanner import Scanner

    from eap_knowledge.formats.structured import _CountingLoader

    assert yaml.__version__ == "6.0.3"
    upstream = _code_lines(inspect.getsource(Scanner.scan_to_next_token))
    ours = _code_lines(inspect.getsource(_CountingLoader.scan_to_next_token))

    def strip(lines: list[str]) -> str:
        import ast
        import re

        text = "\n".join(lines)
        tree = ast.parse(text)

        class Drop(ast.NodeTransformer):
            def generic_visit(self, node):  # type: ignore[override]
                super().generic_visit(node)
                for field in ("body", "orelse"):
                    stmts = getattr(node, field, None)
                    if isinstance(stmts, list):
                        kept = [s for s in stmts if not re.search(r"eap_|\bline, start\b", ast.unparse(s))]
                        setattr(node, field, kept or ([ast.Pass()] if field == "body" and stmts else []))
                return node

        return ast.unparse(Drop().visit(tree))

    assert strip(ours) == strip(upstream) == "\n".join(upstream)


def test_yaml_comments_are_bounded():
    from eap_knowledge.formats.structured import MAX_YAML_COMMENTS

    ok = "a: 1\n" + "#\n" * MAX_YAML_COMMENTS
    extract_yaml(ok, title="t", structured_cap=len(ok))
    over = "a: 1\n" + "#\n" * (MAX_YAML_COMMENTS + 1)
    with pytest.raises(ExtractionFailed) as err:
        extract_yaml(over, title="t", structured_cap=len(over))
    assert err.value.code == "extract_limit" and "comments" in str(err.value)


def test_yaml_over_the_structured_cap_is_refused_before_scanning():
    text = "# note\na: 1\n"
    extract_yaml(text, title="t", structured_cap=len(text))
    with pytest.raises(ExtractionFailed) as err:
        extract_yaml(text, title="t", structured_cap=len(text) - 1)
    assert err.value.code == "extract_limit"
