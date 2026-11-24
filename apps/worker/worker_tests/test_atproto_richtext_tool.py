"""`atproto.richtext` (eap_worker.atproto_richtext_tool): Bluesky facets with UTF-8 byte offsets. Pure."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eap_schemas.tools import TOOL_DECLARATIONS, AtprotoRichtextInput
from eap_worker.atproto_richtext_tool import atproto_richtext

TAG = "app.bsky.richtext.facet#tag"
LINK = "app.bsky.richtext.facet#link"


def run(text, **kwargs):
    return atproto_richtext(None, text=text, **kwargs)


def sliced(out):
    """Each facet's UTF-8 slice of the final text, decoded, with its feature."""
    raw = out["text"].encode("utf-8")
    return [(raw[f["index"]["byteStart"]:f["index"]["byteEnd"]].decode("utf-8"), f["features"][0]) for f in out["facets"]]


def test_chinese_text_with_appended_tags_has_exact_byte_offsets():
    out = run("台北今天放晴，出門記得帶傘。", append_tags=["Taiwan", "twsky"])
    assert out["text"] == "台北今天放晴，出門記得帶傘。\n#Taiwan #twsky"
    assert sliced(out) == [("#Taiwan", {"$type": TAG, "tag": "Taiwan"}), ("#twsky", {"$type": TAG, "tag": "twsky"})]
    first = out["facets"][0]["index"]
    assert first["byteStart"] == len("台北今天放晴，出門記得帶傘。\n".encode())  # not the character count
    assert first["byteStart"] != len("台北今天放晴，出門記得帶傘。\n")
    assert out["graphemes"] == len("台北今天放晴，出門記得帶傘。\n#Taiwan #twsky")


def test_emoji_and_cjk_before_a_tag():
    out = run("好天氣👨‍👩‍👧‍👦🌤️ 出發 #Taiwan")
    assert sliced(out) == [("#Taiwan", {"$type": TAG, "tag": "Taiwan"})]
    assert out["graphemes"] == len("好天氣") + 2 + 1 + 2 + 1 + 7  # family and sun are one grapheme each


def test_append_is_idempotent():
    once = run("你好", append_tags=["Taiwan", "twsky"])
    twice = run(once["text"], append_tags=["Taiwan", "twsky"])
    assert twice == once
    assert run("你好\n#Taiwan #twsky\n", append_tags=["Taiwan", "twsky"])["text"] == "你好\n#Taiwan #twsky\n"


def test_a_partial_match_is_not_idempotent():
    out = run("你好 #twsky", append_tags=["Taiwan", "twsky"])
    assert out["text"] == "你好 #twsky\n#Taiwan #twsky" and len(out["facets"]) == 3


def test_a_link_and_a_tag_in_the_same_text():
    out = run("看這裡 https://example.com/a?b=1 #Taiwan，")
    assert sliced(out) == [
        ("https://example.com/a?b=1", {"$type": LINK, "uri": "https://example.com/a?b=1"}),
        ("#Taiwan", {"$type": TAG, "tag": "Taiwan"}),
    ]
    starts = [f["index"]["byteStart"] for f in out["facets"]]
    assert starts == sorted(starts)


def test_trailing_punctuation_is_cut_from_links():
    assert sliced(run("see https://example.com/x."))[0][1]["uri"] == "https://example.com/x"
    assert sliced(run("(https://example.com/x)"))[0][1]["uri"] == "https://example.com/x"
    assert sliced(run("https://en.wikipedia.org/wiki/A_(b)"))[0][1]["uri"] == "https://en.wikipedia.org/wiki/A_(b)"


def test_a_tag_next_to_cjk_punctuation_follows_atproto():
    # Trailing punctuation, full-width included, is cut.
    out = run("今天 #Taiwan，")
    assert sliced(out) == [("#Taiwan", {"$type": TAG, "tag": "Taiwan"})]
    # The `#` must follow whitespace or the start: after 「 it is not a tag.
    assert run("「#Taiwan」")["facets"] == []
    assert run("今天#Taiwan")["facets"] == []


def test_a_tag_runs_to_whitespace_like_atproto():
    # No whitespace after the tag: the CJK that follows is part of it (the official behaviour).
    assert sliced(run("#Taiwan真好")) == [("#Taiwan真好", {"$type": TAG, "tag": "Taiwan真好"})]


def test_an_all_digit_tag_is_not_a_tag():
    assert run("排名 #1 和 #2024")["facets"] == []
    assert len(run("#2024年")["facets"]) == 1


def test_full_width_hash_and_tag_length():
    assert sliced(run("＃台灣"))[0][1]["tag"] == "台灣"
    assert len(run("#" + "a" * 64)["facets"]) == 1
    assert run("#" + "a" * 65)["facets"] == []


def test_detect_can_turn_tags_and_links_off():
    text = "https://example.com #Taiwan"
    assert [f["features"][0]["$type"] for f in run(text, detect={"tags": False, "links": True})["facets"]] == [LINK]
    assert [f["features"][0]["$type"] for f in run(text, detect={"tags": True, "links": False})["facets"]] == [TAG]
    assert run(text, detect={"tags": False, "links": False})["facets"] == []


def test_a_tag_inside_a_link_never_overlaps():
    out = run("https://example.com/#frag #x")
    assert [f["features"][0]["$type"] for f in out["facets"]] == [LINK, TAG]


def test_too_long_is_an_error_not_a_truncation():
    out = run("字" * 301)
    assert out == {"error": {"code": "text_too_long", "message": "the text is 301 graphemes, over the limit of 300", "graphemes": 301, "max_graphemes": 300}}
    assert "error" not in run("字" * 300)
    assert run("字" * 295, append_tags=["Taiwan"])["error"]["graphemes"] == 303  # the appended tags count
    assert "error" not in run("字" * 301, max_graphemes=400)


def test_an_all_digit_append_tag_is_refused():
    assert run("x", append_tags=["2024"])["error"]["code"] == "invalid_tag"


def test_empty_text_with_tags():
    assert run("", append_tags=["Taiwan"])["text"] == "#Taiwan"


def test_schema_bounds():
    ok = {"text": "t"}
    assert AtprotoRichtextInput(**ok).max_graphemes == 300 and AtprotoRichtextInput(**ok).detect.tags is True
    for bad in (
        {"text": "t", "append_tags": ["a"] * 9},
        {"text": "t", "append_tags": [""]},
        {"text": "t", "append_tags": ["a" * 65]},
        {"text": "t", "append_tags": ["a b"]},
        {"text": "t", "max_graphemes": 0},
        {"text": "t", "extra": 1},
        {"text": "t", "detect": {"mentions": True}},
        {"text": "x" * 20001},
    ):
        with pytest.raises(ValidationError):
            AtprotoRichtextInput(**bad)
    AtprotoRichtextInput(text="t", append_tags=["a" * 64] * 8)


def test_the_declaration_is_pure_and_its_example_validates():
    decl = TOOL_DECLARATIONS["atproto.richtext"]
    assert decl.side_effect is False
    out = atproto_richtext(None, **decl.example)
    assert set(out) == {"text", "facets", "graphemes"} and set(decl.result_example) == set(out)
