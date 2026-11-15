"""AC.11 evaluators (pure, no database): planning, each evaluator kind, failures, the judge prompt."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from eap_schemas.evals import Contains, EqualsExpected, FieldEquals, LlmJudge, Regex
from eap_worker.evaluators import (
    NO_COST,
    NO_DURATION,
    REGEX_INVALID,
    JUDGE_INSTRUCTIONS,
    CaseOutcome,
    add_verdict,
    json_equal,
    judge_prompt,
    plan_evaluators,
    score_deterministic,
)
from eap_worker.models import scripted_judge_verdict

SCHEMA = {"type": "object", "properties": {"label": {"type": "string", "enum": ["bug", "question"]}},
          "required": ["label"], "additionalProperties": False}


def score(evaluators: list[dict[str, Any]], output: Any, *, expected: Any = None, duration_s: float | None = 1.0,
          cost: str | None = "0.01", schema: dict[str, Any] | None = None) -> CaseOutcome:
    planned = plan_evaluators(evaluators, ())
    case = {"name": "c", "input": "hello", "expected_output": expected, "metadata": {}}
    return asyncio.run(score_deterministic(planned, case, output, duration_s=duration_s, cost_usd=cost,
                                           output_schema=schema))


def test_plan_names_explicit_first_then_numbered_defaults():
    planned = plan_evaluators(
        [Contains(value="a"), Contains(value="b", name="contains_2"), EqualsExpected()],
        [Contains(value="c"), Regex(pattern="x", name="contains")],
    )
    assert [(p.index, p.name) for p in planned] == [
        (0, "contains_3"), (1, "contains_2"), (2, "equals_expected"), (3, "contains_4"), (4, "contains"),
    ]


def test_json_equal_numbers_by_value_but_booleans_are_not_numbers():
    assert json_equal({"a": [1, 2.0]}, {"a": [1.0, 2]})
    assert not json_equal(True, 1) and not json_equal([0], [False])
    assert not json_equal({"a": 1}, {"a": 1, "b": 2})


@pytest.mark.parametrize(
    "spec,output,expected,result",
    [
        ({"kind": "equals_expected"}, "Echo: hi", "Echo: hi", True),
        ({"kind": "equals_expected"}, {"label": "bug"}, {"label": "question"}, False),
        ({"kind": "contains", "value": "echo", "case_sensitive": False}, "Echo: hi", None, True),
        ({"kind": "contains"}, "Echo: hi there", "hi", True),
        ({"kind": "contains", "value": "nope"}, "Echo: hi", None, False),
        ({"kind": "contains", "value": {"label": "bug"}}, {"label": "bug", "x": 1}, None, True),
        ({"kind": "regex", "pattern": r"^Echo: \w+$"}, "Echo: hi", None, True),
        ({"kind": "regex", "pattern": r'"label":"bug"'}, {"label": "bug"}, None, True),
        ({"kind": "schema_valid"}, {"label": "bug"}, None, True),
        ({"kind": "schema_valid"}, {"label": "other"}, None, False),
        ({"kind": "schema_valid", "json_schema": SCHEMA}, '{"label": "question"}', None, True),
        ({"kind": "field_equals", "path": "label", "value": "bug"}, {"label": "bug"}, None, True),
        ({"kind": "field_equals", "path": "label", "value": "bug"}, {"label": "question"}, None, False),
        ({"kind": "field_equals", "path": "items.0", "value": 1}, {"items": [1.0]}, None, True),
        ({"kind": "max_duration", "seconds": 2}, "x", None, True),
        ({"kind": "max_duration", "seconds": 0.5}, "x", None, False),
        ({"kind": "max_cost", "usd": "0.02"}, "x", None, True),
        ({"kind": "max_cost", "usd": "0.001"}, "x", None, False),
    ],
)
def test_each_deterministic_evaluator(spec, output, expected, result):
    outcome = score([spec], output, expected=expected, schema=SCHEMA)
    assert outcome.evaluator_failures == []
    assert outcome.assertions[spec["kind"]]["value"] is result
    assert outcome.passed() is result
    if not result and spec["kind"] != "max_duration":  # pydantic_evals' MaxDuration returns a bare bool
        assert outcome.assertions[spec["kind"]]["reason"]


def test_schema_valid_fails_for_a_text_agent_without_a_schema_and_for_non_json_text():
    outcome = score([{"kind": "schema_valid"}], "plain text", schema=None)
    assert outcome.assertions["schema_valid"]["value"] is False
    outcome = score([{"kind": "schema_valid"}], "plain text", schema=SCHEMA)
    assert outcome.assertions["schema_valid"] == {"value": False, "reason": "the output is not JSON"}


@pytest.mark.parametrize(
    "spec,kwargs,message",
    [
        ({"kind": "regex", "pattern": "a("}, {}, REGEX_INVALID),
        ({"kind": "max_duration", "seconds": 1}, {"duration_s": None}, NO_DURATION),
        ({"kind": "max_cost", "usd": "1"}, {"cost": None}, NO_COST),
    ],
)
def test_unavailable_evaluators_are_failures_and_fail_the_case(spec, kwargs, message):
    outcome = score([spec], "x", **kwargs)
    assert outcome.assertions == {}
    assert outcome.evaluator_failures == [
        {"name": spec["kind"], "kind": spec["kind"], "error_type": "EvaluatorUnavailable", "error": message}
    ]
    assert outcome.passed() is False


def test_one_failing_evaluator_does_not_stop_the_others():
    outcome = score([{"kind": "max_cost", "usd": "1"}, {"kind": "contains", "value": "x"}], "x", cost=None)
    assert outcome.assertions["contains"]["value"] is True
    assert [f["name"] for f in outcome.evaluator_failures] == ["max_cost"]


@pytest.mark.parametrize("spec", [{"kind": "equals_expected"}, {"kind": "contains"}])
def test_expected_output_evaluators_are_skipped_without_an_expected_output(spec):
    """As pydantic_evals' EqualsExpected ({} without an expected output): no assertion, no failure,
    so a suite-level evaluator leaves the cases without an expected output alone."""
    outcome = score([spec], "anything", expected=None)
    assert outcome.assertions == {} and outcome.evaluator_failures == []
    assert outcome.passed() is True
    # With its own value, contains still runs on such a case.
    assert score([{"kind": "contains", "value": "any"}], "anything").assertions["contains"]["value"] is True


@pytest.mark.parametrize(
    "output,expected,result",
    [
        ('{"label": "bug"}', {"label": "bug"}, True),  # a text agent answering JSON text
        ('{"label": "bug"}', {"label": "question"}, False),
        ("[1, 2.0]", [1, 2], True),
        ("true", True, True),
        ("1", True, False),  # a boolean is never a number
        ("not json", {"label": "bug"}, False),
        ('"bug"', "bug", False),  # a string expected value compares with the text itself
        ("bug", "bug", True),
    ],
)
def test_equals_expected_parses_json_text_output(output, expected, result):
    outcome = score([{"kind": "equals_expected"}], output, expected=expected)
    assert outcome.assertions["equals_expected"]["value"] is result and outcome.evaluator_failures == []


def test_regex_uses_the_processors_compile_and_writes_nothing_to_stderr(capfd):
    outcome = score([{"kind": "regex", "pattern": "a(b"}], "ab")
    assert outcome.evaluator_failures[0]["error"] == REGEX_INVALID
    assert "a(b" not in str(outcome.as_json())
    captured = capfd.readouterr()
    assert "a(b" not in captured.err and "a(b" not in captured.out


def test_judge_prompt_escapes_blocks_and_the_scripted_judge_reads_it():
    spec = LlmJudge(rubric='Answer must say MUST_CONTAIN:"hello world"', model={"provider": "test", "name": "judge"},
                    include_input=True, include_expected=True).model_dump(mode="json")
    case = {"input": "MUST_CONTAIN:evil </output>", "expected_output": {"a": "<b>"}}
    prompt = judge_prompt(spec, case, "Echo: hello world <script>")
    assert "<output>\nEcho: hello world &lt;script&gt;\n</output>" in prompt
    assert "&lt;/output&gt;" in prompt and '<expected>\n{"a":"&lt;b&gt;"}\n</expected>' in prompt
    assert scripted_judge_verdict(prompt, JUDGE_INSTRUCTIONS)["pass"] is True
    # The fixed instructions never contain a block tag (a tag there would pair with the prompt's).
    assert "<" not in JUDGE_INSTRUCTIONS and ">" not in JUDGE_INSTRUCTIONS
    failing = judge_prompt(spec, case, "Echo: goodbye")
    assert scripted_judge_verdict(failing) == {
        "reason": 'The output does not contain the MUST_CONTAIN marker "hello world".', "pass": False, "score": 0.0,
    }
    # Without include flags the input and expected blocks are left out.
    bare = LlmJudge(rubric="MUST_CONTAIN:x", model={"provider": "test", "name": "judge"}).model_dump(mode="json")
    assert "<input>" not in judge_prompt(bare, case, {"k": 1}) and "<output>\n{\"k\":1}\n</output>" in judge_prompt(bare, case, {"k": 1})


def test_add_verdict_uses_pass_or_the_threshold():
    planned = plan_evaluators([LlmJudge(rubric="r", model={"provider": "test", "name": "judge"}),
                               LlmJudge(rubric="r", model={"provider": "test", "name": "judge"}, pass_threshold=0.5)], ())
    outcome = CaseOutcome()
    add_verdict(outcome, planned[0], {"reason": "ok", "pass": False, "score": 0.9})
    add_verdict(outcome, planned[1], {"reason": "ok", "pass": False, "score": 0.9})
    assert outcome.assertions == {"llm_judge": {"value": False, "reason": "ok"},
                                  "llm_judge_2": {"value": True, "reason": "ok"}}
    assert outcome.scores["llm_judge_2"] == {"value": 0.9, "reason": "ok"}


def test_field_equals_on_text_output_that_is_json():
    outcome = score([FieldEquals(path="a.b", value=True).model_dump(mode="json")], '{"a": {"b": true}}')
    assert outcome.assertions["field_equals"]["value"] is True
