"""Ports: interfaces shared by services and providers (no web or platform-entry imports)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID


class RunLauncher(Protocol):
    def launch(
        self,
        *,
        run_id: UUID,
        tenant_id: str,
        agent_id: UUID,
        version: int,
        spec_json: str,
        input_text: str,
        traceparent: str | None = None,
    ) -> None: ...

    def launch_tool(
        self,
        *,
        run_id: UUID,
        tenant_id: str,
        tool_id: str,
        input_json: str,
        traceparent: str | None = None,
    ) -> None: ...

    def launch_workflow(
        self,
        *,
        run_id: UUID,
        tenant_id: str,
        workflow_version_id: str,
        input_json: str,
        traceparent: str | None = None,
    ) -> None: ...

    def launch_kb_ingest(
        self,
        *,
        run_id: UUID,
        tenant_id: str,
        knowledge_base_id: str,
        input_json: str,
        traceparent: str | None = None,
    ) -> None: ...

    def launch_eval(
        self,
        *,
        run_id: UUID,
        tenant_id: str,
        input_json: str,
        traceparent: str | None = None,
    ) -> None: ...

    def launch_kb_source_sync(
        self,
        *,
        run_id: UUID,
        tenant_id: str,
        source_id: str,
        request_json: str,
        traceparent: str | None = None,
    ) -> None: ...

    def launch_score(self, *, run_id: UUID, tenant_id: str, scorer_ids: list[str]) -> None: ...

    def notify_approval(self, *, workflow_id: str, step_id: str, decision: str) -> None: ...

    def notify_tool_approval(
        self, *, run_id: str, tool_call_id: str, decision: str, workflow_id: str | None = None
    ) -> None: ...

    def cancel_workflow_tree(self, *, workflow_id: str) -> None: ...

    def recovery_attempts(self, run_ids: list[str]) -> dict[str, int]: ...

    def recovered_run_ids(self, *, since: datetime, until: datetime) -> list[str]: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class StartedRun:
    """A run recorded and handed to the orchestrator: the stored row (`run`, as RunStore.get_run returns it)
    and whether this call created it (False: a retry of a recorded run_id)."""

    run: dict[str, Any]
    created: bool

    @property
    def run_id(self) -> UUID:
        return UUID(str(self.run["id"]))


class RunWaitTimeout(Exception):
    """`RunStarter.wait_for_run` ran out of time; the run's tree was cancelled before this was raised."""


class RunStarter(Protocol):
    """What a service needs to run an agent once and read its answer (implemented by services.runs.RunService)."""

    def start_agent_run(
        self,
        tenant_id: str,
        agent_id: UUID,
        *,
        input_text: str,
        run_id: UUID | None = None,
        version: int | None = None,
        request_context: dict[str, Any] | None = None,
    ) -> StartedRun:
        """Record a run of the tenant's agent (its latest published version unless `version`) idempotently
        (a client `run_id`, default fresh) and hand it to the worker. Raises ServiceError: NotFound (agent),
        Conflict (archived, no version, run_id used by a different run), Unavailable (hand-off failed)."""
        ...

    def wait_for_run(
        self, tenant_id: str, run_id: UUID, *, timeout: float, poll_interval: float = 0.5
    ) -> dict[str, Any]:
        """Poll until the run has a terminal status and return the stored row; on timeout cancel the run's
        tree and raise RunWaitTimeout."""
        ...

    def read_run_json(self, tenant_id: str, run_id: UUID) -> dict[str, Any]:
        """The run as GET /runs/{id} answers it (NotFound for an unknown run or another tenant's)."""
        ...
