"""Shared, execution-free declarations for platform tools."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any, Literal

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


class SecretsRequestInput(BaseModel):
    """Input for ``secrets.request``: the secret's name (the Secrets store's name pattern) and why."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    purpose: str


class KbSourceGetInput(BaseModel):
    """Input for ``kb.source.get``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str


class SettingsConnectionCheckInput(BaseModel):
    """Input for ``settings.connection.check``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    connection_id: str


class KbSourceSyncInput(BaseModel):
    """Input for ``kb.source.sync``. `part_numbers` only in a targeted sync (at most 100, each 1..128 characters of
    ``[A-Za-z0-9._/-]``; the launch checks them and answers a stable code)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str
    mode: Literal["targeted", "incremental", "full"]
    part_numbers: list[str] = []


class WorkflowRunWaitInput(BaseModel):
    """Input for ``workflow.run.wait``: wait up to `wait_seconds` (1..300) for a run to end."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    wait_seconds: int = Field(default=30, ge=1, le=300)


class WorkflowRunResultInput(BaseModel):
    """Input for ``workflow.run.result``: the bounded report of a sync run; with `require_success` a failed,
    cancelled or attention-needing run is a stable error."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    require_success: bool = False


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
    "secrets.request": ToolDeclaration(
        id="secrets.request",
        title="Request a secret",
        description=(
            "Ask the person for a credential. They type it into a masked card; you receive only a "
            "reference (secret:<name>) to pass as an argument, never the value"
        ),
        side_effect=True,
        external_idempotent=False,
        input_model=SecretsRequestInput,
    ),
    "kb.source.get": ToolDeclaration(
        id="kb.source.get",
        title="Get a knowledge-base source",
        description=(
            "Read one knowledge-base source of the tenant: its knowledge base, connection, kind, provider, "
            "name, enabled flag, non-secret config, last successful sync and a summary of its checkpoint"
        ),
        side_effect=False,
        external_idempotent=False,
        input_model=KbSourceGetInput,
    ),
    "settings.connection.check": ToolDeclaration(
        id="settings.connection.check",
        title="Check a connection",
        description=(
            "Check that a tenant connection works (its stored credentials are accepted by the provider). "
            "Returns ok, the time of the check and a stable error code; never a credential"
        ),
        side_effect=False,
        external_idempotent=False,
        input_model=SettingsConnectionCheckInput,
    ),
    "kb.source.sync": ToolDeclaration(
        id="kb.source.sync",
        title="Start a knowledge-base source sync",
        description=(
            "Start a targeted (with part_numbers), incremental or full synchronisation of a knowledge-base "
            "source. Returns the run id of the sync; use workflow.run.wait and workflow.run.result to follow it"
        ),
        side_effect=True,
        external_idempotent=True,
        input_model=KbSourceSyncInput,
    ),
    "workflow.run.wait": ToolDeclaration(
        id="workflow.run.wait",
        title="Wait for a run",
        description=(
            "Wait up to wait_seconds (1 to 300) for a run of the tenant to end. Returns its status, when it "
            "last changed and whether it has ended"
        ),
        side_effect=False,
        external_idempotent=False,
        input_model=WorkflowRunWaitInput,
    ),
    "workflow.run.result": ToolDeclaration(
        id="workflow.run.result",
        title="Get a sync run's report",
        description=(
            "The bounded report of a knowledge-base source sync run: counts, checkpoint and up to 50 errors. "
            "With require_success a failed, cancelled or attention-needing run is returned as an error"
        ),
        side_effect=False,
        external_idempotent=False,
        input_model=WorkflowRunResultInput,
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


# `secrets.request` (secret input milestone): asks the person for a credential through the approval card.
SECRETS_REQUEST_TOOL_ID = "secrets.request"

# The Architect's tools that change what is live or what runs by itself. A built-in agent that lists
# one must list it in `approvals.tools` too (defined here so the shared set below needs no import of
# `eap_schemas.architect`, which imports this package).
ARCHITECT_GATED_TOOL_IDS: tuple[str, ...] = (
    "architect.publish_agent",
    "architect.restore_agent_version",
    "architect.archive_agent",
    "architect.publish_workflow",
    "architect.restore_workflow_version",
    "architect.archive_workflow",
    "architect.set_workflow_schedule",
)

# Tools that never run without a person's recorded approval: the API refuses a spec that lists one
# without an `approvals.tools` entry (422 approval_required) and the worker refuses the run.
ALWAYS_GATED_TOOL_IDS: frozenset[str] = frozenset({SECRETS_REQUEST_TOOL_ID}) | frozenset(ARCHITECT_GATED_TOOL_IDS)


def ungated_always_gated_tools(tools: Iterable[str], approvals_tools: Iterable[str] | None) -> list[str]:
    """The always-gated tools in `tools` that `approvals_tools` (spec.approvals.tools) does not list."""
    gated = set(approvals_tools or ())
    return [t for t in tools if t in ALWAYS_GATED_TOOL_IDS and t not in gated]


def builtin_tool_name(tool_id: str) -> str:
    """The model-facing name of a built-in tool id (dots become `__`)."""
    return tool_id.replace(".", "__")
