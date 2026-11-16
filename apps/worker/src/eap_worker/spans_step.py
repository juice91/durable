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

from dbos import DBOS

from eap_run_store.spans import materialise_spans as _materialise
from eap_worker.runtime import get_runtime

log = logging.getLogger("eap_worker.spans")



async def materialise_spans(run_id: str, tenant_id: str) -> int:
    """eap_run_store.spans.materialise_spans off the event loop (rows written; 0 when the run is
    not a finished top-level agent, tool or workflow run)."""
    return await asyncio.to_thread(_materialise, get_runtime().engine, tenant_id, run_id)


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
