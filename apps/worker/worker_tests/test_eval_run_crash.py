"""AC.11 crash recovery: kill the worker mid-batch (a case's child done, later children of the batch
still running) and after a case's scoring and judge steps but before its record step; recover in a
fresh process. Afterwards: exactly one result row per case, one judge usage row per key, one event
per key and a gap-free seq on the eval run. Real subprocesses, always stopped in `finally`."""

from __future__ import annotations

import uuid

import pytest
from dbos import DBOSClient

from worker_tests.conftest import DBOS_DB_URL, event_rows, wait_terminal
from worker_tests.eval_run_helpers import JUDGE, cleanup_evals, create_suite, results, start_eval, usage_keys
from worker_tests.test_crash_recovery import APP_VERSION as CRASH_APP_VERSION
from worker_tests.test_crash_recovery import WorkerProcess, _env, crash_schema  # noqa: F401 - fixture
from worker_tests.workflow_run_helpers import agent_spec, cleanup_definitions, publish_agent


@pytest.fixture
def defs(worker, tenant):
    yield
    cleanup_evals(worker.engine, tenant)
    cleanup_definitions(worker.engine, tenant)


@pytest.mark.parametrize(
    "crash",
    [
        pytest.param({"EAP_CRASH_AT": "eval_child_done", "EAP_CRASH_EVENT_KEY": "position:0"}, id="mid-batch"),
        pytest.param({"EAP_CRASH_AT": "eval_case_scored", "EAP_CRASH_EVENT_KEY": "position:1"},
                     id="after-scoring-before-record"),
    ],
)
def test_crash_then_recover_records_each_case_once(worker, tenant, defs, crash_schema, tmp_path, crash):
    agent_id = publish_agent(worker.engine, tenant, agent_spec("echo"))
    cases = [{"name": f"c{i}", "input": f"word{i}"} for i in range(3)]
    evaluators = [{"kind": "contains", "value": "Echo:"},
                  {"kind": "llm_judge", "rubric": "MUST_CONTAIN:word", "model": JUDGE}]
    suite_id = create_suite(worker.engine, tenant, agent_id, cases, evaluators)
    executor_id = f"eval-crash-{uuid.uuid4().hex[:8]}"
    client = DBOSClient(system_database_url=DBOS_DB_URL, dbos_system_schema=crash_schema)
    procs: list[WorkerProcess] = []
    try:
        crashing = WorkerProcess(_env(crash_schema, executor_id, EAP_CRASH_MARKER=str(tmp_path / "marker"), **crash),
                                 tmp_path / "worker-crash.log")
        procs.append(crashing)
        crashing.wait_ready()
        run_id = start_eval(worker.engine, worker.run_store, client, tenant_id=tenant, agent_id=agent_id,
                            suite_id=suite_id, app_version=CRASH_APP_VERSION, max_concurrency=2)
        assert crashing.wait_exit() == 137, (tmp_path / "worker-crash.log").read_text()
        assert worker.run_store.get_run(tenant, run_id)["status"] == "running"
        recorded_before = len(results(worker.engine, tenant, run_id))
        recovering = WorkerProcess(_env(crash_schema, executor_id), tmp_path / "worker-recover.log")
        procs.append(recovering)
        run = wait_terminal(worker.run_store, tenant, run_id, timeout=120)
    finally:
        for proc in procs:
            proc.kill()
        client.destroy()

    assert run["status"] == "completed", (tmp_path / "worker-recover.log").read_text()
    assert recorded_before <= 1
    rows = results(worker.engine, tenant, run_id)
    assert [r.case_name for r in rows] == ["c0", "c1", "c2"] and all(r.status == "passed" for r in rows)
    keys = usage_keys(worker.engine, run_id)
    assert keys == sorted(f"judge:{r.case_id}:1" for r in rows)  # one judge row per key
    events = event_rows(worker.engine, run_id)
    seqs = [e[0] for e in events]
    assert seqs == list(range(len(seqs)))  # gap-free
    assert len({e[1] for e in events}) == len(events)
    assert [e[2] for e in events].count("eval_case_result") == 3
    assert [e[2] for e in events].count("run_completed") == 1
