"""Workflow schedules (parity round 2, D2): tenant-scoped CRUD over `workflow_schedules`.

A schedule fires the workflow's LATEST published version at fire time (the worker tick, a later
task). Create and update validate cron (5 fields), timezone (IANA) and the input against that
version's input_schema; `next_fire_at` is recomputed from now on create and on every PATCH, which
also clears `last_error` (a stopped schedule is resumed with `{"active": true}`).

Mounted BEFORE the workflows router so `/workflows/schedules` is not read as a workflow id.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from eap_api.auth import Principal, current_principal
from eap_api.concurrency import IfMatch, etag_responses, read_response, stamp
from eap_api.database import get_session
from eap_api.request_guard import GuardedJsonRoute
from eap_api.routes.runs import StoresDep
from eap_api.schedules import check_cron, get_zone, next_fire, validate_schedule_input
from eap_api.schemas import ErrorDetail
from eap_api.workflows.models import WorkflowModel, WorkflowVersionModel
from eap_run_store.definition_errors import WORKFLOW_ARCHIVED_CODE, WORKFLOW_ARCHIVED_NEW_WORK, DefinitionConflict
from eap_run_store import ScheduleNameConflict, ScheduleStore
from eap_run_store.cursor import MAX_CURSOR_LENGTH, InvalidRunCursor, RunCursor
from eap_schemas.workflows import WorkflowScheduleSpec, WorkflowScheduleUpdate, WorkflowSpec

router = APIRouter(
    prefix="/workflows", tags=["workflow schedules"], route_class=GuardedJsonRoute,
    responses={404: {"model": ErrorDetail}, 409: {"model": ErrorDetail}, 422: {"model": ErrorDetail}},
)
PrincipalDep = Annotated[Principal, Depends(current_principal)]
SessionDep = Annotated[Session, Depends(get_session)]


def get_schedule_store(request: Request, stores: StoresDep) -> ScheduleStore:
    store = getattr(request.app.state, "schedule_store", None)
    if store is None:
        run_store, _ = stores
        store = request.app.state.schedule_store = ScheduleStore(run_store.engine)
    return store


StoreDep = Annotated[ScheduleStore, Depends(get_schedule_store)]


def _utc(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _json(row: dict[str, Any], workflow_name: str | None) -> dict[str, Any]:
    return {
        "id": row["id"], "workflow_id": row["workflow_id"], "workflow_name": workflow_name, "name": row["name"],
        "cron": row["cron"], "timezone": row["timezone"], "input": row["input"], "active": row["active"],
        "last_error": row["last_error"], "last_fired_at": _utc(row["last_fired_at"]),
        "next_fire_at": _utc(row["next_fire_at"]), "created_at": _utc(row["created_at"]),
        "updated_at": _utc(row["updated_at"]), "revision": row["revision"],
    }


def _fail(code: int, error: str, **extra: Any) -> HTTPException:
    return HTTPException(status_code=code, detail={"code": error, **extra})


def _workflow_names(db: Session, tenant_id: str, ids: set[str]) -> dict[str, str]:
    if not ids:
        return {}
    rows = db.query(WorkflowModel.id, WorkflowModel.name).filter(
        WorkflowModel.tenant_id == tenant_id, WorkflowModel.id.in_(ids)).all()
    return {i: n for i, n in rows}


def _workflow_or_404(db: Session, tenant_id: str, workflow_id: str, *, allow_archived: bool = False) -> WorkflowModel:
    """The tenant's workflow; an archived one is 409 `workflow_archived` unless `allow_archived`
    (reading its schedules, pausing one)."""
    workflow = db.query(WorkflowModel).filter(
        WorkflowModel.id == workflow_id, WorkflowModel.tenant_id == tenant_id).first()
    if workflow is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workflow not found")
    if workflow.archived and not allow_archived:
        raise DefinitionConflict(WORKFLOW_ARCHIVED_NEW_WORK, code=WORKFLOW_ARCHIVED_CODE)
    return workflow


def _latest_spec(db: Session, workflow: WorkflowModel) -> WorkflowSpec:
    row = (db.query(WorkflowVersionModel)
           .filter(WorkflowVersionModel.workflow_id == workflow.id, WorkflowVersionModel.tenant_id == workflow.tenant_id)
           .order_by(WorkflowVersionModel.version.desc()).first())
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workflow has no published version")
    return WorkflowSpec.model_validate(row.spec)


def _check_cron_zone(cron: str, zone: str) -> None:
    if not check_cron(cron):
        raise _fail(422, "invalid_cron", message="cron must be 5 fields: minute hour day month weekday")
    if get_zone(zone) is None:
        raise _fail(422, "invalid_timezone", message="timezone must be an IANA name such as UTC or Asia/Taipei")


def _check_input(spec: WorkflowSpec, value: Any) -> None:
    problems = validate_schedule_input(spec, value)
    if problems:
        raise _fail(422, "input_schema_errors", errors=problems)


def _page(db: Session, store: ScheduleStore, tenant_id: str, workflow_id: str | None, limit: int,
          before: str | None) -> dict[str, Any]:
    cursor = None
    if before is not None:
        try:
            cursor = RunCursor.decode(before)
        except InvalidRunCursor:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                                "before is not a valid cursor; pass the previous page's next_before verbatim")
    items, next_before = store.list(tenant_id, workflow_id, limit, cursor)
    names = _workflow_names(db, tenant_id, {i["workflow_id"] for i in items})
    return {"items": [_json(i, names.get(i["workflow_id"])) for i in items], "next_before": next_before}


LimitQ = Annotated[int, Query(ge=1, le=200)]
BeforeQ = Annotated[str | None, Query(max_length=MAX_CURSOR_LENGTH)]


@router.get("/schedules")
def list_all_schedules(principal: PrincipalDep, db: SessionDep, store: StoreDep, limit: LimitQ = 50,
                       before: BeforeQ = None) -> dict[str, Any]:
    """Every schedule of the tenant, newest first; `next_before` pages (pass it back as `before`)."""
    return _page(db, store, principal.tenant_id, None, limit, before)


@router.get("/schedules/{schedule_id}", responses={404: {"model": ErrorDetail}, **etag_responses(write=False)})
def get_schedule(schedule_id: str, principal: PrincipalDep, db: SessionDep, store: StoreDep) -> JSONResponse:
    row = store.get(principal.tenant_id, schedule_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Schedule not found")
    names = _workflow_names(db, principal.tenant_id, {row["workflow_id"]})
    return read_response(_json(row, names.get(row["workflow_id"])), row["revision"])


@router.patch("/schedules/{schedule_id}", responses={**etag_responses()})
def update_schedule(schedule_id: str, body: WorkflowScheduleUpdate, principal: PrincipalDep, db: SessionDep,
                    store: StoreDep, pre: IfMatch) -> JSONResponse:
    """Change a schedule. Send the ETag you read as `If-Match`: a stale one is 412 `revision_mismatch`
    with the current schedule (see eap_api.concurrency)."""
    tenant = principal.tenant_id
    current = store.get(tenant, schedule_id)
    if current is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Schedule not found")

    def latest() -> tuple[dict[str, Any], int]:
        row = store.get(tenant, schedule_id) or current
        names = _workflow_names(db, tenant, {row["workflow_id"]})
        return _json(row, names.get(row["workflow_id"])), row["revision"]

    pre.check(current["revision"], latest)
    fields = body.model_dump(exclude_unset=True)
    pausing = fields.keys() == {"active"} and fields["active"] is False
    workflow = _workflow_or_404(db, tenant, current["workflow_id"], allow_archived=pausing)
    if fields.keys() == {"active"} and fields["active"] is False:
        # Pausing must always work: a stored cron or input that no longer validates (the workflow
        # changed) is not re-checked, and next_fire_at is left alone.
        fields["last_error"] = None
    else:
        cron = fields.get("cron", current["cron"])
        zone = fields.get("timezone", current["timezone"])
        _check_cron_zone(cron, zone)
        _check_input(_latest_spec(db, workflow), fields.get("input", current["input"]))
        fields["next_fire_at"] = next_fire(cron, zone, datetime.now(timezone.utc))
        fields["last_error"] = None
    try:
        with pre.guard(latest):
            row = store.update(tenant, schedule_id, expected_revision=pre.expected, **fields)
    except ScheduleNameConflict:
        raise _fail(409, "schedule_name_taken") from None
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Schedule not found")
    return pre.respond(_json(row, workflow.name), row["revision"])


@router.delete("/schedules/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_schedule(schedule_id: str, principal: PrincipalDep, store: StoreDep) -> Response:
    if not store.delete(principal.tenant_id, schedule_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Schedule not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{workflow_id}/schedules")
def list_workflow_schedules(workflow_id: uuid.UUID, principal: PrincipalDep, db: SessionDep, store: StoreDep,
                            limit: LimitQ = 50, before: BeforeQ = None) -> dict[str, Any]:
    _workflow_or_404(db, principal.tenant_id, str(workflow_id), allow_archived=True)
    return _page(db, store, principal.tenant_id, str(workflow_id), limit, before)


@router.post("/{workflow_id}/schedules", status_code=status.HTTP_201_CREATED, response_class=JSONResponse)
def create_schedule(workflow_id: uuid.UUID, body: WorkflowScheduleSpec, principal: PrincipalDep, db: SessionDep,
                    store: StoreDep) -> JSONResponse:
    tenant = principal.tenant_id
    workflow = _workflow_or_404(db, tenant, str(workflow_id))
    spec = _latest_spec(db, workflow)
    _check_cron_zone(body.cron, body.timezone)
    _check_input(spec, body.input)
    try:
        row = store.create(tenant, workflow.id, name=body.name, cron=body.cron, timezone=body.timezone,
                           input=body.input, next_fire_at=next_fire(body.cron, body.timezone, datetime.now(timezone.utc)))
    except ScheduleNameConflict:
        raise _fail(409, "schedule_name_taken") from None
    return stamp(JSONResponse(status_code=status.HTTP_201_CREATED, content=_json(row, workflow.name)), row["revision"])
