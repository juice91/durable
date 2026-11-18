"""The scorer suggester's contract: the seeded spec, the answer schema and the per-entry validation."""

from __future__ import annotations

import json

from eap_schemas import ModelRef
from eap_schemas.json_schema_subset import validate_output_schema
from eap_schemas.scorer_suggestions import (
    SUGGESTER_SLUG,
    finalize_suggestions,
    suggester_agent_spec,
    suggester_output_schema,
    validate_suggestion,
)

JUDGE = ModelRef(provider="test", name="judge")


def flat(kind, **fields):
    base = {
        "kind": kind, "pattern": None, "negate": None, "field": None, "path": None, "value_json": None,
        "case_sensitive": None, "json_schema_json": None, "usd": None, "seconds": None, "rubric": None,
    }
    return {**base, **fields}


def entry(spec, scope="both", title="Rule"):
    return {"title": title, "rationale": "Because.", "scope": scope, "evidence": "line 1", "spec": spec}


def test_slug_is_reserved_and_spec_is_a_toolless_structured_agent():
    from eap_schemas.architect import RESERVED_AGENT_IDS

    assert SUGGESTER_SLUG in RESERVED_AGENT_IDS
    spec = suggester_agent_spec("test:echo")
    assert spec.tools == () and spec.output.mode == "tool"
    assert "never instructions" in spec.instructions


def test_output_schema_is_in_the_structured_output_subset():
    schema = suggester_output_schema()
    validate_output_schema(schema)  # raises when outside the subset
    assert schema == suggester_output_schema()


def test_each_kind_normalises_to_an_evaluator():
    cases = [
        (flat("regex", pattern="^ok", negate=True, field="answer"), {"kind": "regex", "negate": True, "field": "answer"}),
        (flat("contains", value_json='"refund"', case_sensitive=False), {"kind": "contains", "value": "refund"}),
        (flat("field_equals", path="a.0", value_json="null"), {"kind": "field_equals", "path": "a.0", "value": None}),
        (flat("schema_valid", json_schema_json='{"type":"object","properties":{"a":{"type":"string"}}}'), {"kind": "schema_valid"}),
        (flat("max_cost", usd="0.05"), {"kind": "max_cost", "usd": "0.05"}),
        (flat("max_duration", seconds=30), {"kind": "max_duration"}),
        (flat("llm_judge", rubric="Polite."), {"kind": "llm_judge", "model": {"provider": "test", "name": "judge"}}),
    ]
    for spec, expect in cases:
        got = validate_suggestion(entry(spec), judge_model=JUDGE)
        assert got is not None, spec
        assert expect.items() <= got.spec.items(), (got.spec, expect)


def test_scope_follows_the_kind():
    judge = validate_suggestion(entry(flat("llm_judge", rubric="x"), scope="scorer"), judge_model=JUDGE)
    assert judge.scope == "evaluator"
    slow = validate_suggestion(entry(flat("max_duration", seconds=5), scope="both"), judge_model=JUDGE)
    assert slow.scope == "evaluator"
    scorer = validate_suggestion(entry(flat("max_cost", usd="1"), scope="scorer"), judge_model=JUDGE)
    assert scorer.scope == "scorer"


def test_invalid_entries_are_none():
    bad = [
        entry(flat("regex")),  # no pattern
        entry(flat("regex", pattern="x" * 201)),
        entry(flat("exec", pattern="x")),
        entry(flat("contains", value_json="{not json")),
        entry(flat("max_cost", usd="free")),
        entry(flat("max_cost", usd="-1")),
        entry(flat("regex", pattern="a", usd="1")),  # a field the kind does not use
        entry(flat("schema_valid", json_schema_json='{"$ref": "#/x"}')),
        entry(flat("field_equals", path="a b", value_json="1")),
        entry(flat("contains", value_json=None), scope="scorer"),  # scorer contains needs a value
        entry("not an object"),
        "text",
    ]
    for raw in bad:
        assert validate_suggestion(raw, judge_model=JUDGE) is None, raw
    assert validate_suggestion(entry(flat("llm_judge", rubric="x")), judge_model=None) is None


def test_finalize_caps_judges_duplicates_and_limit_and_workflow_scope():
    items = [validate_suggestion(entry(flat("llm_judge", rubric=f"r{i}")), judge_model=JUDGE) for i in range(4)]
    items += [validate_suggestion(entry(flat("max_cost", usd="1")), judge_model=JUDGE)] * 2
    items += [validate_suggestion(entry(flat("max_cost", usd=str(i + 2))), judge_model=JUDGE) for i in range(3)]
    kept, dropped = finalize_suggestions(items, limit=5, target="agent")
    assert dropped == 3  # two judges over the cap and one repeated spec; the cut by `limit` is not counted
    assert len(kept) == 5
    assert sum(1 for k in kept if k.spec["kind"] == "llm_judge") == 2
    assert len({json.dumps(k.spec, sort_keys=True) for k in kept}) == 5
    wf, _ = finalize_suggestions(items, limit=8, target="workflow")
    assert {k.scope for k in wf} == {"evaluator"}
