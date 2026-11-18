"""`text.check` (eap_worker.text_check_tool): mechanical checks of a text before it is published. Pure."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eap_schemas.tools import TOOL_DECLARATIONS, TextCheckInput
from eap_worker.text_check_tool import text_check

GOOD = "據報導，某位歌手將在明年春天舉辦巡迴演唱會，首站選在台北。"


def check(text, source=None, **rules):
    return text_check(None, text=text, source=source, rules=rules)


def test_a_good_text_passes_with_its_grapheme_count():
    out = check(GOOD, min_graphemes=10, max_graphemes=60, forbid_urls=True, forbid_hashtags=True, forbid_emoji=True,
                forbid_simplified_chinese=True, min_han_ratio=0.5)
    assert out == {"ok": True, "text": GOOD, "reason": None, "graphemes": len(GOOD)}


def test_no_rules_passes_anything_non_empty():
    assert check("hi")["ok"] is True


@pytest.mark.parametrize("raw", ["", "   \n ", "「」", "<think>only thoughts</think>", "<think>cut off"])
def test_empty(raw):
    assert check(raw)["reason"] == "empty" and check(raw)["ok"] is False


def test_cleaning_strips_think_tags_quotes_the_label_and_line_breaks():
    assert check("<think>hmm</think>\n摘要：「歌手宣布\n開唱」")["text"] == "歌手宣布開唱"
    assert check('"A quoted line"')["text"] == "A quoted line"
    assert check("『書名』")["text"] == "書名"  # the whole text is a quoted pair, so it is removed
    assert check("「前半」和「後半」")["text"] == "「前半」和「後半」"  # two quotations are not one wrapping pair


def test_too_long_and_too_short_count_graphemes_not_code_points():
    family = "👨‍👩‍👧‍👦"  # one grapheme, seven code points
    assert check(family * 3, max_graphemes=3)["ok"] is True and check(family * 3, max_graphemes=3)["graphemes"] == 3
    assert check(family * 4, max_graphemes=3)["reason"] == "too_long"
    assert check("太短了", min_graphemes=5)["reason"] == "too_short"
    assert check("剛好五個字喔", min_graphemes=6)["ok"] is True
    # the longer check comes first
    assert check("x" * 10, min_graphemes=20, max_graphemes=5)["reason"] == "too_long"


@pytest.mark.parametrize("text", [
    "詳見 https://example.com/a", "見 www.example.com", "網址 example.com 可看", "網址 example。com 可看",
    "ｈｔｔｐｓ：／／example 全形", "寄到 a@b.cc", "路徑 a/b", "反斜線 \\x",
])
def test_url(text):
    assert check(text, forbid_urls=True)["reason"] == "url"
    assert check(text)["ok"] is True  # only when the rule is on


def test_a_date_with_a_slash_is_not_a_url():
    assert check("10/27 開播的新劇引發討論", forbid_urls=True)["ok"] is True


def test_hashtag():
    assert check("今天的新聞 #娛樂", forbid_hashtags=True)["reason"] == "hashtag"
    assert check("今天的新聞 #娛樂")["ok"] is True


@pytest.mark.parametrize("text", ["歌手開唱😊", "好消息 ❤️", "手勢👍🏽", "家庭👨‍👩‍👧"])
def test_emoji(text):
    assert check(text, forbid_emoji=True)["reason"] == "emoji"
    assert check(text)["ok"] is True


def test_cjk_punctuation_is_not_emoji():
    assert check("歌手開唱，場場爆滿。（全場）", forbid_emoji=True)["ok"] is True


def test_simplified():
    assert check("歌手宣布这场演唱会", forbid_simplified_chinese=True)["reason"] == "simplified"
    assert check("歌手宣布這場演唱會", forbid_simplified_chinese=True)["ok"] is True
    assert check("歌手宣布这场演唱会")["ok"] is True


def test_forbid_terms_ignore_width_and_spacing_forbid_terms_ci_also_case():
    assert check("影片是視頻格式", forbid_terms=["視頻"])["reason"] == "forbidden_term"
    assert check("我 是 真 人", forbid_terms=["我是真人"])["reason"] == "forbidden_term"
    assert check("我不是ai喔", forbid_terms=["不是AI"])["ok"] is True  # case-sensitive list
    assert check("我不是ai喔", forbid_terms_ci=["不是AI"])["reason"] == "forbidden_term"
    assert check("ＳＰＯＮＳＯＲ內容", forbid_terms_ci=["sponsor"])["reason"] == "forbidden_term"
    assert check("乾淨的句子", forbid_terms=["視頻"], forbid_terms_ci=["x"])["ok"] is True


def test_a_rumour_term_needs_an_attribution_term():
    rules = {"rumour_terms": ["疑似", "傳出"], "attribution_terms": ["據報導", "據傳"]}
    assert check("某藝人疑似與友人同行", **rules)["reason"] == "unattributed_rumour"
    assert check("傳出某藝人要結婚了", **rules)["reason"] == "unattributed_rumour"
    assert check("據報導，某藝人疑似與友人同行", **rules)["ok"] is True
    assert check("某藝人與友人同行", **rules)["ok"] is True  # no rumour term, nothing to attribute
    assert check("某藝人疑似與友人同行", rumour_terms=["疑似"])["reason"] == "unattributed_rumour"  # no attribution list


def test_min_han_ratio():
    assert check("Hello wonderful world 你好", min_han_ratio=0.5)["reason"] == "not_chinese"
    assert check("Hello 你好你好你好你好", min_han_ratio=0.5)["ok"] is True
    assert check("，。！ ？", min_han_ratio=0.5)["ok"] is True  # no letters at all: nothing to judge


def test_verbatim_run_is_measured_on_normalised_text_against_the_source():
    source = "某歌手昨天在台北小巨蛋宣布，明年將舉辦三十場巡迴演唱會，門票下週開賣。"
    copy = "消息指出某歌手昨天在台北小巨蛋宣布明年將舉辦三十場"  # punctuation differs, a long run is shared
    assert check(copy, source=source, max_verbatim_run=15)["reason"] == "verbatim"
    assert check("歌手預告明年要在各地開唱，票務下週釋出。", source=source, max_verbatim_run=15)["ok"] is True
    assert check(copy, source=source)["ok"] is True  # no limit, no check
    assert check(copy, max_verbatim_run=15)["ok"] is True  # no source, no check
    assert check("ＡＢＣ１２３あいうえおかきくけこさしすせそ", source="abc123あいうえお、かきくけこ，さしすせそ", max_verbatim_run=15)["reason"] == "verbatim"
    # a run exactly one short of the limit passes
    assert check("巡迴演唱會門票下週", source=source, max_verbatim_run=10)["ok"] is True
    assert check("巡迴演唱會門票下週", source=source, max_verbatim_run=9)["reason"] == "verbatim"


def test_checks_run_in_the_documented_order():
    # empty < too_long < too_short < url < hashtag < emoji < simplified < forbidden_term < rumour < not_chinese < verbatim
    text = "https://x.com #t 😊 这 視頻 疑似"
    assert check(text, forbid_urls=True, forbid_hashtags=True)["reason"] == "url"
    assert check(text, forbid_hashtags=True, forbid_emoji=True)["reason"] == "hashtag"
    assert check(text, forbid_emoji=True, forbid_simplified_chinese=True)["reason"] == "emoji"
    assert check(text, forbid_simplified_chinese=True, forbid_terms=["視頻"])["reason"] == "simplified"
    assert check(text, forbid_terms=["視頻"], rumour_terms=["疑似"])["reason"] == "forbidden_term"
    assert check(text, rumour_terms=["疑似"], min_han_ratio=0.9)["reason"] == "unattributed_rumour"


def test_a_failing_check_still_returns_the_cleaned_text():
    out = check("摘要：「看 https://x.com」", forbid_urls=True)
    assert out == {"ok": False, "text": "看 https://x.com", "reason": "url", "graphemes": 15}


def test_input_limits():
    with pytest.raises(ValidationError):
        TextCheckInput(text="x", rules={"min_han_ratio": 1.5})
    with pytest.raises(ValidationError):
        TextCheckInput(text="x", rules={"forbid_terms": ["t"] * 201})
    with pytest.raises(ValidationError):
        TextCheckInput(text="x", rules={"nonsense": True})
    assert TOOL_DECLARATIONS["text.check"].side_effect is False
