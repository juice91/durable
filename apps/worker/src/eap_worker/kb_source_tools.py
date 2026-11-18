"""The functional tools of the knowledge-base source sync (design: Public tool contracts).

    kb.source.get              read one source (tenant-scoped, non-secret config)
    settings.connection.check  check a connection through its adapter (the shared eap_kb_sources.check)
    kb.source.sync             start a sync: exactly once per (calling run, tool call), through the shared
                               `eap_run_store.kb_source_launch.launch_sync` under `run_side_effect` keyed
                               `{run_id}:{tool_call_id}`; the sync's run id is `tool_run_id(...)`, so a replay
                               names the same run
    workflow.run.wait          cooperative polling of the run row (asyncio.sleep, 3 s apart), at most 300 s; answers
                               `{status: "cancelled", terminal: true}` at once when the calling workflow run has
                               a `cancel` event (the parent was cancelled: the sync is being cancelled with it)
    workflow.run.result        the bounded report of a sync run, with stable error codes for `require_success`
                               (`run_cancelled` also when the calling workflow run was cancelled)

A refusal is a result `{"error": {"code", "message"}}`, never an exception text: the codes are stable and carry
nothing a provider, a database or a credential said. Every read is scoped by `ctx.deps.tenant_id`; another
tenant's source, connection or run is `*_not_found`. When the calling run is a workflow run, `wait` and `result`
refuse a sync run started by another run (`run_not_found`), so a workflow only sees its own children.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, Literal

from pydantic import Field
from pydantic_ai import RunContext

from eap_kb_sources.check import check_connection
from eap_kb_sources.errors import CONNECTION_NOT_FOUND, SOURCE_NOT_FOUND, SourceError
from eap_run_store import CANCEL_EVENT_KEY, RunStore
from eap_run_store.kb_source_launch import (
    LaunchFailed,
    SourceLaunchError,
    launch_sync,
    source_view,
    tool_run_id,
)
from eap_run_store.kb_sources import SourceStore, SyncStore
from eap_run_store.store import TERMINAL_RUN_STATUSES
from eap_runtime import RunDeps, ToolRegistry, run_side_effect
from eap_schemas.tools import TOOL_DECLARATIONS
from eap_secrets import SecretsError

from eap_worker.config import QUEUE_NAME

log = logging.getLogger("eap_worker.kb_source_tools")

TOOL_IDS = ("kb.source.get", "settings.connection.check", "kb.source.sync", "workflow.run.wait", "workflow.run.result")
WORKFLOW_NAME = "eap.kb_source_sync"
POLL_SECONDS = 3.0  # between reads of the run row inside workflow.run.wait (2 to 5 s)
MAX_REPORT_ERRORS = 50
RUN_NOT_FOUND = "run_not_found"
SYNC_FAILED = "sync_failed"
SYNC_NEEDS_ATTENTION = "sync_needs_attention"
RUN_CANCELLED = "run_cancelled"
SYNC_NOT_FINISHED = "sync_not_finished"
ENQUEUE_FAILED = "enqueue_failed"


def _error(code: str, message: str = "") -> dict[str, Any]:
    return {"error": {"code": code, "message": message or code.replace("_", " ")}}


def _engine() -> Any:
    from eap_worker.runtime import get_runtime

    return get_runtime().engine


def _uuid(value: str) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        return None


# ------------------------------------------------------------------------------------ the enqueue


def _launch_kb_source_sync(
    *, run_id: uuid.UUID, tenant_id: str, source_id: str, request_json: str, traceparent: str | None = None
) -> None:
    """Enqueue `eap.kb_source_sync(run_id, tenant_id, source_id, request_json)` on the agent-run queue, workflow
    id = run id, exactly as the API's launcher does (a repeated enqueue attaches to the existing workflow)."""
    from eap_observability import dbos_trace_attributes
    from eap_worker.kb_source_sync import _dbos_client
    from eap_worker.runtime import get_runtime

    options: dict[str, Any] = {
        "queue_name": QUEUE_NAME,
        "workflow_name": WORKFLOW_NAME,
        "workflow_id": str(run_id),
        "app_version": get_runtime().settings.app_version,
    }
    attributes = dbos_trace_attributes(traceparent)
    if attributes is not None:
        options["attributes"] = attributes
    _dbos_client().enqueue(options, str(run_id), tenant_id, str(source_id), request_json)


class _Launcher:
    def launch_kb_source_sync(self, **kw: Any) -> None:
        _launch_kb_source_sync(**kw)  # looked up at call time (tests replace it)


# -------------------------------------------------------------------------------------- the tools


async def kb_source_get(ctx: RunContext[RunDeps], source_id: str) -> dict[str, Any]:
    """Read one knowledge-base source of the tenant."""
    sid = _uuid(source_id)
    source = await asyncio.to_thread(SourceStore(_engine()).get, ctx.deps.tenant_id, sid) if sid else None
    if source is None:
        return _error(SOURCE_NOT_FOUND, "source not found")
    return source_view(source)


async def settings_connection_check(
    ctx: RunContext[RunDeps], connection_id: str, require_ok: bool = False
) -> dict[str, Any]:
    """Check a connection; `{ok, checked_at, error_code}` (a stable code, never a credential). With
    `require_ok` a check that is not ok is `{error: {code: <error_code or connection_failed>, message}}`."""
    from eap_worker.secret_refs import secret_store

    cid = _uuid(connection_id)
    if cid is None:
        return _error(CONNECTION_NOT_FOUND, "connection not found")
    engine = _engine()
    try:
        secrets = secret_store(engine)
    except SecretsError:
        return _error("secret_unreadable", "the secret store is not available")
    try:
        checked = await check_connection(engine, ctx.deps.tenant_id, cid, secrets)
    except SourceError as exc:
        if exc.code == CONNECTION_NOT_FOUND:
            return _error(CONNECTION_NOT_FOUND, "connection not found")
        raise
    if require_ok and not checked.get("ok"):
        failed_code = str(checked.get("error_code") or "connection_failed")
        return _error(failed_code, f"the connection check failed ({failed_code})")
    return checked


def sync_summary(args: dict[str, Any]) -> str:
    """The approval card's wording from the arguments alone (the source's name is added by
    `approval_summary_text`, which can read it)."""
    return f"Start a {args.get('mode')} sync of source '{args.get('source_id')}'"


def approval_summary_text(engine: Any, tenant_id: str, args: dict[str, Any]) -> str | None:
    """`Start a <mode> sync of source '<name>'`, with the name read now (blocking); None when it cannot be read."""
    sid = _uuid(str(args.get("source_id")))
    source = SourceStore(engine).get(tenant_id, sid) if sid else None
    if source is None:
        return None
    return f"Start a {args.get('mode')} sync of source '{source.name}'"


async def kb_source_sync(
    ctx: RunContext[RunDeps], source_id: str, mode: Literal["targeted", "incremental", "full"],
    part_numbers: list[str] = [],  # noqa: B006 - matches the declaration's default
    restart: bool = False,
) -> dict[str, Any]:
    """Start a sync of a knowledge-base source; `{run_id, status: "queued"}`. Once per (calling run, tool call)."""
    if ctx.tool_call_id is None:
        raise ValueError("tool_call_id is required for kb.source.sync")
    tenant_id, parent = ctx.deps.tenant_id, str(ctx.deps.run_id)
    sid = _uuid(source_id)
    if sid is None:
        return _error(SOURCE_NOT_FOUND, "source not found")
    run_id = tool_run_id(tenant_id, sid, parent, ctx.tool_call_id)
    key = f"{parent}:{ctx.tool_call_id}"
    parts = list(part_numbers)

    def call() -> dict[str, Any]:
        try:
            return launch_sync(
                _engine(), _Launcher(), tenant_id=tenant_id, source_id=sid, mode=mode, part_numbers=parts, restart=restart,
                run_id=run_id, parent_run_id=parent, created_by="tool",
            )
        except SourceLaunchError as exc:  # a deterministic refusal: recorded, so a replay answers the same
            return _error(exc.code, exc.message)

    try:
        result, _performed = await asyncio.to_thread(
            run_side_effect, ctx.deps.side_effects, key, call, external_idempotent=True
        )
    except LaunchFailed:
        # The run is recorded (failed hand-off) and the reservation stays open: the same call enqueues it again.
        return _error(ENQUEUE_FAILED, "the sync was recorded but could not be started; retry the call")
    return result


def _calling_run_is_workflow(engine: Any, tenant_id: str, run_id: Any) -> bool:
    run = RunStore(engine).get_run(tenant_id, run_id) if run_id is not None else None
    return run is not None and run.get("kind") == "workflow"


def _visible_sync(engine: Any, tenant_id: str, caller: Any, run_id: str) -> Any:
    """The sync row of `run_id` for this caller, or None. Inside a workflow run only its own children."""
    sync = SyncStore(engine).get_by_run(tenant_id, run_id)
    if sync is not None and _calling_run_is_workflow(engine, tenant_id, caller) and sync.parent_run_id != str(caller):
        return None
    return sync


def _parent_cancelled(tenant_id: str, caller: Any, sync: Any) -> bool:
    """Whether `sync` was started by the calling run and that run has a `cancel` event (POST /runs/{caller}/cancel
    writes it before anything else, so the sync is cancelled even while its own end is still being written)."""
    if sync is None or caller is None or sync.parent_run_id != str(caller):
        return False
    from eap_worker.runtime import get_runtime

    event = get_runtime().event_log.get_event(tenant_id, caller, CANCEL_EVENT_KEY)
    return event is not None and event.type == "cancel_requested"


def _poll(engine: Any, tenant_id: str, caller: Any, run_id: str) -> dict[str, Any] | None:
    run = RunStore(engine).get_run(tenant_id, uuid.UUID(run_id))
    if run is None:
        return None
    sync = SyncStore(engine).get_by_run(tenant_id, run_id)
    if sync is not None and _visible_sync(engine, tenant_id, caller, run_id) is None:
        return None
    updated = run["updated_at"]
    terminal = run["status"] in TERMINAL_RUN_STATUSES
    cancelled = not terminal and _parent_cancelled(tenant_id, caller, sync)
    return {
        "status": "cancelled" if cancelled else run["status"],
        "updated_at": updated.isoformat() if updated is not None else None,
        "terminal": terminal or cancelled,
    }


async def workflow_run_wait(
    ctx: RunContext[RunDeps], run_id: str, wait_seconds: int = Field(default=30, ge=1, le=300)  # noqa: B008
) -> dict[str, Any]:
    """Wait up to `wait_seconds` for a run of the tenant to end (cooperative polling of its row)."""
    rid = _uuid(run_id)
    if rid is None:
        return _error(RUN_NOT_FOUND, "run not found")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + int(wait_seconds)
    while True:
        view = await asyncio.to_thread(_poll, _engine(), ctx.deps.tenant_id, ctx.deps.run_id, rid)
        if view is None:
            return _error(RUN_NOT_FOUND, "run not found")
        remaining = deadline - loop.time()
        if view["terminal"] or remaining <= 0:
            return view
        await asyncio.sleep(min(POLL_SECONDS, remaining))


def _cancelled(run: dict[str, Any], sync: Any, report: dict[str, Any]) -> bool:
    return (
        sync.status == "cancelled"
        or report.get("error_code") == RUN_CANCELLED
        or RUN_CANCELLED in str(run.get("error") or "")
    )


def _report(run: dict[str, Any], sync: Any) -> dict[str, Any]:
    report: dict[str, Any] = {}
    if run["status"] in TERMINAL_RUN_STATUSES and run.get("output"):
        try:
            value = json.loads(run["output"])
            report = value if isinstance(value, dict) else {}
        except (TypeError, ValueError):
            report = {}
    counters = sync.counters or {}

    def num(name: str) -> int:
        value = report.get(name, counters.get(name, 0))
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    status = "cancelled" if run["status"] in TERMINAL_RUN_STATUSES and _cancelled(run, sync, report) else run["status"]
    errors = report.get("errors")
    out: dict[str, Any] = {
        "run_id": str(run["id"]),
        "status": status,
        "source_id": sync.source_id,
        "mode": sync.mode,
        "discovered": num("discovered"),
        "inserted": num("inserted"),
        "updated": num("updated"),
        "unchanged": num("unchanged"),
        "documents_queued": num("documents_queued"),
        "failed": num("failed"),
        "checkpoint": report.get("checkpoint") if isinstance(report.get("checkpoint"), dict) else {},
        "errors": (errors if isinstance(errors, list) else list(sync.bounded_errors or []))[:MAX_REPORT_ERRORS],
    }
    for name in ("complete", "stop_reason", "error_code", "removed", "budget", "source_revision"):
        if name in report:
            out[name] = report[name]
    return out


def _ended_cancelled(tenant_id: str, run_id: str) -> bool:
    from eap_worker.runtime import get_runtime

    end = get_runtime().event_log.get_event(tenant_id, uuid.UUID(run_id), "end")
    return end is not None and (end.data or {}).get("error_code") == RUN_CANCELLED


def _result(engine: Any, tenant_id: str, caller: Any, run_id: str, require_success: bool) -> dict[str, Any]:
    run = RunStore(engine).get_run(tenant_id, uuid.UUID(run_id))
    sync = _visible_sync(engine, tenant_id, caller, run_id)
    if run is None or sync is None:
        return _error(RUN_NOT_FOUND, "run not found")
    out = _report(run, sync)
    if run["status"] in TERMINAL_RUN_STATUSES and out["status"] != "cancelled" and _ended_cancelled(tenant_id, run_id):
        out["status"] = "cancelled"  # a cancel from the API ends the run `failed` with `end.error_code = run_cancelled`
    if run["status"] not in TERMINAL_RUN_STATUSES and _parent_cancelled(tenant_id, caller, sync):
        out["status"] = "cancelled"  # the parent was cancelled: this sync is being cancelled with it
        if require_success:
            return _error(RUN_CANCELLED, "the sync was cancelled")
    if require_success:
        if run["status"] not in TERMINAL_RUN_STATUSES:
            return _error(SYNC_NOT_FINISHED, "the sync has not finished")
        if out["status"] == "cancelled":
            return _error(RUN_CANCELLED, "the sync was cancelled")
        if run["status"] == "needs_attention":
            return _error(SYNC_NEEDS_ATTENTION, "the sync needs attention")
        if run["status"] == "failed":
            return _error(SYNC_FAILED, "the sync failed")
    return out


async def workflow_run_result(ctx: RunContext[RunDeps], run_id: str, require_success: bool = False) -> dict[str, Any]:
    """The bounded report of a sync run."""
    rid = _uuid(run_id)
    if rid is None:
        return _error(RUN_NOT_FOUND, "run not found")
    return await asyncio.to_thread(_result, _engine(), ctx.deps.tenant_id, ctx.deps.run_id, rid, bool(require_success))


def register_kb_source_tools(registry: ToolRegistry) -> None:
    funcs = {
        "kb.source.get": kb_source_get,
        "settings.connection.check": settings_connection_check,
        "kb.source.sync": kb_source_sync,
        "workflow.run.wait": workflow_run_wait,
        "workflow.run.result": workflow_run_result,
    }
    for tool_id in TOOL_IDS:
        registry.register(
            TOOL_DECLARATIONS[tool_id], funcs[tool_id],
            approval_summary=sync_summary if tool_id == "kb.source.sync" else None,
        )


__all__ = ["POLL_SECONDS", "TOOL_IDS", "approval_summary_text", "register_kb_source_tools", "sync_summary"]
