"""Workflow schedules (parity round 2, D2): tenant-scoped CRUD over `workflow_schedules`.

The business logic behind routes/schedules.py. A schedule fires the workflow's LATEST published version at fire
time (the worker tick). Create and update validate cron (5 fields), timezone (IANA) and the input against that
version's input_schema; `next_fire_at` is recomputed from now on create and on every update, which also clears
`last_error` (a stopped schedule is resumed with `{"active": true}`). Refusals are `DefinitionError`s whose body
is what the API has always answered (`{"detail": {"code", "message"}}` for the validation codes); a stale
revision is `Conflict(current=...)`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from eap_api.services import access as acl
from eap_api.services.access import Access, AccessBound
from eap_api.schedules import check_cron, get_zone, next_fire, validate_schedule_input
from eap_api.services.revision_conflict import stale_revision
from eap_api.workflows.models import WorkflowModel, WorkflowVersionModel
from eap_run_store import ScheduleNameConflict, ScheduleStore
from eap_run_store.cursor import MAX_CURSOR_LENGTH, InvalidRunCursor, RunCursor  # noqa: F401 - re-exported
from eap_run_store.definition_errors import (
    WORKFLOW_ARCHIVED_CODE,
    WORKFLOW_ARCHIVED_NEW_WORK,
    DefinitionConflict,
    refuse,
)
from eap_run_store.revisions import RevisionMismatch
from eap_schemas.workflows import WorkflowSpec

Versioned = tuple[dict[str, Any], int]


def _utc(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def schedule_json(row: dict[str, Any], workflow_name: str | None, last_run: dict[str, Any] | None = None) -> dict[str, Any]:
    last_run = last_run or {}
    return {
        "id": row["id"], "workflow_id": row["workflow_id"], "workflow_name": workflow_name, "name": row["name"],
        "cron": row["cron"], "timezone": row["timezone"], "input": row["input"], "active": row["active"],
        "last_error": row["last_error"], "last_fired_at": _utc(row["last_fired_at"]),
        "next_fire_at": _utc(row["next_fire_at"]), "created_at": _utc(row["created_at"]),
        "updated_at": _utc(row["updated_at"]), "revision": row["revision"],
        # The newest run a tick of this schedule created (null before the first): its status, and for a
        # finished run the `status` of its JSON output (`outside_slot`), which the schedule row shows.
        "last_run_id": last_run.get("run_id"), "last_run_status": last_run.get("status"),
        "last_run_detail": last_run.get("detail"),
    }


def _fail(code: int, error: str, **extra: Any) -> Any:
    return refuse(code, {"code": error, **extra})  # type: ignore[arg-type]


class ScheduleService(AccessBound):
    def __init__(self, session: Session, store: ScheduleStore, access: Access | None = None) -> None:
        self._db = session
        self._store = store
        self._access = access  # None: no caller to check (see eap_api.services.access)

    def _require(self, action: str, schedule_id: str | None) -> None:
        acl.require(self._access, action, "schedule", schedule_id)

    def _workflow_names(self, tenant_id: str, ids: set[str]) -> dict[str, str]:
        if not ids:
            return {}
        rows = self._db.query(WorkflowModel.id, WorkflowModel.name).filter(
            WorkflowModel.tenant_id == tenant_id, WorkflowModel.id.in_(ids)).all()
        return {i: n for i, n in rows}

    def _workflow(self, tenant_id: str, workflow_id: str, *, allow_archived: bool = False) -> WorkflowModel:
        """The tenant's workflow; an archived one is 409 `workflow_archived` unless `allow_archived`
        (reading its schedules, pausing one)."""
        workflow = self._db.query(WorkflowModel).filter(
            WorkflowModel.id == workflow_id, WorkflowModel.tenant_id == tenant_id).first()
        if workflow is None:
            raise refuse(404, "Workflow not found")
        if workflow.archived and not allow_archived:
            raise DefinitionConflict(WORKFLOW_ARCHIVED_NEW_WORK, code=WORKFLOW_ARCHIVED_CODE)
        return workflow

    def _latest_spec(self, workflow: WorkflowModel) -> WorkflowSpec:
        row = (self._db.query(WorkflowVersionModel)
               .filter(WorkflowVersionModel.workflow_id == workflow.id,
                       WorkflowVersionModel.tenant_id == workflow.tenant_id)
               .order_by(WorkflowVersionModel.version.desc()).first())
        if row is None:
            raise refuse(404, "Workflow has no published version")
        return WorkflowSpec.model_validate(row.spec)

    @staticmethod
    def _check_cron_zone(cron: str, zone: str) -> None:
        if not check_cron(cron):
            raise _fail(422, "invalid_cron", message="cron must be 5 fields: minute hour day month weekday")
        if get_zone(zone) is None:
            raise _fail(422, "invalid_timezone", message="timezone must be an IANA name such as UTC or Asia/Taipei")

    @staticmethod
    def _check_input(spec: WorkflowSpec, value: Any) -> None:
        problems = validate_schedule_input(spec, value)
        if problems:
            raise _fail(422, "schedule_input_invalid", message=" ".join(problems))

    # ------------------------------------------------------------------------------- reads

    def page(self, tenant_id: str, workflow_id: str | None, limit: int, before: str | None) -> dict[str, Any]:
        """Schedules newest first (of one workflow when given: it must exist, archived or not)."""
        if workflow_id is not None:
            acl.require(self._access, "read", "workflow", workflow_id)
            self._workflow(tenant_id, workflow_id, allow_archived=True)
        cursor = None
        if before is not None:
            try:
                cursor = RunCursor.decode(before)
            except InvalidRunCursor:
                raise refuse(422, "before is not a valid cursor; pass the previous page's next_before verbatim") from None
        items, next_before = self._store.list(
            tenant_id, workflow_id, limit, cursor, schedule_ids=acl.sql_ids(acl.visible(self._access, "schedule"))
        )
        names = self._workflow_names(tenant_id, {i["workflow_id"] for i in items})
        last = self._store.last_runs(tenant_id, [i["id"] for i in items])
        return {"items": [schedule_json(i, names.get(i["workflow_id"]), last.get(i["id"])) for i in items],
                "next_before": next_before}

    def get(self, tenant_id: str, schedule_id: str) -> Versioned:
        self._require("read", schedule_id)
        row = self._store.get(tenant_id, schedule_id)
        if row is None:
            raise refuse(404, "Schedule not found")
        names = self._workflow_names(tenant_id, {row["workflow_id"]})
        last = self._store.last_runs(tenant_id, [row["id"]]).get(row["id"])
        return schedule_json(row, names.get(row["workflow_id"]), last), row["revision"]

    # ------------------------------------------------------------------------------- writes

    def update(self, tenant_id: str, schedule_id: str, fields: dict[str, Any], expected_revision: int | None) -> Versioned:
        """Change a schedule (`fields`: only what the caller set). A stale `expected_revision` is refused."""
        self._require("edit", schedule_id)
        store = self._store
        current = store.get(tenant_id, schedule_id)
        if current is None:
            raise refuse(404, "Schedule not found")

        def latest() -> Versioned:
            row = store.get(tenant_id, schedule_id) or current
            names = self._workflow_names(tenant_id, {row["workflow_id"]})
            return schedule_json(row, names.get(row["workflow_id"])), row["revision"]

        if expected_revision is not None and current["revision"] != expected_revision:
            raise stale_revision(expected_revision, *latest())
        fields = dict(fields)
        pausing = fields.keys() == {"active"} and fields["active"] is False
        workflow = self._workflow(tenant_id, current["workflow_id"], allow_archived=pausing)
        if pausing:
            # Pausing must always work: a stored cron or input that no longer validates (the workflow
            # changed) is not re-checked, and next_fire_at is left alone.
            fields["last_error"] = None
        else:
            cron = fields.get("cron", current["cron"])
            zone = fields.get("timezone", current["timezone"])
            self._check_cron_zone(cron, zone)
            self._check_input(self._latest_spec(workflow), fields.get("input", current["input"]))
            fields["next_fire_at"] = next_fire(cron, zone, datetime.now(timezone.utc))
            fields["last_error"] = None
        try:
            row = store.update(tenant_id, schedule_id, expected_revision=expected_revision, **fields)
        except RevisionMismatch:
            raise stale_revision(expected_revision, *latest()) from None
        except ScheduleNameConflict:
            raise _fail(409, "schedule_name_taken") from None
        if row is None:
            raise refuse(404, "Schedule not found")
        return schedule_json(row, workflow.name), row["revision"]

    def delete(self, tenant_id: str, schedule_id: str) -> None:
        self._require("archive", schedule_id)
        if not self._store.delete(tenant_id, schedule_id):
            raise refuse(404, "Schedule not found")
        acl.unregister(self._access, "schedule", schedule_id)

    def create(
        self, tenant_id: str, workflow_id: str, *, name: str, cron: str, timezone_name: str, input: dict[str, Any]
    ) -> Versioned:
        # A schedule starts runs of its workflow: the creator needs the right to create schedules and to run it.
        acl.require(self._access, "run", "workflow", str(workflow_id))
        acl.require(self._access, "create", "schedule", None)
        workflow = self._workflow(tenant_id, workflow_id)
        spec = self._latest_spec(workflow)
        self._check_cron_zone(cron, timezone_name)
        self._check_input(spec, input)
        try:
            row = self._store.create(
                tenant_id, workflow.id, name=name, cron=cron, timezone=timezone_name, input=input,
                next_fire_at=next_fire(cron, timezone_name, datetime.now(timezone.utc)),
            )
        except ScheduleNameConflict:
            raise _fail(409, "schedule_name_taken") from None
        acl.register(self._access, "schedule", row["id"])  # before the response: the creator owns what it made
        return schedule_json(row, workflow.name), row["revision"]
