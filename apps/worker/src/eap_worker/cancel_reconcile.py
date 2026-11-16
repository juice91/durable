"""Cancel reconciler (workflow loops milestone, D12 step 4, LP.8): the scheduled DBOS workflow
`eap.cancel_reconcile` that finishes cancels the API did not finish.

`POST /runs/{id}/cancel` writes the durable request (`cancel_requested`, key `cancel`), then
cascades the DBOS cancel (`cancel_children=True`), then writes the guarded terminal state
(RunStore.cancel_run_tree). If the API dies, or the system database or eap_app fails, after the
request and before the terminal write, the run would stay "running" or "recovering" forever: DBOS
never recovers a CANCELLED workflow, so no worker would ever write its `end`. This sweep closes
that gap:

    eap.cancel_reconcile.find                    RunStore.find_unfinished_cancels: workflow runs
                                                 whose `cancel` is older than the threshold and
                                                 that (or one of whose child runs) have no `end`
    per run, in that order:
      DBOS.cancelWorkflow (cancel_children=True) step 2 again (idempotent; DBOS records it)
      eap.cancel_reconcile.finish                step 3 again: RunStore.cancel_run_tree

- Every worker applies the same schedule (an upsert by name, as eap.memory_retention), and DBOS
  enqueues one workflow per tick with id `sched-eap.cancel_reconcile-<scheduled time>`, so a tick
  runs once across workers.
- The configuration travels in the schedule's context: `{"after_seconds", "limit"}`.
- Deterministic on replay: the run list is the `find` step's recorded output, and both later
  operations are recorded. A run whose finish fails (after the step's retries) is logged and left
  for the next tick; the others still finish.
- Both steps are idempotent, and a retried POST /cancel does the same work at once, so a run the
  API and the sweep finish together still gets exactly one `end`.

Settings (read when the worker applies the schedule):
- EAP_CANCEL_RECONCILE_SCHEDULE: a DBOS cron with seconds first; default `0 * * * * *` (every
  60 s). `off` removes the shared schedule.
- EAP_CANCEL_RECONCILE_AFTER_SECONDS: how old a cancel must be before the sweep takes it over
  (default 30; the API normally finishes within a request).
- EAP_CANCEL_RECONCILE_LIMIT: most runs per tick (default 100; the rest wait for the next tick,
  oldest request first).

Query plan: find_unfinished_cancels filters `execution_events.event_key = 'cancel'` across
tenants, which no index serves (the `(tenant_id, type, ts)` index leads with the tenant). A
partial index `execution_events (ts) WHERE event_key = 'cancel'` needs a migration
(`0020_workflow_loops`, deferred; D14 plans none).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from dbos import DBOS

from eap_run_store.spans import materialise_spans
from eap_worker.runtime import get_runtime

log = logging.getLogger("eap_worker.cancel_reconcile")

CANCEL_RECONCILE_WORKFLOW_NAME = "eap.cancel_reconcile"
CANCEL_RECONCILE_SCHEDULE_NAME = "eap.cancel_reconcile"
FIND_STEP = "eap.cancel_reconcile.find"
FINISH_STEP = "eap.cancel_reconcile.finish"

SCHEDULE_ENV = "EAP_CANCEL_RECONCILE_SCHEDULE"
AFTER_SECONDS_ENV = "EAP_CANCEL_RECONCILE_AFTER_SECONDS"
LIMIT_ENV = "EAP_CANCEL_RECONCILE_LIMIT"
DEFAULT_SCHEDULE = "0 * * * * *"  # every 60 s (DBOS crons put the seconds first)
DEFAULT_AFTER_SECONDS = 30
DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
OFF = "off"


class CancelReconcileConfigError(ValueError):
    """An invalid EAP_CANCEL_RECONCILE_* setting (raised by ReconcileConfig.from_env, which
    `python -m eap_worker` calls before DBOS launches or recovers anything)."""

    def __init__(self, name: str, value: str):
        super().__init__(name, value)
        self.name = name
        self.value = value

    def __str__(self) -> str:
        if self.name == SCHEDULE_ENV:
            return f"{self.name} must be a DBOS cron expression (seconds first) or 'off', not {self.value!r}"
        return f"{self.name} must be a positive integer, not {self.value!r}"


def _positive_int(env: Mapping[str, str], name: str, default: int, maximum: int | None = None) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise CancelReconcileConfigError(name, raw) from None
    if value < 1 or (maximum is not None and value > maximum):
        raise CancelReconcileConfigError(name, raw)
    return value


def valid_cron(schedule: str) -> bool:
    """The check DBOS.apply_schedules makes (croniter, seconds first; 5 fields also pass)."""
    from croniter import croniter

    return bool(croniter.is_valid(schedule, second_at_beginning=True))


@dataclass(frozen=True)
class ReconcileConfig:
    schedule: str = DEFAULT_SCHEDULE
    after_seconds: int = DEFAULT_AFTER_SECONDS
    limit: int = DEFAULT_LIMIT
    off: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ReconcileConfig":
        env = os.environ if env is None else env
        schedule = (env.get(SCHEDULE_ENV) or "").strip() or DEFAULT_SCHEDULE
        if schedule.lower() == OFF:
            return cls(off=True)
        if not valid_cron(schedule):
            # Before launch: DBOS.apply_schedules would only reject it after recovery has started.
            raise CancelReconcileConfigError(SCHEDULE_ENV, schedule)
        return cls(
            schedule=schedule,
            after_seconds=_positive_int(env, AFTER_SECONDS_ENV, DEFAULT_AFTER_SECONDS),
            limit=_positive_int(env, LIMIT_ENV, DEFAULT_LIMIT, MAX_LIMIT),
        )

    def to_context(self) -> dict[str, Any]:
        return {"after_seconds": self.after_seconds, "limit": self.limit}

    @classmethod
    def from_context(cls, context: Any) -> "ReconcileConfig":
        """The tick's configuration from its schedule context; defaults for a malformed one."""
        if not isinstance(context, Mapping):
            return cls()
        after, limit = context.get("after_seconds"), context.get("limit")
        return cls(
            after_seconds=after if isinstance(after, int) and after >= 1 else DEFAULT_AFTER_SECONDS,
            limit=limit if isinstance(limit, int) and 1 <= limit <= MAX_LIMIT else DEFAULT_LIMIT,
        )


# --------------------------------------------------------------------------- steps


@DBOS.step(name=FIND_STEP, retries_allowed=True, max_attempts=3)
def find_unfinished_cancels_step(after_seconds: int, limit: int) -> list[dict[str, str]]:
    """The runs to finish, oldest request first: `[{tenant_id, run_id, requested_at}]` (strings)."""
    rows = get_runtime().run_store.find_unfinished_cancels(timedelta(seconds=after_seconds), limit)
    return [
        {
            "tenant_id": str(r["tenant_id"]),
            "run_id": str(r["run_id"]),
            "requested_at": r["requested_at"].isoformat() if isinstance(r["requested_at"], datetime) else str(r["requested_at"]),
        }
        for r in rows
    ]


@DBOS.step(name=FINISH_STEP, retries_allowed=True, max_attempts=3)
def finish_cancel_step(tenant_id: str, run_id: str) -> dict[str, Any]:
    """D12 step 3 for one run: RunStore.cancel_run_tree (idempotent). Returns counts and the root's
    status only."""
    rt = get_runtime()
    result = rt.run_store.cancel_run_tree(tenant_id, UUID(run_id))
    try:  # the cancelled run's own workflow never reaches its record_spans step
        materialise_spans(rt.engine, tenant_id, run_id)
    except Exception:  # noqa: BLE001 - spans are best effort; the cancel is done
        log.warning("run %s: spans not recorded after cancel", run_id)
    return {
        "run_id": run_id,
        "status": result.status,
        "ended": len(result.ended),
        "approvals_closed": result.approvals_closed,
    }


# --------------------------------------------------------------------------- the workflow


def reconcile(config: ReconcileConfig) -> dict[str, Any]:
    """The workflow body (deterministic given the step outputs). Returns counts only."""
    pending = find_unfinished_cancels_step(config.after_seconds, config.limit)
    finished = failed = ended = 0
    for item in pending:
        try:
            # Step 2 again (idempotent): a crash may have come before the cascade, so the run's DBOS
            # workflow could still be running. Recorded by DBOS as `DBOS.cancelWorkflow`.
            DBOS.cancel_workflow(item["run_id"], cancel_children=True)
            done = finish_cancel_step(item["tenant_id"], item["run_id"])
        except Exception as exc:  # noqa: BLE001 - one run's failure never stops the others
            failed += 1
            log.warning(
                "cancel reconcile: run %s not finished (%s); the next tick retries",
                item["run_id"], type(exc).__name__,
            )
            continue
        finished += 1
        ended += done["ended"]
        log.info("cancel reconcile: run %s finished as %s", item["run_id"], done["status"])
    return {"found": len(pending), "finished": finished, "failed": failed, "ended": ended}


@DBOS.workflow(name=CANCEL_RECONCILE_WORKFLOW_NAME)
def cancel_reconcile(scheduled_at: datetime, context: Any) -> dict[str, Any]:
    """The scheduled sweep (DBOS passes the tick time and the schedule context)."""
    summary = reconcile(ReconcileConfig.from_context(context))
    if summary["found"]:
        log.info(
            "cancel reconcile at %s: found=%d finished=%d failed=%d ended=%d",
            scheduled_at.isoformat(), summary["found"], summary["finished"], summary["failed"], summary["ended"],
        )
    return summary


def apply_cancel_reconcile_schedule(config: ReconcileConfig | None = None) -> str:
    """Register (or replace) the schedule after DBOS.launch(). `applied` (an upsert by name, safe on
    every start and every worker), or `removed` for EAP_CANCEL_RECONCILE_SCHEDULE=off."""
    config = config or ReconcileConfig.from_env()
    if config.off:
        DBOS.delete_schedule(CANCEL_RECONCILE_SCHEDULE_NAME)
        log.info("cancel reconcile off: schedule removed")
        return "removed"
    stored = DBOS.get_schedule(CANCEL_RECONCILE_SCHEDULE_NAME)
    if stored is not None and (stored.get("schedule") != config.schedule or stored.get("context") != config.to_context()):
        log.warning(
            "cancel reconcile: the stored schedule differs from this worker's settings and is replaced;"
            " configure every worker alike"
        )
    DBOS.apply_schedules(
        [
            {
                "schedule_name": CANCEL_RECONCILE_SCHEDULE_NAME,
                "workflow_fn": cancel_reconcile,
                "schedule": config.schedule,
                "context": config.to_context(),
            }
        ]
    )
    log.info(
        "cancel reconcile scheduled: schedule=%s after=%ds limit=%d",
        config.schedule, config.after_seconds, config.limit,
    )
    return "applied"


__all__ = [
    "CANCEL_RECONCILE_SCHEDULE_NAME",
    "CANCEL_RECONCILE_WORKFLOW_NAME",
    "CancelReconcileConfigError",
    "ReconcileConfig",
    "apply_cancel_reconcile_schedule",
    "cancel_reconcile",
    "finish_cancel_step",
    "find_unfinished_cancels_step",
    "reconcile",
]
