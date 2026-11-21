"""Request bodies shared by the REST routes and the MCP control tools.

Pydantic models only: no web framework and no data access.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from eap_api.services.scorer_suggestions import DEFAULT_SUGGESTIONS, MAX_SUGGESTIONS
from eap_api.services.scorers import SCORER_DESCRIPTION_MAX_CHARS, SCORER_NAME_MAX_CHARS

MAX_COMMENT_CHARS = 2000  # a workflow approval or input step's comment
MAX_TOOL_COMMENT_CHARS = 500  # a tool approval's comment


class ScorerCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=SCORER_NAME_MAX_CHARS)
    description: str | None = Field(default=None, max_length=SCORER_DESCRIPTION_MAX_CHARS)
    evaluator: dict[str, Any]




class Attachment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sampling_rate: Decimal = Field(default=Decimal(1), gt=0, le=1, max_digits=5, decimal_places=4)




class SuggestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max: int = Field(default=DEFAULT_SUGGESTIONS, ge=1, le=MAX_SUGGESTIONS)
    include_runs: bool = True




class WorkflowRunCreate(BaseModel):
    """Body of POST /workflows/{workflow_id}/runs. `version` (optional) pins a published version,
    else the latest; `run_id` (optional, client-generated) makes a retry idempotent."""

    model_config = ConfigDict(extra="forbid")

    input: dict[str, Any]
    version: int | None = Field(default=None, ge=1)
    run_id: uuid.UUID | None = None
    rerun_of: uuid.UUID | None = None
    pause_after_each_step: bool = False


class ApprovalDecision(BaseModel):
    """Body of .../approve and .../reject (optional; an empty body means no comment)."""

    model_config = ConfigDict(extra="forbid")

    comment: str | None = Field(default=None, max_length=MAX_COMMENT_CHARS)




class ResumeSubmission(BaseModel):
    """Body of .../resume: the object the input step's `resume_schema` describes."""

    model_config = ConfigDict(extra="forbid")

    data: dict[str, Any]
    comment: str | None = Field(default=None, max_length=MAX_COMMENT_CHARS)




class ToolApprovalDecision(BaseModel):
    """Body of .../tool-approvals/{tool_call_id}/approve|deny (optional; empty means no comment)."""

    model_config = ConfigDict(extra="forbid")

    comment: str | None = Field(default=None, max_length=MAX_TOOL_COMMENT_CHARS)



