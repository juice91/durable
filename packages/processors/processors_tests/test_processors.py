"""eap_processors: PII detection tables, RE2 validation, chain semantics, bounds, import rule."""

from __future__ import annotations

import ast
import pickle
import sys
import time
from pathlib import Path

import pytest

from eap_processors import (
    BUDGET_EXCEEDED,
    INPUT_TOO_LARGE,
    MAX_PROGRAM_SIZE,
    MAX_REGEX_INPUT_CHARS,
    MAX_REDACTED_MATCHES,
    TOO_MANY_MATCHES,
    MatchBudget,
    MatchBudgetExceeded,
    MAX_PROCESSOR_INPUT_CHARS,
    InvalidProcessorConfig,
    ProcessorNeedsModel,
    Re2PatternError,
    apply_deterministic,
    compile_processors,
    compiled_chain,
    detect,
    iban_valid,
    luhn_valid,
    processor_errors,
    re2_pattern_error,
)
from eap_schemas.processors import PII_KINDS, PROCESSOR_SPEC_ADAPTER


def spec(data: dict):
    return PROCESSOR_SPEC_ADAPTER.validate_python(data)


def found(text: str, kinds=PII_KINDS) -> list[tuple[str, str]]:
    return [(d.kind, text[d.start : d.end]) for d in detect(text, kinds)]


@pytest.mark.parametrize(
    "text, expected",
    [
        ("write to alice.smith+tag@example.co.uk.", [("email", "alice.smith+tag@example.co.uk")]),
        ("not an email: alice@localhost", []),
        ("card 4111 1111 1111 1111 now", [("credit_card", "4111 1111 1111 1111")]),
        ("card 4111-1111-1111-1111", [("credit_card", "4111-1111-1111-1111")]),
        ("amex 378282246310005", [("credit_card", "378282246310005")]),
        ("Luhn-invalid 4111 1111 1111 1112", []),
        ("all zeros 0000 0000 0000 0000", []),
        ("12 digits 411111111111", []),
        ("amex 3782 822463 10005", [("credit_card", "3782 822463 10005")]),
        ("mixed separators 4111 1111-1111 1111", []),
        ("card then year 4111111111111111 2024", [("credit_card", "4111111111111111")]),
        ("iban DE89 3704 0044 0532 0130 00 AND", [("iban", "DE89 3704 0044 0532 0130 00")]),
        ("iban GB82WEST12345698765432.", [("iban", "GB82WEST12345698765432")]),
        ("bad check digits DE88 3704 0044 0532 0130 00", []),
        ("IBAN-shaped prefix AB12 4111111111111111", [("credit_card", "4111111111111111")]),
        ("ip 192.168.1.10 here", [("ipv4", "192.168.1.10")]),
        ("octet 256.1.1.1", []),
        ("leading zero 01.2.3.4", []),
        ("version 1.2.3.4.5", []),
        ("ssn 123-45-6789", [("us_ssn", "123-45-6789")]),
        ("ssn 000-12-3456 666-12-3456 900-12-3456 123-00-4567 123-45-0000", []),
        ("call +44 20 7946 0958 today", [("phone", "+44 20 7946 0958")]),
        ("call (555) 123-4567", [("phone", "(555) 123-4567")]),
        ("call 555.123.4567 or 1-800-555-1234", [("phone", "555.123.4567"), ("phone", "1-800-555-1234")]),
        ("date 2024-01-15 and order 12345678", []),
    ],
)
def test_pii_detection_table(text: str, expected: list[tuple[str, str]]) -> None:
    assert found(text) == expected


def test_only_wanted_kinds_are_detected() -> None:
    text = "bob@example.com 4111 1111 1111 1111"
    assert found(text, ["email"]) == [("email", "bob@example.com")]
    assert found(text, ["credit_card"]) == [("credit_card", "4111 1111 1111 1111")]


def test_valid_iban_is_never_read_as_a_card_but_an_invalid_one_claims_nothing() -> None:
    # A valid IBAN claims its span even when `iban` is not a wanted kind.
    assert found("GB82 WEST 1234 5698 7654 32", ["credit_card", "phone"]) == []
    # An IBAN-shaped run that fails mod 97 does not hide a card number inside it.
    assert found("AB12 4111111111111111", ["iban", "credit_card"]) == [("credit_card", "4111111111111111")]
    assert found("XX00 4111 1111 1111 1111 x", ["credit_card"]) == [("credit_card", "4111 1111 1111 1111")]


def test_checksums() -> None:
    assert luhn_valid("4111111111111111") and not luhn_valid("4111111111111112")
    assert iban_valid("DE89370400440532013000") and not iban_valid("DE89370400440532013001")


def test_pii_redact_and_block_outcomes_carry_no_matched_text() -> None:
    sample = "mail bob@example.com, card 4111 1111 1111 1111, mail eve@example.org"
    redact = compile_processors([spec({"type": "pii_redact", "config": {}})])
    result = apply_deterministic(redact, sample)
    assert result.text == "mail [REDACTED:email], card [REDACTED:credit_card], mail [REDACTED:email]"
    outcome = result.outcomes[0]
    assert outcome.action == "redact" and dict(outcome.match_counts) == {"email": 2, "credit_card": 1}

    block = compile_processors([spec({"type": "pii_redact", "config": {"action": "block"}})])
    blocked = apply_deterministic(block, sample)
    assert blocked.blocked and blocked.text is None
    for out in (outcome, blocked.outcomes[0]):
        dumped = repr(out.event_data("input", 0))
        for secret in ("bob@example.com", "eve@example.org", "4111"):
            assert secret not in dumped


def test_regex_blocklist_block_redact_and_counts() -> None:
    patterns = ["(?i)secret", r"\bfoo\b", "sec"]
    block = compile_processors([spec({"type": "regex_blocklist", "config": {"patterns": patterns}})])
    result = apply_deterministic(block, "a SECRET and foo, secret")
    assert result.blocked and result.text is None
    # Block runs one search per pattern: 1 means "matched".
    assert dict(result.outcomes[0].match_counts) == {"pattern_0": 1, "pattern_1": 1, "pattern_2": 1}

    redact = compile_processors(
        [spec({"type": "regex_blocklist", "config": {"patterns": patterns, "action": "redact"}})]
    )
    # Overlapping matches of two patterns are redacted as one span.
    assert apply_deterministic(redact, "a secret foo").text == "a [REDACTED:regex] [REDACTED:regex]"
    clean = apply_deterministic(redact, "nothing here")
    assert clean.text == "nothing here" and clean.outcomes[0].action == "allow"


def test_regex_unicode_offsets() -> None:
    redact = compile_processors([spec({"type": "regex_blocklist", "config": {"patterns": ["é+"], "action": "redact"}})])
    assert apply_deterministic(redact, "aééb ü é").text == "a[REDACTED:regex]b ü [REDACTED:regex]"


@pytest.mark.parametrize(
    "pattern",
    [r"(a)\1", r"(?=a)b", r"(?!a)b", r"(?<=a)b", r"(?<!a)b", "[", "a{1001}", "a*", "x|", "^", "(?i)",
     r"\b", r"\B", "$", r"(?m)^", r"\bx?", r"(?:\b)+"],
)
def test_re2_rejects_backtracking_constructs_and_empty_matches(pattern: str) -> None:
    assert re2_pattern_error(pattern) is not None
    errors = processor_errors(
        [], [spec({"type": "regex_blocklist", "config": {"patterns": ["ok", pattern]}})]
    )
    assert len(errors) == 1 and errors[0].startswith("output_processors[0].config.patterns[1]: ")


def test_valid_specs_have_no_errors() -> None:
    procs = [
        spec({"type": "pii_redact"}),
        spec({"type": "regex_blocklist", "config": {"patterns": [r"(?i)\bpassword\b", "[0-9]{6}"]}}),
        spec({"type": "max_length", "config": {"max_chars": 10}}),
        spec({"type": "moderation", "config": {"model": {"provider": "test", "name": "moderation"}}}),
    ]
    assert processor_errors(procs, procs) == []


@pytest.mark.parametrize("action", ["block", "redact"])
@pytest.mark.parametrize("pattern", ["a+b|a", "(a+)+$", "(a|a)*b", "(.*a){20}x", "(x+x+)+y", "(\\w+\\s?)+$"])
def test_pathological_patterns_finish_in_bounded_time(pattern: str, action: str) -> None:
    # `a+b|a` on a run of `a`: every search after a match scans to the end, so finding all matches
    # is quadratic; block stops at the first match and redact at the match cap.
    procs = compile_processors(
        [spec({"type": "regex_blocklist", "config": {"patterns": [pattern] * 1 + ["zz" + str(i) for i in range(19)],
                                                      "action": action}})]
    )
    started = time.perf_counter()
    result = apply_deterministic(procs, "a" * (32 * 1024))
    assert time.perf_counter() - started < 1.0
    if pattern == "a+b|a":
        assert result.blocked
        if action == "redact":
            assert result.outcomes[0].reason == TOO_MANY_MATCHES and not result.outcomes[0].match_counts


def test_redaction_match_cap_is_per_chain_and_fails_closed() -> None:
    redact = spec({"type": "regex_blocklist", "config": {"patterns": ["x"], "action": "redact"}})
    procs = compile_processors([redact])
    at_cap = apply_deterministic(procs, "x " * MAX_REDACTED_MATCHES)
    assert at_cap.outcomes[0].action == "redact"
    over = apply_deterministic(procs, "x " * (MAX_REDACTED_MATCHES + 1))
    assert over.blocked and over.text is None and over.outcomes[0].reason == TOO_MANY_MATCHES
    # Two processors share the chain's cap: each finds just over half of it.
    chain = compile_processors([redact, spec({"type": "regex_blocklist", "config": {"patterns": ["y"], "action": "redact"}})])
    split = apply_deterministic(chain, "x y " * (MAX_REDACTED_MATCHES // 2 + 1))
    assert split.blocked and split.outcomes[-1].reason == TOO_MANY_MATCHES


def test_time_budget_fails_closed_for_regex_and_pii() -> None:
    expired = MatchBudget(-1.0, MAX_REDACTED_MATCHES)
    regex = compile_processors([spec({"type": "regex_blocklist", "config": {"patterns": ["x"], "action": "redact"}})])
    result = apply_deterministic(regex, "x x", expired)
    assert result.blocked and result.outcomes[0].reason == BUDGET_EXCEEDED
    pii = compile_processors([spec({"type": "pii_redact"})])
    result = apply_deterministic(pii, "bob@example.com eve@example.com", MatchBudget(-1.0, None))
    assert result.blocked and result.outcomes[0].reason == BUDGET_EXCEEDED
    with pytest.raises(MatchBudgetExceeded):
        detect("bob@example.com eve@example.com", PII_KINDS, MatchBudget(-1.0, None))


def test_pii_scans_are_linear_on_adversarial_text() -> None:
    procs = compile_processors([spec({"type": "pii_redact"})])
    for unit in ("1.", "AB12 ", "x@y.zz ", "1111 ", "+12 (1) ", "a@b.cc."):
        text = (unit * (32 * 1024 // len(unit) + 1))[: 32 * 1024]
        started = time.perf_counter()
        apply_deterministic(procs, text)
        assert time.perf_counter() - started < 1.0, unit


def test_pii_on_32_kib_is_fast() -> None:
    procs = compile_processors([spec({"type": "pii_redact"})])
    text = ("bob@example.com 4111 1111 1111 1111 192.168.0.1 +44 20 7946 0958 " * 600)[: 32 * 1024]
    started = time.perf_counter()
    apply_deterministic(procs, text)
    assert time.perf_counter() - started < 1.0


def test_max_length_block_and_truncate_count_code_points() -> None:
    truncate = compile_processors([spec({"type": "max_length", "config": {"max_chars": 3, "action": "truncate"}})])
    result = apply_deterministic(truncate, "ééééé")
    assert result.text == "ééé" and result.outcomes[0].action == "truncate"
    assert dict(result.outcomes[0].match_counts) == {"chars_over_limit": 2}
    block = compile_processors([spec({"type": "max_length", "config": {"max_chars": 5}})])
    assert apply_deterministic(block, "12345").outcomes[0].action == "allow"
    assert apply_deterministic(block, "123456").blocked


def test_input_over_the_bound_is_blocked_fail_closed() -> None:
    procs = compile_processors([spec({"type": "regex_blocklist", "config": {"patterns": ["zzz"], "action": "redact"}})])
    result = apply_deterministic(procs, "a" * (MAX_PROCESSOR_INPUT_CHARS + 1))
    assert result.blocked and result.outcomes[0].reason == INPUT_TOO_LARGE


def test_chain_runs_in_order_and_stops_at_first_block() -> None:
    procs = compile_processors(
        [
            spec({"type": "pii_redact"}),
            spec({"type": "regex_blocklist", "config": {"patterns": [r"REDACTED:email"]}}),
            spec({"type": "max_length", "config": {"max_chars": 1}}),
        ]
    )
    result = apply_deterministic(procs, "hi bob@example.com")
    assert result.blocked and [o.processor_type for o in result.outcomes] == ["pii_redact", "regex_blocklist"]


def test_moderation_compiles_but_does_not_apply() -> None:
    (moderation,) = compile_processors(
        [spec({"type": "moderation", "config": {"model": {"provider": "test", "name": "moderation"}}})]
    )
    assert moderation.deterministic is False
    with pytest.raises(ProcessorNeedsModel):
        moderation.apply("text")


def test_patterns_are_compiled_once_per_pattern() -> None:
    procs = (spec({"type": "regex_blocklist", "config": {"patterns": ["abc"]}}),)
    (first,), (second,) = compiled_chain(procs), compiled_chain(procs)
    assert first.patterns[0] is second.patterns[0]


def test_invalid_config_raises_with_location() -> None:
    with pytest.raises(InvalidProcessorConfig) as info:
        compile_processors([spec({"type": "regex_blocklist", "config": {"patterns": ["(?=x)"]}})], "input_processors")
    assert str(info.value).startswith("input_processors[0].config.patterns[0]: not valid RE2 syntax")


def test_exceptions_are_pickle_safe() -> None:
    for exc in (InvalidProcessorConfig("loc", "why"), ProcessorNeedsModel("moderation"), Re2PatternError("why"),
                MatchBudgetExceeded(BUDGET_EXCEEDED)):
        clone = pickle.loads(pickle.dumps(exc))
        assert type(clone) is type(exc) and str(clone) == str(exc)


def test_package_imports_only_stdlib_eap_schemas_and_re2() -> None:
    allowed = {"eap_schemas", "eap_processors", "re2", "__future__"}
    package = Path(__file__).resolve().parents[1] / "src" / "eap_processors"
    for path in package.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for name in names:
                top = name.split(".")[0]
                # Third-party modules are not in stdlib_module_names, so any of them fails here.
                assert top in allowed or top in sys.stdlib_module_names, f"{path.name} imports {name}"
                assert top not in ("re", "importlib"), f"{path.name} imports {name}"
            if isinstance(node, ast.Name):
                assert node.id != "__import__", f"{path.name} uses __import__"


# The reviewer's DFA-busting shapes: A keeps ~1,000 NFA states alive on random a/b text (slow NFA
# fallback); B has a large program the DFA still handles.
SHAPE_A_AT_CAP = "[ab]*a[ab]{988}c|q{i}"  # program size 999
SHAPE_A_UNDER = "[ab]*a[ab]{960}c|q{i}"  # program size ~970
SHAPE_B_AT_CAP = "[a-q][^u-z]{{124}}x{i}"  # program size 998-1000
SHAPE_B_UNDER = "[a-q][^u-z]{{120}}x{i}"  # program size ~967


def _random_ab(n: int) -> str:
    import random

    rng = random.Random(7)
    return "".join(rng.choice("ab") for _ in range(n))


def _patterns(shape: str) -> list[str]:
    return [shape.replace("{988}", "{{988}}").replace("{960}", "{{960}}").format(i=i) for i in range(20)]


@pytest.mark.parametrize("shape", [SHAPE_A_AT_CAP, SHAPE_A_UNDER, SHAPE_B_AT_CAP, SHAPE_B_UNDER])
def test_shapes_at_and_under_the_program_size_cap_are_accepted(shape: str) -> None:
    from eap_processors.regex import compile_re2

    for pattern in _patterns(shape):
        assert re2_pattern_error(pattern) is None, pattern
        assert compile_re2(pattern).programsize <= MAX_PROGRAM_SIZE


@pytest.mark.parametrize("pattern", ["[ab]*a[ab]{999}c|q", "[a-q][^u-z]{900}x", "[a-q][^u-z]{125}x", r"\pL+", ".{150}x"])
def test_patterns_over_the_program_size_cap_are_rejected(pattern: str) -> None:
    error = re2_pattern_error(pattern)
    assert error is not None and error.startswith("is too complex (RE2 program size")


@pytest.mark.parametrize("shape", [SHAPE_A_AT_CAP, SHAPE_A_UNDER, SHAPE_B_AT_CAP, SHAPE_B_UNDER])
@pytest.mark.parametrize("size", [32 * 1024, MAX_REGEX_INPUT_CHARS])
@pytest.mark.parametrize("action", ["block", "redact"])
def test_worst_accepted_patterns_end_within_budget_or_fail_closed(shape: str, size: int, action: str) -> None:
    """Safety: never ALLOW after the deadline, judged by the engine's own clock (the budget's last
    passed check, made after the final search), not by wall time, so load cannot fail it. Bounded
    work: thread CPU time is at most the budget plus a few uninterruptible searches, scaled by this
    machine's measured cost of one search (CPU time, so other processes do not count)."""
    from eap_processors.regex import compile_re2

    budget_s = 0.3
    text = _random_ab(size)
    patterns = _patterns(shape)
    probe = compile_re2(patterns[0])
    cpu = time.thread_time()
    probe.search(text)
    one_search_cpu = time.thread_time() - cpu

    procs = compile_processors(
        [spec({"type": "regex_blocklist", "config": {"patterns": patterns, "action": action}})] * 8
    )
    budget = MatchBudget(budget_s, MAX_REDACTED_MATCHES)
    cpu = time.thread_time()
    result = apply_deterministic(procs, text, budget)
    used_cpu = time.thread_time() - cpu

    # CPU spent while waiting on the wall-clock deadline can only be less than the budget; past it,
    # at most the search in flight (redact: a few, each up to one search) and the final checks.
    assert used_cpu < budget_s + 4 * one_search_cpu + 0.5, (used_cpu, one_search_cpu)
    last = result.outcomes[-1]
    if last.action == "allow":
        assert len(result.outcomes) == 8 and result.text == text
        assert budget.last_passed_check is not None and budget.last_passed_check <= budget.deadline
    else:
        assert result.blocked and last.reason == BUDGET_EXCEEDED and result.text is None


def test_budget_records_its_last_passed_check() -> None:
    budget = MatchBudget(60.0, None)
    assert budget.last_passed_check is None
    procs = compile_processors([spec({"type": "regex_blocklist", "config": {"patterns": ["zzz"]}})])
    assert apply_deterministic(procs, "abc", budget).outcomes[0].action == "allow"
    assert budget.last_passed_check is not None and budget.last_passed_check <= budget.deadline
    expired = MatchBudget(-1.0, None)
    assert apply_deterministic(procs, "abc", expired).blocked and expired.last_passed_check is None


def test_deadline_is_checked_even_when_no_pattern_matches() -> None:
    for action in ("block", "redact"):
        procs = compile_processors([spec({"type": "regex_blocklist", "config": {"patterns": ["zzz"], "action": action}})])
        result = apply_deterministic(procs, "no match here", MatchBudget(-1.0, MAX_REDACTED_MATCHES))
        assert result.blocked and result.outcomes[0].reason == BUDGET_EXCEEDED
    pii = compile_processors([spec({"type": "pii_redact"})])
    result = apply_deterministic(pii, "nothing personal", MatchBudget(-1.0, None))
    assert result.blocked and result.outcomes[0].reason == BUDGET_EXCEEDED


def test_regex_text_cap_is_lower_than_the_pii_cap() -> None:
    regex = compile_processors([spec({"type": "regex_blocklist", "config": {"patterns": ["zzz"]}})])
    assert apply_deterministic(regex, "a" * MAX_REGEX_INPUT_CHARS).outcomes[0].action == "allow"
    over = apply_deterministic(regex, "a" * (MAX_REGEX_INPUT_CHARS + 1))
    assert over.blocked and over.outcomes[0].reason == INPUT_TOO_LARGE
    pii = compile_processors([spec({"type": "pii_redact"})])
    assert apply_deterministic(pii, "a" * (MAX_REGEX_INPUT_CHARS + 1)).outcomes[0].action == "allow"
