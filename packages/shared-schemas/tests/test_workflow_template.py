"""`{"$template": <json>}` in a step's input: a literal whose strings may hold `{{ <ref path> }}` placeholders.

It makes a header (`Bearer {{steps.s.output.token}}`) or a nested request body from step outputs expressible.
The placeholders are refs like any other: the same path grammar and the same availability rules."""

import pytest
from pydantic import ValidationError

from eap_schemas.workflows import WorkflowSpec, iter_refs, template_paths

TOOLS = (
    {"id": "a", "type": "tool", "tool_id": "clock.now", "input": {}, "next": "b"},
    {"id": "b", "type": "tool", "tool_id": "http.request", "input": {
        "method": "POST", "url": "https://x.example/y",
        "headers": {"$template": {"Authorization": "Bearer {{ steps.a.output.now }}"}},
        "json": {"$template": {"who": "{{input.name}}", "nested": {"list": ["{{steps.a.output}}", "lit"]}}},
    }},
)


def spec(*steps, **extra):
    return {"name": "t", "input_schema": {"type": "object"}, "start": steps[0]["id"], "steps": list(steps), **extra}


def test_a_template_validates_and_its_placeholders_are_refs():
    parsed = WorkflowSpec.model_validate(spec(*TOOLS))
    assert iter_refs(parsed.steps[1]) == ["steps.a.output.now", "input.name", "steps.a.output"]


def test_a_template_reading_a_later_step_is_refused_like_a_ref():
    late = {"id": "a", "type": "tool", "tool_id": "clock.now", "next": "b",
            "input": {"x": {"$template": "t={{steps.b.output.now}}"}}}
    with pytest.raises(ValidationError, match="does not run before"):
        WorkflowSpec.model_validate(spec(late, {"id": "b", "type": "tool", "tool_id": "clock.now", "input": {}}))


@pytest.mark.parametrize("bad", [
    "{{ nope.x }}", "{{steps.a.out}}", "{{}}", "{{ input.x", "input.x }}", "{{ steps.a.output.now }} {{", "{{{{input.x}}}}",
    "{{loop.l.index.x}}",
])
def test_a_malformed_placeholder_is_refused(bad):
    step = {"id": "a", "type": "tool", "tool_id": "clock.now", "input": {"x": {"$template": {"k": [bad]}}}}
    with pytest.raises(ValidationError):
        WorkflowSpec.model_validate(spec(step))


@pytest.mark.parametrize("bad", [{"$template": 1, "x": 2}, {"$template": "{{input.a}}", "$ref": "input.a"}])
def test_a_template_is_a_whole_value(bad):
    step = {"id": "a", "type": "tool", "tool_id": "clock.now", "input": {"x": bad}}
    with pytest.raises(ValidationError):
        WorkflowSpec.model_validate(spec(step))


def test_a_template_is_depth_bounded_and_keys_are_never_templated():
    deep: object = "{{input.x}}"
    for _ in range(40):
        deep = [deep]
    with pytest.raises(ValidationError, match="nested"):
        WorkflowSpec.model_validate(spec({"id": "a", "type": "tool", "tool_id": "clock.now", "input": {"x": {"$template": deep}}}))
    assert template_paths({"{{input.x}}": "plain"}) == []  # a key is data
    assert template_paths(["a {{input.x}} b", {"k": "{{steps.s.output.y.z}}"}, 3, None, True]) == ["input.x", "steps.s.output.y.z"]


def test_workflow_return_is_a_declared_pure_tool():
    from eap_runtime import default_registry
    from eap_schemas.tools import TOOL_DECLARATIONS

    assert TOOL_DECLARATIONS["workflow.return"].side_effect is False
    registry = default_registry()
    assert registry.get_declaration("workflow.return") is TOOL_DECLARATIONS["workflow.return"]
    assert registry.get("workflow.return")(None, {"error": {"code": "x"}}) == {"error": {"code": "x"}}
