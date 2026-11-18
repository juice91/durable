"""`text.check`: mechanical checks of a text before it is published. Pure: no I/O.

A workflow step puts it between a writing agent and a publishing step; the rules are arguments, so the platform knows
no publisher, language policy or term list. The checks are ported from a production news-summary pipeline.

- Arguments: `eap_schemas.tools.TextCheckInput` (`text`, optional `source`, `rules`).
- Cleaning, always: think-tags removed (an unclosed one empties the text), line breaks joined (no space between
  Han characters), a leading `摘要：` and one wrapping pair of quotes (`「」 『』 “” ""`) removed.
- Result: `{ok, text, reason, graphemes}`; `text` is the cleaned text even when a check fails; `graphemes` counts
  extended grapheme clusters (the `regex` package). `reason` is the first failed check, in this order: `empty`, `too_long`
  (over max_graphemes), `too_short` (under min_graphemes), `url` (forbid_urls: http(s), www, a dotted host in
  any spelling, @, backslash, a slash beside a Latin letter; a date such as 10/27 passes), `hashtag` (#),
  `emoji`, `simplified` (a Simplified-only character), `forbidden_term`, `unattributed_rumour`, `not_chinese`
  (Han characters are under min_han_ratio of the letters), `verbatim` (a run of max_verbatim_run or more shared
  with `source`); or null.
- Matching: forbid_terms, rumour_terms and attribution_terms compare after NFKC with whitespace removed
  (forbid_terms and the two lists below: letter case counts; forbid_terms_ci: case is ignored). The verbatim run is measured on NFKC text with
  punctuation, symbols and whitespace removed.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Annotated, Any

import regex
from pydantic import Field
from pydantic_ai import RunContext

from eap_runtime import RunDeps, ToolRegistry
from eap_schemas.tools import TEXT_CHECK_TOOL_ID, TOOL_DECLARATIONS, TextCheckRules

# Characters used in Simplified Chinese and not in standard Taiwan text. It only needs to catch a drift into
# Simplified, so characters Taiwan text also uses are left out and a false positive never blocks a correct text.
SIMPLIFIED_ONLY = frozenset(
    "这们说为国时会来对发经现么过还没让无与东车长门问间听见觉应开关进运认识买卖书习学实总统选举议员机业务济场区县华产网络电视图万亿岁钱银价种样热风飞鸟马鱼乐历尔报导军战农医药汇币涨个当从两将动头边爱带帮办备贝笔毕变标宾补财参惨层尝厂彻陈称诚处传创词达单弹敌递点调顶冻独读断队吨夺恶儿罚饭访费妇该盖赶刚钢纲给巩贡购顾观馆惯广归规贵汉话坏欢环换挥获货积级极际继坚简剑渐践鉴键讲奖胶阶节结洁紧尽惊旧剧壳课块宽矿亏扩阔兰蓝栏烂劳类离礼丽联连脸练粮辆疗邻临灵领刘龙楼录陆虑乱论罗妈吗满梦庙灭鸣难闹宁浓盘赔喷苹齐气弃迁浅枪墙桥亲轻倾庆穷权劝确荣软润杀伤赏烧绍设摄绳师势试释寿树数帅双谁顺丝虽随孙损态谈汤涛讨题条铁厅团维伟卫稳雾戏细吓线宪乡详响项协写谢兴寻压鸭严盐验阳养爷页仪艺忆义译阴隐营赢拥优邮犹语预园远愿约跃杂灾载赞脏则责贼张账针侦阵镇争证郑织职执纸质钟众终肿轴昼烛专转装壮状资综纵邹组钻"
)

_THINK = re.compile(r"<think>.*?</think>", re.S)
_HAN = regex.compile(r"\p{Script=Han}")
_CJK_EDGE = r"\p{Script=Han}　-〿＀-￯"
_CJK_BREAK = regex.compile(f"(?<=[{_CJK_EDGE}]) (?=[{_CJK_EDGE}])")
_EMOJI = regex.compile(r"[\p{Extended_Pictographic}️‍\U0001F3FB-\U0001F3FF]")
_URL = re.compile(r"https?|www|://|[a-z0-9-]\s*(?:\.|。|點|dot)\s*[a-z]{2,}", re.I)
_LINK_CHARS = re.compile(r"[@\\]|[a-z]/|/[a-z]", re.I)
_NOISE = regex.compile(r"[\p{P}\p{S}\s]")
_NOT_LETTER = regex.compile(r"[\s\p{P}\p{S}]")
_QUOTES = (("「", "」"), ("『", "』"), ("“", "”"), ('"', '"'))


def _strip_think(text: str) -> str:
    text = _THINK.sub("", text)
    return "" if "<think>" in text else text


def clean(raw: str) -> str:
    text = _strip_think(raw).strip()
    text = re.sub(r"\s+", " ", text)
    text = _CJK_BREAK.sub("", text)
    text = re.sub(r"^摘要：\s*", "", text)
    for opening, closing in _QUOTES:
        inner = text[len(opening):-len(closing)]
        if len(text) > 1 and text.startswith(opening) and text.endswith(closing) and opening not in inner and closing not in inner:
            return inner.strip()
    return text


def _fold(text: str, ignore_case: bool) -> str:
    folded = unicodedata.normalize("NFKC", text)
    return re.sub(r"\s+", "", folded.casefold() if ignore_case else folded)


def _has_any(text: str, terms: list[str], ignore_case: bool = False) -> bool:
    folded = _fold(text, ignore_case)
    return any(folded_term in folded for t in terms if (folded_term := _fold(t, ignore_case)))


def _units(text: str) -> str:
    return _NOISE.sub("", unicodedata.normalize("NFKC", text))


def _shares_run(text: str, source: str, length: int) -> bool:
    """True when a run of `length` normalised characters occurs in both."""
    a, b = _units(text), _units(source)
    if len(a) < length or len(b) < length:
        return False
    seen = {b[i:i + length] for i in range(len(b) - length + 1)}
    return any(a[i:i + length] in seen for i in range(len(a) - length + 1))


def _result(ok: bool, text: str, reason: str | None, graphemes: int) -> dict[str, Any]:
    return {"ok": ok, "text": text, "reason": reason, "graphemes": graphemes}


def text_check(
    ctx: RunContext[RunDeps],
    text: Annotated[str, Field(max_length=20000)],
    rules: TextCheckRules,
    source: Annotated[str, Field(max_length=100000)] | None = None,
) -> dict[str, Any]:
    """Clean and check a text (see the module docstring)."""
    rules = rules if isinstance(rules, TextCheckRules) else TextCheckRules.model_validate(rules)
    cleaned = clean(text)
    graphemes = len(regex.findall(r"\X", cleaned))

    def fail(reason: str) -> dict[str, Any]:
        return _result(False, cleaned, reason, graphemes)

    if not cleaned:
        return fail("empty")
    if rules.max_graphemes is not None and graphemes > rules.max_graphemes:
        return fail("too_long")
    if rules.min_graphemes is not None and graphemes < rules.min_graphemes:
        return fail("too_short")
    folded = unicodedata.normalize("NFKC", cleaned)
    if rules.forbid_urls and (_URL.search(folded) or _LINK_CHARS.search(folded)):
        return fail("url")
    if rules.forbid_hashtags and "#" in folded:
        return fail("hashtag")
    if rules.forbid_emoji and _EMOJI.search(cleaned):
        return fail("emoji")
    if rules.forbid_simplified_chinese and any(ch in SIMPLIFIED_ONLY for ch in cleaned):
        return fail("simplified")
    if _has_any(cleaned, rules.forbid_terms) or _has_any(cleaned, rules.forbid_terms_ci, True):
        return fail("forbidden_term")
    if rules.rumour_terms and _has_any(cleaned, rules.rumour_terms) and not _has_any(cleaned, rules.attribution_terms):
        return fail("unattributed_rumour")
    if rules.min_han_ratio is not None:
        letters = [c for c in cleaned if not _NOT_LETTER.match(c)]
        han = [c for c in letters if _HAN.match(c)]
        if len(han) < rules.min_han_ratio * len(letters):
            return fail("not_chinese")
    if rules.max_verbatim_run is not None and source and _shares_run(cleaned, source, rules.max_verbatim_run):
        return fail("verbatim")
    return _result(True, cleaned, None, graphemes)


def register_text_check_tool(registry: ToolRegistry) -> None:
    registry.register(TOOL_DECLARATIONS[TEXT_CHECK_TOOL_ID], text_check)


__all__ = ["SIMPLIFIED_ONLY", "clean", "register_text_check_tool", "text_check"]
