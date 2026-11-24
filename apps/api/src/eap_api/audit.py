"""Audit of mutating calls (design section 9): one `audit_log` row per mutating API call, written by an ASGI middleware
so no route can skip it, and one per mutating MCP control tool call (`record_mcp_call`).

What a row holds: who (the principal the identity chain produced: kind, subject, on-behalf-of), what (the route's
function name, e.g. `approve_run`), on which resource (the route's first path id), the outcome and the request id,
plus the method, route template and status in `detail`. Never a body, a header, a query string, a key or a token:
`detail` goes through the store's allowlist.

Written when the response starts, so the status is known and a long stream does not delay it. A call that did not
authenticate has no principal and no row (a flood of anonymous 401s must not grow the table). A failure to write is
logged (without the row) and never fails the request: the change itself already happened or was refused."""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any

import anyio
from eap_run_store import AuditEntry, AuditStore
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from eap_api.deps import audit_store_of

log = logging.getLogger("eap_api.audit")

MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# Not changes of the platform's data: telemetry ingest and the chat / run transports (every run row names its actor).
SKIPPED_PREFIXES = ("/api/v1/otlp", "/api/v1/ag-ui")
API_PREFIX = "/api/v1/"
REQUEST_ID_HEADER = b"x-request-id"
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


def outcome_for(status: int) -> str:
    if status < 400:
        return "success"
    return "denied" if status in (401, 403) else "failed"


def _request_id(scope: Scope) -> str:
    for name, value in scope.get("headers", []):
        if name == REQUEST_ID_HEADER:
            text = value.decode("latin-1")
            if _SAFE_REQUEST_ID.match(text):
                return text
    return uuid.uuid4().hex


def _resource(path: str, params: dict[str, Any]) -> tuple[str | None, str | None, str]:
    """`/api/v1/agents/<id>/versions` -> ("agents", the id, "/api/v1/agents/{agent_id}/versions"): the first path segment
    after the prefix is the resource kind, the first path parameter the resource id, and the template is the path with
    each parameter value put back as its name (no value of the call is ever kept)."""
    segments = [s for s in path[len(API_PREFIX):].split("/") if s]
    kind = segments[0] if segments else None
    resource_id = next((str(v) for v in params.values()), None)
    template = path
    for name, value in params.items():
        template = template.replace(str(value), "{" + name + "}", 1)
    return kind, (resource_id[:200] if resource_id else None), template[:200]


class AuditMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if (
            scope["type"] != "http"
            or scope.get("method") not in MUTATING
            or not path.startswith(API_PREFIX)
            or path.startswith(SKIPPED_PREFIXES)
        ):
            await self.app(scope, receive, send)
            return
        request_id = _request_id(scope)
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_audit(message: Message) -> None:
            if message["type"] == "http.response.start":
                await self._record(scope, int(message["status"]), request_id)
                message = {**message, "headers": [*message.get("headers", []), (REQUEST_ID_HEADER, request_id.encode())]}
            await send(message)

        await self.app(scope, receive, send_with_audit)

    async def _record(self, scope: Scope, status: int, request_id: str) -> None:
        principal = scope.get("state", {}).get("iam_principal")
        route = scope.get("route")
        if principal is None or route is None:
            return
        params = scope.get("path_params") or {}
        kind, resource_id, template = _resource(scope["path"], params)
        actor_kind, actor_subject = principal.kind, principal.subject
        extra = getattr(principal, "extra", None) or {}
        if principal.kind == "api_key" and extra.get("owner"):  # a personal key: the person is the actor
            actor_kind, actor_subject = "user", str(extra["owner"])
        entry = AuditEntry(
            tenant_id=principal.tenant_id,
            actor_kind=actor_kind,
            actor_subject=actor_subject,
            on_behalf_of=principal.on_behalf_of,
            action=route.name,
            outcome=outcome_for(status),
            resource_kind=kind,
            resource_id=resource_id,
            request_id=request_id,
            run_id=str(params["run_id"]) if "run_id" in params else None,
            detail=_detail(principal, scope["method"], template, status),
        )
        await write_audit(scope["app"], entry)


def _detail(principal: Any, method: str, template: str, status: int) -> dict[str, Any]:
    detail: dict[str, Any] = {"method": method, "route": template, "status": status}
    if principal.kind == "api_key" and (getattr(principal, "extra", None) or {}).get("owner"):
        detail["via_api_key"] = principal.subject  # a personal key: its id (the non-secret part of the prefix)
    # A subject that is a stable id (zitadel:<sub>) cannot be read by a person: the e-mail at the time rides along so the
    # audit view can show it. A subject that already is the e-mail (cfaccess rows) needs nothing.
    email = (getattr(principal, "extra", None) or {}).get("email")
    if isinstance(email, str) and email and email.lower() != principal.subject.lower():
        detail["actor_email"] = email.lower()
    return detail


async def write_audit(app: Any, entry: AuditEntry) -> None:
    store: AuditStore = audit_store_of(app)
    try:
        await anyio.to_thread.run_sync(store.record, entry)
    except Exception:  # noqa: BLE001 - never fail the request over its log; the row is not logged (it names people)
        log.exception("audit_log write failed for action %s", entry.action)


async def record_mcp_change(ctx: Any, tool: str, outcome: str) -> None:
    """A mutating MCP control tool call (a `manage` tool): who (the key, the Architect run or the person), which tool,
    the outcome. Never the arguments or the result (`eap_api.mcp_control.audit` keeps the same rule)."""
    principal = ctx.principal
    detail: dict[str, Any] = {"tool": tool}
    if principal.kind == "api_key" and principal.owner_subject:
        kind, subject = "user", principal.owner_subject
        detail["via_api_key"] = principal.key_id
    elif principal.kind == "api_key":
        kind, subject = "api_key", str(principal.key_id)
    elif principal.kind == "architect_run":
        kind, subject = "run", str(principal.user_id)
    else:
        kind, subject = "user", str(principal.user_id)
    await write_audit(
        ctx.app,
        AuditEntry(
            tenant_id=ctx.tenant_id, actor_kind=kind, actor_subject=subject, action=f"mcp.{tool}", outcome=outcome,
            resource_kind="mcp", detail=detail,
        ),
    )
