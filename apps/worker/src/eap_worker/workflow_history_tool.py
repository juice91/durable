"""`workflow.history`: what the calling workflow's own recent runs returned. Read-only.

A workflow uses it to skip items its recent runs rejected: `where {status: check_failed}`, `select url` gives the
urls to add to an exclude list. The data access is `eap_run_store.workflow_history`; this is the thin tool over it,
run in the calling run's tenant. The call is one recorded workflow tool step, so a replay returns the recorded
result, not a fresh read.

- Arguments: `eap_schemas.tools.WorkflowHistoryInput`.
- Result: `{items, count}` (`truncated: true` when cut to fit a step's output).
- Error `not_in_workflow`, as `{error: {code, message}}`: the calling run is not a workflow run (an agent cannot
  call it; the Architect does not offer it to agents).
"""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic_ai import RunContext

from eap_run_store.workflow_history import NotAWorkflowRun, workflow_history as read_history
from eap_runtime import RunDeps, ToolRegistry
from eap_schemas.tools import TOOL_DECLARATIONS, WORKFLOW_HISTORY_TOOL_ID


async def workflow_history(
    ctx: RunContext[RunDeps],
    since_hours: int = 24,
    where: dict[str, str | int | float | bool] | None = None,
    select: str | None = None,
    statuses: list[str] = ["completed"],  # noqa: B006 - never mutated
    limit: int = 100,
    distinct: bool | None = None,
) -> dict[str, Any]:
    """Read the calling workflow's recent runs (see the module docstring)."""
    from eap_worker.runtime import get_runtime

    engine = get_runtime().engine
    try:
        return await asyncio.to_thread(
            lambda: read_history(
                engine, ctx.deps.tenant_id, str(ctx.deps.run_id), since_hours=since_hours, where=where, select=select,
                statuses=statuses or ["completed"], limit=limit, distinct=distinct,
            )
        )
    except NotAWorkflowRun:
        return {"error": {"code": "not_in_workflow", "message": "workflow.history can only be called from a workflow run"}}


def register_workflow_history_tool(registry: ToolRegistry) -> None:
    registry.register(TOOL_DECLARATIONS[WORKFLOW_HISTORY_TOOL_ID], workflow_history)


__all__ = ["register_workflow_history_tool", "workflow_history"]
