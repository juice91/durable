"""Follow-up to AC.11: an agent archived while its eval run runs. Archiving stops new work
(eap_api.agent_archive), so the eval run checks `agents.archived` once per batch (the recorded step
`eap.eval_agent_archived`); once archived, no further child run starts, every case not started is a
result with status `error` and error_code `agent_archived` (no child run), and the eval run fails
`eval_agent_archived`. Cases already started keep their results."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from eap_worker import eval_run
from eap_worker.eval_run import CASE_AGENT_ARCHIVED, eval_child_run_id
from worker_tests.conftest import APP_VERSION, event_rows, wait_terminal
from worker_tests.test_conversation import step_names
from worker_tests.eval_run_helpers import child_runs, cleanup_evals, create_suite, results, start_eval
from worker_tests.workflow_run_helpers import agent_spec, cleanup_definitions, publish_agent

CASES = [{"name": f"case {i}", "input": f"word{i}"} for i in range(3)]
EVALUATORS = [{"kind": "contains", "value": "Echo:"}]


@pytest.fixture
def defs(worker, tenant):
    yield
    cleanup_evals(worker.engine, tenant)
    cleanup_definitions(worker.engine, tenant)


def _archive(engine, tenant_id: str, agent_id: str) -> None:
    with engine.begin() as conn:
        conn.execute(text("UPDATE agents SET archived = true WHERE id = :a AND tenant_id = :t"),
                     {"a": agent_id, "t": tenant_id})


def _setup(worker, tenant):
    agent_id = publish_agent(worker.engine, tenant, agent_spec("echo"))
    suite_id = create_suite(worker.engine, tenant, agent_id, CASES, EVALUATORS)
    return agent_id, suite_id


def _assert_failed_archived(worker, tenant, run_id, run) -> None:
    assert run["status"] == "failed", run
    events = event_rows(worker.engine, run_id)
    end = events[-1]
    assert end[2] == "run_failed" and end[3]["error_code"] == "eval_agent_archived", end
    assert "archived" in run["error"]


def test_archived_mid_run_stops_the_remaining_cases(worker, client, tenant, defs, monkeypatch):
    agent_id, suite_id = _setup(worker, tenant)
    original = eval_run.eval_record_case_step

    async def record_then_archive(run_id, tenant_id, result, judge_usage):
        out = await original(run_id, tenant_id, result, judge_usage)
        if tenant_id == tenant and result["position"] == 0:
            _archive(worker.engine, tenant, agent_id)  # archived after the first batch's case
        return out

    monkeypatch.setattr(eval_run, "eval_record_case_step", record_then_archive)
    run_id = start_eval(worker.engine, worker.run_store, client, tenant_id=tenant, agent_id=agent_id,
                        suite_id=suite_id, app_version=APP_VERSION, max_concurrency=1)
    run = wait_terminal(worker.run_store, tenant, run_id, timeout=120)
    _assert_failed_archived(worker, tenant, run_id, run)

    rows = results(worker.engine, tenant, run_id)
    assert [r.case_name for r in rows] == [c["name"] for c in CASES]
    first, *stopped = rows
    # The case already started ran to the end and keeps its result.
    assert first.status == "passed" and first.child_run_id == eval_child_run_id(str(run_id), first.case_id)
    # The rest never started: an error result each, with the clear code and no child run.
    for r in stopped:
        assert (r.status, r.error_code, r.child_run_id, r.output) == ("error", CASE_AGENT_ARCHIVED, None, None)
    children = child_runs(worker.engine, run_id)
    assert [str(c.id) for c in children] == [first.child_run_id]
    # Every case has its eval_case_result event (progress views see all of them), then run_failed.
    events = event_rows(worker.engine, run_id)
    assert [e[1] for e in events[1:-1]] == [f"case:{r.case_id}" for r in rows]
    assert [e[3]["error_code"] for e in events[1:-1]] == [None, CASE_AGENT_ARCHIVED, CASE_AGENT_ARCHIVED]


def test_archived_before_the_first_batch_starts_no_child(worker, client, tenant, defs):
    agent_id, suite_id = _setup(worker, tenant)
    # Archived between the API's check and the worker's start (the API refuses new eval runs).
    _archive(worker.engine, tenant, agent_id)
    run_id = start_eval(worker.engine, worker.run_store, client, tenant_id=tenant, agent_id=agent_id,
                        suite_id=suite_id, app_version=APP_VERSION, max_concurrency=2)
    run = wait_terminal(worker.run_store, tenant, run_id, timeout=120)
    _assert_failed_archived(worker, tenant, run_id, run)
    rows = results(worker.engine, tenant, run_id)
    assert [(r.status, r.error_code, r.child_run_id) for r in rows] == [("error", CASE_AGENT_ARCHIVED, None)] * 3
    assert child_runs(worker.engine, run_id) == []


def test_an_agent_not_archived_runs_every_batch(worker, client, worker_settings, tenant, defs):
    agent_id, suite_id = _setup(worker, tenant)
    run_id = start_eval(worker.engine, worker.run_store, client, tenant_id=tenant, agent_id=agent_id,
                        suite_id=suite_id, app_version=APP_VERSION, max_concurrency=2)
    run = wait_terminal(worker.run_store, tenant, run_id, timeout=120)
    assert run["status"] == "completed", run
    assert [r.status for r in results(worker.engine, tenant, run_id)] == ["passed"] * 3
    steps = step_names(worker_settings.dbos_system_schema, run_id)
    # One recorded check per batch, each right before that batch's children are created.
    assert steps.count("eap.eval_agent_archived") == 2 and "eap.eval_stop_cases" not in steps
    checks = [i for i, s in enumerate(steps) if s == "eap.eval_agent_archived"]
    assert all(steps[i + 1] == "eap.eval_create_children" for i in checks)
