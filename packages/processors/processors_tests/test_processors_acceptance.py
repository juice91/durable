"""P0.9 acceptance for eap_processors: RE2 rules, the quadratic-pattern bound, budgets and caps,
PII (Luhn and IBAN), and "matched text never leaves the engine". Independent of test_processors.py."""

from __future__ import annotations

import ast
import json
import re
import time
from pathlib import Path

import pytest

import eap_processors
from eap_processors import (
    BUDGET_EXCEEDED,
    INPUT_TOO_LARGE,
    MAX_PROCESSOR_INPUT_CHARS,
    MAX_PROGRAM_SIZE,
    MAX_REGEX_INPUT_CHARS,
    OUTCOME_REASONS,
    MAX_REDACTED_MATCHES,
    PATTERN_CACHE_SIZE,
    RE2_MAX_MEM,
    TOO_MANY_MATCHES,
    InvalidProcessorConfig,
    MatchBudget,
    ProcessorNeedsModel,
    Re2PatternError,
    apply_deterministic,
    compile_processors,
    compile_re2,
    detect,
    iban_valid,
    luhn_valid,
    new_budget,
    processor_errors,
    re2_pattern_error,
)
from eap_processors.engine import CHAIN_TIME_BUDGET_SECONDS
from eap_schemas.processors import PII_KINDS, PROCESSOR_SPEC_ADAPTER

SECRET = "ZQSECRET9931"  # stands in for matched text: it must never appear in an outcome or event


def spec(kind: str, **config):
    return PROCESSOR_SPEC_ADAPTER.validate_python({"type": kind, "config": config} if config else {"type": kind})


def blocklist(*patterns: str, action: str = "block"):
    return compile_processors([spec("regex_blocklist", patterns=list(patterns), action=action)])


def pii(**config):
    return compile_processors([spec("pii_redact", **config)])


# --- RE2 rules -----------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "pattern",
    [r"(a)\1", r"(?P<n>a)(?P=n)", r"(?=a)b", r"(?!a)b", r"(?<=a)b", r"(?<!a)b", r"(?>a)", r"a*+", r"a++", r"(a",
     r"[a", r"*a", r"a{1001}", r"((((a{100}){100}){100}){100})", r"\p{Nope}"],
)
def test_backtracking_syntax_and_unsupported_constructs_are_refused(pattern):
    reason = re2_pattern_error(pattern)
    assert reason is not None and reason.startswith("not valid RE2 syntax") and len(reason) <= 260
    with pytest.raises(Re2PatternError):
        compile_re2(pattern)
    with pytest.raises(InvalidProcessorConfig) as info:
        compile_processors([spec("regex_blocklist", patterns=["ok", pattern])], "input_processors")
    assert str(info.value).startswith("input_processors[0].config.patterns[1]: ")


@pytest.mark.parametrize("pattern", [r"\b", r"$", r"^", r"(?m)^", r"(?m)$", r"\B", r"\A", r"\z", r"(?:)", r"a*", r"a?",
                                     r"x|", r"\b|$", r"(?:^|$)", r"(?i)\b"])
def test_zero_width_and_empty_matching_patterns_are_refused(pattern):
    assert re2_pattern_error(pattern) is not None
    with pytest.raises(Re2PatternError):
        compile_re2(pattern)
    assert processor_errors([spec("regex_blocklist", patterns=[pattern])], [])


@pytest.mark.parametrize("pattern", [r"\bfoo\b", r"^foo$", r"(?i)secret\d+", r"foo|bar", r"[a-z]+@[a-z]+", r"\p{Greek}+",
                                     "x" * 200])
def test_ordinary_patterns_compile_and_validate_clean(pattern):
    assert re2_pattern_error(pattern) is None
    assert processor_errors([spec("regex_blocklist", patterns=[pattern])], [spec("regex_blocklist", patterns=[pattern])]) == []


def test_errors_name_the_phase_index_and_pattern_position():
    good = spec("regex_blocklist", patterns=["ok"])
    bad = spec("regex_blocklist", patterns=["ok", "fine", r"(?=x)"])
    errors = processor_errors([good, bad], [bad, good, spec("regex_blocklist", patterns=[r"\b"])])
    assert [e.split(": ")[0] for e in errors] == [
        "input_processors[1].config.patterns[2]",
        "output_processors[0].config.patterns[2]",
        "output_processors[2].config.patterns[0]",
    ]


def test_re2_memory_limit_and_pattern_cache_constants():
    assert RE2_MAX_MEM == 2 * 1024 * 1024 and PATTERN_CACHE_SIZE == 64
    for i in range(PATTERN_CACHE_SIZE * 2):
        compile_re2(f"cachetest{i}x")
    from eap_processors import regex

    info = regex._compile_cached.cache_info()
    assert info.maxsize == PATTERN_CACHE_SIZE and info.currsize <= PATTERN_CACHE_SIZE
    assert compile_re2("same-pattern") is compile_re2("same-pattern")


# --- the quadratic pattern and other pathological input ------------------------------------------

@pytest.mark.parametrize("action", ["block", "redact"])
def test_quadratic_pattern_on_32_kib_finishes_under_one_second(action):
    chain = blocklist("a+b|a", action=action)
    text = "a" * (32 * 1024)
    started = time.perf_counter()
    result = apply_deterministic(chain, text)
    assert time.perf_counter() - started < 1.0
    assert result.blocked and result.text is None
    outcome = result.outcomes[0]
    if action == "block":
        assert outcome.action == "block" and dict(outcome.match_counts) == {"pattern_0": 1} and outcome.reason is None
    else:
        assert outcome.reason == TOO_MANY_MATCHES and dict(outcome.match_counts) == {}


@pytest.mark.parametrize("pattern", [r"(a+)+$", r"(a|a)*b", r"(.*a){20}x", r"(x+x+)+y", r"(\w+\s?)+$", r"(a*)*b"])
@pytest.mark.parametrize("action", ["block", "redact"])
def test_classic_redos_patterns_are_linear_and_fast(pattern, action):
    chain = blocklist(pattern, action=action)
    for text in ("a" * 40 + "!", "a" * 5000 + "!", "a " * 8000 + "!"):
        started = time.perf_counter()
        apply_deterministic(chain, text)
        assert time.perf_counter() - started < 0.05, (pattern, action, len(text))


def test_twenty_patterns_each_quadratic_stay_bounded_in_both_actions():
    patterns = [f"a+b{i}|a" for i in range(20)]
    for action in ("block", "redact"):
        started = time.perf_counter()
        result = apply_deterministic(blocklist(*patterns, action=action), "a" * (32 * 1024))
        assert time.perf_counter() - started < 2.0 and result.blocked


# --- block action: one linear search per pattern -------------------------------------------------

def test_block_counts_are_one_per_matching_pattern_with_original_indexes():
    chain = blocklist("zzz", "foo", "never", "bar")
    result = apply_deterministic(chain, "foo foo foo bar bar")
    assert result.blocked and result.text is None
    assert dict(result.outcomes[0].match_counts) == {"pattern_1": 1, "pattern_3": 1}
    assert result.outcomes[0].action == "block" and result.outcomes[0].reason is None


def test_block_with_no_match_allows_and_returns_the_text_unchanged():
    result = apply_deterministic(blocklist("foo", "bar"), "nothing here")
    assert not result.blocked and result.text == "nothing here"
    assert result.outcomes[0].action == "allow" and dict(result.outcomes[0].match_counts) == {}


def test_block_is_not_limited_by_the_redaction_match_cap():
    text = "x " * (MAX_REDACTED_MATCHES * 5)
    outcome = apply_deterministic(blocklist("x"), text).outcomes[0]
    assert outcome.action == "block" and outcome.reason is None and dict(outcome.match_counts) == {"pattern_0": 1}


# --- redact action: cap and budget ---------------------------------------------------------------

def test_redact_replaces_the_union_of_overlapping_matches_once():
    result = apply_deterministic(blocklist("abc", "bcd", "xyz", action="redact"), "abcd and xyz")
    assert result.text == "[REDACTED:regex] and [REDACTED:regex]"
    assert dict(result.outcomes[0].match_counts) == {"pattern_0": 1, "pattern_1": 1, "pattern_2": 1}


def test_redact_cap_is_exactly_1000_matches_per_chain():
    assert MAX_REDACTED_MATCHES == 1000
    chain = blocklist("x", action="redact")
    ok = apply_deterministic(chain, "x " * 1000)
    assert not ok.blocked and ok.text.count("[REDACTED:regex]") == 1000
    over = apply_deterministic(chain, "x " * 1001)
    assert over.blocked and over.text is None and over.outcomes[0].reason == TOO_MANY_MATCHES
    assert dict(over.outcomes[0].match_counts) == {}


def test_cap_is_shared_by_all_patterns_and_processors_of_one_chain():
    two_patterns = blocklist("x", "y", action="redact")
    assert apply_deterministic(two_patterns, "x y " * 501).outcomes[0].reason == TOO_MANY_MATCHES
    both = compile_processors([spec("regex_blocklist", patterns=["x"], action="redact"),
                               spec("regex_blocklist", patterns=["y"], action="redact")])
    assert apply_deterministic(both, "x y " * 500).blocked is False
    assert apply_deterministic(both, "x " * 600 + "y " * 600).outcomes[-1].reason == TOO_MANY_MATCHES
    # A separate call to apply_deterministic gets a fresh budget.
    assert apply_deterministic(both, "x y " * 500).blocked is False


def test_block_with_an_expired_budget_is_budget_exceeded_checked_before_every_search():
    # The deadline is checked before each search, so even a text that matches is not searched.
    for text in ("x x", "no match at all"):
        result = apply_deterministic(blocklist("x"), text, MatchBudget(-1.0, None))
        assert result.blocked and result.text is None
        assert result.outcomes[0].reason == BUDGET_EXCEEDED and dict(result.outcomes[0].match_counts) == {}


def test_expired_budget_blocks_with_budget_exceeded_for_redact_and_pii():
    cases = [(blocklist("x", action="redact"), "x x"), (blocklist("x", action="redact"), "none"),
             (pii(), "a@b.com c@d.com"), (pii(), "nothing"),
             (pii(action="block"), "a@b.com c@d.com")]
    for chain, text in cases:
        result = apply_deterministic(chain, text, MatchBudget(-1.0, None))
        assert result.blocked and result.text is None
        assert result.outcomes[0].reason == BUDGET_EXCEEDED and dict(result.outcomes[0].match_counts) == {}


def test_budget_api_apply_budget_defaults_and_new_budget():
    budget = new_budget()
    assert isinstance(budget, MatchBudget) and budget.max_matches == MAX_REDACTED_MATCHES
    assert budget.deadline - time.monotonic() <= CHAIN_TIME_BUDGET_SECONDS == 5.0
    (processor,) = blocklist("x", action="redact")
    assert processor.apply("a x b").text == "a [REDACTED:regex] b"  # budget=None gives it a fresh one
    assert processor.apply("a x b", new_budget()).outcome.action == "redact"
    assert apply_deterministic((), "unchanged").text == "unchanged"  # budget=None on an empty chain


def test_one_budget_is_shared_across_apply_calls_of_a_chain():
    (processor,) = blocklist("x", action="redact")
    budget = MatchBudget(10.0, 3)
    assert not processor.apply("x x", budget).blocked  # 2 matches used
    assert processor.apply("x x", budget).outcome.reason == TOO_MANY_MATCHES  # 4th match: over 3


def test_input_bounds_regex_128_ki_characters_pii_one_million():
    assert MAX_REGEX_INPUT_CHARS == 128 * 1024 and MAX_PROCESSOR_INPUT_CHARS == 1_000_000
    over_regex = "a" * (MAX_REGEX_INPUT_CHARS + 1)
    for chain in (blocklist("zzz"), blocklist("zzz", action="redact")):
        result = apply_deterministic(chain, over_regex)
        assert result.blocked and result.text is None and result.outcomes[0].reason == INPUT_TOO_LARGE
        assert not apply_deterministic(chain, "a" * MAX_REGEX_INPUT_CHARS).blocked  # exactly at the bound
    # pii_redact keeps the larger bound: 128 Ki + 1 is fine, 1 M + 1 is refused.
    assert not apply_deterministic(pii(), over_regex).blocked
    assert not apply_deterministic(pii(), "a" * MAX_PROCESSOR_INPUT_CHARS).blocked
    refused = apply_deterministic(pii(), "a" * (MAX_PROCESSOR_INPUT_CHARS + 1))
    assert refused.blocked and refused.outcomes[0].reason == INPUT_TOO_LARGE
    big = "a" * (MAX_PROCESSOR_INPUT_CHARS + 1)
    truncated = apply_deterministic(compile_processors([spec("max_length", max_chars=5, action="truncate")]), big)
    assert truncated.text == "aaaaa"  # max_length has no input bound


def test_outcome_reasons_are_exactly_the_engine_and_moderation_reasons():
    # The three engine reasons plus moderation's two fail-closed reasons (P0.17, worker).
    assert OUTCOME_REASONS == {
        INPUT_TOO_LARGE, BUDGET_EXCEEDED, TOO_MANY_MATCHES, "model_not_allowed", "verdict_invalid"
    }


@pytest.mark.parametrize("pattern", [r"\w{500}", r"[a-z]{600}[0-9]{600}", r"(?:\w{200}){5}"])
def test_patterns_over_the_program_size_cap_are_refused_with_the_size(pattern):
    assert MAX_PROGRAM_SIZE == 1000
    reason = re2_pattern_error(pattern)
    assert reason is not None and "is too complex (RE2 program size " in reason
    assert re.search(r"program size (\d+), at most 1000\)", reason) and int(re.search(r"size (\d+)", reason)[1]) > 1000
    with pytest.raises(Re2PatternError):
        compile_re2(pattern)
    (error,) = processor_errors([spec("regex_blocklist", patterns=["ok", pattern])], [])
    assert error.startswith("input_processors[0].config.patterns[1]: is too complex (RE2 program size ")


@pytest.mark.parametrize("pattern", [r"\w{120}", r"[a-z]{100}[0-9]{100}", r"(\w+\s){30}"])
def test_patterns_under_the_program_size_cap_are_accepted(pattern):
    assert re2_pattern_error(pattern) is None


# --- PII: Luhn, IBAN, overlaps, shared deadline --------------------------------------------------

def found(text: str, kinds=PII_KINDS):
    return [(d.kind, text[d.start:d.end]) for d in detect(text, kinds)]


@pytest.mark.parametrize("number", ["4111111111111111", "5555555555554444", "378282246310005", "30569309025904",
                                    "6011111111111117", "4111 1111 1111 1111", "5555-5555-5555-4444"])
def test_luhn_valid_cards_are_detected_and_redacted(number):
    assert found(f"pay {number} now") == [("credit_card", number)]
    assert pii()[0].apply(f"pay {number} now").text == "pay [REDACTED:credit_card] now"


@pytest.mark.parametrize("number", ["4111111111111112", "5555555555554443", "378282246310006", "1234567812345678",
                                    "4111 1111 1111 1112", "0000000000000000"])
def test_luhn_invalid_numbers_are_left_alone(number):
    assert found(f"id {number} end", ["credit_card"]) == []
    result = apply_deterministic(pii(kinds=["credit_card"]), f"id {number} end")
    assert result.text == f"id {number} end" and result.outcomes[0].action == "allow"


def test_checksum_helpers():
    assert luhn_valid("4111111111111111") and not luhn_valid("4111111111111112")
    assert not luhn_valid("") and not luhn_valid("4111 1111") and not luhn_valid("abc")
    assert iban_valid("DE89370400440532013000") and iban_valid("GB82WEST12345698765432")
    assert not iban_valid("DE88370400440532013000") and not iban_valid("DE89") and not iban_valid("")
    assert not iban_valid("GB82west12345698765432")  # upper case only


@pytest.mark.parametrize("iban", ["DE89370400440532013000", "DE89 3704 0044 0532 0130 00", "GB82WEST12345698765432",
                                  "FR1420041010050500013M02606", "NL91ABNA0417164300"])
def test_valid_ibans_are_one_token_and_never_read_as_cards_or_phones(iban):
    assert found(f"to {iban}.") == [("iban", iban)]
    assert found(f"to {iban}.", ["credit_card", "phone"]) == []  # claimed even when `iban` is not wanted
    assert pii(kinds=["iban"])[0].apply(f"to {iban}.").text == "to [REDACTED:iban]."


def test_invalid_iban_shaped_runs_claim_nothing_so_cards_inside_are_found():
    assert found("DE88370400440532013000", ["iban"]) == []
    assert found("AB12 4111111111111111") == [("credit_card", "4111111111111111")]
    assert found("XX00 5555555555554444 tail") == [("credit_card", "5555555555554444")]
    assert found("a DE88 3704 0044 0532 0130 00 z", ["iban"]) == []


def test_overlap_priority_email_wins_and_adjacent_kinds_are_all_found():
    text = "mail 4111111111111111@example.com, card 4111111111111111, ip 10.0.0.1, ssn 123-45-6789"
    assert [k for k, _ in found(text)] == ["email", "credit_card", "ipv4", "us_ssn"]


def test_pii_does_not_count_against_the_redaction_cap_and_shares_the_chain_deadline():
    text = "\n".join(f"user{i}@example.com" for i in range(MAX_REDACTED_MATCHES * 2))
    result = apply_deterministic(pii(), text, new_budget())
    assert not result.blocked and result.outcomes[0].match_counts["email"] == MAX_REDACTED_MATCHES * 2
    chain = compile_processors([spec("pii_redact"), spec("regex_blocklist", patterns=["x"], action="redact")])
    result = apply_deterministic(chain, "a@b.com x x", MatchBudget(-1.0, None))
    assert result.blocked and [o.reason for o in result.outcomes] == [BUDGET_EXCEEDED]  # stops at the first block


def test_pii_block_action_reports_kinds_and_withholds_the_text():
    result = apply_deterministic(pii(kinds=["email", "us_ssn"], action="block"), "bob@example.com 123-45-6789 x@y.io")
    assert result.blocked and result.text is None
    assert dict(result.outcomes[0].match_counts) == {"email": 2, "us_ssn": 1}
    assert apply_deterministic(pii(kinds=["ipv4"], action="block"), "no pii").text == "no pii"


def test_unicode_before_a_match_does_not_shift_offsets():
    text = "\U0001F600é mail bob@example.com 中 card 4111111111111111"
    assert pii()[0].apply(text).text == "\U0001F600é mail [REDACTED:email] 中 card [REDACTED:credit_card]"


# --- matched text never appears in outcomes, events or errors ------------------------------------

LEAKY = [
    (pii(), f"mail {SECRET}@example.com card 4111111111111111"),
    (pii(action="block"), f"mail {SECRET}@example.com"),
    (blocklist(f"{SECRET}\\d+"), f"say {SECRET}42 now"),
    (blocklist(f"{SECRET}\\d+", action="redact"), f"say {SECRET}42 now"),
    (blocklist("a+b|a"), "a" * 4096 + SECRET),
    (blocklist(SECRET, action="redact"), SECRET * (MAX_REDACTED_MATCHES + 1)),
    (compile_processors([spec("max_length", max_chars=3, action="truncate")]), SECRET),
    (compile_processors([spec("max_length", max_chars=3)]), SECRET),
]


@pytest.mark.parametrize("index", range(len(LEAKY)))
def test_outcomes_and_events_never_contain_matched_text(index):
    chain, text = LEAKY[index]
    result = apply_deterministic(chain, text)
    for i, outcome in enumerate(result.outcomes):
        dumped = json.dumps([outcome.to_json(), outcome.event_data("input", i), outcome.event_data("output", i)])
        assert SECRET not in dumped and SECRET not in repr(outcome) and SECRET not in repr(result.outcomes)
        assert all(isinstance(v, int) for v in outcome.match_counts.values())
        assert set(outcome.event_data("input", i)) <= {"phase", "index", "processor_type", "action", "match_counts", "reason"}
    if result.blocked:
        assert result.text is None  # blocked text is not passed on


def test_redacted_text_contains_no_matched_text_and_event_shape_is_fixed():
    result = apply_deterministic(blocklist(SECRET, action="redact"), f"a {SECRET} b {SECRET}")
    assert result.text == "a [REDACTED:regex] b [REDACTED:regex]"
    assert result.outcomes[0].event_data("output", 3) == {
        "phase": "output", "index": 3, "processor_type": "regex_blocklist", "action": "redact",
        "match_counts": {"pattern_0": 2},
    }


def test_moderation_compiles_but_refuses_to_apply_and_exposes_no_text():
    (moderation,) = compile_processors([spec("moderation", model={"provider": "test", "name": "m"})])
    assert moderation.deterministic is False
    with pytest.raises(ProcessorNeedsModel) as info:
        apply_deterministic((moderation,), SECRET)
    assert SECRET not in str(info.value)


# --- the engine never uses Python's `re` on user patterns ----------------------------------------

def test_python_re_is_never_called_while_processing_user_patterns(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("python re used")

    for name in ("compile", "match", "search", "fullmatch", "finditer", "findall", "sub", "subn", "split"):
        monkeypatch.setattr(re, name, forbidden)
    # Patterns unique to this test, so they are compiled (not cache hits) while `re` is patched.
    chain = compile_processors([spec("regex_blocklist", patterns=[f"{SECRET}-uniq-\\d+", "qq+r|q"], action="redact"),
                                spec("pii_redact")])
    result = apply_deterministic(chain, f"{SECRET}-uniq-7 mail a@b.com qqq")
    assert result.text == "[REDACTED:regex] mail [REDACTED:email] [REDACTED:regex]"
    assert apply_deterministic(blocklist("x-unique-9"), "x-unique-9").blocked


def test_source_has_no_re_importlib_dynamic_import_or_eval():
    package = Path(eap_processors.__file__).parent
    for path in package.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                assert not {a.name.split(".")[0] for a in node.names} & {"re", "importlib", "regex"}, path.name
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                assert (node.module or "").split(".")[0] not in {"re", "importlib", "regex"}, path.name
            elif isinstance(node, ast.Name):
                assert node.id not in {"__import__", "eval", "exec"}, path.name
