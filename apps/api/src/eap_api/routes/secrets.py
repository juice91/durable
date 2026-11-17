"""Tenant secret routes (secret input, D2). Values are write-only.

- PUT    /secrets/{name}   body {value} (1..8192 chars) -> 200 {restarted: [server_id]}; stores or
                           replaces the secret, then restarts the tenant's managed MCP servers that refer
                           to it (`secret:<name>` env variables) so the new value is projected
- GET    /secrets          the tenant's secrets: [{name, created_at, updated_at, used_by:
                           [{server_id, name}]}], never a value
- DELETE /secrets/{name}   204; 404 `secret_not_found`; 409 `secret_in_use` {servers: [{server_id, name}]}
                           while a managed server refers to it

Dev principals only (`current_principal`; an API key gets 401). The value is encrypted with the
platform key (EAP_SECRETS_KEY; 503 when unset) and never leaves the API process otherwise: it is not
returned, not logged (the access log carries the route template and status only, never a body or
query) and not echoed in a validation error (the body is parsed here, not by FastAPI). An agent
refers to a secret as `secret:<name>`; the worker resolves it at the moment of use."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eap_run_store.secrets import SecretStore
from eap_schemas.secrets import SECRET_VALUE_MAX_CHARS, is_secret_name
from eap_secrets import SecretsError, load_keyring

from eap_mcp_runtime import McpRuntime
from eap_api.auth import Principal, current_principal
from eap_api.database import get_session
from eap_api.mcp_servers import managed
from eap_api.mcp_servers.managed import get_mcp_runtime
from eap_api.mcp_servers.models import McpRuntimeModel, McpServerModel
from eap_api.routes.mcp_servers import restart_runtime
from eap_api.request_guard import guarded_json_route
from eap_api.schemas import ErrorDetail

SECRET_NOT_FOUND = "secret_not_found"
SECRET_NAME_INVALID = "secret_name_invalid"
SECRET_IN_USE = "secret_in_use"
MAX_SECRET_BODY_BYTES = 64 * 1024
MAX_SECRET_BODY_DEPTH = 4

router = APIRouter(
    tags=["secrets"],
    responses={401: {"model": ErrorDetail}, 404: {"model": ErrorDetail}, 409: {"model": ErrorDetail}, 422: {"model": ErrorDetail}, 503: {"model": ErrorDetail}},
    route_class=guarded_json_route(max_body_bytes=MAX_SECRET_BODY_BYTES, max_depth=MAX_SECRET_BODY_DEPTH),
)

PrincipalDep = Annotated[Principal, Depends(current_principal)]
SessionDep = Annotated[Session, Depends(get_session)]
RuntimeDep = Annotated[McpRuntime, Depends(get_mcp_runtime)]
NameParam = Annotated[str, Path(max_length=200)]


class SecretPut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: Annotated[str, Field(min_length=1, max_length=SECRET_VALUE_MAX_CHARS)]


def get_secret_store(request: Request) -> SecretStore:
    """The SecretStore on the app's engine and the platform keyring; 503 when the key is unset."""
    try:
        ring = load_keyring()
    except SecretsError as exc:  # messages carry no key material
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from None
    return SecretStore(request.app.state.database_engine, ring)


def _name_invalid() -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={"detail": "secret name must match ^[a-z][a-z0-9_.-]{0,63}$", "code": SECRET_NAME_INVALID},
    )


@router.put("/secrets/{name}")
async def put_secret(
    name: NameParam, principal: PrincipalDep, request: Request, db: SessionDep, runtime: RuntimeDep,
    payload: Annotated[Any, Body()] = None,
) -> Response:
    if not is_secret_name(name):
        return _name_invalid()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="body must be a JSON object")
    try:
        body = SecretPut.model_validate(payload)
    except ValidationError as exc:  # locations and fixed messages only, never the input
        parts = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors(include_input=False, include_url=False)]
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="; ".join(parts)) from None
    store = get_secret_store(request)
    await run_in_threadpool(store.put, principal.tenant_id, name, body.value, principal.user_id)
    restarted = await run_in_threadpool(_restart_users, db, principal.tenant_id, name, runtime)
    return JSONResponse(content={"restarted": restarted})


def _restart_users(db: Session, tenant_id: str, name: str, runtime: McpRuntime) -> list[str]:
    """Restart the tenant's enabled managed servers that refer to `name` and have a pod (a stopped or
    pending one picks the new value up when it next starts). Returns the server ids restarted."""
    restarted: list[str] = []
    for used in managed.servers_using(db, tenant_id).get(name, []):
        server = db.scalar(select(McpServerModel).where(
            McpServerModel.tenant_id == tenant_id, McpServerModel.id == used["server_id"]))
        row = db.scalar(select(McpRuntimeModel).where(
            McpRuntimeModel.tenant_id == tenant_id, McpRuntimeModel.server_id == used["server_id"]))
        if server is None or row is None or not server.enabled or row.state not in managed.RESTARTABLE:
            continue
        if restart_runtime(db, server, row, runtime):
            restarted.append(server.id)
    return restarted


@router.get("/secrets")
async def list_secrets(principal: PrincipalDep, request: Request, db: SessionDep) -> JSONResponse:
    store = SecretStore(request.app.state.database_engine, None)  # names never decrypt
    rows = await run_in_threadpool(store.names, principal.tenant_id)
    used = await run_in_threadpool(managed.servers_using, db, principal.tenant_id)
    return JSONResponse(content=[
        {"name": r["name"], "created_at": r["created_at"].isoformat(), "updated_at": r["updated_at"].isoformat(),
         "used_by": used.get(r["name"], [])}
        for r in rows
    ])


@router.delete("/secrets/{name}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_secret(name: NameParam, principal: PrincipalDep, request: Request, db: SessionDep) -> Response:
    users = (await run_in_threadpool(managed.servers_using, db, principal.tenant_id)).get(name, [])
    if users:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": "secret is in use by a managed MCP server", "code": SECRET_IN_USE, "servers": users},
        )
    store = SecretStore(request.app.state.database_engine, None)
    removed = await run_in_threadpool(store.delete, principal.tenant_id, name)
    if not removed:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND, content={"detail": "secret not found", "code": SECRET_NOT_FOUND}
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
