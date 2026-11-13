"""Pure workflow evaluation: $ref resolution, templates, condition typing rules; pickle safety."""

from __future__ import annotations

import pickle

import pytest

from eap_worker.errors import WorkflowStepError, classify
from eap_worker.workflow_eval import MISSING, RefMissing, canonical_json, evaluate, resolve_ref, resolve_template

CTX = {
    "input": {"q": "help urgent", "n": 3, "flag": True, "tags": ["a", 1], "none": None},
    "steps": {"ask": {"output": "Echo: urgent"}, "now": {"output": {"items": [{"id": "x"}]}}},
}


def ref(path):
    return {"$ref": path}


def cond(op, left, right=None):
    return {"op": op, "left": left, "right": right}


def test_resolve_ref_paths_and_missing():
    assert resolve_ref(CTX, "input.q") == "help urgent"
    assert resolve_ref(CTX, "steps.now.output.items.0.id") == "x"
    assert resolve_ref(CTX, "input.none") is None
    assert resolve_ref(CTX, "input.nope") is MISSING
    assert resolve_ref(CTX, "steps.other.output") is MISSING
    assert resolve_ref(CTX, "steps.now.output.items.5") is MISSING


def test_template_resolution_and_missing_ref():
    assert resolve_template({"a": ref("input.n"), "b": {"lit": 1}}, CTX) == {"a": 3, "b": {"lit": 1}}
    with pytest.raises(RefMissing):
        resolve_template({"a": ref("input.nope")}, CTX)


@pytest.mark.parametrize(
    ("condition", "expected"),
    [
        (cond("eq", ref("input.n"), 3.0), True),  # numbers by value
        (cond("eq", ref("input.flag"), 1), False),  # booleans are not numbers
        (cond("eq", ref("input.n"), "3"), False),  # no string coercion
        (cond("ne", ref("input.n"), 4), True),
        (cond("ne", ref("input.nope"), 4), False),  # MISSING makes eq and ne false
        (cond("eq", ref("input.none"), None), True),
        (cond("gt", ref("input.n"), 2), True),
        (cond("lt", ref("input.q"), "z"), True),  # strings by code point
        (cond("gt", ref("input.n"), "2"), False),  # mixed types: false
        (cond("gt", ref("input.flag"), 0), False),  # booleans never compare
        (cond("contains", ref("steps.ask.output"), "urgent"), True),
        (cond("contains", ref("input.tags"), 1.0), True),
        (cond("contains", ref("steps.now.output"), "items"), True),  # object: key present
        (cond("contains", ref("input.n"), 3), False),
        (cond("exists", ref("input.q")), True),
        (cond("exists", ref("input.none")), False),  # null does not exist
        (cond("exists", ref("input.nope")), False),
    ],
)
def test_condition_typing_rules(condition, expected):
    assert evaluate(condition, CTX) is expected


def test_non_finite_numbers_are_not_json():
    with pytest.raises(ValueError):
        canonical_json({"x": float("inf")})
    with pytest.raises(ValueError):
        canonical_json([float("nan")])


def test_errors_are_pickle_safe_and_classified():
    for exc in (WorkflowStepError("approval_rejected", "approve"),
                WorkflowStepError("model_not_allowed", "ask", "failed", "ModelNotAllowed: model 'x:y' is not allowed"),
                RefMissing("input.a.b")):
        clone = pickle.loads(pickle.dumps(exc))
        assert type(clone) is type(exc) and str(clone) == str(exc)
    f = classify(WorkflowStepError("approval_timeout", "approve"))
    assert (f.status, f.code) == ("failed", "approval_timeout")
    assert f.error == "WorkflowStepError: the approval was not decided before its timeout"
    f = classify(WorkflowStepError("side_effect_outcome_unknown", "t", "needs_attention", "SideEffectOutcomeUnknown: check it"))
    assert (f.status, f.code, f.error) == ("needs_attention", "side_effect_outcome_unknown", "SideEffectOutcomeUnknown: check it")


def test_non_finite_parsed_output_is_output_invalid():
    from eap_schemas.workflows import WorkflowSpec
    from eap_worker.workflow_run import final_output
    from eap_worker.workflow_eval import non_finite_path

    spec = WorkflowSpec.model_validate({
        "name": "N", "input_schema": {"type": "object"}, "output_schema": {"type": "number"}, "start": "t",
        "steps": [{"id": "t", "type": "tool", "tool_id": "clock.now"}],
    })
    for text in ("NaN", "Infinity", "-Infinity"):
        with pytest.raises(WorkflowStepError) as err:
            final_output(spec, text)
        assert err.value.code == "output_invalid"
    assert final_output(spec, "2.5") == 2.5
    assert non_finite_path({"a": [1, {"b": float("-inf")}]}) == ["a", 1, "b"]
    assert non_finite_path({"a": [1.5, None]}) is None
