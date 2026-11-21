"""What a run executes, resolved independently of the transport (framework-free core of eap_api.run_options).

`resolve_target` and friends raise `ServiceError` subclasses; eap_api.run_options keeps the old function
names as wrappers that answer the same HTTP refusals.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import func
from sqlalchemy.orm import Session

from eap_api.models import AgentModel, AgentVersionModel
from eap_api.services.errors import Conflict, NotFound, PayloadTooLarge
from eap_run_store.agent_checks import check_publishable, validate_stored_draft
from eap_schemas import AgentSpec

DRAFT = "draft"
# The hand-off `version` argument of a draft test run (published versions start at 1).
DRAFT_HANDOFF_VERSION = 0


def version_row(db: Session, agent_id: uuid.UUID | str, version: int) -> AgentVersionModel | None:
    return (
        db.query(AgentVersionModel)
        .filter(AgentVersionModel.agent_id == str(agent_id), AgentVersionModel.version == version)
        .first()
    )


def resolve_version(db: Session, agent_id: uuid.UUID | str, requested: int | None) -> int:
    """The requested published version, or the latest one when `requested` is None (409 if none, 404 if unknown)."""
    if requested is None:
        latest = db.query(func.max(AgentVersionModel.version)).filter(AgentVersionModel.agent_id == str(agent_id)).scalar()
        if latest is None:
            raise Conflict("Agent has no published version")
        return latest
    if version_row(db, agent_id, requested) is None:
        raise NotFound("Version not found")
    return requested


@dataclass(frozen=True)
class RunTarget:
    """What a run executes: a published version (`version` set) or a draft snapshot (`draft_spec`).
    `kind` is "agent" (`spec` an AgentSpec) or "workflow" (`spec` a WorkflowSpec, never a draft,
    `workflow_version_id` the row the worker starts)."""

    version: int | None
    spec: Any
    spec_dict: dict[str, Any]
    draft_spec: dict[str, Any] | None
    kind: Literal["agent", "workflow"] = "agent"
    workflow_version_id: str | None = None

    @property
    def is_draft(self) -> bool:
        return self.draft_spec is not None

    @property
    def handoff_version(self) -> int:
        return DRAFT_HANDOFF_VERSION if self.version is None else self.version


def _published_target(db: Session, agent_id: uuid.UUID | str, version: int) -> RunTarget:
    row = version_row(db, agent_id, version)
    assert row is not None  # versions are immutable and never deleted
    return RunTarget(version=version, spec=AgentSpec.model_validate(row.spec), spec_dict=row.spec, draft_spec=None)


def _draft_target(db: Session, tenant_id: str, agent: AgentModel) -> RunTarget:
    """The agent's current draft, validated exactly as a publish would, as a snapshot."""
    if agent.draft_spec is None:
        raise Conflict("Agent has no draft")
    spec = validate_stored_draft(agent.draft_spec)
    # Like a publish, except that a deleted knowledge base is the retrieve tool's error at run time
    # (as for a published version), not a 422 here.
    check_publishable(db.connection(), tenant_id, spec, draft_run=True)
    snapshot = spec.model_dump(mode="json")
    return RunTarget(version=None, spec=spec, spec_dict=snapshot, draft_spec=snapshot)


def resolve_target(db: Session, tenant_id: str, agent: AgentModel, requested: int | str | None) -> RunTarget:
    """The target of a new run: the draft (validated like a publish) or a published version
    (latest when `requested` is None; 409 when there is none, 404 for an unknown number)."""
    if requested == DRAFT:
        return _draft_target(db, tenant_id, agent)
    assert requested is None or isinstance(requested, int)
    return _published_target(db, agent.id, resolve_version(db, agent.id, requested))


def resolve_workflow_target(db: Session, tenant_id: str, workflow_id: uuid.UUID | str, requested: int | None) -> RunTarget:
    """The published workflow version an eval run executes (latest when `requested` is None; 409
    when the workflow has none, 404 for an unknown number)."""
    from eap_api.workflows.models import WorkflowVersionModel
    from eap_schemas.workflows import WorkflowSpec

    query = db.query(WorkflowVersionModel).filter(
        WorkflowVersionModel.workflow_id == str(workflow_id), WorkflowVersionModel.tenant_id == tenant_id
    )
    if requested is None:
        row = query.order_by(WorkflowVersionModel.version.desc()).first()
        if row is None:
            raise Conflict("Workflow has no published version")
    else:
        row = query.filter(WorkflowVersionModel.version == requested).first()
        if row is None:
            raise NotFound("Version not found")
    return RunTarget(
        version=row.version,
        spec=WorkflowSpec.model_validate(row.spec),
        spec_dict=row.spec,
        draft_spec=None,
        kind="workflow",
        workflow_version_id=str(row.id),
    )


def stored_target(db: Session, run: dict[str, Any]) -> RunTarget:
    """The target an existing run was recorded with (its draft snapshot or its version)."""
    if run.get("draft_spec") is not None:
        snapshot = run["draft_spec"]
        return RunTarget(
            version=None, spec=AgentSpec.model_validate(snapshot), spec_dict=snapshot, draft_spec=snapshot
        )
    return _published_target(db, run["agent_id"], run["agent_version"])




def check_spec_payload_size(spec_dict: dict[str, Any], input_text: str) -> None:
    """413 when the hand-off payload (spec JSON plus input) exceeds EAP_MAX_RUN_PAYLOAD_BYTES."""
    payload_bytes = len(json.dumps(spec_dict).encode("utf-8")) + len(input_text.encode("utf-8"))
    max_payload_bytes = int(os.environ.get("EAP_MAX_RUN_PAYLOAD_BYTES", str(256 * 1024)))
    if payload_bytes > max_payload_bytes:
        raise PayloadTooLarge(f"Run payload exceeds the {max_payload_bytes}-byte enqueue limit")


