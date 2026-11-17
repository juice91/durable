"""Knowledge-base sources and their syncs (kb source sync, Task 4). Dev principals only.

- POST /knowledge-bases/{kb}/sources                   201 the source; 404 kb or connection; 409 name;
                                                       422 `source_adapter_not_found` | `invalid_source_config`
- GET  /knowledge-bases/{kb}/sources                   {items: [source]}
- GET  /knowledge-bases/{kb}/sources/{id}              the source (config is non-secret; the checkpoint as a summary)
- PATCH /knowledge-bases/{kb}/sources/{id}             any of name, config, enabled
- POST /knowledge-bases/{kb}/sources/{id}/sync         202 {run_id, status: "queued"} through the shared launch
                                                       (eap_run_store.kb_source_launch); body {mode, part_numbers?, run_id?}
- GET  /knowledge-bases/{kb}/sources/{id}/syncs        {items: [sync]}, newest first (`limit` 1-100, default 20)
- GET  /knowledge-bases/{kb}/sources/{id}/syncs/{run}  the sync plus `report` (the run's bounded report once it ends)
                                                       and `run_status`

A source row, run or sync of another tenant answers 404, never 403. Cancelling a sync is
`POST /runs/{run_id}/cancel`.
"""

from __future__ import annotations

import json
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, Query, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from eap_api.auth import Principal, current_principal
from eap_api.launcher import RunLauncher, get_run_launcher
from eap_api.request_guard import guarded_json_route
from eap_api.routes.connections import parse_body, parse_uuid, problem
from eap_kb_sources.check import load_providers
from eap_kb_sources.errors import SOURCE_ADAPTER_NOT_FOUND, SourceError
from eap_kb_sources.registry import adapter_for
from eap_observability import current_trace_context
from eap_run_store import RunStore
from eap_run_store.kb_source_launch import LaunchFailed, SourceLaunchError, launch_sync, source_view
from eap_run_store.kb_sources import SOURCE_KINDS, ConnectionStore, Source, SourceNameConflict, SourceStore, Sync, SyncStore
from eap_run_store.knowledge import KnowledgeStore

MAX_BODY_BYTES = 64 * 1024
MAX_BODY_DEPTH = 6
MAX_SYNC_LIST = 100

router = APIRouter(
    tags=["kb-sources"],
    route_class=guarded_json_route(max_body_bytes=MAX_BODY_BYTES, max_depth=MAX_BODY_DEPTH),
)
PrincipalDep = Annotated[Principal, Depends(current_principal)]
LauncherDep = Annotated[RunLauncher, Depends(get_run_launcher)]
BASE = "/knowledge-bases/{kb_id}/sources"


class SourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_id: str
    name: Annotated[str, Field(min_length=1, max_length=100)]
    kind: str = SOURCE_KINDS[0]
    provider: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class SourcePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    config: dict[str, Any] | None = None
    enabled: bool | None = None


class SyncCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["targeted", "incremental", "full"]
    part_numbers: list[str] = Field(default_factory=list, max_length=1000)
    run_id: uuid.UUID | None = None


def _engine(request: Request) -> Any:
    return request.app.state.database_engine


def _404(code: str, detail: str) -> JSONResponse:
    return problem(code, detail, status.HTTP_404_NOT_FOUND)


def _kb_found(request: Request, tenant_id: str, kb_id: str) -> bool:
    return KnowledgeStore(_engine(request)).get_knowledge_base(tenant_id, kb_id) is not None


def _source_in_kb(request: Request, tenant_id: str, kb_id: str, source_id: str) -> Source | None:
    sid, kid = parse_uuid(source_id), parse_uuid(kb_id)
    if sid is None or kid is None:
        return None
    source = SourceStore(_engine(request)).get(tenant_id, sid)
    return source if source is not None and source.knowledge_base_id == kid else None


def _validate_config(kind: str, provider: str, config: dict[str, Any]) -> JSONResponse | None:
    try:
        load_providers()
        adapter = adapter_for(kind, provider)
    except SourceError as exc:
        if exc.code == SOURCE_ADAPTER_NOT_FOUND:
            return problem(SOURCE_ADAPTER_NOT_FOUND, "no adapter for this kind and provider")
        raise
    try:
        adapter.validate_config(config)
    except ValueError as exc:
        return problem("invalid_source_config", str(exc)[:300])
    return None


def sync_view(s: Sync) -> dict[str, Any]:
    return {
        "run_id": s.run_id, "source_id": s.source_id, "parent_run_id": s.parent_run_id, "mode": s.mode,
        "status": s.status, "counters": s.counters, "errors": s.bounded_errors,
        "started_at": s.started_at.isoformat(),
        "completed_at": s.completed_at.isoformat() if s.completed_at is not None else None,
    }


@router.post(BASE, status_code=status.HTTP_201_CREATED)
async def create_source(
    kb_id: str, request: Request, principal: PrincipalDep, payload: Annotated[Any, Body()] = None
) -> JSONResponse:
    body = parse_body(payload, SourceCreate)
    if isinstance(body, JSONResponse):
        return body
    tenant_id = principal.tenant_id
    if not await run_in_threadpool(_kb_found, request, tenant_id, kb_id):
        return _404("knowledge_base_not_found", "knowledge base not found")
    cid = parse_uuid(body.connection_id)
    if cid is None or await run_in_threadpool(ConnectionStore(_engine(request)).get, tenant_id, cid) is None:
        return _404("connection_not_found", "connection not found")
    if body.kind not in SOURCE_KINDS:
        return problem("invalid_request", "unknown source kind")
    refused = _validate_config(body.kind, body.provider, body.config)
    if refused is not None:
        return refused
    try:
        made = await run_in_threadpool(
            lambda: SourceStore(_engine(request)).create(
                tenant_id, knowledge_base_id=str(uuid.UUID(kb_id)), connection_id=cid, name=body.name,
                kind=body.kind, provider=body.provider, config=body.config, enabled=body.enabled,
            )
        )
    except SourceNameConflict:
        return problem("source_name_conflict", "a source with this name exists", status.HTTP_409_CONFLICT)
    return JSONResponse(status_code=status.HTTP_201_CREATED, content=source_view(made))


@router.get(BASE)
async def list_sources(kb_id: str, request: Request, principal: PrincipalDep) -> JSONResponse:
    tenant_id = principal.tenant_id
    if not await run_in_threadpool(_kb_found, request, tenant_id, kb_id):
        return _404("knowledge_base_not_found", "knowledge base not found")
    rows = await run_in_threadpool(SourceStore(_engine(request)).list_for_kb, tenant_id, str(uuid.UUID(kb_id)))
    return JSONResponse(content={"items": [source_view(s) for s in rows]})


@router.get(BASE + "/{source_id}")
async def get_source(kb_id: str, source_id: str, request: Request, principal: PrincipalDep) -> JSONResponse:
    source = await run_in_threadpool(_source_in_kb, request, principal.tenant_id, kb_id, source_id)
    if source is None:
        return _404("source_not_found", "source not found")
    return JSONResponse(content=source_view(source))


@router.patch(BASE + "/{source_id}")
async def update_source(
    kb_id: str, source_id: str, request: Request, principal: PrincipalDep, payload: Annotated[Any, Body()] = None
) -> JSONResponse:
    body = parse_body(payload, SourcePatch)
    if isinstance(body, JSONResponse):
        return body
    tenant_id = principal.tenant_id
    source = await run_in_threadpool(_source_in_kb, request, tenant_id, kb_id, source_id)
    if source is None:
        return _404("source_not_found", "source not found")
    if body.config is not None:
        refused = _validate_config(source.kind, source.provider, body.config)
        if refused is not None:
            return refused
    try:
        changed = await run_in_threadpool(
            lambda: SourceStore(_engine(request)).update(
                tenant_id, source.id, name=body.name, config=body.config, enabled=body.enabled
            )
        )
    except SourceNameConflict:
        return problem("source_name_conflict", "a source with this name exists", status.HTTP_409_CONFLICT)
    if changed is None:
        return _404("source_not_found", "source not found")
    return JSONResponse(content=source_view(changed))


@router.post(BASE + "/{source_id}/sync", status_code=status.HTTP_202_ACCEPTED)
async def start_sync(
    kb_id: str, source_id: str, request: Request, principal: PrincipalDep, launcher: LauncherDep,
    payload: Annotated[Any, Body()] = None,
) -> JSONResponse:
    body = parse_body(payload, SyncCreate)
    if isinstance(body, JSONResponse):
        return body
    tenant_id = principal.tenant_id
    source = await run_in_threadpool(_source_in_kb, request, tenant_id, kb_id, source_id)
    if source is None:
        return _404("source_not_found", "source not found")
    trace_id, traceparent = current_trace_context()
    try:
        out = await run_in_threadpool(
            lambda: launch_sync(
                _engine(request), launcher, tenant_id=tenant_id, source_id=source.id, mode=body.mode,
                part_numbers=body.part_numbers, run_id=body.run_id or uuid.uuid4(), created_by=principal.user_id,
                traceparent=traceparent, trace_id=trace_id,
            )
        )
    except SourceLaunchError as exc:
        return problem(exc.code, exc.message, exc.status)
    except LaunchFailed as exc:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content={"detail": exc.detail, "run_id": exc.run_id}
        )
    return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=out)


@router.get(BASE + "/{source_id}/syncs")
async def list_syncs(
    kb_id: str, source_id: str, request: Request, principal: PrincipalDep,
    limit: Annotated[int, Query(ge=1, le=MAX_SYNC_LIST)] = 20,
) -> JSONResponse:
    source = await run_in_threadpool(_source_in_kb, request, principal.tenant_id, kb_id, source_id)
    if source is None:
        return _404("source_not_found", "source not found")
    rows = await run_in_threadpool(SyncStore(_engine(request)).list_for_source, principal.tenant_id, source.id, limit)
    return JSONResponse(content={"items": [sync_view(s) for s in rows]})


@router.get(BASE + "/{source_id}/syncs/{run_id}")
async def get_sync(kb_id: str, source_id: str, run_id: str, request: Request, principal: PrincipalDep) -> JSONResponse:
    tenant_id = principal.tenant_id
    source = await run_in_threadpool(_source_in_kb, request, tenant_id, kb_id, source_id)
    rid = parse_uuid(run_id)
    if source is None or rid is None:
        return _404("sync_not_found", "sync not found")
    sync = await run_in_threadpool(SyncStore(_engine(request)).get_by_run, tenant_id, rid)
    if sync is None or sync.source_id != source.id:
        return _404("sync_not_found", "sync not found")
    run = await run_in_threadpool(RunStore(_engine(request)).get_run, tenant_id, uuid.UUID(rid))
    report: Any = None
    if run is not None and run["status"] in ("completed", "failed", "needs_attention") and run.get("output"):
        try:
            report = json.loads(run["output"])
        except (TypeError, ValueError):
            report = None
    return JSONResponse(content={**sync_view(sync), "report": report, "run_status": run["status"] if run else None})
