"""Memory retention sweeper (RAG and memory milestone, RM.15): the scheduled DBOS workflow
`eap.memory_retention`.

Optional: with no retention configured this worker schedules nothing (memory is kept) and leaves
any schedule another worker stored alone; `EAP_MEMORY_RETENTION_DAYS=off` removes the shared
schedule explicitly. Configured, it runs on a cron schedule (daily by default) and deletes, per kind, the rows
older than that kind's retention, tenant by tenant, in bounded batches, through
`MemoryStore.delete_retention_batch`:

    kind                          age column    env (days)
    message_embeddings            created_at    EAP_MEMORY_RETENTION_EMBEDDING_DAYS
    observations                  created_at    EAP_MEMORY_RETENTION_OBSERVATION_DAYS
    conversation_working_memory   updated_at    EAP_MEMORY_RETENTION_WORKING_MEMORY_DAYS
    resource_working_memory       updated_at    EAP_MEMORY_RETENTION_RESOURCE_WORKING_MEMORY_DAYS

The first three default to EAP_MEMORY_RETENTION_DAYS (the plan's single knob); resource working
memory (the user's long-lived note) is swept only when its own variable is set. A per-kind value
`keep` keeps that kind even when EAP_MEMORY_RETENTION_DAYS is set. Forget watermarks are never swept,
and conversations and runs are not memory (they stay). Knowledge base data is never touched.

Durability contract:
- The configuration travels in the schedule's context (set when the worker applies the schedule),
  and every cutoff is `scheduled_at - days`, so a recovered run replays the same decisions.
- Each tenant page and each batch is a DBOS step (one store transaction each); control flow
  depends only on step outputs, so a crash mid-sweep resumes after the last completed step and a
  re-executed batch only deletes what is left (idempotent).
- Bounded: at most EAP_MEMORY_RETENTION_MAX_STEPS steps per run, shared round-robin across the
  kinds (one step per unfinished kind in turn), so one kind's backlog cannot starve the others;
  what is left waits for the next run (logged as `complete=False`).
- Several workers: every configured worker applies the same schedule (an upsert by name; a
  warning when the stored schedule or context differs from this worker's), and DBOS enqueues one
  workflow per tick with id `sched-eap.memory_retention-<scheduled time>`, so a tick runs once.
- Never waits for, nor deadlocks with, delete_conversation or forget_resource, and never touches
  a conversation whose latest turn is queued or running (see the store).

Privacy: logs and the workflow's result carry counts per kind only (no text, no tenant, resource
or conversation ids). Step outputs (DBOS checkpoints, operator-only) hold tenant ids and
conversation id cursors, never resource ids or text.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from dbos import DBOS

from eap_run_store import RETENTION_KINDS, MemoryStore

from eap_worker.runtime import get_runtime

log = logging.getLogger("eap_worker.retention")

RETENTION_WORKFLOW_NAME = "eap.memory_retention"
RETENTION_SCHEDULE_NAME = "eap.memory_retention"
TENANTS_STEP = "eap.memory_retention.tenants"
BATCH_STEP = "eap.memory_retention.batch"

DAYS_ENV = "EAP_MEMORY_RETENTION_DAYS"
KIND_DAYS_ENV = {
    "message_embeddings": "EAP_MEMORY_RETENTION_EMBEDDING_DAYS",
    "observations": "EAP_MEMORY_RETENTION_OBSERVATION_DAYS",
    "conversation_working_memory": "EAP_MEMORY_RETENTION_WORKING_MEMORY_DAYS",
    "resource_working_memory": "EAP_MEMORY_RETENTION_RESOURCE_WORKING_MEMORY_DAYS",
}
# Kinds that fall back to EAP_MEMORY_RETENTION_DAYS (the plan's decision); resource notes are opt-in.
INHERITING_KINDS = ("message_embeddings", "observations", "conversation_working_memory")
SCHEDULE_ENV = "EAP_MEMORY_RETENTION_SCHEDULE"
BATCH_SIZE_ENV = "EAP_MEMORY_RETENTION_BATCH_SIZE"
MAX_STEPS_ENV = "EAP_MEMORY_RETENTION_MAX_STEPS"
DEFAULT_SCHEDULE = "0 3 * * *"  # daily, 03:00 UTC
DEFAULT_BATCH_SIZE = 500
DEFAULT_MAX_STEPS = 2000
MAX_BATCH_SIZE = 10_000  # MemoryStore clamps to MAX_RETENTION_BATCH as well
TENANT_PAGE = 500
KEEP = "keep"
OFF = "off"  # EAP_MEMORY_RETENTION_DAYS=off: remove the shared schedule

assert set(KIND_DAYS_ENV) == set(RETENTION_KINDS)


class RetentionConfigError(ValueError):
    """An invalid retention setting (raised at worker start, before anything is scheduled)."""

    def __init__(self, name: str, problem: str) -> None:
        super().__init__(name, problem)
        self.name = name
        self.problem = problem

    def __str__(self) -> str:
        return f"{self.name}: {self.problem}"


def _days(name: str, raw: str | None) -> int | None:
    """None for unset or `keep`, else a positive number of days."""
    if raw is None or not raw.strip():
        return None
    value = raw.strip().lower()
    if value == KEEP:
        return None
    try:
        days = int(value)
    except ValueError:
        raise RetentionConfigError(name, "must be a positive number of days or 'keep'") from None
    if days < 1:
        raise RetentionConfigError(name, "must be a positive number of days or 'keep'")
    return days


def _bounded_int(name: str, raw: str | None, default: int, maximum: int) -> int:
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        raise RetentionConfigError(name, "must be a positive integer") from None
    if not 1 <= value <= maximum:
        raise RetentionConfigError(name, f"must be between 1 and {maximum}")
    return value


@dataclass(frozen=True)
class RetentionConfig:
    days: Mapping[str, int]  # kind -> retention in days; a kind not present is kept
    batch_size: int = DEFAULT_BATCH_SIZE
    max_steps: int = DEFAULT_MAX_STEPS
    schedule: str = DEFAULT_SCHEDULE
    off: bool = False  # explicit EAP_MEMORY_RETENTION_DAYS=off

    @property
    def enabled(self) -> bool:
        return bool(self.days)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "RetentionConfig":
        env = os.environ if environ is None else environ
        if (env.get(DAYS_ENV) or "").strip().lower() == OFF:
            return cls(days={}, off=True)
        default = _days(DAYS_ENV, env.get(DAYS_ENV))
        days: dict[str, int] = {}
        for kind in RETENTION_KINDS:
            name = KIND_DAYS_ENV[kind]
            raw = env.get(name)
            if raw is not None and raw.strip():
                value = _days(name, raw)
            else:
                value = default if kind in INHERITING_KINDS else None
            if value is not None:
                days[kind] = value
        # DBOS.apply_schedules validates the cron expression at worker start (DBOSException).
        schedule = (env.get(SCHEDULE_ENV) or "").strip() or DEFAULT_SCHEDULE
        return cls(
            days=days,
            batch_size=_bounded_int(BATCH_SIZE_ENV, env.get(BATCH_SIZE_ENV), DEFAULT_BATCH_SIZE, MAX_BATCH_SIZE),
            max_steps=_bounded_int(MAX_STEPS_ENV, env.get(MAX_STEPS_ENV), DEFAULT_MAX_STEPS, 1_000_000),
            schedule=schedule,
        )

    def to_context(self) -> dict[str, Any]:
        """The schedule context: what every scheduled run of this configuration receives."""
        return {"days": dict(self.days), "batch_size": self.batch_size, "max_steps": self.max_steps}

    @classmethod
    def from_context(cls, context: Any) -> "RetentionConfig":
        """The run's configuration from its schedule context (None or malformed: nothing to do)."""
        if not isinstance(context, Mapping):
            return cls(days={})
        raw_days = context.get("days")
        days = {
            k: int(v)
            for k, v in (raw_days.items() if isinstance(raw_days, Mapping) else ())
            if k in RETENTION_KINDS and isinstance(v, int) and v >= 1
        }
        return cls(
            days=days,
            batch_size=max(1, min(int(context.get("batch_size") or DEFAULT_BATCH_SIZE), MAX_BATCH_SIZE)),
            max_steps=max(1, int(context.get("max_steps") or DEFAULT_MAX_STEPS)),
        )


# --------------------------------------------------------------------------------- the sweep

TenantsFn = Callable[[str, str, "str | None", int], list[str]]
BatchFn = Callable[[str, str, str, int, "str | None"], dict[str, Any]]


def as_utc(when: datetime) -> datetime:
    return when.replace(tzinfo=timezone.utc) if when.tzinfo is None else when.astimezone(timezone.utc)


def sweep(config: RetentionConfig, scheduled_at: datetime, tenants: TenantsFn, batch: BatchFn) -> dict[str, Any]:
    """The workflow body: deterministic given `config`, `scheduled_at` and the step outputs.

    Each configured kind (cutoff `scheduled_at - days`) pages through the tenants that have older
    rows and runs batches for each until it is done. The kinds share `config.max_steps`
    round-robin: every pass gives each unfinished kind one step (a tenant page or a batch), in
    RETENTION_KINDS order. Returns counts only: `{"scheduled_at", "complete", "steps", "kinds":
    {kind: {"cutoff", "tenants", "batches", "deleted", "skipped"}}}`."""
    at = as_utc(scheduled_at)
    budget = config.max_steps
    kinds: dict[str, dict[str, Any]] = {}
    state: dict[str, dict[str, Any]] = {}
    for kind in RETENTION_KINDS:
        days = config.days.get(kind)
        if days is None:
            continue
        cutoff = (at - timedelta(days=days)).isoformat()
        kinds[kind] = {"cutoff": cutoff, "tenants": 0, "batches": 0, "deleted": 0, "skipped": 0}
        # page: the current tenant page (None before the first); idx: its tenant being swept.
        state[kind] = {"cutoff": cutoff, "page": None, "idx": 0, "cursor": None, "done": False}
    while budget > 0 and not all(st["done"] for st in state.values()):
        for kind, st in state.items():
            if st["done"] or budget <= 0:
                continue
            page = st["page"]
            if page is None or st["idx"] >= len(page):
                if page is not None and len(page) < TENANT_PAGE:
                    st["done"] = True  # the last page is swept; costs no step
                    continue
                st["page"] = tenants(kind, st["cutoff"], page[-1] if page else None, TENANT_PAGE)
                budget -= 1
                st["idx"], st["cursor"] = 0, None
                if not st["page"]:
                    st["done"] = True
                continue
            totals = kinds[kind]
            if st["cursor"] is None:
                totals["tenants"] += 1
            result = batch(kind, page[st["idx"]], st["cutoff"], config.batch_size, st["cursor"])
            budget -= 1
            totals["batches"] += 1
            totals["deleted"] += int(result["deleted"])
            totals["skipped"] += int(result["skipped"])
            if result["done"]:
                st["idx"], st["cursor"] = st["idx"] + 1, None
            else:
                st["cursor"] = result["next_after"]
    # A finished last page needs no further step: settle it so `complete` is exact.
    for st in state.values():
        if not st["done"] and st["page"] is not None and st["idx"] >= len(st["page"]) and len(st["page"]) < TENANT_PAGE:
            st["done"] = True
    return {
        "scheduled_at": at.isoformat(),
        "complete": all(st["done"] for st in state.values()),
        "steps": config.max_steps - budget,
        "kinds": kinds,
    }


def _store() -> MemoryStore:
    return MemoryStore(get_runtime().engine)


@DBOS.step(name=TENANTS_STEP, retries_allowed=True, max_attempts=3)
def retention_tenants_step(kind: str, cutoff_iso: str, after: str | None, limit: int) -> list[str]:
    """One page of tenants with `kind` rows older than the cutoff (ids only)."""
    return _store().retention_tenants(kind, datetime.fromisoformat(cutoff_iso), after=after, limit=limit)


@DBOS.step(name=BATCH_STEP, retries_allowed=True, max_attempts=3)
def retention_batch_step(kind: str, tenant_id: str, cutoff_iso: str, limit: int, after: str | None) -> dict[str, Any]:
    """One batch delete (one store transaction). Output: counts, `done` and the next cursor."""
    result = _store().delete_retention_batch(
        kind, tenant_id, datetime.fromisoformat(cutoff_iso), limit=limit, after=after
    )
    return {"deleted": result.deleted, "skipped": result.skipped, "done": result.done, "next_after": result.next_after}


@DBOS.workflow(name=RETENTION_WORKFLOW_NAME)
def memory_retention(scheduled_at: datetime, context: Any) -> dict[str, Any]:
    """The scheduled retention sweep (DBOS passes the tick time and the schedule context)."""
    config = RetentionConfig.from_context(context)
    summary = sweep(config, scheduled_at, retention_tenants_step, retention_batch_step)
    for kind, totals in summary["kinds"].items():
        log.info(
            "memory retention swept kind=%s deleted=%d skipped=%d tenants=%d batches=%d",
            kind, totals["deleted"], totals["skipped"], totals["tenants"], totals["batches"],
        )
    if not summary["complete"]:
        log.warning(
            "memory retention stopped at the step limit (%d); the next run continues", config.max_steps
        )
    return summary


def apply_retention_schedule(config: RetentionConfig | None = None) -> str:
    """Register (or replace) the retention schedule after DBOS.launch(). Returns what it did:

    - `applied`: retention is configured; the schedule is upserted by name (safe on every start
      and on several workers). A stored schedule with another cron or context (another worker's
      settings) is logged as a warning and replaced: configure the workers alike.
    - `removed`: `EAP_MEMORY_RETENTION_DAYS=off`; the shared schedule is deleted.
    - `left`: nothing configured on this worker; a schedule stored by another worker is left as
      it is (a worker without the env never turns retention off for the others)."""
    config = config or RetentionConfig.from_env()
    if config.off:
        DBOS.delete_schedule(RETENTION_SCHEDULE_NAME)
        log.info("memory retention off: schedule removed")
        return "removed"
    if not config.enabled:
        if DBOS.get_schedule(RETENTION_SCHEDULE_NAME) is not None:
            log.info("memory retention not configured on this worker; the stored schedule is left as it is")
        return "left"
    stored = DBOS.get_schedule(RETENTION_SCHEDULE_NAME)
    if stored is not None and (stored.get("schedule") != config.schedule or stored.get("context") != config.to_context()):
        log.warning(
            "memory retention: the stored schedule differs from this worker's settings and is replaced;"
            " configure every worker alike"
        )
    DBOS.apply_schedules(
        [
            {
                "schedule_name": RETENTION_SCHEDULE_NAME,
                "workflow_fn": memory_retention,
                "schedule": config.schedule,
                "context": config.to_context(),
            }
        ]
    )
    log.info(
        "memory retention scheduled: schedule=%s kinds=%s batch_size=%d max_steps=%d",
        config.schedule,
        ",".join(f"{k}:{d}d" for k, d in config.days.items()),
        config.batch_size,
        config.max_steps,
    )
    return "applied"
