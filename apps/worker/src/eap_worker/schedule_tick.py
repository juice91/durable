"""Schedule tick (parity round 2, Task 5, decision D2): the scheduled DBOS workflow
`eap.schedule_tick` that starts a run for every due workflow schedule.

    eap.schedule_tick.fire    ONE step: claim_due(session=) FOR UPDATE SKIP LOCKED opens the claim
                              transaction; per schedule validate, create the run, mark_fired/stop;
                              the claim transaction commits once at the end
    start (workflow body)     DBOS.start_workflow_async(workflow_run, ...) per fired run, workflow id =
                              run id (a step cannot start a workflow)

- Every worker applies the same DBOS schedule (an upsert by name), so DBOS starts one tick workflow
  per minute across workers.
- The latest published version fires. The input is checked against that version's input_schema at
  fire time. If the workflow is archived, has no published version, or the input no longer
  validates, NO run is created: the schedule is stopped (`active=false`) with `last_error` =
  `workflow_archived`, `no_published_version` or `input_schema_errors: <path: keyword; ...>`
  (never the input's values).
- A missed window fires once: the run belongs to the stored `next_fire_at`, and the next fire time
  is computed from now, not from that stale value.
- Idempotent: the run id is `uuid5(schedule id, fire time as UTC isoformat)`. Commit order: the
  run row commits FIRST (`create_workflow_run` has its own session, so it is not part of the claim
  transaction), the schedule row (mark_fired/stop) SECOND, in the claim transaction's single commit.
  A crash between them leaves the schedule due; the next tick fires the same fire time, hits the same
  deterministic run id (ON CONFLICT) and DBOS starts one workflow per workflow id, so it is harmless.
- Orphans: a scheduled run still `queued` after ORPHAN_GRACE with NO DBOS workflow_status row (the tick
  died between the commit and the start) is started again. The DBOS system tables live in their own
  database, so the sweep filters candidates from the app DB against `<schema>.workflow_status`.
- A schedule whose fire fails MAX_FIRE_FAILURES ticks in a row (a permanent error such as RunIdConflict)
  is stopped with `last_error` = `fire_failed: <exception class name>`.

Setting (read when the worker applies the schedule): EAP_SCHEDULE_TICK, a DBOS cron with seconds
first; default `0 * * * * *` (every 60 s). `off` removes the shared schedule.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from dbos import DBOS, SetWorkflowID
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError

from eap_run_store import ScheduleStore
from eap_worker.cancel_reconcile import valid_cron
from eap_worker.runtime import get_runtime
from eap_worker.workflow_run import workflow_run

log = logging.getLogger("eap_worker.schedule_tick")

SCHEDULE_TICK_WORKFLOW_NAME = "eap.schedule_tick"
SCHEDULE_TICK_SCHEDULE_NAME = "eap.schedule_tick"
TICK_STEP = "eap.schedule_tick.fire"

SCHEDULE_ENV = "EAP_SCHEDULE_TICK"
DEFAULT_SCHEDULE = "0 * * * * *"  # every 60 s (DBOS crons put the seconds first)
OFF = "off"
CLAIM_LIMIT = 100
MAX_INPUT_BYTES = 32 * 1024  # as the API's MAX_WORKFLOW_INPUT_BYTES
MAX_REPORTED_ERRORS = 5
MAX_FIRE_FAILURES = 5
ORPHAN_PAGE = 200
ORPHAN_MAX_PAGES = 25
ORPHAN_GRACE = timedelta(minutes=2)  # a fired run still queued after this was not started
ORPHAN_WINDOW = timedelta(days=1)


class ScheduleTickConfigError(ValueError):
    """An invalid EAP_SCHEDULE_TICK (raised by ScheduleTickConfig.from_env, which `python -m
    eap_worker` calls before DBOS launches or recovers anything)."""

    def __init__(self, value: str):
        super().__init__(value)
        self.value = value

    def __str__(self) -> str:
        return f"{SCHEDULE_ENV} must be a DBOS cron expression (seconds first) or 'off', not {self.value!r}"


@dataclass(frozen=True)
class ScheduleTickConfig:
    schedule: str = DEFAULT_SCHEDULE
    off: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ScheduleTickConfig":
        env = os.environ if env is None else env
        schedule = (env.get(SCHEDULE_ENV) or "").strip() or DEFAULT_SCHEDULE
        if schedule.lower() == OFF:
            return cls(off=True)
        if not valid_cron(schedule):
            raise ScheduleTickConfigError(schedule)
        return cls(schedule=schedule)


# --------------------------------------------------------------------------- helpers


def _utc(value: datetime) -> datetime:
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def run_id_for(schedule_id: str, fire_at: datetime) -> uuid.UUID:
    return uuid.uuid5(uuid.UUID(schedule_id), _utc(fire_at).isoformat())


def next_fire(cron: str, timezone_name: str, after: datetime) -> datetime:
    """The first fire time strictly after `after`, computed in `timezone_name`, returned in UTC
    (the same rule as the API's eap_api.schedules.next_fire)."""
    from croniter import croniter

    base = _utc(after).astimezone(ZoneInfo(timezone_name))
    return croniter(cron, base).get_next(datetime).astimezone(timezone.utc)


def input_problems(schema: Any, value: Any) -> list[str]:
    """`path: keyword` strings for `value` against the version's input_schema (an EMPTY
    referencing.Registry: no remote $ref is retrieved); never the value or jsonschema's message."""
    import jsonschema
    from referencing import Registry

    try:
        cls = jsonschema.validators.validator_for(schema)
        cls.check_schema(schema)
        errors = sorted(cls(schema, registry=Registry()).iter_errors(value), key=lambda e: [str(p) for p in e.absolute_path])
        problems = [f"{'.'.join(str(p)[:100] for p in e.absolute_path) or 'input'}: {e.validator}" for e in errors[:MAX_REPORTED_ERRORS]]
    except Exception:  # noqa: BLE001 - SchemaError, an unresolvable $ref, ...
        return ["input: the workflow's input schema could not be applied"]
    if not problems and len(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")) > MAX_INPUT_BYTES:
        problems.append(f"input: must be at most {MAX_INPUT_BYTES // 1024} KiB as JSON")
    return problems


_WORKFLOW = text("SELECT archived FROM workflows WHERE id = :w AND tenant_id = :t")
_LATEST = text(
    "SELECT id, version, spec FROM workflow_versions WHERE workflow_id = :w AND tenant_id = :t"
    " ORDER BY version DESC LIMIT 1"
)


# --------------------------------------------------------------------------- the step


def _fire(session: Any, store: ScheduleStore, schedule: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    """One claimed schedule, inside the claim's transaction: stop it (None) or create its run and
    advance it. Returns what to start."""
    rt = get_runtime()
    fire_at = _utc(schedule["next_fire_at"])
    sid, tenant, workflow_id = schedule["id"], schedule["tenant_id"], schedule["workflow_id"]
    with rt.engine.connect() as conn:
        workflow = conn.execute(_WORKFLOW, {"w": workflow_id, "t": tenant}).fetchone()
        version = conn.execute(_LATEST, {"w": workflow_id, "t": tenant}).fetchone()
    reason = None
    if workflow is None or workflow.archived:
        reason = "workflow_archived"
    elif version is None:
        reason = "no_published_version"
    else:
        problems = input_problems((version.spec or {}).get("input_schema") or {}, schedule["input"])
        if problems:
            reason = "input_schema_errors: " + "; ".join(problems)
    if reason is not None:
        store.stop(sid, reason, session=session)
        log.warning("schedule %s stopped: %s", sid, reason.split(":", 1)[0])
        return None
    input_json = json.dumps(schedule["input"], sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    run_id = run_id_for(sid, fire_at)
    rt.run_store.create_workflow_run(tenant, workflow_id, version.version, input_json, run_id, schedule_id=sid)
    return {"run_id": str(run_id), "tenant_id": tenant, "version_id": str(version.id), "input_json": input_json,
            "fire_at": fire_at, "cron": schedule["cron"], "timezone": schedule["timezone"], "id": sid}


_ORPHAN_CANDIDATES = text(
    "SELECT r.id AS run_id, r.tenant_id, v.id AS version_id, r.input AS input_json, r.created_at FROM runs r"
    " JOIN workflow_versions v ON v.workflow_id = r.workflow_id AND v.tenant_id = r.tenant_id"
    " AND v.version = r.workflow_version"
    " WHERE r.schedule_id IS NOT NULL AND r.status = 'queued'"
    " AND r.created_at < :newer_than AND r.created_at > :older_than"
    " AND (r.created_at, r.id::text) > (:after_at, :after_id)"
    " ORDER BY r.created_at, r.id::text LIMIT :n"
)
_dbos_engine: Any = None


def _dbos_engine_for(url: str) -> Any:
    global _dbos_engine
    if _dbos_engine is None:
        _dbos_engine = create_engine(url.replace("postgresql://", "postgresql+psycopg://", 1), pool_pre_ping=True)
    return _dbos_engine


def _with_workflow_status(ids: list[str]) -> set[str]:
    """Of `ids`, those that have a row in the DBOS system schema's workflow_status."""
    settings = get_runtime().settings
    schema = settings.dbos_system_schema.replace('"', '""')
    with _dbos_engine_for(settings.dbos_system_database_url).connect() as conn:
        rows = conn.execute(
            text(f'SELECT workflow_uuid FROM "{schema}".workflow_status WHERE workflow_uuid = ANY(:ids)'),
            {"ids": ids},
        ).all()
    return {r[0] for r in rows}


def _orphans(now: datetime) -> list[dict[str, str]]:
    """Orphans: scheduled runs still `queued` after the grace period that have NO DBOS workflow_status
    row, so their workflow was never started (the tick died between the commit and the start). A queued
    run that DBOS already knows (a legitimate backlog) is not one and is skipped. Candidates are paged
    by (created_at, id) so a backlog never hides a real orphan behind it. Starting one again is a no-op
    for DBOS (one workflow per workflow id)."""
    found: list[dict[str, str]] = []
    after_at, after_id = datetime(1970, 1, 1, tzinfo=timezone.utc), ""
    for _ in range(ORPHAN_MAX_PAGES):
        with get_runtime().engine.connect() as conn:
            rows = conn.execute(
                _ORPHAN_CANDIDATES,
                {"newer_than": now - ORPHAN_GRACE, "older_than": now - ORPHAN_WINDOW, "n": ORPHAN_PAGE,
                 "after_at": after_at, "after_id": after_id},
            ).mappings().all()
        if not rows:
            break
        known = _with_workflow_status([str(r["run_id"]) for r in rows])
        for r in rows:
            if str(r["run_id"]) not in known:
                found.append({"run_id": str(r["run_id"]), "tenant_id": r["tenant_id"],
                              "version_id": str(r["version_id"]), "input_json": r["input_json"]})
        if len(found) >= CLAIM_LIMIT:
            return found[:CLAIM_LIMIT]
        after_at, after_id = rows[-1]["created_at"], str(rows[-1]["run_id"])
    return found


def _is_transient(exc: BaseException | None) -> bool:
    """A connection-level failure anywhere in the cause/context chain: OperationalError,
    InterfaceError, OSError, or a DBAPIError whose connection was invalidated. IntegrityError,
    DataError, ProgrammingError... are permanent."""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, (OperationalError, InterfaceError, OSError)):
            return True
        if isinstance(exc, DBAPIError) and exc.connection_invalidated:
            return True
        exc = exc.__cause__ or exc.__context__
    return False


_TRACKED_ACTIVE = text("SELECT id::text FROM workflow_schedules WHERE id::text = ANY(:ids) AND active")


def _prune_failures() -> None:
    """Drop streak entries of schedules that were deleted or stopped/paused (by state, not by this
    tick's claimed page, which a backlog or SKIP LOCKED makes incomplete)."""
    if not _failures:
        return
    with get_runtime().engine.connect() as conn:
        live = {r[0] for r in conn.execute(_TRACKED_ACTIVE, {"ids": list(_failures)})}
    for stale in [k for k in _failures if k not in live]:
        _failures.pop(stale, None)


def _stop_in_savepoint(store: ScheduleStore, schedule_id: str, reason: str, session: Any) -> None:
    """A DB error inside stop rolls back only the savepoint, never the claim transaction."""
    with session.begin_nested():
        store.stop(schedule_id, reason, session=session)


_failures: dict[str, int] = {}  # consecutive fire failures per schedule id (this process)


@DBOS.step(name=TICK_STEP)
async def schedule_tick_step(now: str) -> dict[str, Any]:
    """Claim, fire and advance the due schedules in ONE transaction: `claim_due(session=)` takes the
    rows `FOR UPDATE SKIP LOCKED` and the locks last until the single commit after every
    `mark_fired`/`stop`, so two workers never fire the same row. If the step dies before the commit,
    the next tick claims the same rows again and the deterministic run id makes the repeat harmless.
    Returns `{"counts", "start"}`: the runs whose workflow the tick workflow starts (a DBOS step
    cannot start one), the fired ones plus orphans of an earlier tick that died after its commit."""
    rt = get_runtime()
    store = ScheduleStore(rt.engine)
    # The real clock decides what is due and when the next fire is: a recovered tick can carry a
    # `now` minutes old, which would land next_fire_at in the past and fire once per missed window.
    now_dt = max(datetime.fromisoformat(now), datetime.now(timezone.utc))
    counts = {"due": 0, "fired": 0, "stopped": 0, "failed": 0}
    start: dict[str, dict[str, str]] = {}
    session = store.Session()
    try:
        due = await asyncio.to_thread(store.claim_due, now_dt, CLAIM_LIMIT, session=session)
        counts["due"] = len(due)
        await asyncio.to_thread(_prune_failures)
        for schedule in due:
            try:
                started = await asyncio.to_thread(_fire, session, store, schedule, now_dt)
            except Exception as exc:  # noqa: BLE001 - one schedule's failure never stops the others
                counts["failed"] += 1
                if _is_transient(exc):  # an outage never stops a schedule or counts
                    log.warning("schedule %s not fired (%s); the next tick retries", schedule["id"], type(exc).__name__)
                    continue
                streak = _failures.get(schedule["id"], 0) + 1
                if streak >= MAX_FIRE_FAILURES:
                    reason = f"fire_failed: {type(exc).__name__}"
                    try:
                        await asyncio.to_thread(_stop_in_savepoint, store, schedule["id"], reason, session)
                        _failures.pop(schedule["id"], None)
                        log.warning("schedule %s stopped: fire_failed (%s)", schedule["id"], type(exc).__name__)
                    except Exception as stop_exc:  # noqa: BLE001 - never lose the other schedules' work
                        log.warning("schedule %s could not be stopped (%s); the streak stays", schedule["id"],
                                    type(stop_exc).__name__)
                else:
                    _failures[schedule["id"]] = streak
                    log.warning("schedule %s not fired (%s, %d/%d); the next tick retries", schedule["id"],
                                type(exc).__name__, streak, MAX_FIRE_FAILURES)
                continue
            _failures.pop(schedule["id"], None)
            if started is None:
                counts["stopped"] += 1
                continue
            # A missed window fires once: the next time comes from now, not the stale next_fire_at.
            await asyncio.to_thread(
                store.mark_fired, started["id"], started["fire_at"],
                next_fire(started["cron"], started["timezone"], now_dt), session=session,
            )
            start[started["run_id"]] = {k: started[k] for k in ("run_id", "tenant_id", "version_id", "input_json")}
            counts["fired"] += 1
        await asyncio.to_thread(session.commit)
    except BaseException:
        await asyncio.to_thread(session.rollback)
        raise
    finally:
        await asyncio.to_thread(session.close)
    for orphan in await asyncio.to_thread(_orphans, now_dt):
        start.setdefault(orphan["run_id"], orphan)
    return {"counts": counts, "start": list(start.values())}


# --------------------------------------------------------------------------- the workflow


@DBOS.workflow(name=SCHEDULE_TICK_WORKFLOW_NAME)
async def schedule_tick(scheduled_at: datetime, context: Any) -> dict[str, int]:
    """The scheduled tick (DBOS passes the tick time and the schedule context)."""
    result = await schedule_tick_step(_utc(scheduled_at).isoformat())  # scheduled_at is for logging; the step reads the real clock
    for item in result["start"]:
        with SetWorkflowID(item["run_id"]):
            await DBOS.start_workflow_async(
                workflow_run, item["run_id"], item["tenant_id"], item["version_id"], item["input_json"]
            )
    counts = result["counts"]
    if counts["due"] or result["start"]:
        log.info("schedule tick at %s: %s started=%d", scheduled_at.isoformat(), counts, len(result["start"]))
    return counts


def apply_schedule_tick(config: ScheduleTickConfig | None = None) -> str:
    """Register (or replace) the schedule after DBOS.launch(): `applied` (an upsert by name, safe on
    every start and every worker), or `removed` for EAP_SCHEDULE_TICK=off."""
    config = config or ScheduleTickConfig.from_env()
    if config.off:
        DBOS.delete_schedule(SCHEDULE_TICK_SCHEDULE_NAME)
        log.info("schedule tick off: schedule removed")
        return "removed"
    DBOS.apply_schedules(
        [{"schedule_name": SCHEDULE_TICK_SCHEDULE_NAME, "workflow_fn": schedule_tick,
          "schedule": config.schedule, "context": {}}]
    )
    log.info("schedule tick scheduled: schedule=%s", config.schedule)
    return "applied"


__all__ = [
    "SCHEDULE_TICK_SCHEDULE_NAME",
    "SCHEDULE_TICK_WORKFLOW_NAME",
    "ScheduleTickConfig",
    "ScheduleTickConfigError",
    "apply_schedule_tick",
    "run_id_for",
    "schedule_tick",
    "schedule_tick_step",
]
