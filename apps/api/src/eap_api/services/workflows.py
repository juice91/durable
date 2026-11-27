"""Workflow definitions: drafts, immutable published versions, archive, output-schema lookups.

The business logic behind routes/workflows.py. The service owns one database session (constructor-injected)
and answers with plain dicts. A write that is guarded by a revision takes the revision the caller read
(`expected_revision`, None for no comparison) and refuses a stale one with `Conflict(current=...)`.
Refusals of the shared definition operations stay `DefinitionError` (eap_run_store), whose body is what the
API has always answered.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from eap_api.models import AgentModel
from eap_api.services import access as acl
from eap_api.services.access import Access, AccessBound
from eap_api.services.agent_grants import AgentGrantService, PublishGrants
from eap_api.services.errors import Conflict
from eap_api.workflows.models import WorkflowModel, WorkflowVersionModel
from eap_run_store import AuditStore
from eap_run_store.definition_errors import refuse
from eap_run_store.definitions import (
    publish_workflow,
    restore_workflow_version,
    set_workflow_archived,
)
from eap_run_store.revisions import RevisionMismatch, bump_row
from eap_run_store.workflow_checks import (  # noqa: F401 - re-exported: the API's users and tests import them from here
    CAP_BOUNDS,
    LOOP_ITERATIONS_ENV,
    RUN_CONCURRENCY_ENV,
    STEP_EXECUTIONS_ENV,
    WARNING_LOOP_AGENT_APPROVALS,
    WARNING_TEXT_OUTPUT_REF,
    WARNING_UNLISTED_OUTPUT_FIELD,
    WARNING_UNPINNED_OUTPUT_REF,
    RefProblem,
    _agent_version,
    _env_cap,
    agent_step_cost_cap_warnings,
    check_workflow_configuration,
    judge_cost_cap_warnings,
    judge_model_problems,
    loop_agent_approval_warnings,
    loop_limit_problems,
    output_fields,
    output_ref_checks,
    output_ref_problem,
    output_schema_of,
)
from eap_run_store.workflow_checks import reference_problems as _reference_problems
from eap_schemas.workflows import WorkflowSpec

Versioned = tuple[dict[str, Any], int]  # (the resource as a read returns it, its revision)


def workflow_json(workflow: WorkflowModel, latest_version: int | None) -> dict[str, Any]:
    return {
        "id": str(workflow.id),
        "name": workflow.name,
        "draft_spec": workflow.draft_spec,
        "latest_version": latest_version,
        "archived": bool(workflow.archived),
        "archived_at": workflow.archived_at.isoformat() if workflow.archived_at else None,
        "revision": workflow.revision,
    }


def _version_json(version: dict[str, Any]) -> dict[str, Any]:
    """A version row as the shared definition operations return it."""
    return {
        "workflow_id": str(version["workflow_id"]),
        "version": version["version"],
        "spec": version["spec"],
        "spec_hash": version["spec_hash"],
        "created_at": version["created_at"].isoformat(),
        "created_by": version["created_by"],
        "published_by": version.get("published_by"),
        "created_via": version["created_via"],
    }


def _version_row_json(version: WorkflowVersionModel) -> dict[str, Any]:
    return {
        "workflow_id": str(version.workflow_id),
        "version": version.version,
        "spec": version.spec,
        "spec_hash": version.spec_hash,
        "created_at": version.created_at.isoformat(),
        "created_by": version.created_by,
        "published_by": version.published_by,
        "created_via": version.created_via,
    }


def stale_revision(expected: int | None, current: Callable[[], Versioned]) -> Conflict:
    """The refusal of a write whose `If-Match` revision is not the row's current one."""
    body, revision = current()
    return Conflict(
        f"this resource changed since you read it (revision {revision}, you sent {expected}); "
        "re-read it or send the current ETag to overwrite",
        code="revision_mismatch",
        current=body,
        current_revision=revision,
    )


class WorkflowService(AccessBound):
    def __init__(self, session: Session, access: Access | None = None) -> None:
        self._db = session
        self._access = access  # None: no caller to check (see eap_api.services.access)

    def _require(self, action: str, workflow_id: str | None) -> None:
        acl.require(self._access, action, "workflow", None if workflow_id is None else str(workflow_id))

    # ----------------------------------------------------------------------------- helpers

    def _get(self, tenant_id: str, workflow_id: str) -> WorkflowModel:
        workflow = (
            self._db.query(WorkflowModel)
            .filter(WorkflowModel.id == str(workflow_id), WorkflowModel.tenant_id == tenant_id)
            .first()
        )
        if workflow is None:
            raise refuse(404, "Workflow not found")
        return workflow

    def _latest_version(self, workflow: WorkflowModel) -> int | None:
        return (
            self._db.query(func.max(WorkflowVersionModel.version))
            .filter(
                WorkflowVersionModel.workflow_id == workflow.id,
                WorkflowVersionModel.tenant_id == workflow.tenant_id,
            )
            .scalar()
        )

    def _run_op(self, op: Callable[[Any], Any]) -> Any:
        """One shared definition operation in this transaction: commit on success, roll back on a refusal."""
        try:
            result = op(self._db.connection())
        except BaseException:
            self._db.rollback()
            raise
        self._db.commit()
        return result

    def reference_problems(self, spec: WorkflowSpec, tenant_id: str, *, lock: bool = False) -> list[str]:
        """What a spec points at that this tenant cannot run (see eap_run_store.workflow_checks)."""
        return _reference_problems(self._db.connection(), spec, tenant_id, lock=lock)

    # ------------------------------------------------------------------------------- drafts

    def create(self, tenant_id: str, spec: WorkflowSpec) -> Versioned:
        self._require("create", None)
        workflow = WorkflowModel(tenant_id=tenant_id, name=spec.name, draft_spec=spec.model_dump(mode="json"))
        self._db.add(workflow)
        self._db.commit()
        self._db.refresh(workflow)
        acl.register(self._access, "workflow", str(workflow.id))  # before the response: the creator owns what it made
        return workflow_json(workflow, None), workflow.revision

    def list(self, tenant_id: str, archived: str = "false") -> list[dict[str, Any]]:
        """Oldest first: the active ones, or `archived` "true" the archived only, "all" both."""
        query = self._db.query(WorkflowModel).filter(WorkflowModel.tenant_id == tenant_id)
        if archived != "all":
            query = query.filter(WorkflowModel.archived.is_(archived == "true"))
        workflows = query.order_by(WorkflowModel.created_at, WorkflowModel.id).all()
        shown = set(acl.keep(acl.visible(self._access, "workflow"), [str(w.id) for w in workflows]))  # one engine call
        workflows = [w for w in workflows if str(w.id) in shown]
        return [workflow_json(w, self._latest_version(w)) for w in workflows]

    def get(self, tenant_id: str, workflow_id: str) -> Versioned:
        self._require("read", workflow_id)
        workflow = self._get(tenant_id, workflow_id)
        return workflow_json(workflow, self._latest_version(workflow)), workflow.revision

    def replace_draft(
        self, tenant_id: str, workflow_id: str, spec: WorkflowSpec, expected_revision: int | None
    ) -> Versioned:
        self._require("edit", workflow_id)
        workflow = self._get(tenant_id, workflow_id)

        def current() -> Versioned:
            self._db.refresh(workflow)
            return workflow_json(workflow, self._latest_version(workflow)), workflow.revision

        if expected_revision is not None and workflow.revision != expected_revision:
            raise stale_revision(expected_revision, current)
        try:
            bump_row(self._db, workflow, expected_revision)
            workflow.name = spec.name
            workflow.draft_spec = spec.model_dump(mode="json")
            self._db.commit()
        except RevisionMismatch:
            self._db.rollback()
            raise stale_revision(expected_revision, current) from None
        self._db.refresh(workflow)
        return workflow_json(workflow, self._latest_version(workflow)), workflow.revision

    # ----------------------------------------------------------------------------- versions

    def _grants(self) -> AgentGrantService:
        return AgentGrantService(self._db, self._access, AuditStore(self._db.get_bind()) if self._access is not None else None)

    def publish_grants(self, tenant_id: str, workflow_id: str) -> dict[str, Any]:
        """What publishing the draft would newly grant (the publish card's line), and whether the caller may approve it."""
        self._require("publish", workflow_id)
        plan = self._grants().plan("workflow", str(workflow_id), self._get(tenant_id, workflow_id).draft_spec)
        return (plan or PublishGrants("workflow", str(workflow_id))).to_json()

    def access_grants(self) -> AgentGrantService:
        return self._grants()

    def access_status(self, tenant_id: str, workflow_id: str) -> dict[str, Any]:
        """The Access section: what this workflow is authorised to use (docs/iam.md, "Agent and workflow grants")."""
        self._require("read", workflow_id)
        workflow = self._get(tenant_id, workflow_id)
        latest = (
            self._db.query(WorkflowVersionModel)
            .filter(WorkflowVersionModel.workflow_id == workflow.id, WorkflowVersionModel.tenant_id == tenant_id)
            .order_by(WorkflowVersionModel.version.desc()).first()
        )
        return self._grants().status("workflow", str(workflow_id), None if latest is None else latest.spec)

    def publish(self, tenant_id: str, user_id: str, workflow_id: str) -> Versioned:
        self._require("publish", workflow_id)
        grants = self._grants()
        plan = grants.plan("workflow", str(workflow_id), self._get(tenant_id, workflow_id).draft_spec)
        grants.require_approvable(plan)  # newly used secrets: an admin approves, and approving grants them
        result = self._run_op(lambda conn: publish_workflow(conn, tenant_id, str(workflow_id), created_by=user_id))
        grants.apply(plan, f"published version {result.version['version']}")
        return {**_version_json(result.version), "warnings": result.warnings}, result.revision

    def restore(
        self, tenant_id: str, user_id: str, workflow_id: str, version_num: int, *, discard_draft: bool = False
    ) -> Versioned:
        self._require("publish", workflow_id)  # a restore publishes a new version
        grants = self._grants()
        old = (
            self._db.query(WorkflowVersionModel)
            .filter(WorkflowVersionModel.workflow_id == str(workflow_id), WorkflowVersionModel.tenant_id == tenant_id,
                    WorkflowVersionModel.version == version_num).first()
        )
        plan = grants.plan("workflow", str(workflow_id), None if old is None else old.spec)
        grants.require_approvable(plan)
        result = self._run_op(
            lambda conn: restore_workflow_version(
                conn, tenant_id, str(workflow_id), version_num, created_by=user_id, discard_draft=discard_draft
            )
        )
        grants.apply(plan, f"restored version {version_num} as version {result.version['version']}")
        return (
            {**_version_json(result.version), "restored_from": version_num, "warnings": result.warnings},
            result.revision,
        )

    def archive(self, tenant_id: str, workflow_id: str, archived: bool, force: bool = False) -> Versioned:
        self._require("archive", workflow_id)
        result = self._run_op(
            lambda conn: set_workflow_archived(conn, tenant_id, str(workflow_id), archived, force=force)
        )
        workflow = self._get(tenant_id, workflow_id)
        self._db.refresh(workflow)
        body = {**workflow_json(workflow, self._latest_version(workflow)), "paused_schedules": list(result.paused_schedules)}
        return body, workflow.revision

    def list_versions(self, tenant_id: str, workflow_id: str) -> list[dict[str, Any]]:
        """Published versions, newest first."""
        self._require("read", workflow_id)
        workflow = self._get(tenant_id, workflow_id)
        versions = (
            self._db.query(WorkflowVersionModel)
            .filter(WorkflowVersionModel.workflow_id == workflow.id, WorkflowVersionModel.tenant_id == tenant_id)
            .order_by(WorkflowVersionModel.version.desc())
            .all()
        )
        return [_version_row_json(v) for v in versions]

    def get_version(self, tenant_id: str, workflow_id: str, version_num: int) -> dict[str, Any]:
        self._require("read", workflow_id)
        workflow = self._get(tenant_id, workflow_id)
        version = (
            self._db.query(WorkflowVersionModel)
            .filter(
                WorkflowVersionModel.workflow_id == workflow.id,
                WorkflowVersionModel.tenant_id == tenant_id,
                WorkflowVersionModel.version == version_num,
            )
            .first()
        )
        if version is None:
            raise refuse(404, "Version not found")
        return _version_row_json(version)

    def agent_output_schema(self, tenant_id: str, agent_id: str, version: int | None) -> dict[str, Any]:
        """The output schema an agent step would read: the pinned `version`, else the latest published one."""
        acl.require(self._access, "read", "agent", str(agent_id))
        agent = (
            self._db.query(AgentModel)
            .filter(AgentModel.id == str(agent_id), AgentModel.tenant_id == tenant_id)
            .first()
        )
        if agent is None or agent.archived:
            raise refuse(404, "Agent not found")
        row = _agent_version(self._db.connection(), tenant_id, str(agent_id), version)
        if row is None:
            raise refuse(404, "Version not found")
        schema = output_schema_of(row.spec)
        return {
            "agent_id": str(agent_id),
            "version": row.version,
            "pinned": version is not None,
            "structured": schema is not None,
            "json_schema": schema,
            "fields": output_fields(schema) if schema is not None else [],
        }
