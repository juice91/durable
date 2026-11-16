"""Schedule tick (parity round 2, Task 5, decision D2): the scheduled DBOS workflow
`eap.schedule_tick` that starts a run for every due workflow schedule.

    eap.schedule_tick.fire    ONE step, ONE transaction: claim_due(session=) FOR UPDATE SKIP LOCKED,
                              per schedule validate + create the run + mark_fired/stop, then a
                              single commit
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
- Idempotent: the run id is `uuid5(schedule id, fire time as UTC isoformat)`. A repeat for the same
  fire time (a tick that crashed before mark_fired, two workers) finds the same run row, and DBOS
  starts one workflow per workflow id.

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
from sqlalchemy import text

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


_ORPHANS = text(
    "SELECT r.id AS run_id, r.tenant_id, v.id AS version_id, r.input AS input_json FROM runs r"
    " JOIN workflow_versions v ON v.workflow_id = r.workflow_id AND v.tenant_id = r.tenant_id"
    " AND v.version = r.workflow_version"
    " WHERE r.schedule_id IS NOT NULL AND r.status = 'queued'"
    " AND r.created_at < :newer_than AND r.created_at > :older_than ORDER BY r.created_at LIMIT :n"
)


def _orphans(now: datetime) -> list[dict[str, str]]:
    """Scheduled runs still `queued` after the grace period: their workflow was never started (the
    tick died between the commit and the start). Starting one again is a no-op for DBOS (one
    workflow per workflow id)."""
    with get_runtime().engine.connect() as conn:
        rows = conn.execute(
            _ORPHANS, {"newer_than": now - ORPHAN_GRACE, "older_than": now - ORPHAN_WINDOW, "n": CLAIM_LIMIT}
        ).mappings().all()
    return [{"run_id": str(r["run_id"]), "tenant_id": r["tenant_id"], "version_id": str(r["version_id"]),
             "input_json": r["input_json"]} for r in rows]


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
    now_dt = datetime.fromisoformat(now)
    counts = {"due": 0, "fired": 0, "stopped": 0, "failed": 0}
    start: dict[str, dict[str, str]] = {}
    session = store.Session()
    try:
        due = await asyncio.to_thread(store.claim_due, now_dt, CLAIM_LIMIT, session=session)
        counts["due"] = len(due)
        for schedule in due:
            try:
                started = await asyncio.to_thread(_fire, session, store, schedule, now_dt)
            except Exception as exc:  # noqa: BLE001 - one schedule's failure never stops the others
                counts["failed"] += 1
                log.warning("schedule %s not fired (%s); the next tick retries", schedule["id"], type(exc).__name__)
                continue
            if started is None:
                counts["stopped"] += 1
                continue
            # A missed window fires once: the next time comes from now, not the stale next_fire_at.
            store.mark_fired(
                started["id"], started["fire_at"], next_fire(started["cron"], started["timezone"], now_dt),
                session=session,
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
    result = await schedule_tick_step(datetime.now(timezone.utc).isoformat())
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
