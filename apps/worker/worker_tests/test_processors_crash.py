"""P0.17 crash recovery of the processors runtime: kill the worker inside the input processor step,
during the moderation model request, after a `processor_result` write, and inside the output
processor step; a fresh process (same executor id) recovers the run. Exactly one moderation usage
row, one event per processor, contiguous seq, and the same processed text as before the crash."""

from __future__ import annotations

import uuid

import pytest
from dbos import DBOSClient
from sqlalchemy import text

from worker_tests.conftest import DBOS_DB_URL, enqueue_run, event_rows, wait_terminal
from worker_tests.test_crash_recovery import APP_VERSION, WorkerProcess, _env, crash_schema  # noqa: F401
from worker_tests.test_processors_runtime import EMAIL, PII, echo_spec, moderation

INPUT_SPEC = echo_spec(input_processors=[PII, moderation()])
OUTPUT_SPEC = echo_spec(output_processors=[PII, moderation()])

CASES = {
    # name: (spec, crash env, events written before the crash)
    "input_step": (INPUT_SPEC, {"EAP_CRASH_AT": "input_processors"}, []),
    "input_moderation": (INPUT_SPEC, {"EAP_CRASH_AT": "moderation_request"}, []),
    "after_input_result": (
        INPUT_SPEC,
        {"EAP_CRASH_AT": "after_event_write", "EAP_CRASH_EVENT_KEY": "processor:input:1"},
        ["start", "processor:input:0", "processor:input:1"],
    ),
    "after_output_result": (
        OUTPUT_SPEC,
        {"EAP_CRASH_AT": "after_event_write", "EAP_CRASH_EVENT_KEY": "processor:output:1:*"},
        ["start", "processor:output:0:model:0", "processor:output:1:model:0"],
    ),
    "output_step": (OUTPUT_SPEC, {"EAP_CRASH_AT": "output_processors"}, ["start"]),
    "output_moderation": (OUTPUT_SPEC, {"EAP_CRASH_AT": "moderation_request"}, ["start"]),
}


def _usage_rows(engine, run_id) -> list[tuple[str, int]]:
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(
            text("SELECT request_key, input_tokens FROM run_model_requests WHERE run_id = :r ORDER BY request_key"),
            {"r": str(run_id)},
        )]


@pytest.mark.parametrize("case", list(CASES))
def test_processor_crash_recovers_once(worker, tenant, crash_schema, tmp_path, case):  # noqa: F811
    spec, crash_env, before = CASES[case]
    phase = "input" if spec.get("input_processors") else "output"
    executor_id = f"proc-crash-{uuid.uuid4().hex[:8]}"
    first = WorkerProcess(_env(crash_schema, executor_id, **crash_env), tmp_path / "worker1.log")
    second: WorkerProcess | None = None
    client: DBOSClient | None = None
    try:
        first.wait_ready()
        client = DBOSClient(system_database_url=DBOS_DB_URL, dbos_system_schema=crash_schema)
        run_id = enqueue_run(client, worker.run_store, tenant_id=tenant, spec=spec,
                             input_text=f"mail {EMAIL} now", app_version=APP_VERSION)
        assert first.wait_exit() == 137, (tmp_path / "worker1.log").read_text()
        pre = event_rows(worker.engine, run_id)
        assert [e[1] for e in pre] == before
        assert worker.run_store.get_run(tenant, run_id)["status"] == "running"

        second = WorkerProcess(_env(crash_schema, executor_id), tmp_path / "worker2.log")
        run = wait_terminal(worker.run_store, tenant, run_id, timeout=90)
        assert run["status"] == "completed", (run["error"], (tmp_path / "worker2.log").read_text())

        events = event_rows(worker.engine, run_id)
        assert [e[0] for e in events] == list(range(len(events)))  # contiguous seq, no gap
        keys = [e[1] for e in events]
        assert len(keys) == len(set(keys))
        assert events[: len(pre)] == pre  # what was written before the crash is unchanged
        suffix = "" if phase == "input" else ":model:0"
        assert [k for k in keys if k.startswith("processor:")] == [
            f"processor:{phase}:0{suffix}", f"processor:{phase}:1{suffix}"]
        # Exactly one moderation request recorded (and one main model request).
        usage = _usage_rows(worker.engine, run_id)
        assert [k for k, _ in usage] == sorted(["model:0", f"moderation:{phase}:1{suffix}"])
        # Same processed text on replay.
        if phase == "input":
            assert events[0][3]["input"] == "mail [REDACTED:email] now"
            assert run["output"] == "Echo: mail [REDACTED:email] now"
        else:
            assert run["output"] == "Echo: mail [REDACTED:email] now"
        assert EMAIL not in str([e[3] for e in events if e[1] != "start" or phase == "input"])
    finally:
        first.stop()
        if second is not None:
            second.stop()
        if client is not None:
            client.destroy()
