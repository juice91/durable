"""`POST /api/v1/hooks/{trigger_id}`: the inbound webhook endpoint (design 4).

This is the ONE route a sign-in gate in front of the host must let through (scope that exception to this exact path
prefix), so the HMAC signature is its only gate and nothing else under `/api/v1/hooks/` may exist (a test lists the
route table). It takes no `current_principal`, no API key and no bearer: an `Authorization` header is never read.

Order of checks, cheapest first, with uniform answers:

1. per-IP token bucket -> 429 + Retry-After
2. the trigger's gate (enabled, approved): unknown, disabled, archived, pending or edited-after-approval -> 404 `not found`
3. per-trigger token bucket -> 429
4. the body, read capped at the trigger's limit before any HMAC -> 413
5. the signature, then the timestamp window (the `webhook` identity provider) -> 401 `invalid signature`, whatever the reason
6. only now parse, dedupe, validate, map, limit and start the run (`WebhookIngestService`) -> 202 / 200 / 415 / 422 / 429 / 503

The answer carries no run id, tenant id or reason that helps a prober. Refusals that cannot be tied to an authenticated
sender are counted in memory, never written as rows. No CORS headers (the CORS middleware is scoped to `/ag-ui/*`).
"""

from __future__ import annotations

import logging
from typing import Any

from eap_iam import AuthError
from eap_observability.redaction import log_failure
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from eap_api.auth import authenticate_webhook
from eap_api.deps import WebhookGuardDep
from eap_api.inbound_guard import INVALID_SIGNATURE_BODY, NOT_FOUND_BODY, client_ip, dummy_work, trusted_proxies_from_env
from eap_api.request_guard import read_capped_body
from eap_api.service_deps.webhooks import WebhookIngestServiceDep
from eap_api.services.errors import InvalidInput, NotFound, ServiceError, TooManyRequests, Unavailable
from eap_api.services.webhook_ingest import VerifiedDelivery

log = logging.getLogger("eap_api.webhooks")

router = APIRouter(tags=["webhooks"])

_ACCEPTED = {"accepted": 202, "duplicate": 200}
_UNSUPPORTED = frozenset({"body_not_utf8"})


def _retry_after(seconds: float | int) -> dict[str, str]:
    return {"Retry-After": str(max(int(seconds) + 1, 1))}


def _uniform(status: int, body: dict[str, Any], headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(status_code=status, content=body, headers=headers)


def _refusal(exc: ServiceError) -> JSONResponse:
    """A service refusal as the sender sees it: fixed messages and, for a 422, locations and keywords only."""
    if isinstance(exc, NotFound):
        return _uniform(404, NOT_FOUND_BODY)
    if isinstance(exc, TooManyRequests):
        return _uniform(429, {"detail": exc.message}, _retry_after(exc.retry_after))
    if isinstance(exc, Unavailable):
        return _uniform(503, {"detail": exc.message}, _retry_after(2))
    if isinstance(exc, InvalidInput):
        body: dict[str, Any] = {"detail": exc.message, "code": exc.code}
        if exc.details is not None:
            body["errors"] = exc.details
        return _uniform(415 if exc.code in _UNSUPPORTED else 422, body)
    return _uniform(500, {"detail": "internal error"})


@router.post(
    "/hooks/{trigger_id}",
    summary="Receive a signed webhook delivery",
    responses={
        200: {"description": "A duplicate of an accepted delivery: no second run."},
        202: {"description": "Accepted: a run was started (the body is `{status, delivery}`)."},
        401: {"description": "`{\"detail\": \"invalid signature\"}` for anything signature related."},
        404: {"description": "`{\"detail\": \"not found\"}` for an unknown or not enabled trigger."},
        413: {"description": "Body over the trigger's cap."},
        415: {"description": "Body is not UTF-8 JSON."},
        422: {"description": "Body or payload invalid (locations and keywords only, never values)."},
        429: {"description": "Rate or concurrency limit; `Retry-After`."},
        503: {"description": "Hand-off failed or a live attempt holds the delivery; `Retry-After`."},
    },
)
async def receive_webhook(
    trigger_id: str, request: Request, guard: WebhookGuardDep, service: WebhookIngestServiceDep
) -> JSONResponse:
    peer = client_ip(request.client.host if request.client else None, request.headers, trusted_proxies_from_env())
    wait = guard.acquire_ip(peer)
    if wait is not None:
        guard.count(None, "rate_limited_ip")
        return _uniform(429, {"detail": "rate limit exceeded"}, _retry_after(wait))
    try:
        gate = await run_in_threadpool(service.gate, trigger_id)
    except NotFound:
        guard.count(None, "not_enabled")
        await run_in_threadpool(dummy_work)
        return _uniform(404, NOT_FOUND_BODY)
    wait = guard.acquire_trigger(gate.trigger_id, gate.rate_per_minute)
    if wait is not None:
        guard.count(gate.trigger_id, "rate_limited")
        return _uniform(429, {"detail": "rate limit exceeded"}, _retry_after(wait))
    body = await read_capped_body(request, gate.max_body_bytes)
    if not isinstance(body, bytes):
        guard.count(gate.trigger_id, "too_large")
        return _uniform(413, {"detail": "request body too large"})
    try:
        principal = await authenticate_webhook(request, gate.trigger_id, body)
    except AuthError as exc:
        guard.count(gate.trigger_id, exc.reason or "unauthenticated")
        return _uniform(404, NOT_FOUND_BODY) if exc.status == 404 else _uniform(401, INVALID_SIGNATURE_BODY)
    extra = principal.extra
    verified = extra["verified"]
    delivery = VerifiedDelivery(
        tenant_id=principal.tenant_id, trigger_id=principal.subject, dedupe_key=extra["dedupe_key"],
        key_kind=extra["key_kind"], secret_slot=verified.secret_slot, skew_seconds=verified.skew_seconds,
        config_hash=extra["config_hash"],
    )
    try:
        result = await run_in_threadpool(
            lambda: service.handle(
                delivery, body, content_type=request.headers.get("content-type"),
                provider_delivery_id=request.headers.get("x-github-delivery"), event_label=request.headers.get("x-github-event"),
            )
        )
    except ServiceError as exc:
        return _refusal(exc)
    except Exception as exc:  # noqa: BLE001 - never an exception text to a public caller
        log_failure(log, "webhook route failed", exc, code="webhook_route_failed", level=logging.ERROR)
        return _uniform(500, {"detail": "internal error"})
    return _uniform(_ACCEPTED[result["status"]], result)
