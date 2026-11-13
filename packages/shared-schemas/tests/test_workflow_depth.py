"""Nesting bounds in workflow specs: deep literals are validation errors, never RecursionError."""

import uuid

import pytest
from pydantic import ValidationError

from eap_schemas.workflows import MAX_LITERAL_DEPTH, AgentStep, WorkflowSpec, nesting_depth


def nested(levels: int, leaf=1):
    for _ in range(levels):
        leaf = [leaf]
    return leaf


def spec(input_value=None, when=None, input_schema=None) -> dict:
    steps = [{"id": "a", "type": "agent", "agent_id": str(uuid.uuid4()), "input": {"q": input_value}}]
    if when is not None:
        steps.insert(0, {"id": "b", "type": "branch", "cases": [{"when": when, "next": "a"}], "default": "a"})
    return {
        "name": "W",
        "input_schema": input_schema or {"type": "object"},
        "start": steps[0]["id"],
        "steps": steps,
    }


def test_nesting_depth_is_iterative_and_stops_past_the_limit():
    assert nesting_depth(1, 5) == 0
    assert nesting_depth({"a": [1, {"b": []}]}, 5) == 4
    assert nesting_depth(nested(100_000), 32) == 33


@pytest.mark.parametrize("levels", [MAX_LITERAL_DEPTH + 1, 1_000, 20_000])
def test_deep_template_value_is_a_validation_error_naming_the_step(levels):
    with pytest.raises(ValidationError) as err:
        WorkflowSpec.model_validate(spec(nested(levels)))
    assert "step 'a': input: value of 'q' is nested more than 32 levels deep" in str(err.value)


def test_template_value_at_the_cap_is_accepted():
    assert WorkflowSpec.model_validate(spec(nested(MAX_LITERAL_DEPTH))).steps[0].input["q"] == nested(32)


def test_deep_value_in_a_step_on_its_own_is_rejected():
    with pytest.raises(ValidationError, match="nested more than 32 levels deep"):
        AgentStep.model_validate({"id": "a", "type": "agent", "agent_id": str(uuid.uuid4()), "input": {"q": nested(5_000)}})


def test_deep_spec_outside_templates_is_rejected():
    deep_schema = {"type": "object", "properties": {"x": nested(1_000)}}
    with pytest.raises(ValidationError, match="workflow spec is nested more than 64 levels deep"):
        WorkflowSpec.model_validate(spec(1, input_schema=deep_schema))


def test_condition_operands_accept_bounded_object_and_array_literals():
    when = {"op": "contains", "left": ["a", {"k": [1, 2]}], "right": {"$ref": "input.tag"}}
    parsed = WorkflowSpec.model_validate(spec(1, when=when))
    assert parsed.steps[0].cases[0].when.left == ["a", {"k": [1, 2]}]
    eq = {"op": "eq", "left": {"$ref": "input.obj"}, "right": {"a": 1, "b": [True, None]}}
    assert WorkflowSpec.model_validate(spec(1, when=eq)).steps[0].cases[0].when.right == {"a": 1, "b": [True, None]}


def test_condition_operand_literal_cannot_hide_a_ref_or_nest_too_deep():
    hidden = {"op": "eq", "left": {"x": {"$ref": "input.a"}}, "right": 1}
    with pytest.raises(ValidationError, match=r"step 'b': cases\.0\.when\.left: \$ref is only allowed as a whole value"):
        WorkflowSpec.model_validate(spec(1, when=hidden))
    deep = {"op": "eq", "left": {"$ref": "input.a"}, "right": nested(3_000)}
    with pytest.raises(ValidationError, match=r"step 'b': cases\.0\.when\.right: operand is nested more than 32 levels deep"):
        WorkflowSpec.model_validate(spec(1, when=deep))
