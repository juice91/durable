"""Process-wide resources for the worker: settings, app-DB engine, stores, tool registry.

DBOS steps and the agent's tools reach these through `get_runtime()` rather than through step
arguments, so nothing with a DB connection ever has to cross a step boundary. A process that only
recovers workflows (a restarted worker) builds them lazily from the environment.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from functools import cached_property
from typing import Any
from uuid import UUID

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from eap_run_store import EventLog, PostgresSideEffectStore, RunStore, create_tables
from eap_runtime import RunDeps, ToolRegistry, default_registry
from eap_schemas import AgentSpec
from eap_worker.config import DELTA_CHANNEL, WorkerSettings
from eap_worker.crash import crash_point
from eap_worker.tickets import FakeExternalTicketService, create_ticket_tables

log = logging.getLogger("eap_worker")

# Postgres NOTIFY payloads must stay under 8000 bytes; deltas larger than this are split.
_MAX_DELTA_CHARS = 2000


@dataclass(frozen=True)
class WorkerDeps(RunDeps):
    """Run deps for the generic agent: the runtime's RunDeps plus the run's published spec."""

    spec: AgentSpec
    allowed_tools: frozenset[str]


class _CrashAfterCallTicketService:
    """Wraps the external service so the `after_tool_call` crash point sits between the external
    call returning and its result being recorded (side_effects.complete + DBOS checkpoint)."""

    def __init__(self, inner: FakeExternalTicketService) -> None:
        self.inner = inner

    def create(self, title: str, body: str, *, idempotency_key: str) -> dict:
        result = self.inner.create(title, body, idempotency_key=idempotency_key)
        crash_point("after_tool_call")
        return result


class WorkerRuntime:
    def __init__(self, settings: WorkerSettings) -> None:
        self.settings = settings
        self.engine: Engine = create_engine(settings.app_database_url, pool_pre_ping=True)
        self.run_store = RunStore(self.engine)
        self.event_log = EventLog(self.engine)
        self.tickets = FakeExternalTicketService(self.engine)
        self.registry: ToolRegistry = default_registry()

    def ensure_schema(self) -> None:
        create_tables(self.engine)
        create_ticket_tables(self.engine)

    @cached_property
    def allowed_tools(self) -> frozenset[str]:
        if self.settings.allowed_tools is None:
            return frozenset(self.registry.tool_ids())
        return self.settings.allowed_tools

    def deps_for(self, run_id: str, tenant_id: str, spec: AgentSpec) -> WorkerDeps:
        return WorkerDeps(
            tenant_id=tenant_id,
            run_id=UUID(run_id),
            side_effects=PostgresSideEffectStore(self.engine, tenant_id),
            ticket_service=_CrashAfterCallTicketService(self.tickets),
            spec=spec,
            allowed_tools=self.allowed_tools,
        )

    def publish_delta(self, run_id: str, model_request: int, attempt: str, delta: str) -> None:
        with self.engine.begin() as conn:
            for i in range(0, len(delta), _MAX_DELTA_CHARS):
                payload: dict[str, Any] = {
                    "run_id": run_id,
                    "model_request": model_request,
                    "attempt": attempt,
                    "delta": delta[i : i + _MAX_DELTA_CHARS],
                }
                conn.execute(text("SELECT pg_notify(:ch, :payload)"), {"ch": DELTA_CHANNEL, "payload": json.dumps(payload)})


_runtime: WorkerRuntime | None = None
_lock = threading.Lock()


def configure_runtime(settings: WorkerSettings) -> WorkerRuntime:
    global _runtime
    with _lock:
        _runtime = WorkerRuntime(settings)
        _runtime.ensure_schema()
        return _runtime


def get_runtime() -> WorkerRuntime:
    if _runtime is None:
        return configure_runtime(WorkerSettings.from_env())
    return _runtime
