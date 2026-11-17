"""Approval summarisers on the tool registry."""

import pytest

from eap_runtime import ToolRegistry, UnknownToolError


def _publish(agent_id: str) -> str:
    return agent_id


def test_summary_for_uses_the_registered_summariser() -> None:
    registry = ToolRegistry()
    registry.register(
        "test.publish",
        _publish,
        description="Publish",
        approval_summary=lambda args: f"Publish agent {args['agent_id']}",
    )
    assert registry.summary_for("test.publish", {"agent_id": "a1"}) == "Publish agent a1"


def test_summary_for_is_none_without_a_summariser() -> None:
    registry = ToolRegistry()
    registry.register("test.plain", _publish, description="Plain")
    assert registry.summary_for("test.plain", {"agent_id": "a1"}) is None


def test_summary_for_an_unknown_tool_raises() -> None:
    with pytest.raises(UnknownToolError):
        ToolRegistry().summary_for("test.nope", {})
