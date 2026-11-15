"""AC.6 crash recovery (implementer): a worker killed while an agent run waits for a tool approval.

1. `EAP_CRASH_AT=tool_approval_waiting`: the worker exits right after the waiting event and inbox
   row are written; the call is approved (with the wake-up) while no worker runs; a fresh process
   recovers, replays to the wait, finds the decision and runs the tool exactly once.
2. The worker is killed (SIGKILL) while it sleeps in the wait's DBOS.recv; the decision event is
   written and its wake-up sent while it is down; recovery finds the decision event.

Afterwards: one tool_result, one completed side effect, one ticket, a gap-free seq, one event per
key. Real subprocesses, always stopped in `finally`."""

from __future__ import annotations

import json
import uuid

import pytest
from dbos import DBOSClient

from eap_run_store.approvals import tool_approval_topic
from worker_tests.conftest import DBOS_DB_URL, enqueue_run, event_rows, wait_for, wait_terminal
from worker_tests.test_conversation import step_names
from worker_tests.test_crash_recovery import APP_VERSION, WorkerProcess, _env, crash_schema  # noqa: F401 - fixture
from worker_tests.test_tool_approval import TICKET_ARGS, gated_spec, side_effects

PROMPT = json.dumps({"tool": "tickets.create", "args": TICKET_ARGS})


def _waiting_call(worker, run_id) -> str:
    def found():
        rows = [d for _s, _k, t, d in event_rows(worker.engine, run_id) if t == "tool_approval_waiting"]
        return rows[0]["tool_call_id"] if rows else None

    return wait_for(found, timeout=60, what="the tool_approval_waiting event")


def _approve(worker, client, tenant, run_id, call_id) -> None:
    outcome, _ = worker.event_log.append_tool_decision(
        tenant, run_id, call_id, "tool_approved", {"tool_call_id": call_id, "decided_by": "alice", "comment": None}
    )
    assert outcome == "created"
    client.send(str(run_id), {"tool_call_id": call_id, "decision": "approved"}, tool_approval_topic(str(run_id)))


def _assert_clean(worker, tenant, run_id, call_id) -> None:
    events = event_rows(worker.engine, run_id)
    seqs = [e[0] for e in events]
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs))), seqs
    assert len({e[1] for e in events}) == len(events)
    keys = [e[1] for e in events]
    assert keys.count(f"tool:{call_id}:approval:waiting") == 1 and keys.count(f"tool:{call_id}:result") == 1
    assert sum(1 for e in events if e[2] == "run_completed") == 1
    assert side_effects(worker, tenant) == [(f"{run_id}:{call_id}", "completed")]
    assert worker.tickets.ticket_count(tenant, f"{run_id}:") == 1


@pytest.mark.parametrize("how", ["crash_point", "kill_in_recv"])
def test_crash_while_waiting_then_approve_runs_the_tool_once(worker, tenant, crash_schema, tmp_path, how):
    executor_id = f"ta-crash-{uuid.uuid4().hex[:8]}"
    store = worker.run_store
    client = DBOSClient(system_database_url=DBOS_DB_URL, dbos_system_schema=crash_schema)
    procs: list[WorkerProcess] = []
    try:
        extra = {"EAP_CRASH_AT": "tool_approval_waiting", "EAP_CRASH_MARKER": str(tmp_path / "marker")}
        first = WorkerProcess(_env(crash_schema, executor_id, **(extra if how == "crash_point" else {})),
                              tmp_path / "worker1.log")
        procs.append(first)
        first.wait_ready()
        run_id = enqueue_run(client, store, tenant_id=tenant, spec=gated_spec(), input_text=PROMPT,
                             app_version=APP_VERSION)
        call_id = _waiting_call(worker, run_id)
        if how == "crash_point":
            assert first.wait_exit() == 137, (tmp_path / "worker1.log").read_text()
        else:
            wait_for(lambda: "DBOS.sleep" in step_names(crash_schema, run_id) or None, timeout=60,
                     what="the wait's recv")
            first.kill()
        assert store.get_run(tenant, run_id)["status"] == "running"
        assert side_effects(worker, tenant) == []  # nothing reserved before approval
        _approve(worker, client, tenant, run_id, call_id)  # decided (and woken) while no worker runs

        recovering = WorkerProcess(_env(crash_schema, executor_id), tmp_path / "worker2.log")
        procs.append(recovering)
        row = wait_terminal(store, tenant, run_id, timeout=90)
        assert row["status"] == "completed", (row["error"], (tmp_path / "worker2.log").read_text()[-3000:])
        assert "outcome=success" in row["output"]
        _assert_clean(worker, tenant, run_id, call_id)
    finally:
        for proc in procs:
            proc.kill()
        client.destroy()
