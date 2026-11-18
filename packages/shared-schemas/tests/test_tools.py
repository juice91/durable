"""Tool declaration contract tests."""

import pytest
from pydantic import BaseModel, ValidationError

from eap_schemas.tools import TOOL_DECLARATIONS, ToolDeclaration


def test_builtin_tool_declarations_expose_stable_input_schemas() -> None:
    """The catalog describes the built-in tool inputs without runtime dependencies."""
    assert list(TOOL_DECLARATIONS) == [
        "clock.now", "tickets.create", "secrets.request", "kb.source.get", "settings.connection.check",
        "kb.source.sync", "workflow.run.wait", "workflow.run.result", "http.request", "workflow.return",
        "feed.parse",
    ]
    assert TOOL_DECLARATIONS["clock.now"].input_json_schema() == {
        "additionalProperties": False,
        "properties": {},
        "type": "object",
    }
    assert TOOL_DECLARATIONS["tickets.create"].input_json_schema() == {
        "additionalProperties": False,
        "properties": {
            "body": {"type": "string"},
            "title": {"type": "string"},
        },
        "required": ["title", "body"],
        "type": "object",
    }


def test_builtin_tool_declarations_expose_catalog_metadata() -> None:
    """Catalog consumers receive the product metadata and safety flags."""
    clock = TOOL_DECLARATIONS["clock.now"]
    tickets = TOOL_DECLARATIONS["tickets.create"]

    assert (clock.title, clock.description, clock.side_effect, clock.external_idempotent) == (
        "Current time",
        "Returns current UTC ISO time",
        False,
        False,
    )
    assert (tickets.title, tickets.description, tickets.side_effect, tickets.external_idempotent) == (
        "Create ticket",
        "Create a new ticket",
        True,
        True,
    )


def test_tool_declaration_rejects_ids_outside_agent_spec_pattern() -> None:
    """A declaration cannot introduce an ID that AgentSpec would reject."""

    class EmptyInput(BaseModel):
        pass

    with pytest.raises(ValidationError, match="tool id must match"):
        ToolDeclaration(
            id="Bad Tool",
            title="Bad tool",
            description="Invalid declaration",
            side_effect=False,
            external_idempotent=False,
            input_model=EmptyInput,
        )
