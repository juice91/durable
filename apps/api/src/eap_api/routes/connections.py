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
provider body or an exception text. `config.provider` names the adapter (e.g. "github") and must
be set; `base_url`, when given, is an https URL.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from eap_api.auth import Principal, current_principal
from eap_api.concurrency import IfMatch, etag_responses, read_response, stamp
from eap_api.request_guard import guarded_json_route
from eap_api.service_deps.connections import ConnectionServiceDep
from eap_api.services.connections import parse_uuid  # noqa: F401  (kb_sources imports it from here)

MAX_BODY_BYTES = 64 * 1024
MAX_BODY_DEPTH = 6

router = APIRouter(
    tags=["connections"],
    route_class=guarded_json_route(max_body_bytes=MAX_BODY_BYTES, max_depth=MAX_BODY_DEPTH),
)
PrincipalDep = Annotated[Principal, Depends(current_principal)]


def problem(code: str, detail: str, http: int = status.HTTP_422_UNPROCESSABLE_CONTENT, **extra: Any) -> JSONResponse:
    """A `{detail, code}` refusal; kb_sources answers its own refusals with it (until it moves to a service)."""
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


@router.post("/settings/connections", status_code=status.HTTP_201_CREATED)
async def create_connection(
    service: ConnectionServiceDep, principal: PrincipalDep, payload: Annotated[Any, Body()] = None
) -> JSONResponse:
    made = await run_in_threadpool(service.create, principal.tenant_id, payload)
    return stamp(JSONResponse(status_code=status.HTTP_201_CREATED, content=made), made["revision"])


@router.get("/settings/connections")
async def list_connections(service: ConnectionServiceDep, principal: PrincipalDep) -> JSONResponse:
    return JSONResponse(content=await run_in_threadpool(service.list, principal.tenant_id))


@router.get("/settings/connections/{connection_id}", responses={**etag_responses(write=False)})
async def get_connection(connection_id: str, service: ConnectionServiceDep, principal: PrincipalDep) -> JSONResponse:
    found = await run_in_threadpool(service.get, principal.tenant_id, connection_id)
    return read_response(found, found["revision"])


@router.patch("/settings/connections/{connection_id}", responses={**etag_responses()})
async def update_connection(
    connection_id: str, service: ConnectionServiceDep, principal: PrincipalDep, pre: IfMatch,
    payload: Annotated[Any, Body()] = None,
) -> JSONResponse:
    """Change a connection. Send the ETag you read as `If-Match`: a stale one is 412 `revision_mismatch`
    with the current connection (see eap_api.concurrency)."""
    changed = await run_in_threadpool(service.update, principal.tenant_id, connection_id, payload, pre.expected)
    return pre.respond(changed, changed["revision"])


@router.delete("/settings/connections/{connection_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def delete_connection(connection_id: str, service: ConnectionServiceDep, principal: PrincipalDep) -> Response:
    await run_in_threadpool(service.delete, principal.tenant_id, connection_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/settings/connections/{connection_id}/check")
async def check(connection_id: str, service: ConnectionServiceDep, principal: PrincipalDep) -> JSONResponse:
    return JSONResponse(content=await service.check(principal.tenant_id, connection_id))
