"""Tenant secret routes (secret input, D2). Values are write-only.

- PUT    /secrets/{name}   body {value} (1..8192 chars) -> 204; stores or replaces the secret
- GET    /secrets          the tenant's secrets: [{name, created_at, updated_at}], never a value
- DELETE /secrets/{name}   204, or 404 `secret_not_found`

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
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eap_run_store.secrets import SecretStore
from eap_schemas.secrets import SECRET_VALUE_MAX_CHARS, is_secret_name
from eap_secrets import SecretsError, load_keyring

from eap_api.auth import Principal, current_principal
from eap_api.request_guard import guarded_json_route
from eap_api.schemas import ErrorDetail

SECRET_NOT_FOUND = "secret_not_found"
SECRET_NAME_INVALID = "secret_name_invalid"
MAX_SECRET_BODY_BYTES = 64 * 1024
MAX_SECRET_BODY_DEPTH = 4

router = APIRouter(
    tags=["secrets"],
    responses={401: {"model": ErrorDetail}, 404: {"model": ErrorDetail}, 422: {"model": ErrorDetail}, 503: {"model": ErrorDetail}},
    route_class=guarded_json_route(max_body_bytes=MAX_SECRET_BODY_BYTES, max_depth=MAX_SECRET_BODY_DEPTH),
)

PrincipalDep = Annotated[Principal, Depends(current_principal)]
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


@router.put("/secrets/{name}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def put_secret(
    name: NameParam, principal: PrincipalDep, request: Request, payload: Annotated[Any, Body()] = None
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
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/secrets")
async def list_secrets(principal: PrincipalDep, request: Request) -> JSONResponse:
    store = SecretStore(request.app.state.database_engine, None)  # names never decrypt
    rows = await run_in_threadpool(store.names, principal.tenant_id)
    return JSONResponse(content=[
        {"name": r["name"], "created_at": r["created_at"].isoformat(), "updated_at": r["updated_at"].isoformat()}
        for r in rows
    ])


@router.delete("/secrets/{name}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_secret(name: NameParam, principal: PrincipalDep, request: Request) -> Response:
    store = SecretStore(request.app.state.database_engine, None)
    removed = await run_in_threadpool(store.delete, principal.tenant_id, name)
    if not removed:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND, content={"detail": "secret not found", "code": SECRET_NOT_FOUND}
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
