"""Workflow schedules (parity round 2, D2): tenant-scoped CRUD over `workflow_schedules`.

A schedule fires the workflow's LATEST published version at fire time (the worker tick, a later
task). Create and update validate cron (5 fields), timezone (IANA) and the input against that
version's input_schema; `next_fire_at` is recomputed from now on create and on every PATCH, which
also clears `last_error` (a stopped schedule is resumed with `{"active": true}`).

Mounted BEFORE the workflows router so `/workflows/schedules` is not read as a workflow id.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response, status
from fastapi.responses import JSONResponse

from eap_api.auth import Principal, current_principal
from eap_api.concurrency import IfMatch, etag_responses, read_response, stamp
from eap_api.request_guard import GuardedJsonRoute
from eap_api.schemas import ErrorDetail
from eap_api.service_deps.schedules import ScheduleServiceDep
from eap_api.services.schedules import MAX_CURSOR_LENGTH
from eap_schemas.workflows import WorkflowScheduleSpec, WorkflowScheduleUpdate

router = APIRouter(
    prefix="/workflows", tags=["workflow schedules"], route_class=GuardedJsonRoute,
    responses={404: {"model": ErrorDetail}, 409: {"model": ErrorDetail}, 422: {"model": ErrorDetail}},
)
PrincipalDep = Annotated[Principal, Depends(current_principal)]

LimitQ = Annotated[int, Query(ge=1, le=200)]
BeforeQ = Annotated[str | None, Query(max_length=MAX_CURSOR_LENGTH)]


@router.get("/schedules")
def list_all_schedules(principal: PrincipalDep, service: ScheduleServiceDep, limit: LimitQ = 50,
                       before: BeforeQ = None) -> dict[str, Any]:
    """Every schedule of the tenant, newest first; `next_before` pages (pass it back as `before`)."""
    return service.page(principal.tenant_id, None, limit, before)


@router.get("/schedules/{schedule_id}", responses={404: {"model": ErrorDetail}, **etag_responses(write=False)})
def get_schedule(schedule_id: str, principal: PrincipalDep, service: ScheduleServiceDep) -> JSONResponse:
    body, revision = service.get(principal.tenant_id, schedule_id)
    return read_response(body, revision)


@router.patch("/schedules/{schedule_id}", responses={**etag_responses()})
def update_schedule(schedule_id: str, body: WorkflowScheduleUpdate, principal: PrincipalDep,
                    service: ScheduleServiceDep, pre: IfMatch) -> JSONResponse:
    """Change a schedule. Send the ETag you read as `If-Match`: a stale one is 412 `revision_mismatch`
    with the current schedule (see eap_api.concurrency)."""
    result, revision = service.update(
        principal.tenant_id, schedule_id, body.model_dump(exclude_unset=True), pre.expected)
    return pre.respond(result, revision)


@router.delete("/schedules/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_schedule(schedule_id: str, principal: PrincipalDep, service: ScheduleServiceDep) -> Response:
    service.delete(principal.tenant_id, schedule_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{workflow_id}/schedules")
def list_workflow_schedules(workflow_id: uuid.UUID, principal: PrincipalDep, service: ScheduleServiceDep,
                            limit: LimitQ = 50, before: BeforeQ = None) -> dict[str, Any]:
    return service.page(principal.tenant_id, str(workflow_id), limit, before)


@router.post("/{workflow_id}/schedules", status_code=status.HTTP_201_CREATED, response_class=JSONResponse)
def create_schedule(workflow_id: uuid.UUID, body: WorkflowScheduleSpec, principal: PrincipalDep,
                    service: ScheduleServiceDep) -> JSONResponse:
    result, revision = service.create(
        principal.tenant_id, str(workflow_id), name=body.name, cron=body.cron, timezone_name=body.timezone,
        input=body.input)
    return stamp(JSONResponse(status_code=status.HTTP_201_CREATED, content=result), revision)
