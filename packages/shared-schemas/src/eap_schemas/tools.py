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
# names. `final_result` (agent capabilities milestone) is the output tool of a spec with
# structured output (eap_schemas.output); it is reserved the same way. `knowledge.search` is the
# hidden tool behind the knowledge base search tester (tool runs only; excluded from the agent tool
# picker).
RETRIEVE_TOOL_NAME = "retrieve"
UPDATE_WORKING_MEMORY_TOOL_NAME = "update_working_memory"
OUTPUT_TOOL_NAME = "final_result"
RESERVED_TOOL_NAMES: frozenset[str] = frozenset(
    {RETRIEVE_TOOL_NAME, UPDATE_WORKING_MEMORY_TOOL_NAME, OUTPUT_TOOL_NAME}
)
KNOWLEDGE_SEARCH_TOOL_ID = "knowledge.search"
MAX_SEARCH_QUERY_CHARS = 2000


class KnowledgeSearchInput(BaseModel):
    """Input of the hidden ``knowledge.search`` tool (the knowledge base search tester, RM.11).

    The tenant always comes from the run, never from this input. `filter` follows the spec filter
    rules (`eap_schemas.knowledge.validate_filter`)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    knowledge_base_id: str
    query: str = Field(min_length=1, max_length=MAX_SEARCH_QUERY_CHARS)
    top_k: int = Field(default=8, ge=1, le=20)
    min_score: float | None = Field(default=None, ge=0.0, le=1.0)
    filter: dict[str, Any] | None = None

    @field_validator("knowledge_base_id")
    @classmethod
    def _uuid(cls, value: str) -> str:
        import uuid

        try:
            return str(uuid.UUID(value))
        except ValueError:
            raise ValueError("knowledge_base_id must be a UUID") from None

    @field_validator("query")
    @classmethod
    def _query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query must not be blank")
        return value

    @field_validator("filter")
    @classmethod
    def _filter(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        from eap_schemas.knowledge import validate_filter

        checked = validate_filter(value)
        return None if checked is None else {k: list(v) if isinstance(v, tuple) else v for k, v in checked.items()}


# Hidden platform tools: registered in the worker for tool runs only, never in TOOL_DECLARATIONS
# (so never in the tool catalog, the agent tool picker or a workflow tool step), and refused in an
# agent spec's `tools` at publish and at run time.
KNOWLEDGE_SEARCH_DECLARATION = ToolDeclaration(
    id=KNOWLEDGE_SEARCH_TOOL_ID,
    title="Knowledge base search",
    description="Search one knowledge base of the tenant (the knowledge base search tester)",
    side_effect=False,
    external_idempotent=False,
    input_model=KnowledgeSearchInput,
)
HIDDEN_TOOL_DECLARATIONS: dict[str, ToolDeclaration] = {KNOWLEDGE_SEARCH_TOOL_ID: KNOWLEDGE_SEARCH_DECLARATION}


def builtin_tool_name(tool_id: str) -> str:
    """The model-facing name of a built-in tool id (dots become `__`)."""
    return tool_id.replace(".", "__")
