"""Agents as tools survive a worker kill: while the child runs, and after it finished but before the
caller recorded the tool result. Recovery resumes both workflows; the child is never started twice.
Real subprocesses, always stopped in `finally`."""

from __future__ import annotations

import json
import uuid

import pytest
from dbos import DBOSClient

from worker_tests.conftest import DBOS_DB_URL, enqueue_run, event_rows, wait_terminal
from worker_tests.test_agent_tools import ask, children, supervisor_spec
from worker_tests.test_crash_recovery import APP_VERSION, WorkerProcess, _env, crash_schema  # noqa: F401 - fixture
from worker_tests.workflow_run_helpers import agent_spec, cleanup_definitions, publish_agent


@pytest.fixture
def defs(worker, tenant):
    yield
    cleanup_definitions(worker.engine, tenant)


@pytest.mark.parametrize(
    "crash_at, child_model, child_tools",
    [
        ("after_agent_tool_start", "echo", ()),  # killed right after the child workflow was started
        ("after_agent_tool_result", "ticket", ("tickets.create",)),  # child done, caller has recorded nothing
    ],
)
def test_a_kill_around_an_agent_tool_call_recovers_with_one_child_run(
    worker, tenant, defs, crash_schema, tmp_path, crash_at, child_model, child_tools
):
    executor_id = f"at-crash-{uuid.uuid4().hex[:8]}"
    child_id = publish_agent(worker.engine, tenant, agent_spec(child_model, tools=child_tools, name="Helper"))
    spec = supervisor_spec({"agent_id": child_id, "name": "helper"})
    client = DBOSClient(system_database_url=DBOS_DB_URL, dbos_system_schema=crash_schema)
    procs: list[WorkerProcess] = []
    try:
        extra = {"EAP_CRASH_AT": crash_at, "EAP_CRASH_MARKER": str(tmp_path / "marker")}
        first = WorkerProcess(_env(crash_schema, executor_id, **extra), tmp_path / "worker1.log")
        procs.append(first)
        first.wait_ready()
        run_id = enqueue_run(client, worker.run_store, tenant_id=tenant, spec=spec,
                             input_text=ask(("helper", "please help")), app_version=APP_VERSION)
        assert first.wait_exit() == 137, (tmp_path / "worker1.log").read_text()
        assert worker.run_store.get_run(tenant, run_id)["status"] == "running"

        recovering = WorkerProcess(_env(crash_schema, executor_id), tmp_path / "worker2.log")
        procs.append(recovering)
        row = wait_terminal(worker.run_store, tenant, run_id, timeout=120)
        assert row["status"] == "completed", (row["error"], (tmp_path / "worker2.log").read_text()[-3000:])
        kids = children(worker.engine, run_id)
        assert len(kids) == 1 and kids[0]["status"] == "completed"
        assert row["output"].startswith("Supervisor: ok: ") and kids[0]["id"] in row["output"]

        events = event_rows(worker.engine, run_id)
        seqs = [e[0] for e in events]
        assert seqs == list(range(seqs[0], seqs[0] + len(seqs))), seqs
        keys = [e[1] for e in events]
        assert len(set(keys)) == len(keys)  # one event per key
        call_id = kids[0]["parent_step_id"]
        assert keys.count(f"agent_tool:{call_id}:started") == 1
        assert keys.count(f"agent_tool:{call_id}:completed") == 1
        assert keys.count(f"tool:{call_id}:result") == 1
        assert sum(1 for e in events if e[2] == "run_completed") == 1
        child_events = event_rows(worker.engine, kids[0]["id"])
        assert [e[2] for e in child_events].count("run_completed") == 1
        if child_tools:
            assert worker.tickets.ticket_count(tenant, f"{kids[0]['id']}:") == 1  # the child's tool ran once
    finally:
        for proc in procs:
            proc.kill()
        client.destroy()
