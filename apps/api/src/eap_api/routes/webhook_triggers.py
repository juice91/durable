"""Webhook trigger management (design 2, 3, 4.6 and 8) for Studio and scripts.

- POST   /webhook-triggers                 create (201), always `disabled`; `generate: true` returns the new secret ONCE
- GET    /webhook-triggers                 the tenant's triggers (`?include_archived`, `?workflow_id`)
- GET    /webhook-triggers/{id}            one trigger with its publish-style warnings and in-process refusal counters
- PATCH  /webhook-triggers/{id}            change it (`If-Match`); a grant change returns it to `disabled`
- POST   /webhook-triggers/{id}/enable-request   ask for it to be enabled (`pending_enable`): the card in Studio
- POST   /webhook-triggers/{id}/enable     a person enables it, echoing the `config_hash` the card showed
- POST   /webhook-triggers/{id}/decision   a person approves or rejects a pending request
- POST   /webhook-triggers/{id}/disable    stop the URL
- POST   /webhook-triggers/{id}/archive    retire it for good (the row stays for the log)
- POST   /webhook-triggers/{id}/rotate     `{add}` / `{retire}` / `{generate: true}`: two active secrets, no downtime
- GET    /webhook-triggers/{id}/events     the lifecycle audit rows
- GET    /webhook-deliveries               the delivery log (`?trigger_id`, `?outcome`, `?since`, `?limit`)
- GET    /webhook-deliveries/{id}          one delivery

These routes take a signed-in person only (`current_principal`): an API key or a webhook signature is refused. What the
person may do is decided by the service on the trigger's workflow and secrets (docs/iam.md, "Inbound webhooks").
Enabling is a person's action by construction (the same dependency); MCP can only request it. No response carries a
secret value except the one-time `generated_secret` of a create or rotate (`Cache-Control: no-store`).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Query, Response, status
from fastapi.responses import JSONResponse

from eap_api.auth import Principal, current_principal
from eap_api.concurrency import IfMatch, etag_responses, read_response, stamp
from eap_api.deps import WebhookGuardDep
from eap_api.request_guard import GuardedJsonRoute
from eap_api.schemas import ErrorDetail
from eap_api.service_deps.webhooks import WebhookTriggerServiceDep

PrincipalDep = Annotated[Principal, Depends(current_principal)]

router = APIRouter(
    tags=["webhooks"],
    responses={404: {"model": ErrorDetail}, 422: {"model": ErrorDetail}, 409: {"model": ErrorDetail}},
    route_class=GuardedJsonRoute,
)


def _actor(principal: Principal) -> str:
    return f"user:{principal.user_id}"


def _secret_response(body: dict[str, Any], status_code: int = 200) -> JSONResponse:
    headers = {"Cache-Control": "no-store"} if "generated_secret" in body else None
    return stamp(JSONResponse(status_code=status_code, content=body, headers=headers), body["revision"])  # type: ignore[return-value]


@router.post("/webhook-triggers", status_code=status.HTTP_201_CREATED)
def create_webhook_trigger(
    principal: PrincipalDep, service: WebhookTriggerServiceDep, payload: Annotated[Any, Body()] = None
) -> JSONResponse:
    """Create a `disabled` trigger. Body: name, description, workflow_id, version (a number or "latest"), preset
    (github | standard_webhooks | custom), preset_config (custom only), secrets (existing `secret:<name>` references) or
    `generate: true`, tolerance_seconds, actions.start {payload_schema, input_map}, limits."""
    created = service.create(principal.tenant_id, _actor(principal), payload, allow_generate=True)
    return _secret_response(created, status.HTTP_201_CREATED)


@router.get("/webhook-triggers")
def list_webhook_triggers(
    principal: PrincipalDep, service: WebhookTriggerServiceDep, include_archived: bool = False, workflow_id: str | None = None
) -> JSONResponse:
    return JSONResponse(content={"items": service.list(principal.tenant_id, include_archived=include_archived, workflow_id=workflow_id)})


@router.get("/webhook-triggers/{trigger_id}", responses={**etag_responses(write=False)})
def get_webhook_trigger(
    trigger_id: uuid.UUID, principal: PrincipalDep, service: WebhookTriggerServiceDep, guard: WebhookGuardDep
) -> JSONResponse:
    found = service.get(principal.tenant_id, trigger_id)
    found["refusals_since_start"] = guard.snapshot(found["id"])  # unauthenticated noise: counters, per API process
    return read_response(found, found["revision"])


@router.patch("/webhook-triggers/{trigger_id}", responses={**etag_responses()})
def update_webhook_trigger(
    trigger_id: uuid.UUID, principal: PrincipalDep, service: WebhookTriggerServiceDep, pre: IfMatch,
    payload: Annotated[Any, Body()] = None,
) -> JSONResponse:
    """Send the ETag you read as `If-Match`. A change to the workflow version, preset config, actions, limits or
    tolerance returns an enabled trigger to `disabled`: a person approves the new grant."""
    changed = service.update(principal.tenant_id, _actor(principal), trigger_id, payload, pre.expected)
    return pre.respond(changed, changed["revision"])


@router.post("/webhook-triggers/{trigger_id}/enable-request")
def request_enable_webhook_trigger(trigger_id: uuid.UUID, principal: PrincipalDep, service: WebhookTriggerServiceDep) -> JSONResponse:
    out = service.request_enable(principal.tenant_id, _actor(principal), trigger_id)
    return read_response(out, out["revision"])


@router.post("/webhook-triggers/{trigger_id}/enable")
def enable_webhook_trigger(
    trigger_id: uuid.UUID, principal: PrincipalDep, service: WebhookTriggerServiceDep, payload: Annotated[Any, Body()] = None
) -> JSONResponse:
    """A person enables the trigger: `{config_hash, confirm: true}` with the hash the card showed (409 when it changed)."""
    body = payload if isinstance(payload, dict) else {}
    out = service.enable(
        principal.tenant_id, _actor(principal), trigger_id, config_hash_seen=body.get("config_hash"),
        confirm=body.get("confirm"), by_human=True,
    )
    return read_response(out, out["revision"])


@router.post("/webhook-triggers/{trigger_id}/decision")
def decide_webhook_trigger(
    trigger_id: uuid.UUID, principal: PrincipalDep, service: WebhookTriggerServiceDep, payload: Annotated[Any, Body()] = None
) -> JSONResponse:
    body = payload if isinstance(payload, dict) else {}
    out = service.decide(
        principal.tenant_id, _actor(principal), trigger_id, decision=body.get("decision"),
        config_hash_seen=body.get("config_hash"), by_human=True,
    )
    return read_response(out, out["revision"])


@router.post("/webhook-triggers/{trigger_id}/disable")
def disable_webhook_trigger(trigger_id: uuid.UUID, principal: PrincipalDep, service: WebhookTriggerServiceDep) -> JSONResponse:
    out = service.disable(principal.tenant_id, _actor(principal), trigger_id)
    return read_response(out, out["revision"])


@router.post("/webhook-triggers/{trigger_id}/archive")
def archive_webhook_trigger(trigger_id: uuid.UUID, principal: PrincipalDep, service: WebhookTriggerServiceDep) -> JSONResponse:
    out = service.archive(principal.tenant_id, _actor(principal), trigger_id)
    return read_response(out, out["revision"])


@router.post("/webhook-triggers/{trigger_id}/rotate")
def rotate_webhook_trigger_secret(
    trigger_id: uuid.UUID, principal: PrincipalDep, service: WebhookTriggerServiceDep, payload: Annotated[Any, Body()] = None
) -> JSONResponse:
    out = service.rotate(principal.tenant_id, _actor(principal), trigger_id, payload, allow_generate=True, by_human=True)
    return _secret_response(out)


@router.get("/webhook-triggers/{trigger_id}/events")
def list_webhook_trigger_events(
    trigger_id: uuid.UUID, principal: PrincipalDep, service: WebhookTriggerServiceDep, limit: Annotated[int, Query(ge=1, le=200)] = 100
) -> JSONResponse:
    return JSONResponse(content={"items": service.events(principal.tenant_id, trigger_id, limit)})


@router.get("/webhook-deliveries")
def list_webhook_deliveries(
    principal: PrincipalDep, service: WebhookTriggerServiceDep, trigger_id: uuid.UUID | None = None,
    outcome: Annotated[str | None, Query(max_length=40)] = None, since: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> JSONResponse:
    return JSONResponse(content={"items": service.deliveries(
        principal.tenant_id, trigger_id=trigger_id, outcome=outcome, since=since, limit=limit)})


@router.get("/webhook-deliveries/{delivery_id}")
def get_webhook_delivery(delivery_id: uuid.UUID, principal: PrincipalDep, service: WebhookTriggerServiceDep) -> JSONResponse:
    return JSONResponse(content=service.delivery(principal.tenant_id, str(delivery_id)))
