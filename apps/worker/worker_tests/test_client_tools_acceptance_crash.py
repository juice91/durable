"""AGUI.7 crash recovery (tester): client tool turns across a killed worker (real subprocesses,
always stopped in `finally`).

Turn 1 crashes after `tool:{id}:call` was written, or after the pending `end` event was written;
turn 2 (which carries the client's result) crashes after the `tool:{id}:result` event, or after the
results are written and before the resumed model request. Afterwards: exactly one pending end and
one result event, a gap-free seq, one event per key, one usage row per request key (no duplicate
model call), the tool never executed server side, and the turn after the recovered one continues."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from dbos import DBOSClient

from worker_tests.conftest import DBOS_DB_URL, enqueue_run, event_rows, wait_terminal
from worker_tests.test_client_tools import WEATHER, frontend_spec
from worker_tests.test_crash_recovery import APP_VERSION as CRASH_APP_VERSION
from worker_tests.test_crash_recovery import WorkerProcess, _env, crash_schema  # noqa: F401 - fixture
from worker_tests.test_limits_fallback import usage_rows


class Rig:
    def __init__(self, worker, tenant, schema, tmp_path) -> None:
        self.worker, self.tenant, self.schema, self.tmp = worker, tenant, schema, tmp_path
        self.executor = f"ct-crash-{uuid.uuid4().hex[:8]}"
        self.client = DBOSClient(system_database_url=DBOS_DB_URL, dbos_system_schema=schema)
        self.procs: list[WorkerProcess] = []
        self.conv, _ = worker.run_store.create_conversation(tenant, uuid.uuid4(), title="crash")
        self.spec = frontend_spec()

    def start(self, **extra: str) -> WorkerProcess:
        proc = WorkerProcess(_env(self.schema, self.executor, **extra), self.tmp / f"w{len(self.procs)}.log")
        self.procs.append(proc)
        proc.wait_ready()
        return proc

    def turn(self, text: str, results: list[dict[str, Any]] | None = None) -> uuid.UUID:
        store = self.worker.run_store
        run_id, created = store.create_turn(
            self.tenant, self.conv["id"], 1, text, uuid.uuid4(), client_tools=[WEATHER], client_tool_results=results
        )
        assert created
        enqueue_run(self.client, store, tenant_id=self.tenant, spec=self.spec, input_text=text,
                    app_version=CRASH_APP_VERSION, run_id=run_id, agent_id=uuid.UUID(self.conv["agent_id"]),
                    create_row=False)
        return run_id

    def finish(self, run_id) -> dict[str, Any]:
        return wait_terminal(self.worker.run_store, self.tenant, run_id, timeout=90)

    def close(self) -> None:
        for proc in self.procs:
            proc.kill()
        self.client.destroy()


def clean(worker, run_id) -> list[tuple[int, str, str, dict]]:
    events = event_rows(worker.engine, run_id)
    seqs = [e[0] for e in events]
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs))), seqs  # gap-free
    assert len({e[1] for e in events}) == len(events)  # one event per key
    keys = [r["request_key"] for r in usage_rows(worker.engine, run_id)]
    assert len(keys) == len(set(keys)), keys  # no duplicate model call recorded
    return events


def pending_end(events) -> list[str]:
    ends = [e for e in events if e[2] == "run_completed"]
    assert len(ends) == 1, events
    return ends[0][3]["pending_tool_call_ids"]


@pytest.mark.parametrize(
    "crash",
    [
        pytest.param({"EAP_CRASH_AT": "after_event_write", "EAP_CRASH_EVENT_KEY": "tool:*:call"},
                     id="after-the-call-event-before-the-pending-end"),
        pytest.param({"EAP_CRASH_AT": "after_event_write", "EAP_CRASH_EVENT_KEY": "end"},
                     id="after-the-pending-end-before-finish_run"),
    ],
)
def test_crash_around_the_pending_end_recovers_to_one_pending_end(worker, tenant, crash_schema, tmp_path, crash):
    rig = Rig(worker, tenant, crash_schema, tmp_path)
    try:
        crashing = rig.start(EAP_CRASH_MARKER=str(tmp_path / "marker"), **crash)
        first = rig.turn("weather?")
        assert crashing.wait_exit() == 137, (tmp_path / "w0.log").read_text()
        assert worker.run_store.get_run(tenant, first)["status"] == "running"
        rig.start()  # recovery
        run = rig.finish(first)
        assert run["status"] == "completed" and run["output"] is None, run
        events = clean(worker, first)
        ids = pending_end(events)
        keys = [e[1] for e in events]
        assert len(ids) == 1 and keys.count(f"tool:{ids[0]}:call") == 1
        assert not [e for e in events if e[2] == "tool_result"]  # the tool never executed
        assert [r["request_key"] for r in usage_rows(worker.engine, first)] == ["model:0"]
        # The recovered pending turn is continuable.
        second = rig.turn("", [{"tool_call_id": ids[0], "content": "sunny", "error": None}])
        run = rig.finish(second)
        assert run["status"] == "completed" and run["output"] == "tool said: sunny", run
        clean(worker, second)
    finally:
        rig.close()


@pytest.mark.parametrize(
    "crash",
    [
        pytest.param({"EAP_CRASH_AT": "after_event_write", "EAP_CRASH_EVENT_KEY": "tool:*:result"},
                     id="after-the-result-event"),
        pytest.param({"EAP_CRASH_AT": "model_request"}, id="after-the-results-before-the-resumed-model-request"),
    ],
)
def test_crash_after_the_client_result_before_the_resume_answers_once(worker, tenant, crash_schema, tmp_path, crash):
    rig = Rig(worker, tenant, crash_schema, tmp_path)
    try:
        normal = rig.start()
        first = rig.turn("weather?")
        assert rig.finish(first)["output"] is None
        ids = pending_end(clean(worker, first))
        normal.kill()
        crashing = rig.start(EAP_CRASH_MARKER=str(tmp_path / "marker"), **crash)
        second = rig.turn("", [{"tool_call_id": ids[0], "content": "sunny", "error": None}])
        assert crashing.wait_exit() == 137, (tmp_path / "w1.log").read_text()
        assert worker.run_store.get_run(tenant, second)["status"] == "running"
        rig.start()  # recovery
        run = rig.finish(second)
        assert run["status"] == "completed" and run["output"] == "tool said: sunny", run
        events = clean(worker, second)
        keys = [e[1] for e in events]
        assert keys.count(f"tool:{ids[0]}:result") == 1 and sum(1 for e in events if e[2] == "run_completed") == 1
        assert keys[:2] == ["start", f"tool:{ids[0]}:result"] and keys[-1] == "end"
        assert [r["request_key"] for r in usage_rows(worker.engine, second)] == ["model:0"]
        assert [r["request_key"] for r in usage_rows(worker.engine, first)] == ["model:0"]
        # The first turn's checkpoints were not disturbed.
        assert pending_end(event_rows(worker.engine, first)) == ids
    finally:
        rig.close()


# --------------------------------------------------------------------------- old checkpoints


def _strip_client_keys(schema: str, run_id) -> list[str]:
    """Make the run's recorded `eap.start_run` an OLD checkpoint: delete the AGUI.7 keys from the
    pickled output. Returns the keys that were removed (they must have been there)."""
    import base64
    import pickle

    import psycopg

    keys = ("client_tools", "client_tool_results", "client_tool_pending", "client_turn_chain")
    with psycopg.connect(DBOS_DB_URL, connect_timeout=5) as conn:
        row = conn.execute(
            f'SELECT function_id, output FROM "{schema}".operation_outputs'
            " WHERE workflow_uuid = %s AND function_name = 'eap.start_run'", (str(run_id),)).fetchone()
        assert row is not None, "start_run was not recorded before the crash"
        link = pickle.loads(base64.b64decode(row[1]))  # our own just-recorded checkpoint
        removed = [k for k in keys if k in link]
        for k in keys:
            link.pop(k, None)
        conn.execute(f'UPDATE "{schema}".operation_outputs SET output = %s WHERE workflow_uuid = %s AND function_id = %s',
                     (base64.b64encode(pickle.dumps(link)).decode(), str(run_id), row[0]))
    return removed


@pytest.mark.parametrize("shape", ["plain", "tool"])
def test_a_checkpoint_without_the_client_keys_replays_with_the_golden_step_list(
    worker, tenant, crash_schema, tmp_path, shape
):
    from worker_tests.conftest import ticket_spec
    from worker_tests.test_conversation import step_names
    from worker_tests.test_processors_runtime import BASELINE

    spec = ticket_spec(tools=(), model="echo") if shape == "plain" else ticket_spec()
    golden = BASELINE["echo"] if shape == "plain" else BASELINE["ticket"]
    rig = Rig(worker, tenant, crash_schema, tmp_path)
    try:
        crashing = rig.start(EAP_CRASH_AT="model_request", EAP_CRASH_MARKER=str(tmp_path / "marker"))
        run_id = enqueue_run(rig.client, worker.run_store, tenant_id=tenant, spec=spec, input_text="hello",
                             app_version=CRASH_APP_VERSION)
        assert crashing.wait_exit() == 137, (tmp_path / "w0.log").read_text()
        assert _strip_client_keys(crash_schema, run_id) == [
            "client_tools", "client_tool_results", "client_tool_pending", "client_turn_chain"
        ]
        rig.start()  # recovery replays start_run from the OLD checkpoint
        run = rig.finish(run_id)
        assert run["status"] == "completed", run
        assert step_names(crash_schema, run_id) == golden
        events = clean(worker, run_id)
        assert sum(1 for e in events if e[2] == "run_completed") == 1 and events[-1][3].get("pending_tool_call_ids") is None
    finally:
        rig.close()


def test_an_old_turn_two_checkpoint_after_a_pending_turn_replays_without_client_results(
    worker, tenant, crash_schema, tmp_path
):
    """A conversation turn 2 whose `eap.start_run` was recorded before AGUI.7 (no pending, chain or
    result keys) while turn 1 ended on a client call: the replay continues nothing (no client result
    event), repairs the dangling call in the history and completes."""
    from worker_tests.test_conversation import step_names

    rig = Rig(worker, tenant, crash_schema, tmp_path)
    try:
        normal = rig.start()
        first = rig.turn("weather?")
        assert rig.finish(first)["output"] is None
        normal.kill()
        crashing = rig.start(EAP_CRASH_AT="after_event_write", EAP_CRASH_EVENT_KEY="start",
                             EAP_CRASH_MARKER=str(tmp_path / "marker"))
        second = rig.turn("hello", [{"tool_call_id": pending_end(event_rows(worker.engine, first))[0], "content": "x"}])
        assert crashing.wait_exit() == 137, (tmp_path / "w1.log").read_text()
        removed = _strip_client_keys(crash_schema, second)
        assert removed == ["client_tools", "client_tool_results", "client_tool_pending", "client_turn_chain"]
        rig.start()  # recovery replays start_run from the OLD checkpoint
        run = rig.finish(second)
        assert run["status"] == "completed", run
        events = clean(worker, second)
        assert not [e for e in events if e[2] == "tool_result"]
        assert [e[1] for e in events] == ["start", "model:0:final", "end"]
        assert "eap.load_history" in step_names(crash_schema, second)
    finally:
        rig.close()
