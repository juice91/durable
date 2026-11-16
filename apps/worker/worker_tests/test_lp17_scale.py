"""LP.17 scale: one 1,000-iteration loop on a dedicated worker subprocess.

The loop cap is read from `EAP_WORKFLOW_MAX_LOOP_ITERATIONS` at `eap.start_workflow_run` (so the
value is per worker process, not per test). The in-process session worker was launched with the
default 100 cap, so we start our own `python -m eap_worker` subprocess with the raised cap, its
own DBOS system schema (dropped afterwards), and run one real 1,000-iteration loop with a cheap
body (one `clock.now` tool step, no side effect).

Assertions:
- parent `operation_outputs` rows ≤ 4 per iteration (tick + iteration workflow + DBOS.getResult +
  append_event), plus a small fixed overhead (start_workflow_run, step_begin, step_completed,
  finish_run — well under 20);
- parent's `operation_outputs` output column plus every `workflow_status` row of the tree is
  under 5 MB (`pg_column_size`, which is the compressed on-disk size);
- wall time is recorded (printed in the test output; scripts/test.sh runs this file by default).

scripts/test.sh has no `slow` marker gate, so the test is not marked `slow`; if the run grows
past ~10 minutes we would add `pytest.mark.slow` and gate it there. The scripts/test.sh pytest
command takes no `-m` filter today, so adding the marker would still run the test in that script.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import psycopg
import pytest
from dbos import DBOSClient
from sqlalchemy import text

from worker_tests.conftest import (
    APP_DB_URL,
    DBOS_DB_URL,
    drop_schema,
    new_schema,
    wait_for,
    wait_terminal,
)
from worker_tests.workflow_run_helpers import canonical, cleanup_definitions, publish_workflow


APP_VERSION = "m1-lp17-scale"
ITERATIONS = 1000  # requires EAP_WORKFLOW_MAX_LOOP_ITERATIONS=1000 on the worker
SIZE_LIMIT_BYTES = 5 * 1024 * 1024
OPS_PER_ITERATION_CAP = 4
OVERHEAD_CAP = 20
# A cheap deterministic body: one tool step reading no refs, no side effect.
BODY_STEP = {"id": "x", "type": "tool", "tool_id": "clock.now", "input": {}}


class WorkerProcess:
    """A dedicated `python -m eap_worker` subprocess for this test (crash harness style)."""

    def __init__(self, env: dict[str, str], log_path: Path) -> None:
        self.log_path = log_path
        self._log = log_path.open("w")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "eap_worker"], env=env, stdout=self._log, stderr=subprocess.STDOUT
        )

    def wait_ready(self, timeout: float = 60) -> None:
        def ready() -> bool:
            if self.proc.poll() is not None:
                raise AssertionError(
                    f"worker exited early ({self.proc.returncode}):\n{self.log_path.read_text()}"
                )
            return "EAP_WORKER_READY" in self.log_path.read_text()

        wait_for(ready, timeout=timeout, what="worker ready")

    def stop(self) -> None:
        import signal

        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        self._log.close()


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


def _spec(iterations: int) -> dict:
    return {
        "name": "LP17 scale",
        "input_schema": {"type": "object"},
        "start": "lp",
        "steps": [{
            "id": "lp",
            "type": "loop",
            "max_iterations": iterations,
            "on_max_iterations": "exit",
            # Never satisfied: the loop exits by max_iterations.
            "until": {"op": "eq", "left": {"$ref": "loop.lp.index"}, "right": iterations + 1},
            "body": {"start": "x", "steps": [BODY_STEP]},
        }],
    }


def _operations(schema: str, workflow_id: str) -> int:
    with psycopg.connect(DBOS_DB_URL, connect_timeout=5) as conn:
        return conn.execute(
            f'SELECT count(*) FROM "{schema}".operation_outputs WHERE workflow_uuid = %s',
            (workflow_id,),
        ).fetchone()[0]


def _run_tree_size_bytes(schema: str, run_id: str) -> int:
    with psycopg.connect(DBOS_DB_URL, connect_timeout=5) as conn:
        parent_ops = conn.execute(
            f'SELECT coalesce(sum(pg_column_size(output)), 0) FROM "{schema}".operation_outputs'
            " WHERE workflow_uuid = %s",
            (run_id,),
        ).fetchone()[0]
        statuses = conn.execute(
            f'SELECT coalesce(sum(pg_column_size(s)), 0) FROM "{schema}".workflow_status s'
            " WHERE workflow_uuid = %s OR workflow_uuid LIKE %s",
            (run_id, f"{run_id}:%"),
        ).fetchone()[0]
    return int(parent_ops) + int(statuses)


@pytest.fixture
def scale_schema():
    """A private DBOS schema for the dedicated subprocess worker (dropped afterwards)."""
    schema = new_schema("eap_test_lp17_scale")
    yield schema
    drop_schema(schema)


@pytest.fixture
def defs(worker, tenant):
    yield
    cleanup_definitions(worker.engine, tenant)


def test_a_one_thousand_iteration_loop_stays_within_the_per_iteration_operation_and_size_budget(
    worker, tenant, defs, scale_schema, tmp_path
):
    """One real 1,000-iteration loop on a dedicated worker subprocess with the cap raised to 1,000.
    The parent's DBOS operations stay ≤ 4 per iteration and the run tree's DBOS payload stays
    under 5 MB. Wall time is recorded."""
    executor_id = f"lp17-{uuid.uuid4().hex[:8]}"
    env = _env(scale_schema, executor_id, EAP_WORKFLOW_MAX_LOOP_ITERATIONS=str(ITERATIONS))
    worker_proc = WorkerProcess(env, tmp_path / "lp17-scale-worker.log")
    client: DBOSClient | None = None
    try:
        worker_proc.wait_ready()
        workflow_id, version_id = publish_workflow(worker.engine, tenant, _spec(ITERATIONS))
        run_id = uuid.uuid4()
        worker.run_store.create_workflow_run(tenant, workflow_id, 1, canonical({}), run_id)
        client = DBOSClient(system_database_url=DBOS_DB_URL, dbos_system_schema=scale_schema)
        client.enqueue(
            {
                "queue_name": "eap-agent-runs",
                "workflow_name": "eap.workflow_run",
                "workflow_id": str(run_id),
                "app_version": APP_VERSION,
            },
            str(run_id), tenant, version_id, canonical({}),
        )

        started = time.monotonic()
        run = wait_terminal(worker.run_store, tenant, run_id, timeout=900)
        wall_s = time.monotonic() - started
        assert run["status"] == "completed", (run.get("error"), worker_proc.log_path.read_text()[-4000:])

        # Exactly ITERATIONS iteration-started and iteration-completed events, gap-free seq.
        with worker.engine.connect() as conn:
            rows = list(conn.execute(
                text("SELECT seq, event_key FROM execution_events WHERE run_id = :r ORDER BY seq"),
                {"r": str(run_id)},
            ))
        seqs = [r.seq for r in rows]
        keys = [r.event_key for r in rows]
        assert seqs == list(range(len(rows))), f"seq not contiguous: {seqs[:5]}..{seqs[-5:]}"
        assert len(keys) == len(set(keys)) and keys.count("end") == 1 and keys[-1] == "end"
        started_keys = [k for k in keys if k.startswith("loop:lp.") and k.endswith(":started")
                        and ":step:" not in k]
        completed_keys = [k for k in keys if k.startswith("loop:lp.") and k.endswith(":completed")
                          and ":step:" not in k]
        assert len(started_keys) == ITERATIONS == len(completed_keys), (
            len(started_keys), len(completed_keys))

        # Parent's DBOS operations ≤ 4 per iteration (plus a small fixed overhead).
        parent_ops = _operations(scale_schema, str(run_id))
        assert parent_ops <= OPS_PER_ITERATION_CAP * ITERATIONS + OVERHEAD_CAP, (parent_ops, ITERATIONS)

        # Run tree's DBOS payload under 5 MB.
        size_bytes = _run_tree_size_bytes(scale_schema, str(run_id))
        assert size_bytes < SIZE_LIMIT_BYTES, (size_bytes, SIZE_LIMIT_BYTES)

        print(
            f"LP.17 scale: iterations={ITERATIONS}, wall_time={wall_s:.1f}s, "
            f"parent_operations={parent_ops} (<= {OPS_PER_ITERATION_CAP * ITERATIONS + OVERHEAD_CAP}), "
            f"tree_size_bytes={size_bytes} (< {SIZE_LIMIT_BYTES})"
        )
    finally:
        worker_proc.stop()
        if client is not None:
            client.destroy()
