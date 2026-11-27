"""External run sweep (external agents, inbound design 7.1 and 7.3): the scheduled DBOS workflow
`eap.external_run_sweep` over the runs external clients started.

    eap.external_run_sweep.find        overdue runs (past `external_runs.deadline_at`, not ended, a run parked on an
                                       approval included) and runs still `queued` with no DBOS workflow (the API died
                                       between the run row and the enqueue)
    per overdue run:
      eap.external_run_sweep.request   the durable cancel request and the claim's cancel reason `deadline_exceeded` in
                                       one transaction; a run that somebody else asked to cancel keeps their reason
      DBOS.cancelWorkflow              (cancel_children=True), then eap.cancel_reconcile.finish (guarded terminal
                                       write), then the same for the knowledge-base syncs the run started
    start (workflow body)              DBOS.start_workflow_async(workflow_run, ...) per lost run (workflow id = run id)
    eap.external_run_sweep.tidy        close the claims of ended runs; delete claims that never got a run after their
                                       deadline

The workflow interpreter is unchanged. Every step is idempotent; one run's failure never stops the others, and a cancel
left unfinished is completed by `eap.cancel_reconcile`.

Settings (read when the worker applies the schedule): EAP_EXTERNAL_SWEEP_SCHEDULE, a DBOS cron with seconds first;
default `0 * * * * *` (every 60 s); `off` removes the shared schedule.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from dbos import DBOS, SetWorkflowID

from eap_run_store import CANCEL_EVENT_KEY
from eap_run_store.external_access import DEADLINE_CANCELLER, ExternalAccessStore
from eap_worker.cancel_reconcile import finish_cancel_step, valid_cron
from eap_worker.runtime import get_runtime
from eap_worker.schedule_tick import _with_workflow_status
from eap_worker.workflow_run import workflow_run

log = logging.getLogger("eap_worker.external_run_sweep")

EXTERNAL_SWEEP_WORKFLOW_NAME = "eap.external_run_sweep"
EXTERNAL_SWEEP_SCHEDULE_NAME = "eap.external_run_sweep"
FIND_STEP = "eap.external_run_sweep.find"
REQUEST_STEP = "eap.external_run_sweep.request"
TIDY_STEP = "eap.external_run_sweep.tidy"

SCHEDULE_ENV = "EAP_EXTERNAL_SWEEP_SCHEDULE"
DEFAULT_SCHEDULE = "0 * * * * *"  # every 60 s (DBOS crons put the seconds first)
DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
# A run still queued after this with no DBOS workflow was never handed over (as the schedule tick's orphans).
ORPHAN_GRACE = timedelta(minutes=2)
# A claim with no run is kept this long past its deadline, so a late retry still finds its version.
DEAD_CLAIM_GRACE = timedelta(minutes=5)
OFF = "off"


class ExternalSweepConfigError(ValueError):
    """An invalid EAP_EXTERNAL_SWEEP_SCHEDULE (raised before DBOS launches or recovers anything)."""

    def __init__(self, value: str):
        super().__init__(value)
        self.value = value

    def __str__(self) -> str:
        return f"{SCHEDULE_ENV} must be a DBOS cron expression (seconds first) or 'off', not {self.value!r}"


@dataclass(frozen=True)
class ExternalSweepConfig:
    schedule: str = DEFAULT_SCHEDULE
    limit: int = DEFAULT_LIMIT
    off: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ExternalSweepConfig":
        env = os.environ if env is None else env
        schedule = (env.get(SCHEDULE_ENV) or "").strip() or DEFAULT_SCHEDULE
        if schedule.lower() == OFF:
            return cls(off=True)
        if not valid_cron(schedule):
            raise ExternalSweepConfigError(schedule)
        return cls(schedule=schedule)

    def to_context(self) -> dict[str, Any]:
        return {"limit": self.limit}

    @staticmethod
    def limit_of(context: Any) -> int:
        """The tick's page size from its schedule context; the default for a malformed one."""
        limit = context.get("limit") if isinstance(context, Mapping) else None
        return limit if isinstance(limit, int) and 1 <= limit <= MAX_LIMIT else DEFAULT_LIMIT


def _store() -> ExternalAccessStore:
    return ExternalAccessStore(get_runtime().engine)


# --------------------------------------------------------------------------- steps


@DBOS.step(name=FIND_STEP, retries_allowed=True, max_attempts=3)
def find_step(limit: int) -> dict[str, list[dict[str, Any]]]:
    store = _store()
    overdue = [{"tenant_id": r["tenant_id"], "run_id": str(r["run_id"])} for r in store.overdue_runs(limit)]
    candidates = store.unstarted_runs(ORPHAN_GRACE, limit)
    known = _with_workflow_status([r["run_id"] for r in candidates]) if candidates else set()
    return {"overdue": overdue, "unstarted": [r for r in candidates if r["run_id"] not in known]}


@DBOS.step(name=REQUEST_STEP, retries_allowed=True, max_attempts=3)
def request_step(tenant_id: str, run_id: str) -> dict[str, Any]:
    """`{ours, syncs}`: whether the cancel is the deadline's, and the unended knowledge-base syncs the run started
    (each given its own cancel request)."""
    rt = get_runtime()
    if not _store().request_deadline_cancel(tenant_id, run_id):
        return {"ours": False, "syncs": []}
    syncs = [str(s) for s in rt.run_store.unfinished_child_syncs(tenant_id, run_id)]
    for child in syncs:
        rt.event_log.append(
            tenant_id, UUID(child), CANCEL_EVENT_KEY, "cancel_requested", {"requested_by": DEADLINE_CANCELLER}
        )
    return {"ours": True, "syncs": syncs}


@DBOS.step(name=TIDY_STEP, retries_allowed=True, max_attempts=3)
def tidy_step(limit: int) -> dict[str, int]:
    store = _store()
    return {"closed": store.close_ended_claims(limit), "forgotten": store.delete_dead_claims(DEAD_CLAIM_GRACE, limit)}


# --------------------------------------------------------------------------- the workflow


@DBOS.workflow(name=EXTERNAL_SWEEP_WORKFLOW_NAME)
async def external_run_sweep(scheduled_at: datetime, context: Any) -> dict[str, int]:
    """The scheduled sweep (DBOS passes the tick time and the schedule context)."""
    limit = ExternalSweepConfig.limit_of(context)
    found = await asyncio.to_thread(find_step, limit)
    counts = {"overdue": len(found["overdue"]), "cancelled": 0, "failed": 0, "started": 0}
    for item in found["overdue"]:
        try:
            requested = await asyncio.to_thread(request_step, item["tenant_id"], item["run_id"])
            if not requested["ours"]:
                continue  # it ended, or another cancel is under way (cancel_reconcile finishes that one)
            for run_id in (item["run_id"], *requested["syncs"]):
                await DBOS.cancel_workflow_async(run_id, cancel_children=True)
                await asyncio.to_thread(finish_cancel_step, item["tenant_id"], run_id)
        except Exception as exc:  # noqa: BLE001 - one run's failure never stops the others
            counts["failed"] += 1
            log.warning("external sweep: run %s not cancelled (%s); the next tick retries",
                        item["run_id"], type(exc).__name__)
            continue
        counts["cancelled"] += 1
        log.info("external sweep: run %s passed its deadline and was cancelled", item["run_id"])
    for item in found["unstarted"]:
        with SetWorkflowID(item["run_id"]):
            await DBOS.start_workflow_async(
                workflow_run, item["run_id"], item["tenant_id"], item["version_id"], item["input_json"]
            )
        counts["started"] += 1
    counts.update(await asyncio.to_thread(tidy_step, limit))
    if counts["overdue"] or counts["started"]:
        log.info("external sweep at %s: %s", scheduled_at.isoformat(), counts)
    return counts


def apply_external_sweep_schedule(config: ExternalSweepConfig | None = None) -> str:
    """Register (or replace) the schedule after DBOS.launch(): `applied`, or `removed` for `off`."""
    config = config or ExternalSweepConfig.from_env()
    if config.off:
        DBOS.delete_schedule(EXTERNAL_SWEEP_SCHEDULE_NAME)
        log.info("external run sweep off: schedule removed")
        return "removed"
    DBOS.apply_schedules([{
        "schedule_name": EXTERNAL_SWEEP_SCHEDULE_NAME, "workflow_fn": external_run_sweep,
        "schedule": config.schedule, "context": config.to_context(),
    }])
    log.info("external run sweep scheduled: schedule=%s limit=%d", config.schedule, config.limit)
    return "applied"
