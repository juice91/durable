"""Materialised spans (Task 16): `eap.record_spans` writes a finished run's trace spans to `spans`.

Run once at the end of an agent, tool or workflow run (success or failure), after its terminal
status. It derives the spans the way `GET /runs/{id}/trace` does (TraceReader + trace.flat_spans) and
replaces the run's rows (delete then insert), so a replayed step or a second call leaves the same
rows. Only top-level agent, tool and workflow runs record: a child run's spans are part of its
parent's trace, and a run not finished yet records nothing. Best effort: recording never fails a run.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID

from dbos import DBOS
from sqlalchemy import text

from eap_run_store.spans import SpanStore
from eap_run_store.store import TERMINAL_RUN_STATUSES
from eap_run_store.trace import TraceReader, flat_spans
from eap_worker.runtime import get_runtime

log = logging.getLogger("eap_worker.spans")

SPAN_RUN_KINDS = frozenset({"agent", "tool", "workflow"})
_WORKFLOW_SPEC = text("SELECT spec FROM workflow_versions WHERE tenant_id = :t AND workflow_id = :w AND version = :v")


async def materialise_spans(run_id: str, tenant_id: str) -> int:
    """Replace the run's `spans` rows from its trace; returns the rows written (0 when the run is
    unknown, not finished, a child run or of another kind)."""
    rt = get_runtime()
    run = await asyncio.to_thread(rt.run_store.get_run, tenant_id, UUID(run_id))
    if (
        run is None
        or run["status"] not in TERMINAL_RUN_STATUSES
        or run["kind"] not in SPAN_RUN_KINDS
        or run.get("parent_run_id") is not None
    ):
        return 0

    def build() -> list[dict[str, Any]]:
        def workflow_spec(workflow_id: str, version: int) -> dict[str, Any] | None:
            with rt.engine.connect() as conn:
                row = conn.execute(_WORKFLOW_SPEC, {"t": tenant_id, "w": str(workflow_id), "v": version}).first()
            return dict(row[0]) if row is not None else None

        trace = TraceReader(rt.engine).read(tenant_id, run_id, workflow_spec=workflow_spec)
        return flat_spans(trace) if trace is not None else []

    spans = await asyncio.to_thread(build)
    return await asyncio.to_thread(SpanStore(rt.engine).replace_for_run, tenant_id, run_id, spans)


@DBOS.step(name="eap.record_spans")
async def record_spans_step(run_id: str, tenant_id: str) -> int:
    return await materialise_spans(run_id, tenant_id)


async def record_spans(run_id: str, tenant_id: str) -> None:
    """Run the step; never fails the finished run."""
    from eap_worker.observational import is_dbos_control_flow

    try:
        await record_spans_step(run_id, tenant_id)
    except Exception as exc:  # noqa: BLE001 - the run is finished; its spans are best effort
        if is_dbos_control_flow(exc):
            raise
        log.warning("run %s: spans not recorded (%s)", run_id, type(exc).__name__)


__all__ = ["materialise_spans", "record_spans", "record_spans_step"]
