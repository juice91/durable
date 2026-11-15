"""Starting runs on the orchestration side, per the run hand-off contract.

docs/plans/2026-11-11-milestone-1-vertical-slice.md, "Run hand-off contract": the API never
imports worker code; it enqueues by name through `dbos.DBOSClient` against the DBOS system
database, with workflow_id = run_id, so a repeated enqueue for the same run attaches to the
existing workflow instead of starting a second one (DBOS's default
`workflow_id_reuse_policy` is "return-existing").
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from eap_observability import dbos_trace_attributes
from eap_schemas.knowledge import kb_ingest_partition_key
from fastapi import Request

QUEUE_NAME = "eap-agent-runs"
WORKFLOW_NAME = "eap.agent_run"
# Tool test runs (POST /tools/{tool_id}/runs): their own workflow name, because their arguments are
# not the agent run's, on the same queue, so one worker pool and one app_version serve both.
TOOL_WORKFLOW_NAME = "eap.tool_run"
# Workflow runs (POST /workflows/{workflow_id}/runs): `eap.workflow_run(run_id, tenant_id,
# workflow_version_id, input_json)` on the same queue.
WORKFLOW_RUN_NAME = "eap.workflow_run"
# Knowledge base ingest runs (RAG and memory milestone, RM.7 enqueues, RM.10 implements):
# `eap.kb_ingest(run_id, tenant_id, knowledge_base_id, input_json)` on its own queue,
# KB_INGEST_QUEUE_NAME (partitioned, see below), so a large ingest or reindex never holds agent-run
# concurrency slots (checkpoint review, 2026-11-14), workflow_id = run_id. All four arguments are strings. `input_json` is the run's stored `input`, canonical JSON
# (sorted keys, no spaces), in one of two modes:
#   ingest:  {"doc_version":<int>,"document_id":"<uuid>"}  ingest that version of the document
#   reindex: {"reindex":<int>}                             embed the knowledge base's pending
#                                                          generation <int> (model and dimensions
#                                                          are the knowledge base's pending_* columns)
#
# The queue is partitioned (RAG P1 follow-up): every run is enqueued with the partition key
# `eap_schemas.knowledge.kb_ingest_partition_key(tenant_id, knowledge_base_id)`, so the worker
# draws fairly from every knowledge base with queued runs instead of one FIFO line where a bulk
# import starves single uploads. A DBOS queue is either partitioned or not, so the partitioned one
# has a new name; the worker still registers LEGACY_KB_INGEST_QUEUE_NAME (`eap-kb-ingest`,
# unpartitioned) so runs enqueued there before the upgrade still run (apps/worker/README.md).
KB_INGEST_WORKFLOW_NAME = "eap.kb_ingest"
KB_INGEST_QUEUE_NAME = "eap-kb-ingest-fair"
LEGACY_KB_INGEST_QUEUE_NAME = "eap-kb-ingest"


def approval_topic(step_id: str) -> str:
    """The DBOS message topic an approval step waits on (eap_worker.workflow_run.approval_topic)."""
    return f"approval:{step_id}"

_DEFAULT_DBOS_URL = "postgresql://eap_dbos:eap_dbos_password@localhost:54329/eap_dbos"


@dataclass(frozen=True)
class LauncherSettings:
    system_database_url: str
    system_schema: str
    app_version: str

    @classmethod
    def from_env(cls) -> "LauncherSettings":
        # Same names and fallbacks as the worker (apps/worker/src/eap_worker/config.py).
        return cls(
            system_database_url=os.environ.get(
                "EAP_DBOS_URL", os.environ.get("DBOS_SYSTEM_DATABASE_URL", _DEFAULT_DBOS_URL)
            ),
            system_schema=os.environ.get("EAP_DBOS_SYSTEM_SCHEMA", "dbos"),
            app_version=os.environ.get("EAP_APP_VERSION", "m1"),
        )


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

    def notify_approval(self, *, workflow_id: str, step_id: str, decision: str) -> None: ...

    def close(self) -> None: ...


class DBOSRunLauncher:
    """Enqueues `eap.agent_run`, `eap.tool_run` and `eap.workflow_run` on `eap-agent-runs`, and
    `eap.kb_ingest` on the partitioned `eap-kb-ingest-fair` (keyed by tenant and knowledge base). The DBOSClient is created on first use."""

    def __init__(self, settings: LauncherSettings, client: Any | None = None) -> None:
        self.settings = settings
        self._client = client
        self._lock = threading.Lock()

    def _get_client(self) -> Any:
        with self._lock:
            if self._client is None:
                from dbos import DBOSClient

                self._client = DBOSClient(
                    system_database_url=self.settings.system_database_url,
                    dbos_system_schema=self.settings.system_schema,
                    lazy=True,
                    # Fail the request instead of blocking it while the system DB is down.
                    retry_connection_errors=False,
                )
            return self._client

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
    ) -> None:
        options = self._options(WORKFLOW_NAME, run_id, traceparent)
        self._get_client().enqueue(options, str(run_id), tenant_id, str(agent_id), version, spec_json, input_text)

    def launch_tool(
        self,
        *,
        run_id: UUID,
        tenant_id: str,
        tool_id: str,
        input_json: str,
        traceparent: str | None = None,
    ) -> None:
        """Enqueue `eap.tool_run(run_id, tenant_id, tool_id, input_json)`, workflow_id = run_id."""
        options = self._options(TOOL_WORKFLOW_NAME, run_id, traceparent)
        self._get_client().enqueue(options, str(run_id), tenant_id, tool_id, input_json)

    def launch_workflow(
        self,
        *,
        run_id: UUID,
        tenant_id: str,
        workflow_version_id: str,
        input_json: str,
        traceparent: str | None = None,
    ) -> None:
        """Enqueue `eap.workflow_run(run_id, tenant_id, workflow_version_id, input_json)`, workflow_id = run_id."""
        options = self._options(WORKFLOW_RUN_NAME, run_id, traceparent)
        self._get_client().enqueue(options, str(run_id), tenant_id, workflow_version_id, input_json)

    def launch_kb_ingest(
        self,
        *,
        run_id: UUID,
        tenant_id: str,
        knowledge_base_id: str,
        input_json: str,
        traceparent: str | None = None,
    ) -> None:
        """Enqueue `eap.kb_ingest(run_id, tenant_id, knowledge_base_id, input_json)`, workflow_id = run_id
        (see KB_INGEST_WORKFLOW_NAME for the input modes)."""
        options = self._options(KB_INGEST_WORKFLOW_NAME, run_id, traceparent, queue_name=KB_INGEST_QUEUE_NAME)
        options["queue_partition_key"] = kb_ingest_partition_key(tenant_id, str(knowledge_base_id))
        self._get_client().enqueue(options, str(run_id), tenant_id, str(knowledge_base_id), input_json)

    def notify_approval(self, *, workflow_id: str, step_id: str, decision: str) -> None:
        """Wake the DBOS workflow waiting on approval step `step_id` (DBOS send on its topic). Only a
        wake-up: the decision itself is the run's `step:{step_id}:decision` event."""
        self._get_client().send(workflow_id, {"decision": decision}, approval_topic(step_id))

    def _options(
        self, workflow_name: str, run_id: UUID, traceparent: str | None, *, queue_name: str = QUEUE_NAME
    ) -> dict[str, Any]:
        options: dict[str, Any] = {
            "queue_name": queue_name,
            "workflow_name": workflow_name,
            "workflow_id": str(run_id),
            "app_version": self.settings.app_version,
        }
        # The run's stored request traceparent, as a DBOS workflow attribute: DBOS parents its
        # workflow span on it (durably, so recovery stays on the trace). Absent when telemetry is off.
        attributes = dbos_trace_attributes(traceparent)
        if attributes is not None:
            options["attributes"] = attributes
        return options

    def close(self) -> None:
        with self._lock:
            client, self._client = self._client, None
        if client is not None and hasattr(client, "destroy"):
            client.destroy()


def get_run_launcher(request: Request) -> RunLauncher:
    return request.app.state.run_launcher
