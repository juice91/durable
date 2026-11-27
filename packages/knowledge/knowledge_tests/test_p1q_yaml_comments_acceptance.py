"""RAG P1 quality fixes, acceptance: YAML comments (extractor version 2)."""

from __future__ import annotations

import pytest

from eap_knowledge import EXTRACTOR_VERSIONS, ExtractionFailed
from eap_knowledge.formats.structured import MAX_YAML_ALIASES, extract_yaml


def secs(text: str, **kw):
    return [(s.level, s.heading, s.text) for s in extract_yaml(text, **kw).sections]


def test_top_inline_between_and_list_comments_with_exact_text_and_order():
    got = secs("# top\n# more\n\na: 1 # inline\n# between\nb:\n  - x # in list\n  # list c\n  - y\n")
    assert got == [
        (0, "", ""),  # yaml 3: the short preamble opens the first section
        (1, "a", "top\nmore\n\n# inline\na: 1"),
        (1, "b", "# between\n# in list\n# list c\nb:\n- x\n- y"),
    ]


def test_crlf_gives_the_same_sections_as_lf():
    lf = "# top\n\na: 1 # inline\n# between\nb: 2\n"
    assert secs(lf.replace("\n", "\r\n")) == secs(lf)


def test_hash_in_quoted_and_plain_scalars_is_not_a_comment():
    got = secs('a: "x # no"\nb: \'y # no\'\nc: z#nc\n')
    assert got == [(0, "", ""), (1, "a", "a: 'x # no'"), (1, "b", "b: 'y # no'"), (1, "c", "c: z#nc")]


def test_comments_only_file_is_the_preamble_and_is_not_an_error():
    doc = extract_yaml("# only\n# comments\n")
    assert [(s.level, s.text) for s in doc.sections] == [(0, "only\ncomments")]


def test_comment_after_the_last_key_goes_to_the_last_key():
    assert secs("a: 1\nb: 2\n# the end\n")[-1] == (1, "b", "# the end\nb: 2")


def test_comment_on_an_alias_key_line_is_kept_and_the_alias_expands():
    assert secs("a: &x [1, 2]\nb: *x # tail\n")[2] == (1, "b", "# tail\nb:\n- 1\n- 2")


def test_licence_lines_do_not_leak_but_description_does():
    got = secs("# SPDX-License-Identifier: Apache-2.0\n# Copyright (c) 2020 Acme\n# The real description\n\na: 1\n")
    assert got[:2] == [(0, "", ""), (1, "a", "The real description\n\na: 1")]


def test_version_bump_makes_re_extract_pick_old_docs():
    # A stored v1 extraction is older than the current extractor: that is what selects it.
    assert EXTRACTOR_VERSIONS["yaml"] > 1


def test_alias_count_at_and_over_the_limit_with_comments():
    def doc(n: int) -> str:
        return "# note\nbase: &b {k: v}\n" + "".join(f"k{i}: *b # c{i}\n" for i in range(n))

    extract_yaml(doc(MAX_YAML_ALIASES))
    with pytest.raises(ExtractionFailed) as err:
        extract_yaml(doc(MAX_YAML_ALIASES + 1))
    assert err.value.code == "extract_limit"


def test_a_comment_cannot_smuggle_text_into_the_failure_message():
    with pytest.raises(ExtractionFailed) as err:
        extract_yaml("# SECRET-MARK\na: [1\n")
    assert err.value.code == "extract_failed" and "SECRET-MARK" not in err.value.user_message


def test_mixed_comment_everywhere_does_not_reorder_or_duplicate_keys():
    got = secs("# h\na: 1\n# c1\n# c2\nb: 2 # i\nc:\n  # d\n  x: 1\n")
    assert [h for lvl, h, _ in got if lvl == 1] == ["a", "b", "c"]
    assert got[2][2] == "# c1\n# c2\n# i\nb: 2"
    assert got[3][2] == "# d\nc:\n  x: 1"
