"""Ports: interfaces shared by services and providers (no web or platform-entry imports)."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
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
