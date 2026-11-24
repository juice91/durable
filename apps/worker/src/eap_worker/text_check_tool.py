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
- `verbatim_exempt` (optional; absent = the plain check above, results unchanged): parts of the TEXT, never the source, that
  act as breaks. A break ends the run and a new one starts after it; nothing is removed and joined, so text on both sides
  never forms one longer run. `quoted`: a span from an opener (`「 『 《 〈 “`, or `"`) to its closer, brackets included,
  of at most 40 graphemes; same-kind brackets nest; an unclosed opener, a stray closer and a longer span count normally
  (inner quotes of a longer span are still judged on their own). `numbers`: digits (full-width folded by NFKC) with
  `, . : / -` and the units `年 月 日 時 點 分 %`, spaces allowed between. `terms`: exact strings (after NFKC, case counts).
  With a source the result also has `verbatim_longest_run` (longest counted run) and, on failure, `verbatim_run_text`
  (that run, normalised, at most 60 characters).
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
from eap_schemas.tools import TEXT_CHECK_TOOL_ID, TOOL_DECLARATIONS, TextCheckRules, VerbatimExempt

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


QUOTE_PAIRS = {"「": "」", "『": "』", "《": "》", "〈": "〉", "“": "”"}
QUOTE_MAX_GRAPHEMES = 40
RUN_TEXT_MAX = 60
_BREAK = "\x00"
_NUMBER = re.compile(r"[0-9](?:\s*[0-9,.:/\-]|\s*[年月日時點分%])*")


def _quoted_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    i = 0
    while i < len(text):
        ch = text[i]
        closer = '"' if ch == '"' else QUOTE_PAIRS.get(ch)
        end = -1
        if closer is not None:
            depth = 0
            for j in range(i + 1, len(text)):
                if text[j] == closer:
                    if depth == 0:
                        end = j + 1
                        break
                    depth -= 1
                elif text[j] == ch and ch != closer:
                    depth += 1
        if end != -1 and len(regex.findall(r"\X", text[i:end])) <= QUOTE_MAX_GRAPHEMES:
            spans.append((i, end))
            i = end
        else:
            i += 1
    return spans


def _term_spans(text: str, terms: list[str]) -> list[tuple[int, int]]:
    spans = []
    for term in terms:
        folded = unicodedata.normalize("NFKC", term)
        start = text.find(folded) if folded else -1
        while start != -1:
            spans.append((start, start + len(folded)))
            start = text.find(folded, start + 1)
    return spans


def _counted_segments(text: str, exempt: VerbatimExempt) -> list[str]:
    """The normalised runs of `text` left between its exempt spans (each span is a break)."""
    folded = unicodedata.normalize("NFKC", text)
    spans: list[tuple[int, int]] = []
    if exempt.quoted:
        spans += _quoted_spans(folded)
    if exempt.numbers:
        spans += [m.span() for m in _NUMBER.finditer(folded)]
    if exempt.terms:
        spans += _term_spans(folded, exempt.terms)
    marks = [False] * len(folded)
    for start, end in spans:
        for k in range(start, end):
            marks[k] = True
    broken = "".join(_BREAK if m else c for c, m in zip(folded, marks, strict=True))
    return [seg for seg in re.split(_BREAK + "+", _NOISE.sub("", broken)) if seg]


def _longest_shared(segments: list[str], source: str) -> str:
    """The longest substring of any segment that also occurs in the (normalised) source, via a suffix automaton."""
    nxt: list[dict[str, int]] = [{}]
    link, size = [-1], [0]
    last = 0
    for ch in source:
        cur = len(nxt)
        nxt.append({})
        link.append(-1)
        size.append(size[last] + 1)
        p = last
        while p != -1 and ch not in nxt[p]:
            nxt[p][ch] = cur
            p = link[p]
        if p == -1:
            link[cur] = 0
        else:
            q = nxt[p][ch]
            if size[p] + 1 == size[q]:
                link[cur] = q
            else:
                clone = len(nxt)
                nxt.append(dict(nxt[q]))
                link.append(link[q])
                size.append(size[p] + 1)
                while p != -1 and nxt[p].get(ch) == q:
                    nxt[p][ch] = clone
                    p = link[p]
                link[q] = link[cur] = clone
        last = cur
    best = ""
    for seg in segments:
        state, length = 0, 0
        for pos, ch in enumerate(seg):
            while state and ch not in nxt[state]:
                state = link[state]
                length = size[state]
            if ch in nxt[state]:
                state = nxt[state][ch]
                length += 1
            else:
                state, length = 0, 0
            if length > len(best):
                best = seg[pos - length + 1:pos + 1]
    return best


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
    if rules.max_verbatim_run is not None and source:
        if rules.verbatim_exempt is None:
            if _shares_run(cleaned, source, rules.max_verbatim_run):
                return fail("verbatim")
        else:
            run = _longest_shared(_counted_segments(cleaned, rules.verbatim_exempt), _units(source))
            if len(run) >= rules.max_verbatim_run:
                out = fail("verbatim")
                out.update(verbatim_longest_run=len(run), verbatim_run_text=run[:RUN_TEXT_MAX])
                return out
            return {**_result(True, cleaned, None, graphemes), "verbatim_longest_run": len(run)}
    return _result(True, cleaned, None, graphemes)


def register_text_check_tool(registry: ToolRegistry) -> None:
    registry.register(TOOL_DECLARATIONS[TEXT_CHECK_TOOL_ID], text_check)


__all__ = ["SIMPLIFIED_ONLY", "clean", "register_text_check_tool", "text_check"]
