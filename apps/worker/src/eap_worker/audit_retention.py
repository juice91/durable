"""audit_log retention (IAM P0b, design 6): the scheduled DBOS workflow `eap.audit_retention`.

Rows older than `EAP_AUDIT_RETENTION_DAYS` (default 400; `off` keeps everything) are deleted in bounded batches, all
tenants, oldest first. Same durability contract as `eap_worker.attachment_retention`: the setting travels in the schedule's
context, the cutoff is `scheduled_at - days` (a recovered run replays the same decision), each batch is one DBOS step,
and at most MAX_BATCHES batches run per tick (what is left waits for the next one). Logs and results carry counts only.

    EAP_AUDIT_RETENTION_DAYS       whole days (1-36500) or `off`
    EAP_AUDIT_RETENTION_SCHEDULE   seconds-first cron (default "0 15 4 * * *", daily 04:15 UTC); `off` removes the schedule
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from dbos import DBOS
from eap_run_store import AuditStore
from eap_run_store.audit import DEFAULT_RETENTION_DAYS, RETENTION_ENV, retention_days_from_env

from eap_worker.runtime import get_runtime

log = logging.getLogger("eap_worker.audit_retention")

WORKFLOW_NAME = "eap.audit_retention"
SCHEDULE_NAME = "eap.audit_retention"
BATCH_STEP = "eap.audit_retention.batch"
SCHEDULE_ENV = "EAP_AUDIT_RETENTION_SCHEDULE"
DEFAULT_SCHEDULE = "0 15 4 * * *"
BATCH_SIZE = 500
MAX_BATCHES = 200  # 100 000 rows per tick
OFF = "off"


class AuditRetentionConfigError(ValueError):
    """An invalid setting (raised at worker start, before anything is scheduled)."""


@dataclass(frozen=True)
class AuditRetentionConfig:
    days: int | None = DEFAULT_RETENTION_DAYS  # None: keep everything
    schedule: str = DEFAULT_SCHEDULE

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "AuditRetentionConfig":
        env = os.environ if environ is None else environ
        try:
            days = retention_days_from_env(env)
        except ValueError as exc:
            raise AuditRetentionConfigError(str(exc)) from None
        schedule = (env.get(SCHEDULE_ENV) or "").strip() or DEFAULT_SCHEDULE
        return cls(days=days, schedule=schedule)

    @property
    def off(self) -> bool:
        return self.days is None or self.schedule.lower() == OFF

    def to_context(self) -> dict[str, Any]:
        return {"days": self.days}

    @staticmethod
    def days_from_context(context: Any) -> int | None:
        days = context.get("days") if isinstance(context, Mapping) else None
        return days if isinstance(days, int) and not isinstance(days, bool) and days >= 1 else None


BatchFn = Callable[[str, int], int]


def sweep(days: int, scheduled_at: datetime, batch: BatchFn, *, batch_size: int = BATCH_SIZE,
          max_batches: int = MAX_BATCHES) -> dict[str, Any]:
    """Batches until one deletes fewer rows than `batch_size` (done) or the bound. Counts only."""
    at = scheduled_at.replace(tzinfo=timezone.utc) if scheduled_at.tzinfo is None else scheduled_at.astimezone(timezone.utc)
    cutoff = (at - timedelta(days=days)).isoformat()
    deleted = batches = 0
    complete = False
    while batches < max_batches:
        count = batch(cutoff, batch_size)
        batches += 1
        deleted += count
        if count < batch_size:
            complete = True
            break
    return {"scheduled_at": at.isoformat(), "cutoff": cutoff, "complete": complete, "batches": batches, "deleted": deleted}


@DBOS.step(name=BATCH_STEP, retries_allowed=True, max_attempts=3)
def retention_batch_step(cutoff_iso: str, limit: int) -> int:
    return AuditStore(get_runtime().engine).prune_batch(datetime.fromisoformat(cutoff_iso), limit)


@DBOS.workflow(name=WORKFLOW_NAME)
def audit_retention(scheduled_at: datetime, context: Any) -> dict[str, Any]:
    days = AuditRetentionConfig.days_from_context(context)
    if days is None:
        return {"skipped": True}
    summary = sweep(days, scheduled_at, retention_batch_step)
    log.info("audit retention swept deleted=%d batches=%d", summary["deleted"], summary["batches"])
    if not summary["complete"]:
        log.warning("audit retention stopped at the batch limit (%d); the next run continues", MAX_BATCHES)
    return summary


def apply_audit_retention_schedule(config: AuditRetentionConfig | None = None) -> str:
    """Register (or replace) the schedule after DBOS.launch(); `removed` when retention is `off`."""
    config = config or AuditRetentionConfig.from_env()
    if config.off:
        DBOS.delete_schedule(SCHEDULE_NAME)
        log.info("audit retention off: schedule removed")
        return "removed"
    stored = DBOS.get_schedule(SCHEDULE_NAME)
    if stored is not None and (stored.get("schedule") != config.schedule or stored.get("context") != config.to_context()):
        log.warning("audit retention: the stored schedule differs from this worker's settings and is replaced")
    DBOS.apply_schedules(
        [{"schedule_name": SCHEDULE_NAME, "workflow_fn": audit_retention, "schedule": config.schedule, "context": config.to_context()}]
    )
    log.info("audit retention scheduled: schedule=%s days=%d", config.schedule, config.days)
    return "applied"
