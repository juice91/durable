"""Structured title precedence and short-preamble placement (yaml 3; json stays 1)."""

from __future__ import annotations

import json

import pytest

from eap_knowledge import ChunkingConfig, approx_tokens, chunk
from eap_knowledge.formats.structured import TITLE_MAX_CHARS, extract_json, extract_yaml

URL = "https://example.com/a/b/ti%2Cfoo%20bar.yaml?x=1#frag"


def test_caller_title_wins_and_blank_falls_through():
    assert extract_yaml("title: T\n", title="Mine", source_url=URL).title == "Mine"
    assert extract_yaml("title: T\n", title="   ", source_url=URL).title == "T"


def test_key_order_title_name_description_then_file_name():
    assert extract_yaml("description: D\nname: N\ntitle: T\n").title == "T"
    assert extract_yaml("description: D\nname: N\n").title == "N"
    assert extract_yaml("description: D\n").title == "D"
    assert extract_yaml("a: 1\n", source_url=URL).title == "ti,foo bar.yaml"
    assert extract_json('{"a": 1}', source_url="/x/y.json").title == "y.json"


def test_no_title_and_no_url_is_empty():
    assert extract_yaml("a: 1\n").title == ""
    assert extract_json('{"a": 1}').title == ""
    assert extract_yaml("a: 1\n", source_url="").title == ""


def test_multiline_long_and_non_string_values_are_skipped():
    assert extract_yaml("title: |\n  one\n  two\nname: N\n").title == "N"
    assert extract_yaml("title: " + "x" * (TITLE_MAX_CHARS + 1) + "\nname: N\n").title == "N"
    assert extract_yaml("title: " + "x" * TITLE_MAX_CHARS + "\n").title == "x" * TITLE_MAX_CHARS
    for bad in ("5", "[a, b]", "{k: v}", "true", "null"):
        assert extract_yaml(f"title: {bad}\nname: N\n").title == "N"
    assert extract_json('{"title": 5, "name": ["a"], "description": "D"}').title == "D"
    assert extract_json(json.dumps({"title": "a\nb", "name": "N"})).title == "N"
    assert extract_json(json.dumps({"description": "x" * 500}), source_url="u/f.json").title == "f.json"


def test_nested_key_is_not_top_level():
    assert extract_yaml("meta:\n  title: Deep\n", source_url="u/f.yaml").title == "f.yaml"
    assert extract_json('{"meta": {"name": "Deep"}}', source_url="u/f.json").title == "f.json"


def test_first_document_only_in_a_stream():
    assert extract_yaml("a: 1\n---\ntitle: Second\n", source_url="u/f.yaml").title == "f.yaml"


def cfg(v: int, mt: int = 512) -> ChunkingConfig:
    return ChunkingConfig(max_tokens=mt, overlap_tokens=0, min_tokens=8, chunker_version=v)


@pytest.mark.parametrize("v", [1, 2])
def test_short_comment_joins_first_section_chunk(v):
    d = extract_yaml("# Header note\n\ncompatible: foo\nbar: 1\n")
    cs = chunk(d, cfg(v))
    first = next(c for c in cs if "compatible" in c.text)
    assert "Header note" in first.text
    assert first.text.index("Header note") < first.text.index("compatible")
    assert sum("Header note" in c.text for c in cs) == 1


@pytest.mark.parametrize("v", [1, 2])
def test_short_comment_joins_first_section_in_a_multi_document_stream(v):
    cs = chunk(extract_yaml("# Note\na: 1\n---\nb: 2\n"), cfg(v))
    assert any("Note" in c.text and "a:" in c.text for c in cs)


def comment_of(tokens_text: str) -> str:
    return "# " + tokens_text + "\n"


def test_boundary_at_and_over_max_tokens_quarter():
    mt = 128
    limit = mt // 4
    words = ""
    while True:  # largest comment whose text is <= limit tokens
        nxt = words + "w"
        if approx_tokens(nxt) > limit:
            break
        words = nxt
    at, over = words, words + "w"
    assert approx_tokens(at) == limit and approx_tokens(over) == limit + 1
    s_at = extract_yaml(comment_of(at) + "k: 1\n", max_tokens=mt).sections
    s_over = extract_yaml(comment_of(over) + "k: 1\n", max_tokens=mt).sections
    assert at in "".join(s.text for s in s_at if "k" in s.text) and s_at[0].text == ""
    assert s_over[0].text == over and over not in "".join(s.text for s in s_over[1:])


@pytest.mark.parametrize("v", [1, 2])
def test_long_comment_stays_own_bounded_section(v):
    long = "\n".join("# " + "word " * 12 for _ in range(40)) + "\n"
    d = extract_yaml(long + "k: 1\n", max_tokens=128)
    assert d.sections[0].text and "k" not in d.sections[0].text
    cs = chunk(d, cfg(v, 128))
    assert all(c.approx_tokens <= 128 + 8 for c in cs)
    assert any("k: 1" in c.text for c in cs)
    if v == 2:  # v2 never merges the long preamble with the key
        assert any("k: 1" in c.text and "word" not in c.text for c in cs)


def test_comments_only_file():
    d = extract_yaml("# just a note\n# two\n")
    assert [s.text for s in d.sections if s.text] == ["just a note\ntwo"]
    assert extract_yaml("# note\n", source_url="u/f.yaml").title == "f.yaml"


def test_comment_before_a_list_document_joins_the_body():
    d = extract_yaml("# list note\n- a\n- b\n")
    texts = [s.text for s in d.sections if s.text]
    assert len(texts) == 1 and texts[0].startswith("list note") and "- a" in texts[0]


def test_crlf_equals_lf():
    lf = "# Note\r\n\r\nk: 1\r\n".replace("\r\n", "\n")
    a = [(s.level, s.heading, s.text) for s in extract_yaml("# Note\r\n\r\nk: 1\r\n").sections]
    b = [(s.level, s.heading, s.text) for s in extract_yaml(lf).sections]
    assert a == b and any("Note" in t for _, _, t in a)


def test_hash_inside_quotes_is_not_a_comment():
    d = extract_yaml('a: "x # not"\nb: \'y # no\'\n')
    t = "\n".join(s.text for s in d.sections)
    assert "x # not" in t and "y # no" in t


def test_json_has_no_preamble_and_is_stable():
    d = extract_json('{"a": 1}')
    assert [(s.level, s.heading, s.text) for s in d.sections] == [(0, "", ""), (1, "a", "a: 1")]
    assert d == extract_json('{"a": 1}')
