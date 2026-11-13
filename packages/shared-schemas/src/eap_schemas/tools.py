"""Shared, execution-free declarations for platform tools."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

_TOOL_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


def _without_titles(value: Any, *, properties_map: bool = False) -> Any:
    """Remove Pydantic's presentation-only titles from a JSON Schema tree."""
    if isinstance(value, dict):
        return {
            key: _without_titles(item, properties_map=key == "properties")
            for key, item in value.items()
            if properties_map or key != "title"
        }
    if isinstance(value, list):
        return [_without_titles(item) for item in value]
    return value


class ToolDeclaration(BaseModel):
    """Portable tool metadata and its validated input contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    side_effect: bool
    external_idempotent: bool
    input_model: type[BaseModel]

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        """Use the same provider-independent ID contract as AgentSpec."""
        if not _TOOL_ID_PATTERN.fullmatch(value):
            raise ValueError("tool id must match ^[a-z][a-z0-9_.-]{0,63}$")
        return value

    def input_json_schema(self) -> dict[str, Any]:
        """Return the tool input as provider-neutral JSON Schema."""
        schema = _without_titles(self.input_model.model_json_schema())
        schema.pop("description", None)
        return schema


class ClockNowInput(BaseModel):
    """Input for ``clock.now``."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class TicketsCreateInput(BaseModel):
    """Input for ``tickets.create``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str
    body: str


TOOL_DECLARATIONS: dict[str, ToolDeclaration] = {
    "clock.now": ToolDeclaration(
        id="clock.now",
        title="Current time",
        description="Returns current UTC ISO time",
        side_effect=False,
        external_idempotent=False,
        input_model=ClockNowInput,
    ),
    "tickets.create": ToolDeclaration(
        id="tickets.create",
        title="Create ticket",
        description="Create a new ticket",
        side_effect=True,
        external_idempotent=True,
        input_model=TicketsCreateInput,
    ),
}


# Platform tools (RAG and memory milestone). They are added to a run by the platform, never listed
# in `AgentSpec.tools`: `retrieve` when the spec attaches knowledge bases, `update_working_memory`
# when working memory is set and not read-only. No built-in or MCP tool may take these model-facing
# names. `knowledge.search` is the hidden tool behind the knowledge base search tester (tool runs
# only; excluded from the agent tool picker).
RETRIEVE_TOOL_NAME = "retrieve"
UPDATE_WORKING_MEMORY_TOOL_NAME = "update_working_memory"
RESERVED_TOOL_NAMES: frozenset[str] = frozenset({RETRIEVE_TOOL_NAME, UPDATE_WORKING_MEMORY_TOOL_NAME})
KNOWLEDGE_SEARCH_TOOL_ID = "knowledge.search"


def builtin_tool_name(tool_id: str) -> str:
    """The model-facing name of a built-in tool id (dots become `__`)."""
    return tool_id.replace(".", "__")
