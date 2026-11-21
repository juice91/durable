"""Starting runs on the orchestration side, per the run hand-off contract.

docs/plans/2026-11-11-milestone-1-vertical-slice.md, "Run hand-off contract": the API never
imports worker code; it enqueues by name through `dbos.DBOSClient` against the DBOS system
database, with workflow_id = run_id, so a repeated enqueue for the same run attaches to the
existing workflow instead of starting a second one (DBOS's default
`workflow_id_reuse_policy` is "return-existing").
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID, uuid4

from eap_observability import dbos_trace_attributes
# The DBOS topic an agent run's gated tool calls wait on, `tool_approval:{run_id}`: defined in
# eap_run_store.approvals so the API's wake-up and the worker's wait (AC.6) share one builder.
from eap_run_store.approvals import tool_approval_topic
from eap_schemas.knowledge import kb_ingest_partition_key

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
# Eval runs (agent capabilities milestone; AC.12 enqueues, AC.11 implements):
# `eap.eval_run(run_id, tenant_id, input_json)` on the agent-run queue, workflow_id = run_id, where
# `input_json` is the run's stored `input`: the suite snapshot's canonical JSON
# (`EvalSuiteSnapshot.canonical_json()`). The agent version or draft snapshot is read from the run row.
# Size note (AC.12 review, for AC.11): the snapshot can reach ~32 MiB for 500 maximum-size cases, and
# DBOS stores workflow inputs again in its system database. AC.11 may read `runs.input` by run id
# instead and ignore `input_json`; the argument stays so the hand-off shape matches the plan.
EVAL_WORKFLOW_NAME = "eap.eval_run"
# Knowledge-base source syncs (kb source sync, Task 4): `eap.kb_source_sync(run_id, tenant_id, source_id,
# request_json)` on the agent-run queue, workflow_id = run_id; `request_json` is the canonical JSON of
# `{mode, part_numbers}` (eap_run_store.kb_source_launch.launch_sync is the one caller).
KB_SOURCE_SYNC_WORKFLOW_NAME = "eap.kb_source_sync"
# Score on demand (parity round 2): `eap.score_run_on_demand(run_id, tenant_id, scorer_ids)` on the agent-run queue;
# its workflow id is `score:{run_id}:{random}` (a run can be scored any number of times, and its own
# workflow id is the run id).
SCORE_WORKFLOW_NAME = "eap.score_run_on_demand"  # not eap.score_run: that is the per-run scoring step


def approval_topic(step_id: str) -> str:
    """The DBOS message topic an approval step waits on (eap_worker.workflow_run.approval_topic)."""
    return f"approval:{step_id}"

logger = logging.getLogger(__name__)


# The Metrics range counts a run by its last event: look this far before the range start for workflows
# that were created earlier and finished inside it.
RECOVERED_LOOKBACK = timedelta(days=14)
RECOVERED_PAGE = 1000
RECOVERED_MAX_PAGES = 50
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


class DBOSRunLauncher:
    """Enqueues `eap.agent_run`, `eap.tool_run`, `eap.workflow_run` and `eap.eval_run` on `eap-agent-runs`, and
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

    def launch_eval(
        self,
        *,
        run_id: UUID,
        tenant_id: str,
        input_json: str,
        traceparent: str | None = None,
    ) -> None:
        """Enqueue `eap.eval_run(run_id, tenant_id, input_json)`, workflow_id = run_id (see
        EVAL_WORKFLOW_NAME)."""
        options = self._options(EVAL_WORKFLOW_NAME, run_id, traceparent)
        self._get_client().enqueue(options, str(run_id), tenant_id, input_json)

    def launch_kb_source_sync(
        self,
        *,
        run_id: UUID,
        tenant_id: str,
        source_id: str,
        request_json: str,
        traceparent: str | None = None,
    ) -> None:
        """Enqueue `eap.kb_source_sync(run_id, tenant_id, source_id, request_json)`, workflow_id = run_id."""
        options = self._options(KB_SOURCE_SYNC_WORKFLOW_NAME, run_id, traceparent)
        self._get_client().enqueue(options, str(run_id), tenant_id, str(source_id), request_json)

    def launch_score(self, *, run_id: UUID, tenant_id: str, scorer_ids: list[str]) -> None:
        """Enqueue `eap.score_run_on_demand(run_id, tenant_id, scorer_ids)`: the worker scores the finished run with
        those scorers and appends `score` events. Workflow id `score:{run_id}:{random}`."""
        options = self._options(SCORE_WORKFLOW_NAME, run_id, None)
        options["workflow_id"] = f"score:{run_id}:{uuid4().hex}"
        self._get_client().enqueue(options, str(run_id), tenant_id, list(scorer_ids))

    def notify_approval(self, *, workflow_id: str, step_id: str, decision: str) -> None:
        """Wake the DBOS workflow waiting on approval step `step_id` (DBOS send on its topic). Only a
        wake-up: the decision itself is the run's `step:{step_id}:decision` event."""
        self._get_client().send(workflow_id, {"decision": decision}, approval_topic(step_id))

    def notify_tool_approval(
        self, *, run_id: str, tool_call_id: str, decision: str, workflow_id: str | None = None
    ) -> None:
        """Wake the agent run waiting on its gated tool calls: DBOS send of
        `{"tool_call_id", "decision"}` on topic `tool_approval:{run_id}` to `workflow_id` (the
        waiting DBOS workflow named in the tool_approval_waiting event; default the run's own,
        workflow id = run id). Only a wake-up: the decision itself is the run's
        `tool:{tool_call_id}:approval:decision` event."""
        self._get_client().send(
            workflow_id or str(run_id),
            {"tool_call_id": tool_call_id, "decision": decision},
            tool_approval_topic(str(run_id)),
        )

    def cancel_workflow_tree(self, *, workflow_id: str) -> None:
        """Cancel a run's DBOS workflow and, level by level, every child workflow under it (loops
        D12 step 2): parallel branch workflows, iteration workflows and child agent runs.
        `DBOSClient.cancel_workflow(workflow_id, cancel_children=True)` (DBOS 3.5
        `SystemDatabase.cancel_workflows`: one short system-DB transaction per tree level, which
        sets every not yet finished workflow CANCELLED and clears its queue and owner; a finished
        one is left alone). The caller holds no eap_app lock across this call (m7): a worker's
        event append takes the run-row lock. Idempotent; an unknown workflow id is a no-op.
        DBOS fences a cancelled workflow at its next operation only, so the guarded terminal write
        (RunStore.cancel_run_tree) follows."""
        self._get_client().cancel_workflow(workflow_id, cancel_children=True)

    def recovery_attempts(self, run_ids: list[str]) -> dict[str, int]:
        """DBOS `recovery_attempts` of the given runs, one `list_workflows(workflow_ids=...)` call (one query
        for a listing page). A run DBOS has no row for yet is absent (read it as 0). Best effort: when the
        system database is unreachable the run pages still render, without the recovered marker."""
        if not run_ids:
            return {}
        try:
            rows = self._get_client().list_workflows(
                workflow_ids=list(run_ids), load_input=False, load_output=False
            )
        except Exception:  # noqa: BLE001 - a display field must not fail the run page
            logger.warning("recovery_attempts lookup failed", exc_info=True)
            return {}
        return {r.workflow_id: int(r.recovery_attempts or 0) for r in rows}

    def recovered_run_ids(self, *, since: datetime, until: datetime) -> list[str]:
        """Ids of the DBOS workflows created in `[since - RECOVERED_LOOKBACK, until)` that were resumed at least
        once (`recovery_attempts > 1`). Child workflows (`{run_id}:seg:{n}` and other `:`-ids) are not runs and
        are skipped. DBOS has no recovery filter, so this pages the light status rows (no input or output) and
        keeps the few recovered ones; the caller intersects them with the tenant's runs."""
        start = (since - RECOVERED_LOOKBACK).astimezone(UTC).isoformat()
        end = until.astimezone(UTC).isoformat()
        found: list[str] = []
        try:
            client = self._get_client()
            for page in range(RECOVERED_MAX_PAGES):
                rows = client.list_workflows(
                    start_time=start, end_time=end, load_input=False, load_output=False,
                    limit=RECOVERED_PAGE, offset=page * RECOVERED_PAGE,
                )
                found += [r.workflow_id for r in rows if (r.recovery_attempts or 0) > 1 and ":" not in r.workflow_id]
                if len(rows) < RECOVERED_PAGE:
                    break
        except Exception:  # noqa: BLE001
            logger.warning("recovered runs lookup failed", exc_info=True)
            return []
        return found

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


def __getattr__(name: str):
    # `get_run_launcher` lives in eap_api.deps (the one module that reads app.state); importing it here
    # eagerly would be a cycle, since deps imports RunLauncher from this module.
    if name == "get_run_launcher":
        from eap_api.deps import get_run_launcher

        return get_run_launcher
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
