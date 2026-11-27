"""External access management for Studio and scripts (docs/external-access.md). The external surface itself
(`eap_api.external_mcp`, `eap_api.external_a2a`) lives under `/api/v1/external/`; nothing here does.

- GET    /agent-access                                   whether external access is on, and what enabling needs
- GET    /agent-access/clients                           external clients with their keys
- POST   /agent-access/clients                           create (201)
- GET    /agent-access/clients/{id}                      one client (`ETag`)
- PATCH  /agent-access/clients/{id}                      change, enable or disable it (`If-Match`)
- POST   /agent-access/clients/{id}/keys                 a new `run` key; the plaintext is returned ONCE
- DELETE /agent-access/clients/{id}/keys/{key_id}        revoke (204, idempotent)
- POST   /agent-access/clients/{id}/cancel-runs          cancel the client's unended runs (explicit action)
- GET    /agent-access/exports                           workflow exports with their enable cards
- POST   /agent-access/exports                           export a published workflow (201, disabled)
- GET    /agent-access/exports/{id}                      one export (`ETag`)
- PATCH  /agent-access/exports/{id}                      change it (`If-Match`); a change disables it
- POST   /agent-access/exports/{id}/enable               a person enables it, echoing `approval_hash`
- POST   /agent-access/exports/{id}/disable              no new run starts; running runs go on
- GET    /agent-access/a2a-endpoint                      the tenant's A2A endpoint, or `{endpoint: null}`
- PUT    /agent-access/a2a-endpoint                      create or change it (`card_name`, `card_description`, `enabled`)
- POST   /agent-access/a2a-endpoint/rotate               a new endpoint id; the old URL stops answering

People only (`current_principal`): an API key or a run token is refused. What the person may do is decided by the service
(`tenant.manage_external_access` to change, `tenant.read_external_access` to read).
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Path, status
from fastapi.responses import JSONResponse, Response

from eap_api.auth import Principal, current_principal
from eap_api.concurrency import IfMatch, etag_responses, read_response
from eap_api.request_guard import GuardedJsonRoute
from eap_api.schemas import ErrorDetail
from eap_api.service_deps.external_access import ExternalAccessServiceDep

PrincipalDep = Annotated[Principal, Depends(current_principal)]
Payload = Annotated[Any, Body()]
KeyId = Annotated[str, Path(max_length=64)]

router = APIRouter(
    prefix="/agent-access",
    tags=["external-access"],
    responses={403: {"model": ErrorDetail}, 404: {"model": ErrorDetail}, 409: {"model": ErrorDetail},
               422: {"model": ErrorDetail}},
    route_class=GuardedJsonRoute,
)


def _actor(principal: Principal) -> str:
    return f"user:{principal.user_id}"


@router.get("")
def get_external_access_settings(principal: PrincipalDep, service: ExternalAccessServiceDep) -> JSONResponse:
    return JSONResponse(content=service.settings())


@router.get("/a2a-endpoint")
def get_a2a_endpoint(principal: PrincipalDep, service: ExternalAccessServiceDep) -> JSONResponse:
    return JSONResponse(content=service.get_a2a_endpoint(principal.tenant_id))


@router.put("/a2a-endpoint")
def save_a2a_endpoint(
    principal: PrincipalDep, service: ExternalAccessServiceDep, payload: Payload = None
) -> JSONResponse:
    return JSONResponse(content=service.save_a2a_endpoint(principal.tenant_id, _actor(principal), payload))


@router.post("/a2a-endpoint/rotate")
def rotate_a2a_endpoint(principal: PrincipalDep, service: ExternalAccessServiceDep) -> JSONResponse:
    return JSONResponse(content=service.rotate_a2a_endpoint(principal.tenant_id))


@router.get("/clients")
def list_external_clients(principal: PrincipalDep, service: ExternalAccessServiceDep) -> JSONResponse:
    return JSONResponse(content={"items": service.list_clients(principal.tenant_id)})


@router.post("/clients", status_code=status.HTTP_201_CREATED)
def create_external_client(
    principal: PrincipalDep, service: ExternalAccessServiceDep, payload: Payload = None
) -> JSONResponse:
    created = service.create_client(principal.tenant_id, _actor(principal), payload)
    return read_response(created, created["revision"], status.HTTP_201_CREATED)


@router.get("/clients/{client_id}", responses={**etag_responses(write=False)})
def get_external_client(
    client_id: uuid.UUID, principal: PrincipalDep, service: ExternalAccessServiceDep
) -> JSONResponse:
    found = service.get_client(principal.tenant_id, client_id)
    return read_response(found, found["revision"])


@router.patch("/clients/{client_id}", responses={**etag_responses()})
def update_external_client(
    client_id: uuid.UUID, principal: PrincipalDep, service: ExternalAccessServiceDep, pre: IfMatch,
    payload: Payload = None,
) -> JSONResponse:
    changed = service.update_client(principal.tenant_id, client_id, payload, pre.expected)
    return pre.respond(changed, changed["revision"])


@router.post("/clients/{client_id}/keys", status_code=status.HTTP_201_CREATED)
def create_external_client_key(
    client_id: uuid.UUID, principal: PrincipalDep, service: ExternalAccessServiceDep, payload: Payload = None
) -> JSONResponse:
    """`{name, workflow_ids, expires_at?, allowed_origins?}`. The response is the only place the key ever appears."""
    created = service.create_key(principal.tenant_id, principal.user_id, client_id, payload)
    return JSONResponse(status_code=status.HTTP_201_CREATED, content=created, headers={"Cache-Control": "no-store"})


@router.delete("/clients/{client_id}/keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def revoke_external_client_key(
    client_id: uuid.UUID, key_id: KeyId, principal: PrincipalDep, service: ExternalAccessServiceDep
) -> Response:
    service.revoke_key(principal.tenant_id, client_id, key_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/clients/{client_id}/cancel-runs")
def cancel_external_client_runs(
    client_id: uuid.UUID, principal: PrincipalDep, service: ExternalAccessServiceDep
) -> JSONResponse:
    return JSONResponse(content=service.cancel_client_runs(principal.tenant_id, principal.user_id, client_id))


@router.get("/exports")
def list_workflow_exports(principal: PrincipalDep, service: ExternalAccessServiceDep) -> JSONResponse:
    return JSONResponse(content={"items": service.list_exports(principal.tenant_id)})


@router.post("/exports", status_code=status.HTTP_201_CREATED)
def create_workflow_export(
    principal: PrincipalDep, service: ExternalAccessServiceDep, payload: Payload = None
) -> JSONResponse:
    created = service.create_export(principal.tenant_id, _actor(principal), payload)
    return read_response(created, created["revision"], status.HTTP_201_CREATED)


@router.get("/exports/{export_id}", responses={**etag_responses(write=False)})
def get_workflow_export(
    export_id: uuid.UUID, principal: PrincipalDep, service: ExternalAccessServiceDep
) -> JSONResponse:
    found = service.get_export(principal.tenant_id, export_id)
    return read_response(found, found["revision"])


@router.patch("/exports/{export_id}", responses={**etag_responses()})
def update_workflow_export(
    export_id: uuid.UUID, principal: PrincipalDep, service: ExternalAccessServiceDep, pre: IfMatch,
    payload: Payload = None,
) -> JSONResponse:
    changed = service.update_export(principal.tenant_id, export_id, payload, pre.expected)
    return pre.respond(changed, changed["revision"])


@router.post("/exports/{export_id}/enable")
def enable_workflow_export(
    export_id: uuid.UUID, principal: PrincipalDep, service: ExternalAccessServiceDep, payload: Payload = None
) -> JSONResponse:
    """`{approved_hash, confirm: true}` with the `approval_hash` the card showed (409 when it changed)."""
    body = payload if isinstance(payload, dict) else {}
    out = service.enable_export(
        principal.tenant_id, _actor(principal), export_id, approved_hash=body.get("approved_hash"),
        confirm=body.get("confirm"),
    )
    return read_response(out, out["revision"])


@router.post("/exports/{export_id}/disable")
def disable_workflow_export(
    export_id: uuid.UUID, principal: PrincipalDep, service: ExternalAccessServiceDep
) -> JSONResponse:
    out = service.disable_export(principal.tenant_id, export_id)
    return read_response(out, out["revision"])
