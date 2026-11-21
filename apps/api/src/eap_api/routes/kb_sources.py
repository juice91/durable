"""Knowledge-base sources and their syncs (kb source sync, Task 4). Dev principals only.

- POST /knowledge-bases/{kb}/sources                   201 the source; 404 kb or connection; 409 name;
                                                       422 `source_adapter_not_found` | `invalid_source_config`
- GET  /knowledge-bases/{kb}/sources                   {items: [source]}
- GET  /knowledge-bases/{kb}/sources/{id}              the source (config is non-secret; the checkpoint as a summary)
- PATCH /knowledge-bases/{kb}/sources/{id}             any of name, config, enabled
- POST /knowledge-bases/{kb}/sources/{id}/sync         202 {run_id, status: "queued"} through the shared launch
                                                       (the shared sync launch); body {mode, part_numbers?, restart?, run_id?}
- GET  /knowledge-bases/{kb}/sources/{id}/syncs        {items: [sync]}, newest first (`limit` 1-100, default 20)
- GET  /knowledge-bases/{kb}/sources/{id}/syncs/{run}  the sync plus `report` (the run's bounded report once it ends)
                                                       and `run_status`

A source row, run or sync of another tenant answers 404, never 403. Cancelling a sync is
`POST /runs/{run_id}/cancel`.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Query, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from eap_api.auth import Principal, current_principal
from eap_api.concurrency import IfMatch, etag_responses, read_response, stamp
from eap_api.request_guard import guarded_json_route
from eap_api.service_deps.knowledge import KbSourceServiceDep
from eap_api.services.kb_sources import (  # noqa: F401  (re-exported: tests and the MCP tools use these names)
    SourceCreate,
    SourcePatch,
    SyncCreate,
    adapter_view,
    load_workflow_template,
    parse_payload,
)

MAX_BODY_BYTES = 64 * 1024
MAX_BODY_DEPTH = 6
MAX_SYNC_LIST = 100

router = APIRouter(
    tags=["kb-sources"],
    route_class=guarded_json_route(max_body_bytes=MAX_BODY_BYTES, max_depth=MAX_BODY_DEPTH),
)
PrincipalDep = Annotated[Principal, Depends(current_principal)]
BASE = "/knowledge-bases/{kb_id}/sources"


@router.get("/kb-sources/workflow-template")
async def workflow_template(principal: PrincipalDep, service: KbSourceServiceDep) -> JSONResponse:
    """The published-workflow spec for Studio's "Publish the sync workflow" (POST it to /workflows, then publish)."""
    return JSONResponse(content=await run_in_threadpool(service.workflow_template))


@router.get("/kb-sources/adapters")
async def list_source_adapters(principal: PrincipalDep, service: KbSourceServiceDep) -> JSONResponse:
    """The adapters this server has (built in or installed as plug-ins): what Studio's connection and source forms offer."""
    return JSONResponse(content={"items": await run_in_threadpool(service.adapters)})


@router.post(BASE, status_code=status.HTTP_201_CREATED)
async def create_source(
    kb_id: str, principal: PrincipalDep, service: KbSourceServiceDep, payload: Annotated[Any, Body()] = None
) -> JSONResponse:
    body = parse_payload(payload, SourceCreate)
    view, revision = await run_in_threadpool(service.create_source, principal.tenant_id, kb_id, body)
    return stamp(JSONResponse(status_code=status.HTTP_201_CREATED, content=view), revision)


@router.get(BASE)
async def list_sources(kb_id: str, principal: PrincipalDep, service: KbSourceServiceDep) -> JSONResponse:
    return JSONResponse(content=await run_in_threadpool(service.list_sources, principal.tenant_id, kb_id))


@router.get(BASE + "/{source_id}", responses={**etag_responses(write=False)})
async def get_source(kb_id: str, source_id: str, principal: PrincipalDep, service: KbSourceServiceDep) -> JSONResponse:
    view, revision = await run_in_threadpool(service.get_source, principal.tenant_id, kb_id, source_id)
    return read_response(view, revision)


@router.patch(BASE + "/{source_id}", responses={**etag_responses()})
async def update_source(
    kb_id: str, source_id: str, principal: PrincipalDep, service: KbSourceServiceDep, pre: IfMatch,
    payload: Annotated[Any, Body()] = None,
) -> JSONResponse:
    """Change a source. Send the ETag you read as `If-Match`: a stale one is 412 `revision_mismatch`
    with the current source (see eap_api.concurrency)."""
    body = parse_payload(payload, SourcePatch)
    view, revision = await run_in_threadpool(
        service.update_source, principal.tenant_id, kb_id, source_id, body, pre.expected
    )
    return pre.respond(view, revision)


@router.post(BASE + "/{source_id}/sync", status_code=status.HTTP_202_ACCEPTED)
async def start_sync(
    kb_id: str, source_id: str, principal: PrincipalDep, service: KbSourceServiceDep,
    payload: Annotated[Any, Body()] = None,
) -> JSONResponse:
    body = parse_payload(payload, SyncCreate)
    out = await run_in_threadpool(service.start_sync, principal.tenant_id, kb_id, source_id, body, principal.user_id)
    return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=out)


@router.get(BASE + "/{source_id}/syncs")
async def list_syncs(
    kb_id: str, source_id: str, principal: PrincipalDep, service: KbSourceServiceDep,
    limit: Annotated[int, Query(ge=1, le=MAX_SYNC_LIST)] = 20,
) -> JSONResponse:
    return JSONResponse(content=await run_in_threadpool(service.list_syncs, principal.tenant_id, kb_id, source_id, limit))


@router.get(BASE + "/{source_id}/syncs/{run_id}")
async def get_sync(
    kb_id: str, source_id: str, run_id: str, principal: PrincipalDep, service: KbSourceServiceDep
) -> JSONResponse:
    return JSONResponse(content=await run_in_threadpool(service.get_sync, principal.tenant_id, kb_id, source_id, run_id))
