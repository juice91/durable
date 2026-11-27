"""Calling the platform's services from an MCP tool.

An MCP tool is argument validation plus one call into the service the HTTP route calls too, so there
is exactly one implementation of every operation. The service is built with the collaborators
FastAPI would inject (a session, the run stores, the launcher) and its refusal is turned into the
platform's stable error object `{error: {code, message}}`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from fastapi import HTTPException
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from eap_api.auth import AgUiPrincipal, Principal
from eap_api.concurrency import ConcurrencyError, Precondition
from eap_api.deps import database_engine_of, get_run_launcher, open_session, run_launcher_of, run_stores_of
from eap_api.service_deps.iam import build_access
from eap_api.service_errors import service_error_response
from eap_api.services.access import Access, AccessBound
from eap_api.services.errors import ServiceError
from eap_api.ports import DefinitionError

# One word per HTTP status when a route gives no `code` of its own.
STATUS_CODES = {
    400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found", 409: "conflict",
    413: "payload_too_large", 415: "unsupported", 422: "invalid", 429: "rate_limited", 502: "upstream_error",
    503: "unavailable", 504: "timeout",
}
MAX_MESSAGE_CHARS = 600


class McpToolError(Exception):
    """A refusal the tool reports as `{error: {code, message, ...extra}}` with MCP isError."""

    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message[:MAX_MESSAGE_CHARS]
        self.extra = extra

    @property
    def body(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, **self.extra}}


def _detail_text(detail: Any) -> str:
    """A route's `detail` as one line: a string, or a validation list as `loc: msg` parts (never input values)."""
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list):
        parts = []
        for item in detail[:10]:
            if isinstance(item, dict):
                loc = ".".join(str(p) for p in item.get("loc", ()))
                parts.append(f"{loc}: {item.get('msg', '')}".strip(": "))
            else:
                parts.append(str(item))
        return "; ".join(parts)
    return str(detail)


def refusal(status: int, body: Any) -> McpToolError:
    """The tool error for an HTTP refusal: the body's own `code` when it has one, else by status."""
    if isinstance(body, dict):
        detail = body.get("detail", body.get("message", ""))
        code = body.get("code") if isinstance(body.get("code"), str) else STATUS_CODES.get(status, "error")
        extra = {k: v for k, v in body.items() if k in ("workflows", "decision", "decided_by", "decided_at", "current_revision")}
    else:
        detail, code, extra = body, STATUS_CODES.get(status, "error"), {}
    return McpToolError(code, _detail_text(detail) or code.replace("_", " "), **extra)


def service_error_to_tool_error(exc: ServiceError) -> McpToolError:
    """A service's refusal as a tool error: the code the refusal names (a class's default code is the status's
    word), the message, and for a stale revision `current_revision` (never the current resource)."""
    if isinstance(exc.detail_object, dict):  # a structured `detail` the route always answered: read it as that body
        return refusal(service_error_response(exc).status_code, exc.detail_object)
    response = service_error_response(exc)
    body = json.loads(response.body)
    body.pop("current", None)
    if exc.code == type(exc).code:  # the class's own default: the word of its status, as for every refusal
        body.pop("code", None)
    return refusal(response.status_code, body)


def translate(exc: BaseException) -> McpToolError:
    """Any refusal a service raises, as a tool error."""
    if isinstance(exc, McpToolError):
        return exc
    if isinstance(exc, ServiceError):
        return service_error_to_tool_error(exc)
    if isinstance(exc, HTTPException):
        return refusal(exc.status_code, {"detail": exc.detail} if not isinstance(exc.detail, dict) else exc.detail)
    if isinstance(exc, DefinitionError):
        if isinstance(exc.detail, dict):  # a body whose `detail` is {code, message}, as an HTTPException's dict detail
            return refusal(exc.status_code, exc.detail)
        return refusal(exc.status_code, exc.body)
    if isinstance(exc, ConcurrencyError):  # 412 revision_mismatch (and 428/400): no `current` body, the revision only
        return refusal(exc.status_code, {k: v for k, v in exc.body.items() if k != "current"})
    if isinstance(exc, ValidationError):
        parts = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors(include_input=False, include_url=False)]
        return McpToolError("invalid", "; ".join(parts[:10]) or "invalid input")
    raise exc


@dataclass
class Ctx:
    """What a tool call runs with: the app, the principal, and the route dependencies built from them."""

    app: Any
    principal: AgUiPrincipal

    @property
    def tenant_id(self) -> str:
        return self.principal.tenant_id

    @property
    def actor(self) -> Principal:
        """The tenant-scoped principal the routes take; a key's `created_by` is `apikey:<id>`."""
        user = self.principal.user_id or f"apikey:{self.principal.key_id}"
        return Principal(tenant_id=self.principal.tenant_id, user_id=user)

    @property
    def access(self) -> Access:
        """The caller's access to the policy engine (one per call; the principal's core identity)."""
        return build_access(self.app, self.principal)

    @property
    def stores(self) -> Any:
        return run_stores_of(self.app)

    @property
    def launcher(self) -> Any:
        """The launcher FastAPI would inject (honours a dependency override, as the API's tests install)."""
        override = self.app.dependency_overrides.get(get_run_launcher)
        return override() if override is not None else run_launcher_of(self.app)

    @property
    def engine(self) -> Any:
        return database_engine_of(self.app)

    def agent_allowed(self, agent_id: str) -> bool:
        return self.principal.allows_agent(agent_id)

    def workflow_allowed(self, workflow_id: str) -> bool:
        return self.principal.allows_workflow(workflow_id)


def bind(ctx: "Ctx", service: Any) -> Any:
    """The service with this call's access (per-resource authorisation) when it takes one: the one place the tools
    get it, so no tool can build an unchecked resource service by forgetting to."""
    if isinstance(service, AccessBound):
        service.bind_access(ctx.access)
    return service


async def call_service(ctx: Ctx, build: Callable[[Any], Any], fn: Callable[[Any], Any]) -> Any:
    """Run `fn(service)` in a worker thread with `service = build(session)`, a fresh Session of this call
    (the services take their collaborators through the constructor); return its data or raise McpToolError.
    A `ServiceError` becomes the tool error a route's HTTP refusal gave (service_error_to_tool_error); any
    other refusal (DefinitionError, ConcurrencyError, a validation error) is translated by `translate`."""

    def run() -> Any:
        with open_session(ctx.engine) as db:
            return fn(bind(ctx, build(db)))

    try:
        return await run_in_threadpool(run)
    except McpToolError:
        raise
    except Exception as exc:  # noqa: BLE001 - translate re-raises anything that is not a refusal
        raise translate(exc) from None


async def call_service_async(ctx: Ctx, build: Callable[[Any], Any], fn: Callable[[Any], Awaitable[Any]]) -> Any:
    """As `call_service` for a service method that is a coroutine: awaited on the event loop, with the same session."""
    try:
        with open_session(ctx.engine) as db:
            return await fn(bind(ctx, build(db)))
    except McpToolError:
        raise
    except Exception as exc:  # noqa: BLE001 - translate re-raises anything that is not a refusal
        raise translate(exc) from None


REVISION_MISMATCH = "revision_mismatch"


async def call_with_revision(
    ctx: Ctx, read_revision: Callable[[], int | None], attempt: Callable[[Precondition], Any], what: str,
) -> Any:
    """Optimistic concurrency for a write tool (eap_api.concurrency): read the resource's revision, run
    `attempt(Precondition)` with it as `If-Match`, and on a conflict (somebody saved in between) read again
    and re-apply the same change on the fresh base, once. A second conflict is the tool error
    `{error: {code: "revision_mismatch", message, current_revision}}`: the model reads the resource again
    instead of overwriting what it has not seen."""
    last: McpToolError | None = None
    for _ in (1, 2):
        revision = await run_in_threadpool(read_revision)
        pre = Precondition(revision, None if revision is None else f'"{revision}"')
        try:
            return await attempt(pre)
        except McpToolError as exc:
            if exc.code != REVISION_MISMATCH:
                raise
            last = exc
    assert last is not None
    raise McpToolError(
        REVISION_MISMATCH,
        f"The {what} was changed by someone else while this call ran, and again on the retry. Read it again "
        "before saving, so their change is not lost.",
        current_revision=last.extra.get("current_revision"),
    )


def revision_reader(ctx: Ctx, table: str, row_id: str) -> Callable[[], int | None]:
    """`read_revision` for call_with_revision: the tenant's row revision as the service's `get` reports it (None
    when there is no such row, so the write's own 404 answers). `table` is one of agents, workflows, schedules."""
    import uuid

    from eap_api.services.agents import AgentService
    from eap_api.services.schedules import ScheduleService
    from eap_api.services.workflows import WorkflowService
    from eap_api.deps import schedule_store_of

    def read() -> int | None:
        with open_session(ctx.engine) as db:
            try:
                if table == "agents":
                    _row, revision = AgentService(db).get(ctx.tenant_id, uuid.UUID(str(row_id)))
                elif table == "workflows":
                    _row, revision = WorkflowService(db).get(ctx.tenant_id, str(row_id))
                elif table == "schedules":
                    _row, revision = ScheduleService(db, schedule_store_of(ctx.app)).get(ctx.tenant_id, str(row_id))
                else:
                    raise ValueError(f"no revision reader for {table}")
            except Exception as exc:  # noqa: BLE001 - only a refusal is translated; anything else re-raises
                refused = translate(exc)
                if refused.code == "not_found":
                    return None
                raise refused from None
        return None if revision is None else int(revision)

    return read
