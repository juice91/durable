"""LP.14 acceptance (tester): the worker dies right after a judge call (`after_judge_call`),
before the usage row is written. Recovery replays the recorded judge step: no second judge call,
one billed row per key, the same outcome as an uncrashed run."""

from __future__ import annotations

import json
import time
import uuid

from dbos import DBOSClient

from worker_tests.conftest import DBOS_DB_URL, event_rows, wait_terminal
from worker_tests.test_conversation import step_names
from worker_tests.test_crash_recovery import APP_VERSION as CRASH_APP_VERSION
from worker_tests.test_crash_recovery import WorkerProcess, _env, crash_schema  # noqa: F401 - fixture
from worker_tests.test_workflow_evaluate import (  # noqa: F401
    JUDGE,
    JUDGE_COST,
    REFINE_COST,
    _evaluate,
    _judge,
    _refine,
    _usage_rows,
    _wf,
    _writer_spec,
    defs,
)
from worker_tests.workflow_run_helpers import enqueue_workflow_run, publish_agent, publish_workflow


def _crash(worker, tenant, crash_schema, tmp_path, spec, crash_key, input_value, unbilled):
    workflow_id, version_id = publish_workflow(worker.engine, tenant, spec)
    executor_id = f"ev-{uuid.uuid4().hex[:8]}"
    env = {"EAP_CRASH_AT": "after_judge_call", "EAP_CRASH_EVENT_KEY": crash_key}
    first = WorkerProcess(_env(crash_schema, executor_id, **env), tmp_path / "w1.log")
    second = dbos_client = None
    try:
        first.wait_ready()
        dbos_client = DBOSClient(system_database_url=DBOS_DB_URL, dbos_system_schema=crash_schema)
        run_id = enqueue_workflow_run(dbos_client, worker.run_store, tenant_id=tenant, workflow_id=workflow_id,
                                      version_id=version_id, input_value=input_value, app_version=CRASH_APP_VERSION)
        assert first.wait_exit() == 137, (tmp_path / "w1.log").read_text()
        assert "end" not in [e[1] for e in event_rows(worker.engine, run_id)]
        assert unbilled not in _usage_rows(worker, run_id)  # died before that judge's usage row
        second = WorkerProcess(_env(crash_schema, executor_id), tmp_path / "w2.log")
        second.wait_ready()
        run = wait_terminal(worker.run_store, tenant, run_id, timeout=90)
        time.sleep(0.5)
        return run_id, run
    finally:
        first.kill()
        if second is not None:
            second.stop()
        if dbos_client is not None:
            dbos_client.destroy()


def _assert_clean(worker, run_id):
    events = event_rows(worker.engine, run_id)
    keys = [e[1] for e in events]
    assert [e[0] for e in events] == list(range(len(events)))
    assert len(set(keys)) == len(keys) and keys[-1] == "end" and keys.count("end") == 1
    return {e[1]: e[3] for e in events}


def test_crash_after_a_top_level_judge_call_bills_once_and_keeps_the_outcome(
        worker, tenant, defs, crash_schema, tmp_path):
    spec = _wf(_evaluate([{"kind": "contains", "value": "h"}, _judge('MUST_CONTAIN:"hi"')]),
               limits={"max_cost_usd": "1"})
    run_id, run = _crash(worker, tenant, crash_schema, tmp_path, spec, "step:ev:judge_1", {"text": "hi"}, "judge:ev:1")
    assert run["status"] == "completed", (run["error"], (tmp_path / "w2.log").read_text())
    data = _assert_clean(worker, run_id)
    out = json.loads(run["output"])
    assert out["passed"] is True and out["results"][1]["score"] == 1.0
    assert data["step:ev:completed"]["output"] == out
    names = step_names(crash_schema, run_id)
    assert names.count("eap-judge__model.request") == 1  # the judge call is recorded once, never repeated
    assert names.count("eap.workflow_judge_usage") == 1
    rows = _usage_rows(worker, run_id)
    assert set(rows) == {"judge:ev:1"} and rows["judge:ev:1"].cost_usd == JUDGE_COST
    assert worker.run_store.run_tree_cost(tenant, run_id) == JUDGE_COST


def test_crash_after_a_refine_judge_call_in_iteration_one_bills_each_key_once(
        worker, tenant, defs, crash_schema, tmp_path):
    writer = publish_agent(worker.engine, tenant, _writer_spec())
    run_id, run = _crash(worker, tenant, crash_schema, tmp_path, _wf(_refine(writer)),
                         "loop:refine.1:step:judge:judge_0", {}, "judge:refine.1:judge:0")
    assert run["status"] == "completed", (run["error"], (tmp_path / "w2.log").read_text())
    data = _assert_clean(worker, run_id)
    out = json.loads(run["output"])
    assert (out["iterations"], out["exited"]) == (3, "condition")
    assert data["loop:refine.1:step:judge:completed"]["output"]["reason"] == (
        'The output does not contain the MUST_CONTAIN marker "attempt 3".')
    assert set(_usage_rows(worker, run_id)) == {f"judge:refine.{i}:judge:0" for i in range(3)}
    assert worker.run_store.run_tree_cost(tenant, run_id) == 3 * (REFINE_COST + JUDGE_COST)
    assert step_names(crash_schema, f"{run_id}:loop:refine.1").count("eap-judge__model.request") == 1
    assert len(worker.run_store.list_runs(tenant, parent_run_id=run_id)) == 3  # one writer child per iteration
