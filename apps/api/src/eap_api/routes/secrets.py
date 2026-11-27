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

from fastapi import APIRouter, Body, Depends, Path, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from eap_api.auth import Principal, current_principal
from eap_api.request_guard import guarded_json_route
from eap_api.schemas import ErrorDetail
from eap_api.service_deps.connections import SecretServiceDep
from eap_api.services.secrets import SECRET_IN_USE, SECRET_NAME_INVALID, SECRET_NOT_FOUND, SecretInUse  # noqa: F401

MAX_SECRET_BODY_BYTES = 64 * 1024
MAX_SECRET_BODY_DEPTH = 4

router = APIRouter(
    tags=["secrets"],
    responses={401: {"model": ErrorDetail}, 404: {"model": ErrorDetail}, 409: {"model": ErrorDetail}, 422: {"model": ErrorDetail}, 503: {"model": ErrorDetail}},
    route_class=guarded_json_route(max_body_bytes=MAX_SECRET_BODY_BYTES, max_depth=MAX_SECRET_BODY_DEPTH),
)

PrincipalDep = Annotated[Principal, Depends(current_principal)]
NameParam = Annotated[str, Path(max_length=200)]


@router.put("/secrets/{name}")
async def put_secret(
    name: NameParam, principal: PrincipalDep, service: SecretServiceDep, payload: Annotated[Any, Body()] = None,
) -> Response:
    return JSONResponse(content=await run_in_threadpool(service.put, principal.tenant_id, principal.user_id, name, payload))


@router.get("/secrets")
async def list_secrets(principal: PrincipalDep, service: SecretServiceDep) -> JSONResponse:
    return JSONResponse(content=await run_in_threadpool(service.list, principal.tenant_id))


@router.delete("/secrets/{name}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_secret(name: NameParam, principal: PrincipalDep, service: SecretServiceDep) -> Response:
    try:
        await run_in_threadpool(service.delete, principal.tenant_id, name)
    except SecretInUse as exc:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": exc.message, "code": exc.code, "servers": exc.servers, "triggers": exc.triggers},
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
