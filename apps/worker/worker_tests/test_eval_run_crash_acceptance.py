"""AC.11 crash acceptance (tester): a crash mid-batch (a child done, a later one running) and after a
case's scoring/judge but before its record. After recovery in a fresh process: one result and one
child run per case (deterministic ids), one judge usage row per key, run cost = sum of cases (no
double count), gap-free seq. Complements test_eval_run_crash.py. Real subprocesses, killed in finally."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from dbos import DBOSClient

from eap_worker.eval_run import eval_child_run_id
from worker_tests.conftest import DBOS_DB_URL, event_rows, wait_terminal
from worker_tests.eval_run_helpers import JUDGE, child_runs, cleanup_evals, create_suite, results, start_eval, usage_keys
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
        pytest.param({"EAP_CRASH_AT": "eval_child_done", "EAP_CRASH_EVENT_KEY": "position:1"}, id="mid-batch-second-case"),
        pytest.param({"EAP_CRASH_AT": "eval_case_scored", "EAP_CRASH_EVENT_KEY": "position:0"}, id="scored-not-recorded-first"),
        pytest.param({"EAP_CRASH_AT": "eval_case_scored", "EAP_CRASH_EVENT_KEY": "position:2"}, id="scored-not-recorded-next-batch"),
    ],
)
def test_crash_recovery_one_result_one_child_no_duplicate_judge_usage(worker, tenant, defs, crash_schema, tmp_path, crash):
    agent_id = publish_agent(worker.engine, tenant, agent_spec("priced"))
    cases = [{"name": f"c{i}", "input": f"word{i}"} for i in range(4)]
    evaluators = [{"kind": "max_cost", "usd": "0.05"}, {"kind": "llm_judge", "rubric": "MUST_CONTAIN:word", "model": JUDGE}]
    suite_id = create_suite(worker.engine, tenant, agent_id, cases, evaluators)
    executor_id = f"eval-crash-acc-{uuid.uuid4().hex[:8]}"
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
        recovering = WorkerProcess(_env(crash_schema, executor_id), tmp_path / "worker-recover.log")
        procs.append(recovering)
        run = wait_terminal(worker.run_store, tenant, run_id, timeout=120)
    finally:
        for proc in procs:
            proc.kill()
        client.destroy()

    assert run["status"] == "completed", (tmp_path / "worker-recover.log").read_text()
    rows = results(worker.engine, tenant, run_id)
    assert [(r.position, r.case_name, r.status) for r in rows] == [(i, f"c{i}", "passed") for i in range(4)]
    children = child_runs(worker.engine, run_id)
    assert sorted(str(c.id) for c in children) == sorted(eval_child_run_id(str(run_id), r.case_id) for r in rows)
    assert {str(c.id): c.status for c in children} == {str(c.id): "completed" for c in children}
    assert usage_keys(worker.engine, run_id) == sorted(f"judge:{r.case_id}:1" for r in rows)
    assert all(r.cost_usd == Decimal("0.0204") for r in rows)  # 0.02 child + 0.0004 judge, once
    assert run["cost_usd"] == Decimal("0.0816")
    events = event_rows(worker.engine, run_id)
    assert [e[0] for e in events] == list(range(len(events)))
    assert [e[2] for e in events].count("eval_case_result") == 4 and [e[2] for e in events].count("run_completed") == 1
