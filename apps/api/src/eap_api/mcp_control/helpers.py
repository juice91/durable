"""Small shared pieces of the tool bodies: ids, paging, bounded text, the bounded wait for a run."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any


from eap_api.mcp_control.routing import Ctx, McpToolError, call_service
from eap_api.deps import approval_store_of, schedule_store_of
from eap_api.ports import TERMINAL_RUN_STATUSES
from eap_api.service_deps.runs import build_run_service
from eap_api.services.approvals import ApprovalService
from eap_api.services.schedules import ScheduleService
from eap_api.services.workflow_runs import WorkflowRunService

MAX_PAGE = 100
DEFAULT_PAGE = 50
MAX_REPORT_BYTES = 32 * 1024  # a run's input/output as a tool or resource result
MAX_WAIT_SECONDS = 300
POLL_SECONDS = 0.5


def uid(value: str, what: str = "id") -> uuid.UUID:
    """A UUID argument, or the stable `invalid` error naming the argument (an unknown id is not_found, later)."""
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise McpToolError("invalid", f"{what} must be a UUID") from None


def page(items: list[Any], limit: int, offset: int) -> dict[str, Any]:
    """`{items, total, next_offset}`: lists are paged, at most MAX_PAGE items."""
    limit = max(1, min(int(limit), MAX_PAGE))
    offset = max(0, int(offset))
    chunk = items[offset : offset + limit]
    more = offset + len(chunk) < len(items)
    return {"items": chunk, "total": len(items), "next_offset": offset + len(chunk) if more else None}


def clip(text: str | None, limit: int = MAX_REPORT_BYTES) -> tuple[str | None, bool]:
    """`text` cut to `limit` UTF-8 bytes, and whether it was cut."""
    if text is None:
        return None, False
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text, False
    return raw[:limit].decode("utf-8", errors="ignore"), True


MAX_SPEC_BYTES = 256 * 1024  # the shared request body cap (eap_api.request_guard.DEFAULT_MAX_BODY_BYTES)


def guard_json(value: Any, what: str, *, max_bytes: int = MAX_SPEC_BYTES, max_depth: int = 32) -> None:
    """The write routes' body guards for a JSON argument: size, nesting depth and strings that cannot be
    stored (NUL, lone surrogates), refused as `invalid` before anything is validated or stored."""
    from eap_api.request_guard import json_depth_exceeds, unstorable_strings

    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        text.encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError):
        raise McpToolError("invalid", f"{what} is not storable JSON") from None
    if len(text.encode("utf-8")) > max_bytes:
        raise McpToolError("payload_too_large", f"{what} must be at most {max_bytes // 1024} KiB as JSON")
    if json_depth_exceeds(text, max_depth):
        raise McpToolError("invalid", f"{what} is nested more than {max_depth} levels deep")
    if unstorable_strings(value, (what,)):
        raise McpToolError("invalid", f"{what} holds a string that cannot be stored")


def require_unrestricted(ctx: Ctx, what: str) -> None:
    """A key limited to listed agents or workflows cannot create new ones."""
    if not ctx.principal.is_unrestricted:
        raise McpToolError("forbidden", f"this key is restricted to specific agents and workflows and cannot {what}")


def not_found(what: str) -> McpToolError:
    return McpToolError("not_found", f"{what} not found")


def require_agent(ctx: Ctx, agent_id: str) -> None:
    """A key restricted to other agents sees them as it sees an unknown one (404, no probing)."""
    if not ctx.agent_allowed(agent_id):
        raise not_found("Agent")


def require_workflow(ctx: Ctx, workflow_id: str) -> None:
    if not ctx.workflow_allowed(workflow_id):
        raise not_found("Workflow")


def run_service(ctx: Ctx) -> Any:
    """`build` for call_service: a RunService on this call's session."""
    return lambda db: build_run_service(db, ctx.stores[0], ctx.stores[1], ctx.launcher)


def schedules(ctx: Ctx) -> Any:
    return lambda db: ScheduleService(db, schedule_store_of(ctx.app))


def approvals(ctx: Ctx) -> Any:
    return lambda _db: ApprovalService(approval_store_of(ctx.app), ctx.stores[1], ctx.launcher, db=_db)


def workflow_runs(ctx: Ctx) -> Any:
    return lambda db: WorkflowRunService(db, *ctx.stores, ctx.launcher)


async def tenant_run(ctx: Ctx, run_id: str, *, kind: str | None = None) -> dict[str, Any]:
    """The tenant's run row (another tenant's is not_found), optionally of one kind; a key restricted by
    allowlists sees only runs of its agents and workflows."""
    rid = uid(run_id, "run_id")
    run = await call_service(ctx, run_service(ctx), lambda s: s.read_run(ctx.tenant_id, rid))
    if run is None or (kind is not None and (run.get("kind") or "agent") != kind):
        raise not_found("Run")
    if run.get("kind") == "workflow" and run.get("workflow_id") and not ctx.workflow_allowed(str(run["workflow_id"])):
        raise not_found("Run")
    if (run.get("kind") or "agent") == "agent" and run.get("agent_id") and not ctx.agent_allowed(str(run["agent_id"])):
        raise not_found("Run")
    return run


async def wait_for_run(ctx: Ctx, run_id: str, timeout_seconds: float, *, kind: str | None = None) -> tuple[dict[str, Any], bool]:
    """Poll until the run ends or waits for an approval, at most `timeout_seconds` (<= MAX_WAIT_SECONDS).
    Returns the row and whether it settled (terminal or waiting_approval)."""
    timeout = max(0.0, min(float(timeout_seconds), MAX_WAIT_SECONDS))
    deadline = time.monotonic() + timeout
    while True:
        run = await tenant_run(ctx, run_id, kind=kind)
        if run["status"] in TERMINAL_RUN_STATUSES or run.get("display_status") == "waiting_approval":
            return run, True
        if time.monotonic() >= deadline:
            return run, False
        await asyncio.sleep(min(POLL_SECONDS, max(0.0, deadline - time.monotonic())))
