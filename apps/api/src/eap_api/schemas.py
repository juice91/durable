"""Pydantic schemas for API request/response bodies."""

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from eap_schemas import AgentSpec, ModelRef
from pydantic import BaseModel, ConfigDict, Field, field_validator


class AgentCreate(BaseModel):
    """Schema for creating a new agent."""

    model_config = ConfigDict(extra="forbid")

    spec: AgentSpec


class AgentUpdate(BaseModel):
    """Schema for updating an agent's draft spec."""

    model_config = ConfigDict(extra="forbid")

    spec: AgentSpec


class AgentResponse(BaseModel):
    """Schema for agent response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: str
    name: str
    draft_spec: dict | None
    archived: bool
    created_at: datetime
    updated_at: datetime
    latest_version: int | None = None


class AgentListResponse(BaseModel):
    """Schema for agent list response."""

    model_config = ConfigDict(from_attributes=True)

    items: list[AgentResponse]


class AgentVersionResponse(BaseModel):
    """Schema for agent version response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    agent_id: UUID
    tenant_id: str
    version: int
    spec: dict
    spec_hash: str
    created_at: datetime
    created_by: str
    restored_from: int | None = None


class VersionRestore(BaseModel):
    """Body of POST /agents/{id}/versions/{v}/restore (optional). `discard_draft` lets a restore
    replace a draft that has unpublished changes; without it such a restore is a 409."""

    model_config = ConfigDict(extra="forbid")

    discard_draft: bool = False


class AgentVersionListResponse(BaseModel):
    """Schema for agent version list response."""

    model_config = ConfigDict(from_attributes=True)

    items: list[AgentVersionResponse]


class HealthzResponse(BaseModel):
    """Schema for health check response."""

    model_config = ConfigDict(extra="forbid")

    status: str = "ok"


class ErrorDetail(BaseModel):
    """Schema for error detail."""

    model_config = ConfigDict(extra="forbid")

    detail: str


# A run's target: a published version number, "draft" (a draft test run of the agent's current
# draft, validated like a publish and snapshotted on the run), or null for the latest version.
RunVersion = Annotated[int, Field(ge=1)] | Literal["draft"] | None


class RunCreate(BaseModel):
    """Body of POST /agents/{agent_id}/runs. `version` null means the latest published version,
    "draft" a draft test run. `request_context` holds values for the target spec's
    `context_fields` (validated by the API; reserved identity names are refused). `model`
    overrides the spec's model for this run (allowlist and provider rules apply)."""

    model_config = ConfigDict(extra="forbid")

    run_id: UUID  # client-generated; retrying with the same run_id is idempotent
    input: str
    version: RunVersion = None
    request_context: dict[str, Any] | None = None
    model: ModelRef | None = None

    @field_validator("input")
    @classmethod
    def validate_input_size(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 32 * 1024:
            raise ValueError("input must be at most 32 KiB in UTF-8")
        return value


class ConversationCreate(BaseModel):
    """Body of POST /agents/{agent_id}/conversations. `conversation_id` makes a retry idempotent.
    `title` follows the rename rule (trimmed, 1–200 characters, one line); empty or blank is None."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=1000)
    conversation_id: UUID | None = None

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str | None) -> str | None:
        """Same rule as a rename; empty or blank means no title, so the first turn titles it."""
        if value is None or not value.strip():
            return None
        return conversation_title(value)


def conversation_title(value: str) -> str:
    """A conversation title as rename and create accept it: trimmed, 1–200 characters, one line."""
    title = value.strip()
    if not 1 <= len(title) <= 200:
        raise ValueError("title must be 1 to 200 characters")
    if any(not ch.isprintable() for ch in title):
        raise ValueError("title must be a single line without control characters")
    return title


class ConversationRename(BaseModel):
    """Body of PATCH /conversations/{id}: the new title, 1–200 characters after trimming surrounding
    whitespace, on one line (no newline, tab or other control character)."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(max_length=1000)

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        return conversation_title(value)


class TurnCreate(RunCreate):
    """Body of POST /conversations/{id}/runs: the next turn, same fields as a run start."""


class RequestContextValidate(BaseModel):
    """Body of POST /agents/{agent_id}/request-context/validate: the context to check against a
    published version's (`version`, null for the latest) or the draft's (`version: "draft"`)
    context fields. Errors come back exactly as on run start."""

    model_config = ConfigDict(extra="forbid")

    version: RunVersion = None
    request_context: dict[str, Any] | None = None
