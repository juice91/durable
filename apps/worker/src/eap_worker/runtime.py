"""Process-wide resources for the worker: settings, app-DB engine, stores, tool registry.

DBOS steps and the agent's tools reach these through `get_runtime()` rather than through step
arguments, so nothing with a DB connection ever has to cross a step boundary. A process that only
recovers workflows (a restarted worker) builds them lazily from the environment.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any
from uuid import UUID

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from eap_db import require_schema_at_head
from eap_run_store import EventLog, PostgresSideEffectStore, RunStore
from eap_runtime import RequestContext, RunDeps, ToolRegistry, default_registry
from eap_schemas import AgentSpec
from eap_schemas.request_context import RESERVED_CONTEXT_NAMES
from eap_worker.config import DELTA_CHANNEL, WorkerSettings
from eap_worker.crash import crash_point
from eap_worker.tickets import FakeExternalTicketService, TenantTicketService

log = logging.getLogger("eap_worker")

# Postgres NOTIFY payloads must stay under 8000 bytes; deltas larger than this are split.
_MAX_DELTA_CHARS = 2000


def declared_context(spec: AgentSpec, request_context: dict[str, Any] | None) -> dict[str, Any]:
    """Defence in depth (P0.21): only the keys the spec declares as context fields, never a reserved
    identity name (tenant_id, user_id, run_id), even if the stored row holds more. The API already
    validates the context; this guards a tampered or legacy row. Not a step: no layout change."""
    declared = {f.name for f in spec.context_fields} - RESERVED_CONTEXT_NAMES
    return {k: v for k, v in (request_context or {}).items() if k in declared}


@dataclass
class RetrieveTally:
    """How many sources each retrieve call of the run returned (tool_call_id -> count), filled by
    the RetrieveNumbering capability (eap_worker.platform_tools) from each call's result.

    It is per workflow EXECUTION, not durable state: WorkerDeps builds a fresh one each time the
    workflow function runs, and the capability's hook runs in the workflow, outside the call_tool
    step, so on a replay it is rebuilt from the checkpointed results of the earlier call steps, in
    the same order (parallel_execution_mode "sequential"). It is only consulted for calls of the
    CURRENT model response, whose results are not yet in the run's messages."""

    counts: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkerDeps(RunDeps):
    """Run deps for the generic agent: the runtime's RunDeps (with the run's read-only
    `request_context`) plus the run's spec (published version, or a draft run's snapshot).
    `retrieve_tally` is per workflow execution (a fresh one per deps), never durable state."""

    spec: AgentSpec
    allowed_tools: frozenset[str]
    retrieve_tally: RetrieveTally = field(default_factory=RetrieveTally, kw_only=True, compare=False, repr=False)
    # Step processors: (model request n, tool_call id) -> the arguments the tool ran with (or a
    # withheld marker), set by StepProcessorCapability. Per workflow execution, like retrieve_tally.
    step_decisions: dict = field(default_factory=dict, kw_only=True, compare=False, repr=False)


class _CrashAfterCallTicketService:
    """Wraps the external service so the `after_tool_call` crash point sits between the external
    call returning and its result being recorded (side_effects.complete + DBOS checkpoint)."""

    def __init__(self, inner: TenantTicketService) -> None:
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
        # Hidden platform tools (knowledge.search, RM.11): tool runs only; an agent spec that lists
        # one is refused (eap_worker.platform_tools.check_no_hidden_tools).
        from eap_worker.platform_tools import register_platform_tools

        register_platform_tools(self.registry)

    def check_schema(self) -> None:
        """Fail fast unless Alembic has migrated eap_app to head; the worker never creates tables."""
        require_schema_at_head(self.engine)

    @cached_property
    def allowed_tools(self) -> frozenset[str]:
        # Hidden platform tools (knowledge.search) are never allowed for agents or workflow steps;
        # only a tool run may call one (eap_worker.tool_run.check_tool_step, hidden_ok).
        from eap_worker.platform_tools import HIDDEN_TOOL_IDS

        if self.settings.allowed_tools is None:
            return frozenset(self.registry.tool_ids()) - HIDDEN_TOOL_IDS
        return self.settings.allowed_tools - HIDDEN_TOOL_IDS

    def deps_for(
        self, run_id: str, tenant_id: str, spec: AgentSpec, request_context: dict[str, Any] | None = None
    ) -> WorkerDeps:
        return WorkerDeps(
            tenant_id=tenant_id,
            run_id=UUID(run_id),
            side_effects=PostgresSideEffectStore(self.engine, tenant_id),
            ticket_service=_CrashAfterCallTicketService(self.tickets.for_tenant(tenant_id)),
            spec=spec,
            allowed_tools=self.allowed_tools,
            request_context=RequestContext(declared_context(spec, request_context)),
        )

    def tool_deps_for(self, run_id: str, tenant_id: str) -> RunDeps:
        """Deps for a tool test run's single tool call: the same side-effect store and (crash-hooked)
        ticket service an agent run's tools get, without an agent spec."""
        return RunDeps(
            tenant_id=tenant_id,
            run_id=UUID(run_id),
            side_effects=PostgresSideEffectStore(self.engine, tenant_id),
            ticket_service=_CrashAfterCallTicketService(self.tickets.for_tenant(tenant_id)),
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
        runtime = WorkerRuntime(settings)
        try:
            runtime.check_schema()
        except Exception:
            runtime.engine.dispose()
            raise
        _runtime = runtime
        return _runtime


def get_runtime() -> WorkerRuntime:
    if _runtime is None:
        return configure_runtime(WorkerSettings.from_env())
    return _runtime
