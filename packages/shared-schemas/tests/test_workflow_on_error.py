"""`on_error` on tool steps: accepted there, rejected on every other step type."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eap_schemas.architect import ArchitectToolStep
from eap_schemas.workflows import ToolStep, WorkflowSpec


def _spec(*steps):
    return {"name": "x", "input_schema": {"type": "object"}, "start": steps[0]["id"], "steps": list(steps)}


TOOL = {"id": "t", "type": "tool", "tool_id": "clock.now"}


def test_default_is_fail_and_continue_is_accepted():
    assert ToolStep(id="t", tool_id="clock.now").on_error == "fail"
    spec = WorkflowSpec.model_validate(_spec({**TOOL, "on_error": "continue"}))
    assert spec.steps[0].on_error == "continue"
    assert WorkflowSpec.model_validate(_spec(TOOL)).steps[0].on_error == "fail"


def test_other_values_are_rejected():
    with pytest.raises(ValidationError):
        WorkflowSpec.model_validate(_spec({**TOOL, "on_error": "ignore"}))


@pytest.mark.parametrize("step", [
    {"id": "b", "type": "branch", "cases": [{"when": {"op": "exists", "left": {"$ref": "input.x"}}, "next": "t"}],
     "default": "t"},
    {"id": "a", "type": "agent", "agent_id": "00000000-0000-0000-0000-000000000001"},
    {"id": "l", "type": "loop", "mode": "while", "max_iterations": 2, "body": {"start": "t", "steps": [TOOL]}},
])
def test_rejected_on_non_tool_steps_with_a_clear_message(step):
    with pytest.raises(ValidationError) as caught:
        WorkflowSpec.model_validate(_spec({**step, "on_error": "continue"}, TOOL))
    assert "on_error is only allowed on tool steps" in str(caught.value)


def test_architect_tool_step_carries_it_into_the_compiled_step():
    assert ArchitectToolStep(id="t", tool="clock.now").on_error == "fail"
    assert ArchitectToolStep(id="t", tool="clock.now", on_error="continue").on_error == "continue"
