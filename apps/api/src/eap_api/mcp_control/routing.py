"""Calling the platform's route functions from an MCP tool.

An MCP tool is argument validation plus one call into what the HTTP route already runs (the route
function itself, or the shared service the route calls), so there is exactly one implementation of
every operation. The route function is called with the dependencies FastAPI would inject (a
tenant-scoped `Principal`, a session, the run stores, the launcher) and its answer is turned into
the platform's stable error object `{error: {code, message}}` when it is a refusal.
"""

from __future__ import annotations

import inspect
import json
from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable, Iterator

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from eap_api.auth import AgUiPrincipal, Principal
from eap_api.concurrency import ConcurrencyError, Precondition
from eap_api.launcher import get_run_launcher
from eap_run_store.definition_errors import DefinitionError

# One word per HTTP status when a route gives no `code` of its own.
STATUS_CODES = {
    400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found", 409: "conflict",
    413: "payload_too_large", 415: "unsupported", 422: "invalid", 429: "rate_limited", 502: "upstream_error",
    503: "unavailable", 504: "timeout",
}
MAX_MESSAGE_CHARS = 600

DB = object()  # a keyword value replaced by a fresh Session inside the call's thread


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


def unwrap(result: Any) -> Any:
    """A route's return value as data: JSONResponse bodies parsed, a refusal status raised."""
    if isinstance(result, JSONResponse):
        data = json.loads(result.body) if result.body else None
        if result.status_code >= 400:
            raise refusal(result.status_code, data)
        return data
    if hasattr(result, "model_dump"):
        return result.model_dump(mode="json")
    return result


def translate(exc: BaseException) -> McpToolError:
    """Any refusal a route or shared service raises, as a tool error."""
    if isinstance(exc, McpToolError):
        return exc
    if isinstance(exc, HTTPException):
        return refusal(exc.status_code, {"detail": exc.detail} if not isinstance(exc.detail, dict) else exc.detail)
    if isinstance(exc, DefinitionError):
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
    def request(self) -> Any:
        """Stands in for the `Request` of routes that only read `request.app.state`."""
        return SimpleNamespace(app=self.app)

    @property
    def stores(self) -> Any:
        return self.app.state.run_stores

    @property
    def launcher(self) -> Any:
        """The launcher FastAPI would inject (honours a dependency override, as the API's tests install)."""
        override = self.app.dependency_overrides.get(get_run_launcher)
        return override() if override is not None else self.app.state.run_launcher

    @property
    def engine(self) -> Any:
        return self.app.state.database_engine

    def agent_allowed(self, agent_id: str) -> bool:
        return self.principal.allows_agent(agent_id)

    def workflow_allowed(self, workflow_id: str) -> bool:
        return self.principal.allows_workflow(workflow_id)


@contextmanager
def _sessions(ctx: Ctx, kwargs: dict[str, Any]) -> Iterator[dict[str, Any]]:
    session: Session | None = None
    out = dict(kwargs)
    try:
        for key, value in kwargs.items():
            if value is DB:
                session = session or Session(ctx.engine)
                out[key] = session
        yield out
    finally:
        if session is not None:
            session.close()


async def call(ctx: Ctx, fn: Callable[..., Any], **kwargs: Any) -> Any:
    """Run the route function `fn` (sync or async) with `kwargs`, `DB` values replaced by a session of
    this call; return its data or raise McpToolError."""
    try:
        if inspect.iscoroutinefunction(fn):
            with _sessions(ctx, kwargs) as kw:
                return unwrap(await fn(**kw))

        def run() -> Any:
            with _sessions(ctx, kwargs) as kw:
                return unwrap(fn(**kw))

        return await run_in_threadpool(run)
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
    """`read_revision` for call_with_revision: the tenant's row revision (None when there is no such row, so
    the route's own 404 answers). `table` is a literal of ours (agents, workflows)."""
    from sqlalchemy import text

    def read() -> int | None:
        with ctx.engine.connect() as conn:
            value = conn.execute(
                text(f"SELECT revision FROM {table} WHERE id = :i AND tenant_id = :t"),
                {"i": str(row_id), "t": ctx.tenant_id},
            ).scalar_one_or_none()
        return None if value is None else int(value)

    return read
