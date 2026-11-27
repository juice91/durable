"""The door of the external agents' calls (`/api/v1/external/mcp` and the A2A JSON-RPC interface): protocol checks before
a protocol server sees a request, cheapest first. A surface answers its own switch (404) first, then asks `enter`:

1. the per-address bucket, before any credential is read -> 429;
2. a `run` API key through the identity chain, and nothing else (no user token, dev bearer or run token): 401 with one
   fixed body, 403 for another scope or a foreign origin. Unauthenticated noise writes no database row;
3. the body, read capped at 64 KiB -> 413;
4. the per-key bucket: anything over it is 429, except a message the surface answers itself (`soft_limited`), which
   goes on flagged;
5. `ExternalRunService.admit`: the client is enabled (403, audited), and the A2A endpoint is the key's tenant's own
   (404). Behind the key's rate, so that a refused key cannot fill the audit log faster than that.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from eap_iam import ACTION_RUN_EXTERNAL, AuthError, Principal, RequestView
from eap_observability import bind_log_context
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse

from eap_api.deps import database_engine_of, external_guard_of, iam_of, open_session
from eap_api.external_settings import ExternalSettings
from eap_api.inbound_guard import client_ip, trusted_proxies_from_env
from eap_api.request_guard import read_capped_body
from eap_api.service_deps.external_access import external_run_service_of
from eap_api.services.errors import Forbidden, NotFound

log = logging.getLogger("eap_api.external")

MAX_BODY_BYTES = 64 * 1024
MAX_LABEL_CHARS = 80
KEY_KINDS = frozenset({"api_key"})


@dataclass(frozen=True)
class Entered:
    principal: Principal  # `extra["agent"]`: the caller's own name for itself, a label for the audit log
    body: bytes
    retry_after: int | None  # set when the key is over its rate and the surface answers that itself


def printable_label(declared: str | None) -> str | None:
    """A name the caller declares for itself: bounded, printable, never trusted."""
    text = "".join(c for c in (declared or "") if c.isprintable())
    return text[:MAX_LABEL_CHARS] or None


def retry_seconds(wait: float) -> int:
    return max(int(wait) + 1, 1)


def refusal(status: int, code: str, message: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status, headers=headers)


def rate_limited(wait: float) -> JSONResponse:
    return refusal(429, "rate_limited", "rate limit exceeded", {"Retry-After": str(retry_seconds(wait))})


def not_found() -> JSONResponse:
    """What a surface that is off answers: what an unknown route does."""
    return JSONResponse({"detail": "Not Found"}, status_code=404)


def caller_address(request: Request) -> str:
    peer = request.client.host if request.client else None
    return client_ip(peer, request.headers, trusted_proxies_from_env())


async def enter(
    app: Any,
    request: Request,
    settings: ExternalSettings,
    *,
    label: Callable[[Any, str | None], str | None],
    soft_limited: Callable[[Any], bool] = lambda message: False,
    endpoint_id: str | None = None,
) -> Entered | JSONResponse:
    """The refusal, or what entered. `label(message, user_agent)` names the caller for the audit log; `message` is the
    parsed body, or None when it is no JSON (the surface answers that)."""
    guard = external_guard_of(app, settings.ip_rate)
    wait = guard.acquire_ip(caller_address(request))
    if wait is not None:
        guard.count(None, "rate_limited_ip")
        return rate_limited(wait)
    view = RequestView(
        headers={name.lower(): value for name, value in request.headers.items()},
        peer_host=request.client.host if request.client else None, accepts=KEY_KINDS, action=ACTION_RUN_EXTERNAL,
    )
    try:
        principal = await iam_of(app).chain.authenticate(view)
    except AuthError as exc:
        guard.count(None, "unauthenticated")
        if exc.status == 401:
            return refusal(401, "unauthorized", "a run key is required", {"WWW-Authenticate": "Bearer"})
        return refusal(exc.status, "forbidden" if exc.status == 403 else "unavailable", exc.detail)
    bind_log_context(tenant_id=principal.tenant_id)
    body = await read_capped_body(request, MAX_BODY_BYTES)
    if not isinstance(body, bytes):
        return refusal(413, "payload_too_large", f"request body too large (max {MAX_BODY_BYTES} bytes)")
    try:
        message = json.loads(body) if body else None
    except (ValueError, RecursionError):
        message = None
    principal = principal.model_copy(
        update={"extra": {**principal.extra, "agent": label(message, request.headers.get("user-agent"))}}
    )
    wait = guard.acquire_key(f"{principal.tenant_id}:{principal.subject}", settings.key_rate)
    if wait is not None:
        if not soft_limited(message):
            return rate_limited(wait)
        return Entered(principal, body, retry_seconds(wait))

    def admit() -> None:
        with open_session(database_engine_of(app)) as session:
            external_run_service_of(app, session).admit(principal, endpoint_id)

    try:
        await run_in_threadpool(admit)
    except Forbidden as exc:
        return refusal(403, exc.code, exc.message)
    except NotFound:
        return not_found()
    return Entered(principal, body, None)
