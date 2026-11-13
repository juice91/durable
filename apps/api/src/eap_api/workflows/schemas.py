"""Request bodies for /api/v1/workflows."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from eap_schemas.workflows import WorkflowSpec


class WorkflowCreate(BaseModel):
    """Body of POST /workflows: the initial draft."""

    model_config = ConfigDict(extra="forbid")

    spec: WorkflowSpec


class WorkflowUpdate(BaseModel):
    """Body of PATCH /workflows/{id}: replaces the draft."""

    model_config = ConfigDict(extra="forbid")

    spec: WorkflowSpec
