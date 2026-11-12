"""Crash smoke test: kill the worker process mid-tool, recover in a fresh process.

The full task-9 crash matrix lives elsewhere; this proves the hook + recovery path end to end.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
from dbos import DBOSClient
from sqlalchemy import text

from worker_tests.conftest import (
    APP_DB_URL,
    DBOS_DB_URL,
    drop_schema,
    enqueue_run,
    event_rows,
    new_schema,
    ticket_spec,
    wait_for,
    wait_terminal,
)

APP_VERSION = "m1-crash-test"


class WorkerProcess:
    def __init__(self, env: dict[str, str], log_path: Path) -> None:
        self.log_path = log_path
        self._log = log_path.open("w")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "eap_worker"], env=env, stdout=self._log, stderr=subprocess.STDOUT
        )

    def wait_ready(self, timeout: float = 60) -> None:
        def ready() -> bool:
            if self.proc.poll() is not None:
                raise AssertionError(f"worker exited early ({self.proc.returncode}):\n{self.log_path.read_text()}")
            return "EAP_WORKER_READY" in self.log_path.read_text()

        wait_for(ready, timeout=timeout, what="worker ready")

    def wait_exit(self, timeout: float = 60) -> int:
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.kill()
            raise AssertionError(f"worker did not exit:\n{self.log_path.read_text()}")

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.kill()
        self._log.close()

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=10)


@pytest.fixture
def crash_schema():
    schema = new_schema("eap_test_crash")
    yield schema
    drop_schema(schema)


def _env(schema: str, executor_id: str, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("EAP_CRASH")}
    env.update(
        EAP_DATABASE_URL=APP_DB_URL,
        EAP_DBOS_URL=DBOS_DB_URL,
        EAP_DBOS_SYSTEM_SCHEMA=schema,
        EAP_APP_VERSION=APP_VERSION,
        EAP_EXECUTOR_ID=executor_id,
        EAP_QUEUE_POLL_SECONDS="0.2",
        PYTHONUNBUFFERED="1",
    )
    env.update(extra)
    return env


def test_crash_after_tool_call_recovers_in_fresh_process(worker, tenant, crash_schema, tmp_path):
    executor_id = f"crash-{uuid.uuid4().hex[:8]}"
    first = WorkerProcess(_env(crash_schema, executor_id, EAP_CRASH_AT="after_tool_call"), tmp_path / "worker1.log")
    client: DBOSClient | None = None
    second: WorkerProcess | None = None
    try:
        first.wait_ready()
        client = DBOSClient(system_database_url=DBOS_DB_URL, dbos_system_schema=crash_schema)
        run_id = enqueue_run(
            client, worker.run_store, tenant_id=tenant, spec=ticket_spec(), input_text="Laptop will not boot",
            app_version=APP_VERSION,
        )

        # 1. The worker dies right after the external call returned, before the result was recorded.
        assert first.wait_exit() == 137, (tmp_path / "worker1.log").read_text()
        assert worker.tickets.ticket_count(tenant, f"{run_id}:") == 1
        assert worker.tickets.call_count(tenant, f"{run_id}:") == 1
        with worker.engine.connect() as conn:
            states = conn.execute(
                text("SELECT state FROM side_effects WHERE tenant_id = :t"), {"t": tenant}
            ).scalars().all()
        assert states == ["reserved"]
        assert worker.run_store.get_run(tenant, run_id)["status"] == "running"
        keys_before = [e[1] for e in event_rows(worker.engine, run_id)]
        assert keys_before[:2] == ["start", "model:0:final"] and keys_before[-1].endswith(":call")

        # 2. A fresh process with the same executor_id recovers the pending workflow on launch.
        second = WorkerProcess(_env(crash_schema, executor_id), tmp_path / "worker2.log")
        run = wait_terminal(worker.run_store, tenant, run_id, timeout=90)
        assert run["status"] == "completed", (run["error"], (tmp_path / "worker2.log").read_text())
        assert "Recovering" in (tmp_path / "worker2.log").read_text()

        # Exactly one external effect; the tool step is at-least-once, so the idempotent service
        # saw the same key twice and deduplicated it.
        assert worker.tickets.ticket_count(tenant, f"{run_id}:") == 1
        assert worker.tickets.call_count(tenant, f"{run_id}:") == 2

        events = event_rows(worker.engine, run_id)
        assert [e[0] for e in events] == list(range(len(events)))
        keys = [e[1] for e in events]
        assert len(keys) == len(set(keys))
        call_id = events[2][3]["tool_call_id"]
        assert keys == ["start", "model:0:final", f"tool:{call_id}:call", f"tool:{call_id}:result", "model:1:final", "end"]
        assert keys[:3] == keys_before  # the pre-crash tool_call_id was replayed, not regenerated
        assert events[-1][2] == "run_completed"
    finally:
        first.kill()
        if second is not None:
            second.stop()
        if client is not None:
            client.destroy()


def test_crash_during_turn_three_replays_the_same_history_in_a_fresh_process(worker, tenant, crash_schema, tmp_path):
    """Kill the worker inside turn 3's model request; a fresh process must give the model exactly the
    history the crashed attempt was given (the `eap.load_history` step is replayed, not re-run)."""
    import psycopg

    from worker_tests.test_conversation import HISTORY_SPEC

    store = worker.run_store
    executor_id = f"conv-crash-{uuid.uuid4().hex[:8]}"
    conv, _ = store.create_conversation(tenant, uuid.uuid4())
    agent_id = uuid.UUID(conv["agent_id"])
    client = DBOSClient(system_database_url=DBOS_DB_URL, dbos_system_schema=crash_schema)
    procs: list[WorkerProcess] = []

    def turn(text_in: str) -> uuid.UUID:
        run_id, _ = store.create_turn(tenant, conv["id"], 1, text_in, uuid.uuid4())
        enqueue_run(client, store, tenant_id=tenant, spec=HISTORY_SPEC, input_text=text_in, app_version=APP_VERSION,
                    run_id=run_id, agent_id=agent_id, create_row=False)
        return run_id

    try:
        healthy = WorkerProcess(_env(crash_schema, executor_id), tmp_path / "worker-healthy.log")
        procs.append(healthy)
        healthy.wait_ready()
        for prompt in ("My name is Ada.", "I live in Lisbon."):
            assert wait_terminal(store, tenant, turn(prompt), timeout=90)["status"] == "completed"
        healthy.stop()

        # Turn 3: the crash hook fires right after the first (and only) delta: the whole answer the
        # model computed from the history it was given.
        listener = psycopg.connect(APP_DB_URL.replace("postgresql+psycopg://", "postgresql://", 1), autocommit=True)
        listener.execute("LISTEN eap_run_deltas")
        crashing = WorkerProcess(
            _env(crash_schema, executor_id, EAP_CRASH_AT="model_request", EAP_CRASH_MARKER=str(tmp_path / "marker")),
            tmp_path / "worker-crash.log",
        )
        procs.append(crashing)
        crashing.wait_ready()
        run3 = turn("What is my name?")
        assert crashing.wait_exit() == 137, (tmp_path / "worker-crash.log").read_text()
        assert store.get_run(tenant, run3)["status"] == "running"
        keys_before = [e[1] for e in event_rows(worker.engine, run3)]
        assert keys_before == ["start"]

        recovering = WorkerProcess(_env(crash_schema, executor_id), tmp_path / "worker-recover.log")
        procs.append(recovering)
        run = wait_terminal(store, tenant, run3, timeout=90)
        assert run["status"] == "completed", (run["error"], (tmp_path / "worker-recover.log").read_text())
        assert "Recovering" in (tmp_path / "worker-recover.log").read_text()

        deltas = []
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and len(deltas) < 2:
            for note in listener.notifies(timeout=0.5, stop_after=10):
                import json

                payload = json.loads(note.payload)
                if payload["run_id"] == str(run3):
                    deltas.append(payload)
        listener.close()
        assert len(deltas) == 2, deltas
        crashed, retried = deltas
        assert crashed["attempt"] != retried["attempt"]
        expected = (
            "Turn 3. You said: My name is Ada. | I live in Lisbon. | What is my name?. I answered 2 times before."
        )
        # Same history before and after the crash: identical answers, and it carries turn 1's fact.
        assert crashed["delta"] == retried["delta"] == run["output"] == expected

        with psycopg.connect(DBOS_DB_URL, connect_timeout=5) as conn:
            steps = [
                r[0]
                for r in conn.execute(
                    f'SELECT function_name FROM "{crash_schema}".operation_outputs WHERE workflow_uuid = %s',
                    (str(run3),),
                )
            ]
        assert steps.count("eap.load_history") == 1

        events = event_rows(worker.engine, run3)
        assert [e[0] for e in events] == list(range(len(events)))
        assert [e[1] for e in events] == ["start", "model:0:final", "end"]
        turns = store.list_turns(tenant, conv["id"])
        assert [(t["turn"], t["status"]) for t in turns] == [(1, "completed"), (2, "completed"), (3, "completed")]
    finally:
        for proc in procs:
            proc.kill()
        client.destroy()
