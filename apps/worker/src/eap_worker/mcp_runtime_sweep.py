"""The managed MCP runtime sweep (`eap.mcp_runtime_sweep`): a scheduled workflow, every minute.

State transitions it makes on `mcp_runtimes` (all conditional on the state it read, so a caller that
changed the row meanwhile wins):

- `running` / `idle`, last used (`last_used_at`, else `updated_at`) longer ago than EAP_MCP_IDLE_MINUTES
  (default 30)  ->  `stopping`; then `McpRuntime.stop`, which scales the server down and keeps its
  objects; then `stopped`. A caller that arrives while the row is `stopping` waits for `stopped` and
  then starts it (eap_worker.mcp.ensure_managed_running). When `stop` raises, the row becomes `failed`
  with `last_error = stop_failed: <exception class>`; `stopping` for longer than 10 minutes becomes
  `failed` with `last_error = stop_timed_out`.
  A runtime whose server has `keep_warm` set (mcp_servers.keep_warm) is never claimed here.
- `starting` for longer than 10 minutes (`updated_at`)  ->  `failed` with `last_error = start_timed_out`
  (a worker that died while starting, or a pod that never became ready).

The state is claimed first (one UPDATE ... RETURNING), the adapter is called second, so two workers
never stop the same runtime twice; the row only becomes `stopped` once `stop` has returned, so a late
stop can never scale down a runtime a caller has just started.

Settings: EAP_MCP_RUNTIME_SWEEP_SCHEDULE (a DBOS cron, seconds first; default `0 * * * * *`; `off`
removes the schedule), EAP_MCP_IDLE_MINUTES.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from dbos import DBOS
from sqlalchemy import text
from sqlalchemy.engine import Engine

from eap_mcp_runtime import McpRuntime, RuntimeHandle
from eap_worker.mcp import get_mcp_runtime
from eap_worker.runtime import get_runtime

log = logging.getLogger("eap_worker.mcp_runtime_sweep")

WORKFLOW_NAME = "eap.mcp_runtime_sweep"
SCHEDULE_NAME = "eap.mcp_runtime_sweep"
STEP_NAME = "eap.mcp_runtime_sweep.run"
SCHEDULE_ENV = "EAP_MCP_RUNTIME_SWEEP_SCHEDULE"
IDLE_MINUTES_ENV = "EAP_MCP_IDLE_MINUTES"
DEFAULT_SCHEDULE = "0 * * * * *"  # every minute (DBOS crons put the seconds first)
DEFAULT_IDLE_MINUTES = 30
STARTING_TIMEOUT = timedelta(minutes=10)
OFF = "off"
BATCH = 100


class RuntimeSweepConfigError(ValueError):
    def __init__(self, name: str, problem: str) -> None:
        super().__init__(name, problem)
        self.name, self.problem = name, problem

    def __str__(self) -> str:
        return f"{self.name}: {self.problem}"


def idle_minutes(env: Mapping[str, str] | None = None) -> int:
    """EAP_MCP_IDLE_MINUTES: a positive integer; 30 when unset or unusable."""
    raw = (os.environ if env is None else env).get(IDLE_MINUTES_ENV, "")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_IDLE_MINUTES
    return value if 1 <= value <= 7 * 24 * 60 else DEFAULT_IDLE_MINUTES


@dataclass(frozen=True)
class RuntimeSweepConfig:
    schedule: str = DEFAULT_SCHEDULE
    off: bool = False

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "RuntimeSweepConfig":
        env = os.environ if environ is None else environ
        raw = (env.get(SCHEDULE_ENV) or "").strip()
        if raw.lower() == OFF:
            return cls(off=True)
        raw_idle = (env.get(IDLE_MINUTES_ENV) or "").strip()
        if raw_idle and not (raw_idle.isdigit() and int(raw_idle) >= 1):
            raise RuntimeSweepConfigError(IDLE_MINUTES_ENV, "must be a positive integer")
        return cls(schedule=raw or DEFAULT_SCHEDULE)  # DBOS validates the cron at apply time


_FAIL_STARTING = text(
    "UPDATE mcp_runtimes SET state = 'failed', last_error = 'start_timed_out', updated_at = :now "
    "WHERE state = 'starting' AND updated_at < :cutoff RETURNING id"
)
_FAIL_STOPPING = text(
    "UPDATE mcp_runtimes SET state = 'failed', last_error = 'stop_timed_out', updated_at = :now "
    "WHERE state = 'stopping' AND updated_at < :cutoff RETURNING id"
)
_CLAIM_IDLE = text(
    "UPDATE mcp_runtimes SET state = 'stopping', last_error = NULL, updated_at = :now "
    "WHERE state IN ('running', 'idle') AND COALESCE(last_used_at, updated_at) < :cutoff "
    "AND NOT EXISTS (SELECT 1 FROM mcp_servers s WHERE s.id = mcp_runtimes.server_id AND s.keep_warm) "
    "RETURNING id, namespace, cr_name"
)
_STOP_FAILED = text(
    "UPDATE mcp_runtimes SET state = 'failed', last_error = :err, updated_at = :now WHERE id = :i AND state = 'stopping'"
)
_STOPPED = text("UPDATE mcp_runtimes SET state = 'stopped', updated_at = :now WHERE id = :i AND state = 'stopping'")


async def sweep_runtimes(
    engine: Engine, runtime: McpRuntime, *, now: datetime | None = None, idle_minutes: int = DEFAULT_IDLE_MINUTES
) -> dict[str, int]:
    """One sweep. Returns `{"stopped", "failed_starting", "stop_failed"}` counts (never ids or names)."""
    now = now or datetime.now(timezone.utc)

    def claim() -> tuple[int, list[Any]]:
        with engine.begin() as conn:
            failed = conn.execute(_FAIL_STARTING, {"now": now, "cutoff": now - STARTING_TIMEOUT}).all()
            conn.execute(_FAIL_STOPPING, {"now": now, "cutoff": now - STARTING_TIMEOUT})
            idle = conn.execute(_CLAIM_IDLE, {"now": now, "cutoff": now - timedelta(minutes=idle_minutes)}).all()
        return len(failed), idle

    failed_starting, idle = await asyncio.to_thread(claim)
    stopped = stop_failed = 0
    for row in idle:
        try:
            await runtime.stop(RuntimeHandle(row.namespace, row.cr_name))
            with engine.begin() as conn:
                conn.execute(_STOPPED, {"now": datetime.now(timezone.utc), "i": row.id})
            stopped += 1
        except Exception as exc:  # noqa: BLE001 - one runtime's failure never stops the sweep
            stop_failed += 1
            log.warning("mcp runtime %s could not be stopped (%s)", row.id, type(exc).__name__)

            def mark(i: Any = row.id, e: str = f"stop_failed: {type(exc).__name__}") -> None:
                with engine.begin() as conn:
                    conn.execute(_STOP_FAILED, {"err": e, "now": now, "i": i})

            await asyncio.to_thread(mark)
    if failed_starting or idle:
        log.info("mcp runtime sweep: stopped=%d stop_failed=%d starting_failed=%d", stopped, stop_failed, failed_starting)
    return {"stopped": stopped, "failed_starting": failed_starting, "stop_failed": stop_failed}


@DBOS.step(name=STEP_NAME)
async def sweep_step() -> dict[str, int]:
    """The real clock is read here, inside the step: a replayed workflow must not stop runtimes by a stale time."""
    return await sweep_runtimes(get_runtime().engine, get_mcp_runtime(), now=datetime.now(timezone.utc), idle_minutes=idle_minutes())


@DBOS.workflow(name=WORKFLOW_NAME)
async def mcp_runtime_sweep(scheduled_at: datetime, context: Any) -> dict[str, int]:
    """The scheduled sweep (DBOS passes the tick time and the schedule context). The real clock decides
    what is idle: a recovered tick must not stop runtimes by a stale time."""
    del scheduled_at, context
    return await sweep_step()


def apply_runtime_sweep_schedule(config: RuntimeSweepConfig | None = None) -> str:
    """Register (or replace) the schedule after DBOS.launch(): `applied` (an upsert by name, safe on
    every start and every worker) or `removed` for EAP_MCP_RUNTIME_SWEEP_SCHEDULE=off."""
    config = config or RuntimeSweepConfig.from_env()
    if config.off:
        DBOS.delete_schedule(SCHEDULE_NAME)
        log.info("mcp runtime sweep off: schedule removed")
        return "removed"
    DBOS.apply_schedules(
        [{"schedule_name": SCHEDULE_NAME, "workflow_fn": mcp_runtime_sweep, "schedule": config.schedule, "context": {}}]
    )
    log.info("mcp runtime sweep scheduled: schedule=%s idle_minutes=%d", config.schedule, idle_minutes())
    return "applied"


__all__ = [
    "IDLE_MINUTES_ENV",
    "RuntimeSweepConfig",
    "RuntimeSweepConfigError",
    "SCHEDULE_NAME",
    "apply_runtime_sweep_schedule",
    "idle_minutes",
    "mcp_runtime_sweep",
    "sweep_runtimes",
]
