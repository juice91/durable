"""Tenant connections to external providers (kb source sync, Task 4). Dev principals only.

- POST   /settings/connections              201 the connection; 409 `connection_name_conflict`;
                                            422 `secret_ref_required` for a credential that is not `secret:<name>`
- GET    /settings/connections              {items: [connection]}
- GET    /settings/connections/{id}         the connection; 404
- PATCH  /settings/connections/{id}         any of name, base_url, config, credential_refs, enabled
- DELETE /settings/connections/{id}         204; 409 `connection_in_use` while a source refers to it
- POST   /settings/connections/{id}/check   200 {ok, checked_at, error_code}; bounded by
                                            EAP_KB_SOURCE_CHECK_TIMEOUT_SECONDS (default 10, 0.5-30)

A connection holds `credential_refs` (logical name -> `secret:<name>`), never a value: a body whose reference
is not a secret reference is refused without echoing it, and a response shows the references only. The check
resolves the references inside the request (eap_kb_sources.check) and answers a stable code, never a token, a
provider body or an exception text. `config.provider` names the adapter (e.g. "acme_catalog") and must
be set; `base_url`, when given, is an https URL.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Body, Depends, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eap_api.auth import Principal, current_principal
from eap_api.request_guard import guarded_json_route
from eap_api.routes.secrets import get_secret_store
from eap_kb_sources.check import check_connection
from eap_kb_sources.errors import CONNECTION_NOT_FOUND, SourceError
from eap_run_store.kb_sources import (
    CONNECTION_KINDS,
    Connection,
    ConnectionInUse,
    ConnectionNameConflict,
    ConnectionStore,
)
from eap_schemas.secrets import is_secret_ref

MAX_BODY_BYTES = 64 * 1024
MAX_BODY_DEPTH = 6

router = APIRouter(
    tags=["connections"],
    route_class=guarded_json_route(max_body_bytes=MAX_BODY_BYTES, max_depth=MAX_BODY_DEPTH),
)
PrincipalDep = Annotated[Principal, Depends(current_principal)]


def problem(code: str, detail: str, http: int = status.HTTP_422_UNPROCESSABLE_CONTENT, **extra: Any) -> JSONResponse:
    return JSONResponse(status_code=http, content={"detail": detail, "code": code, **extra})


def parse_body(payload: Any, model: type[BaseModel]) -> Any:
    """The validated model, or a 422 JSONResponse built from locations and fixed messages (never the input)."""
    if not isinstance(payload, dict):
        return problem("invalid_request", "body must be a JSON object")
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        parts = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors(include_input=False, include_url=False)]
        return problem("invalid_request", "; ".join(parts))


def parse_uuid(value: str) -> str | None:
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


class ConnectionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=100)]
    kind: str = CONNECTION_KINDS[0]
    base_url: Annotated[str, Field(max_length=500)] | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    credential_refs: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class ConnectionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    base_url: Annotated[str, Field(max_length=500)] | None = None
    config: dict[str, Any] | None = None
    credential_refs: dict[str, Any] | None = None
    enabled: bool | None = None


def view(c: Connection) -> dict[str, Any]:
    checked = c.last_checked_at
    return {
        "id": c.id, "name": c.name, "kind": c.kind, "base_url": c.base_url, "config": c.config,
        "credential_refs": c.credential_refs, "enabled": c.enabled,
        "last_checked_at": checked.isoformat() if checked is not None else None,
        "last_status": c.last_status, "last_error_code": c.last_error_code,
        "created_at": c.created_at.isoformat(), "updated_at": c.updated_at.isoformat(),
    }


def _refs_problem(refs: dict[str, Any] | None) -> JSONResponse | None:
    if refs is None:
        return None
    for key, ref in refs.items():
        if not isinstance(key, str) or not key or is_secret_ref(ref) is None:
            return problem("secret_ref_required", "credential_refs maps logical names to 'secret:<name>' references")
    return None


def _url_problem(base_url: str | None) -> JSONResponse | None:
    if base_url is None:
        return None
    parts = urlsplit(base_url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        return problem("invalid_request", "base_url must be an https URL without credentials")
    return None


def _provider_problem(config: dict[str, Any] | None) -> JSONResponse | None:
    if config is None:
        return None
    provider = config.get("provider")
    if not isinstance(provider, str) or not provider:
        return problem("invalid_request", "config.provider is required")
    return None


def _store(request: Request) -> ConnectionStore:
    return ConnectionStore(request.app.state.database_engine)


def _not_found() -> JSONResponse:
    return problem(CONNECTION_NOT_FOUND, "connection not found", status.HTTP_404_NOT_FOUND)


@router.post("/settings/connections", status_code=status.HTTP_201_CREATED)
async def create_connection(
    request: Request, principal: PrincipalDep, payload: Annotated[Any, Body()] = None
) -> JSONResponse:
    body = parse_body(payload, ConnectionCreate)
    if isinstance(body, JSONResponse):
        return body
    if body.kind not in CONNECTION_KINDS:
        return problem("invalid_request", "unknown connection kind")
    for refused in (_refs_problem(body.credential_refs), _url_problem(body.base_url), _provider_problem(body.config)):
        if refused is not None:
            return refused
    try:
        made = await run_in_threadpool(
            lambda: _store(request).create(
                principal.tenant_id, name=body.name, kind=body.kind, base_url=body.base_url, config=body.config,
                credential_refs=body.credential_refs, enabled=body.enabled,
            )
        )
    except ConnectionNameConflict:
        return problem("connection_name_conflict", "a connection with this name exists", status.HTTP_409_CONFLICT)
    return JSONResponse(status_code=status.HTTP_201_CREATED, content=view(made))


@router.get("/settings/connections")
async def list_connections(request: Request, principal: PrincipalDep) -> JSONResponse:
    rows = await run_in_threadpool(_store(request).list, principal.tenant_id)
    return JSONResponse(content={"items": [view(c) for c in rows]})


@router.get("/settings/connections/{connection_id}")
async def get_connection(connection_id: str, request: Request, principal: PrincipalDep) -> JSONResponse:
    cid = parse_uuid(connection_id)
    found = await run_in_threadpool(_store(request).get, principal.tenant_id, cid) if cid else None
    return view_or_404(found)


def view_or_404(found: Connection | None) -> JSONResponse:
    return _not_found() if found is None else JSONResponse(content=view(found))


@router.patch("/settings/connections/{connection_id}")
async def update_connection(
    connection_id: str, request: Request, principal: PrincipalDep, payload: Annotated[Any, Body()] = None
) -> JSONResponse:
    body = parse_body(payload, ConnectionPatch)
    if isinstance(body, JSONResponse):
        return body
    cid = parse_uuid(connection_id)
    if cid is None:
        return _not_found()
    for refused in (_refs_problem(body.credential_refs), _url_problem(body.base_url), _provider_problem(body.config)):
        if refused is not None:
            return refused
    try:
        changed = await run_in_threadpool(
            lambda: _store(request).update(
                principal.tenant_id, cid, name=body.name, base_url=body.base_url, config=body.config,
                credential_refs=body.credential_refs, enabled=body.enabled,
            )
        )
    except ConnectionNameConflict:
        return problem("connection_name_conflict", "a connection with this name exists", status.HTTP_409_CONFLICT)
    return view_or_404(changed)


@router.delete("/settings/connections/{connection_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_connection(connection_id: str, request: Request, principal: PrincipalDep) -> Response:
    cid = parse_uuid(connection_id)
    if cid is None:
        return _not_found()
    try:
        removed = await run_in_threadpool(_store(request).delete, principal.tenant_id, cid)
    except ConnectionInUse:
        return problem("connection_in_use", "a source refers to this connection", status.HTTP_409_CONFLICT)
    if not removed:
        return _not_found()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/settings/connections/{connection_id}/check")
async def check(connection_id: str, request: Request, principal: PrincipalDep) -> JSONResponse:
    cid = parse_uuid(connection_id)
    if cid is None:
        return _not_found()
    secrets = get_secret_store(request)
    try:
        result = await check_connection(request.app.state.database_engine, principal.tenant_id, cid, secrets)
    except SourceError as exc:
        if exc.code == CONNECTION_NOT_FOUND:
            return _not_found()
        raise
    return JSONResponse(content=result)
