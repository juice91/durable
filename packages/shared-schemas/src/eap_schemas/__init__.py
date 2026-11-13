"""Shared Pydantic schemas for the Enterprise Agent Platform."""

from datetime import datetime, timezone
from typing import Any, Literal, Tuple
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_serializer
from pydantic.functional_validators import model_validator

from eap_schemas.mcp import McpToolRef, mcp_tool_name
from eap_schemas.model_ref import ModelRef
from eap_schemas.processors import (
    MAX_PROCESSORS_PER_PHASE,
    PROCESSOR_DECLARATIONS,
    ModerationVerdict,
    ProcessorDeclaration,
    ProcessorSpec,
)
from eap_schemas.request_context import MAX_CONTEXT_FIELDS, ContextField
from eap_schemas.tools import TOOL_DECLARATIONS, ToolDeclaration

__all__ = (
    "AgentSpec",
    "AgentVersion",
    "ContextField",
    "McpToolRef",
    "ModelRef",
    "ModelSettings",
    "ModerationVerdict",
    "PROCESSOR_DECLARATIONS",
    "ProcessorDeclaration",
    "ProcessorSpec",
    "RunEvent",
    "RunEventType",
    "TOOL_DECLARATIONS",
    "ToolDeclaration",
    "canonical_spec_hash",
    "mcp_tool_name",
)


class ModelSettings(BaseModel):
    """Settings for model inference."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, gt=0)


class AgentSpec(BaseModel):
    """Specification for an agent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    spec_version: Literal[1] = 1
    name: str
    instructions: str = ""
    model: ModelRef
    tools: Tuple[str, ...] = ()
    model_settings: ModelSettings = Field(default_factory=ModelSettings)
    # MCP tools of the tenant's registered servers. Left out of every dump while empty, so specs
    # stored before this field existed keep their exact JSON and canonical_spec_hash.
    mcp_tools: Tuple[McpToolRef, ...] = ()
    # Guardrails (eap_schemas.processors) and typed request context (eap_schemas.request_context),
    # milestone P0. Same rule as mcp_tools: left out of every dump while empty, so the JSON and
    # canonical_spec_hash of every spec stored before they existed stay exactly the same.
    input_processors: Tuple[ProcessorSpec, ...] = Field(default=(), max_length=MAX_PROCESSORS_PER_PHASE)
    output_processors: Tuple[ProcessorSpec, ...] = Field(default=(), max_length=MAX_PROCESSORS_PER_PHASE)
    context_fields: Tuple[ContextField, ...] = Field(default=(), max_length=MAX_CONTEXT_FIELDS)

    @model_serializer(mode="wrap")
    def _omit_empty_optional_fields(self, handler):
        data = handler(self)
        if isinstance(data, dict):
            for name in _OMIT_WHEN_EMPTY:
                if not getattr(self, name):
                    data.pop(name, None)
        return data

    @model_validator(mode="after")
    def check_context_fields(self):
        """Context field names are unique."""
        seen: set[str] = set()
        for field in self.context_fields:
            if field.name in seen:
                raise ValueError(f"duplicate context field: {field.name}")
            seen.add(field.name)
        return self

    @model_validator(mode="after")
    def check_tool_names(self):
        """MCP tool pairs are unique and no two tools share a model-facing name."""
        if not self.mcp_tools:
            return self
        names = {tool_id.replace(".", "__"): tool_id for tool_id in self.tools}
        seen: set[tuple[str, str]] = set()
        for ref in self.mcp_tools:
            if (ref.server, ref.tool) in seen:
                raise ValueError(f"duplicate MCP tool: {ref.server}/{ref.tool}")
            seen.add((ref.server, ref.tool))
            name = mcp_tool_name(ref.server, ref.tool)
            if name in names:
                raise ValueError(f"MCP tool {ref.server}/{ref.tool} has the same tool name as {names[name]}: {name}")
            names[name] = f"{ref.server}/{ref.tool}"
        return self

    @field_validator("name", mode="before")
    @classmethod
    def strip_name(cls, v):
        """Strip whitespace from name."""
        if isinstance(v, str):
            return v.strip()
        return v

    @field_validator("name")
    @classmethod
    def validate_name(cls, v):
        """Validate name after stripping."""
        if not v:
            raise ValueError("name cannot be empty")
        if len(v) > 100:
            raise ValueError("name must be at most 100 characters")
        return v

    @field_validator("instructions")
    @classmethod
    def validate_instructions(cls, v):
        """Keep workflow instructions within the durable payload budget."""
        if len(v.encode("utf-8")) > 32 * 1024:
            raise ValueError("instructions must be at most 32 KiB in UTF-8")
        return v

    @field_validator("tools", mode="before")
    @classmethod
    def validate_tools(cls, v):
        """Validate tool IDs and check for duplicates using exact regex."""
        import re

        if not v:
            return ()

        tool_ids = list(v)
        seen = set()
        pattern = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")

        for tool_id in tool_ids:
            if not tool_id:
                raise ValueError("tool id cannot be empty")
            if not pattern.fullmatch(tool_id):
                raise ValueError(f"tool id must match ^[a-z][a-z0-9_.-]{{0,63}}$ (ASCII only, max 64 chars): {tool_id}")
            if tool_id in seen:
                raise ValueError(f"duplicate tool id: {tool_id}")
            seen.add(tool_id)

        return tuple(tool_ids)


# AgentSpec fields added after specs were first stored: omitted from dumps (and so from the hash)
# while empty.
_OMIT_WHEN_EMPTY = ("mcp_tools", "input_processors", "output_processors", "context_fields")


def canonical_spec_hash(spec: AgentSpec) -> str:
    """Compute SHA256 hash of AgentSpec JSON with sorted keys.

    Equal specs give equal hashes; any field change changes the hash.
    """
    import hashlib
    import json

    json_str = json.dumps(
        spec.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(json_str.encode()).hexdigest()


class AgentVersion(BaseModel):
    """Versioned agent specification."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: UUID
    tenant_id: str
    version: int = Field(ge=1)
    spec: AgentSpec
    spec_hash: str
    created_at: AwareDatetime

    @field_validator("agent_id", mode="before")
    @classmethod
    def validate_agent_id(cls, v):
        """Convert to UUID if needed."""
        if isinstance(v, str):
            from uuid import UUID as UUIDType
            return UUIDType(v)
        return v

    @field_validator("tenant_id", mode="before")
    @classmethod
    def validate_tenant_id(cls, v):
        """Validate tenant_id is non-empty string."""
        if isinstance(v, str) and not v:
            raise ValueError("tenant_id cannot be empty")
        return v

    @field_validator("created_at", mode="before")
    @classmethod
    def validate_created_at_datetime(cls, v):
        """Ensure created_at is timezone-aware datetime."""
        if isinstance(v, str):
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                raise ValueError("created_at must be a timezone-aware datetime")
            return dt
        if hasattr(v, "tzinfo") and v.tzinfo is None:
            raise ValueError("created_at must be a timezone-aware datetime")
        if not hasattr(v, "tzinfo"):
            raise ValueError("created_at must be a datetime object")
        return v

    @model_validator(mode="after")
    def verify_spec_hash(self):
        """Verify spec_hash matches canonical hash of spec."""
        expected_hash = canonical_spec_hash(self.spec)
        if self.spec_hash != expected_hash:
            raise ValueError(
                f"spec_hash mismatch: expected {expected_hash}, got {self.spec_hash}"
            )
        return self


RunEventType = Literal[
    "run_started",
    "text_delta",  # transient only: sent over NOTIFY, never stored
    "model_message",
    "tool_call",
    "tool_result",
    "run_completed",
    "run_failed",
    # Agent runs whose spec has processors (P0.17): one per processor that ran, keyed
    # `processor:{phase}:{i}`; type, action, match counts and reason codes, never matched text.
    "processor_result",
    # Workflow runs (eap.workflow_run): one node's lifecycle, keyed `step:{step_id}:<phase>`.
    "step_started",
    "step_waiting",  # an approval step is waiting for a decision
    "step_approved",  # written by the API (POST .../approve), key `step:{step_id}:decision`
    "step_rejected",  # written by the API (POST .../reject), key `step:{step_id}:decision`
    "step_timed_out",  # written by the worker when the approval wait timed out, same key
    "step_completed",
    "step_failed",
]


class RunEvent(BaseModel):
    """Event in a run execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    seq: int = Field(ge=0)
    type: RunEventType
    data: dict[str, Any] = Field(default_factory=dict)
    ts: AwareDatetime

    @field_validator("run_id", mode="before")
    @classmethod
    def validate_run_id(cls, v):
        """Convert to UUID if needed."""
        if isinstance(v, str):
            from uuid import UUID as UUIDType
            return UUIDType(v)
        return v

    @field_validator("ts", mode="before")
    @classmethod
    def validate_ts_datetime(cls, v):
        """Ensure ts is timezone-aware datetime."""
        if isinstance(v, str):
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                raise ValueError("ts must be a timezone-aware datetime")
            return dt
        if hasattr(v, "tzinfo") and v.tzinfo is None:
            raise ValueError("ts must be a timezone-aware datetime")
        if not hasattr(v, "tzinfo"):
            raise ValueError("ts must be a datetime object")
        return v


# Import after definitions to avoid circular issues
from datetime import datetime, timezone
