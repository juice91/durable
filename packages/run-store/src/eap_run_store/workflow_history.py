"""What a workflow's own recent runs returned: the data behind the `workflow.history` tool.

One read over `runs`, scoped by the tenant and the CALLING run's workflow (any version of it), never the
calling run itself. It uses the existing index `ix_runs_tenant_workflow_created` (tenant_id, workflow_id,
created_at): the rows come newest first from the index, so only the window is read. A run's output is JSON text
(`workflow.return`'s value); `where`, `select` and `distinct` are applied here, in Python, over at most
`MAX_SCAN` rows, so an output that is not valid JSON can never fail the query.

Blocking SQL: call it from a thread.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

HISTORY_STATUSES = ("completed", "skipped", "failed")
MAX_SCAN = 5000  # rows read at most, newest first
MAX_RESULT_BYTES = 30 * 1024  # a step's output is capped at 32 KiB; stay under it

_CALLER = text("SELECT workflow_id FROM runs WHERE tenant_id = :t AND id = :r AND kind = 'workflow'")
_RUNS = text(
    "SELECT output FROM runs WHERE tenant_id = :t AND workflow_id = :wf AND kind = 'workflow' "
    "AND parent_run_id IS NULL AND id <> :r AND status = ANY(:st) AND created_at >= :since "
    "AND output IS NOT NULL ORDER BY created_at DESC, id DESC LIMIT :n"
)


class NotAWorkflowRun(LookupError):
    """The calling run is not a workflow run of this tenant (no workflow to read the history of)."""


def _parse(raw: str) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return raw  # an output that is not JSON stays the text it is


def _same(a: Any, b: Any) -> bool:
    """JSON equality that keeps a boolean apart from a number (True is not 1)."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    return type(a) is type(b) and a == b


def _pick(value: Any, path: str) -> tuple[bool, Any]:
    """The value at a dot path (object keys, list indexes), and whether the path exists."""
    for part in path.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return False, None
    return True, value


def _key(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def workflow_history(
    engine: Engine, tenant_id: str, run_id: str, *, since_hours: int = 24, where: dict[str, Any] | None = None,
    select: str | None = None, statuses: list[str] | tuple[str, ...] = ("completed",), limit: int = 100,
    distinct: bool | None = None, now: datetime | None = None,
) -> dict[str, Any]:
    """`{items, count}` (plus `truncated: true` when the result was cut to fit a step's output): the outputs, or
    the `select`ed values, of the calling run's workflow's other runs of this tenant, newest first. Raises
    NotAWorkflowRun when `run_id` is not a workflow run of the tenant."""
    chosen = [s for s in statuses if s in HISTORY_STATUSES]
    dedupe = bool(select) if distinct is None else distinct
    since = (now or datetime.now(timezone.utc)) - timedelta(hours=since_hours)
    with engine.connect() as conn:
        caller = conn.execute(_CALLER, {"t": tenant_id, "r": run_id}).first()
        if caller is None or caller[0] is None:
            raise NotAWorkflowRun(run_id)
        rows = conn.execute(
            _RUNS, {"t": tenant_id, "wf": caller[0], "r": run_id, "st": chosen, "since": since, "n": MAX_SCAN}
        ).all()
    items: list[Any] = []
    seen: set[str] = set()
    size = 0
    truncated = False
    for (raw,) in rows:
        output = _parse(raw)
        if where:
            if not isinstance(output, dict) or not all(k in output and _same(output[k], v) for k, v in where.items()):
                continue
        if select:
            found, value = _pick(output, select)
            if not found:
                continue
        else:
            value = output
        if dedupe:
            k = _key(value)
            if k in seen:
                continue
            seen.add(k)
        cost = len(_key(value).encode()) + 2
        if size + cost > MAX_RESULT_BYTES:
            truncated = True
            break
        size += cost
        items.append(value)
        if len(items) >= limit:
            break
    out: dict[str, Any] = {"items": items, "count": len(items)}
    if truncated:
        out["truncated"] = True
    return out


__all__ = ["HISTORY_STATUSES", "MAX_RESULT_BYTES", "MAX_SCAN", "NotAWorkflowRun", "workflow_history"]
