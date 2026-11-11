"""Pydantic schemas for API request/response bodies."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from eap_schemas import AgentSpec
from pydantic import BaseModel, ConfigDict, Field


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


class RunCreate(BaseModel):
    """Body of POST /agents/{agent_id}/runs. `version` null means the latest published version."""

    model_config = ConfigDict(extra="forbid")

    run_id: UUID  # client-generated; retrying with the same run_id is idempotent
    input: str
    version: int | None = Field(default=None, ge=1)
