"""Unsent chat attachments expire: the scheduled DBOS workflow `eap.attachment_retention`.

An upload (`POST /attachments`) that no message has referenced (`chat_attachments.message_id IS
NULL`) is removed, row and blob, once it is older than `EAP_ATTACHMENT_ORPHAN_HOURS` (default 24).
Linked attachments belong to their conversation and are never touched.

    EAP_ATTACHMENT_ORPHAN_HOURS       age in hours after which an unsent upload expires (default 24)
    EAP_ATTACHMENT_RETENTION_SCHEDULE seconds-first cron (default "0 30 3 * * *", daily 03:30 UTC);
                                      `off` removes the shared schedule (nothing expires)

Order per batch (spec D0): the rows are deleted first, in one statement guarded by
`message_id IS NULL` (an upload linked since it was listed survives), then their blobs. A crash
between the two leaves a blob without a row. The BlobStore cannot list its keys, so this pass cannot
find such blobs: they stay until a bucket lifecycle rule removes them (configure one on
`attachments/` in R2, 7 days). A row without a blob (the other order, from the API's delete) is a row
like any other and goes with its age.

Durability: the configuration travels in the schedule's context and the cutoff is
`scheduled_at - hours`, so a recovered run replays the same decision. Each batch is one DBOS step;
control flow depends only on step outputs. Bounded: at most MAX_BATCHES batches (BATCH_SIZE rows) per
run; what is left waits for the next run. Logs and the result carry counts only (the checkpoints hold
tenant ids, operator-only).
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from dbos import DBOS

from eap_run_store.chat_attachments import ChatAttachmentStore

from eap_worker.attachments import blob_store
from eap_worker.runtime import get_runtime

log = logging.getLogger("eap_worker.attachment_retention")

WORKFLOW_NAME = "eap.attachment_retention"
SCHEDULE_NAME = "eap.attachment_retention"
BATCH_STEP = "eap.attachment_retention.batch"

HOURS_ENV = "EAP_ATTACHMENT_ORPHAN_HOURS"
SCHEDULE_ENV = "EAP_ATTACHMENT_RETENTION_SCHEDULE"
DEFAULT_HOURS = 24
DEFAULT_SCHEDULE = "0 30 3 * * *"  # seconds first: daily, 03:30 UTC
MAX_HOURS = 24 * 365
BATCH_SIZE = 500
MAX_BATCHES = 200  # 100 000 uploads per run
OFF = "off"


class AttachmentRetentionConfigError(ValueError):
    """An invalid setting (raised at worker start, before anything is scheduled)."""


@dataclass(frozen=True)
class AttachmentRetentionConfig:
    hours: int = DEFAULT_HOURS
    schedule: str = DEFAULT_SCHEDULE
    off: bool = False

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "AttachmentRetentionConfig":
        env = os.environ if environ is None else environ
        schedule = (env.get(SCHEDULE_ENV) or "").strip() or DEFAULT_SCHEDULE
        raw = (env.get(HOURS_ENV) or "").strip()
        try:
            hours = int(raw) if raw else DEFAULT_HOURS
        except ValueError:
            raise AttachmentRetentionConfigError(f"{HOURS_ENV}: must be a whole number of hours") from None
        if not 1 <= hours <= MAX_HOURS:
            raise AttachmentRetentionConfigError(f"{HOURS_ENV}: must be between 1 and {MAX_HOURS}")
        return cls(hours=hours, schedule=schedule, off=schedule.lower() == OFF)

    def to_context(self) -> dict[str, Any]:
        return {"hours": self.hours}

    @classmethod
    def hours_from_context(cls, context: Any) -> int:
        hours = context.get("hours") if isinstance(context, Mapping) else None
        if isinstance(hours, int) and not isinstance(hours, bool) and hours >= 1:
            return min(hours, MAX_HOURS)
        return DEFAULT_HOURS


BatchFn = Callable[[str, int], dict[str, Any]]


def as_utc(when: datetime) -> datetime:
    return when.replace(tzinfo=timezone.utc) if when.tzinfo is None else when.astimezone(timezone.utc)


def sweep(hours: int, scheduled_at: datetime, batch: BatchFn, *, batch_size: int = BATCH_SIZE,
          max_batches: int = MAX_BATCHES) -> dict[str, Any]:
    """The workflow body: batches until one lists fewer rows than `batch_size` (done) or the bound.
    Returns `{"scheduled_at", "cutoff", "complete", "batches", "deleted", "blob_failures", "tenants"}`."""
    at = as_utc(scheduled_at)
    cutoff = (at - timedelta(hours=hours)).isoformat()
    tenants: dict[str, int] = {}
    deleted = failures = batches = 0
    complete = False
    while batches < max_batches:
        result = batch(cutoff, batch_size)
        batches += 1
        deleted += int(result["deleted"])
        failures += int(result["blob_failures"])
        for tenant, count in result["tenants"].items():
            tenants[tenant] = tenants.get(tenant, 0) + int(count)
        if int(result["listed"]) < batch_size:
            complete = True
            break
    return {
        "scheduled_at": at.isoformat(), "cutoff": cutoff, "complete": complete, "batches": batches,
        "deleted": deleted, "blob_failures": failures, "tenants": tenants,
    }


def _store() -> ChatAttachmentStore:
    return ChatAttachmentStore(get_runtime().engine)


def delete_batch(store: ChatAttachmentStore, cutoff: datetime, limit: int, remove_blobs: Callable[[list[str]], int]) -> dict[str, Any]:
    """One batch: list expired unsent uploads, delete the rows (guarded by `message_id IS NULL`),
    then their blobs. `remove_blobs` returns how many blobs it failed to delete."""
    ids = store.unsent_before(cutoff, limit)
    removed = store.delete_unsent_ids(ids)
    failures = remove_blobs([key for _, key in removed]) if removed else 0
    per_tenant: dict[str, int] = {}
    for tenant, _ in removed:
        per_tenant[tenant] = per_tenant.get(tenant, 0) + 1
    return {"listed": len(ids), "deleted": len(removed), "blob_failures": failures, "tenants": per_tenant}


def _remove_blobs(keys: list[str]) -> int:
    async def run() -> int:
        failed = 0
        store = blob_store()
        for key in keys:
            try:
                await store.delete(key)
            except Exception:  # noqa: BLE001 - one unreachable key must not stop the batch
                failed += 1
                log.exception("attachment retention could not delete a blob")
        return failed

    return asyncio.run(run())


@DBOS.step(name=BATCH_STEP, retries_allowed=True, max_attempts=3)
def retention_batch_step(cutoff_iso: str, limit: int) -> dict[str, Any]:
    """One batch (one store transaction for the rows, then the blobs). Output: counts per tenant."""
    return delete_batch(_store(), datetime.fromisoformat(cutoff_iso), limit, _remove_blobs)


@DBOS.workflow(name=WORKFLOW_NAME)
def attachment_retention(scheduled_at: datetime, context: Any) -> dict[str, Any]:
    """The scheduled sweep (DBOS passes the tick time and the schedule context)."""
    hours = AttachmentRetentionConfig.hours_from_context(context)
    summary = sweep(hours, scheduled_at, retention_batch_step)
    log.info(
        "attachment retention swept deleted=%d blob_failures=%d tenants=%d batches=%d",
        summary["deleted"], summary["blob_failures"], len(summary["tenants"]), summary["batches"],
    )
    if not summary["complete"]:
        log.warning("attachment retention stopped at the batch limit (%d); the next run continues", MAX_BATCHES)
    return summary


def apply_attachment_retention_schedule(config: AttachmentRetentionConfig | None = None) -> str:
    """Register (or replace) the schedule after DBOS.launch(). `applied` by default; `removed` when
    `EAP_ATTACHMENT_RETENTION_SCHEDULE=off` (the shared schedule is deleted)."""
    config = config or AttachmentRetentionConfig.from_env()
    if config.off:
        DBOS.delete_schedule(SCHEDULE_NAME)
        log.info("attachment retention off: schedule removed")
        return "removed"
    stored = DBOS.get_schedule(SCHEDULE_NAME)
    if stored is not None and (stored.get("schedule") != config.schedule or stored.get("context") != config.to_context()):
        log.warning(
            "attachment retention: the stored schedule differs from this worker's settings and is replaced;"
            " configure every worker alike"
        )
    DBOS.apply_schedules(
        [{
            "schedule_name": SCHEDULE_NAME,
            "workflow_fn": attachment_retention,
            "schedule": config.schedule,
            "context": config.to_context(),
        }]
    )
    log.info("attachment retention scheduled: schedule=%s orphan_hours=%d", config.schedule, config.hours)
    return "applied"
