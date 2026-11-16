"""LP.11 acceptance (tester): crash replay around the tick (`after_loop_tick`).

The worker dies right after `eap.workflow_loop_tick` is checkpointed and before the parent acts on
it. Recovery replays the recorded tick: it is not run or billed again, and the cancel and cap
decisions come out the same, whatever happened to the world in between.
"""

from __future__ import annotations

import time
import uuid
from decimal import Decimal

from dbos import DBOSClient

from worker_tests.conftest import DBOS_DB_URL, event_rows, wait_terminal
from worker_tests.test_conversation import step_names
from worker_tests.test_crash_recovery import APP_VERSION as CRASH_APP_VERSION
from worker_tests.test_crash_recovery import WorkerProcess, _env, crash_schema  # noqa: F401 - fixture
from worker_tests.test_workflow_loop_tick import BEGIN, TICK, _ask, _loop, _tool, _wf, defs  # noqa: F401
from worker_tests.workflow_run_helpers import agent_spec, enqueue_workflow_run, publish_agent, publish_workflow
from worker_tests.test_workflow_loop_cancel_acceptance import _body
from worker_tests.test_workflow_loop_tick import _iteration_starts


def _crash(worker, tenant, crash_schema, tmp_path, spec, crash_key, *, while_dead=None, status="completed"):
    """Run `spec` on a worker that dies at the tick `crash_key`; `while_dead(run_id)` runs before
    the replacement worker starts. Returns (run_id, run, step names)."""
    workflow_id, version_id = publish_workflow(worker.engine, tenant, spec)
    executor_id = f"tk-{uuid.uuid4().hex[:8]}"
    env = {"EAP_CRASH_AT": "after_loop_tick", "EAP_CRASH_EVENT_KEY": crash_key}
    first = WorkerProcess(_env(crash_schema, executor_id, **env), tmp_path / "w1.log")
    second = dbos_client = None
    try:
        first.wait_ready()
        dbos_client = DBOSClient(system_database_url=DBOS_DB_URL, dbos_system_schema=crash_schema)
        run_id = enqueue_workflow_run(dbos_client, worker.run_store, tenant_id=tenant, workflow_id=workflow_id,
                                      version_id=version_id, input_value={}, app_version=CRASH_APP_VERSION)
        assert first.wait_exit() == 137, (tmp_path / "w1.log").read_text()
        assert "end" not in [e[1] for e in event_rows(worker.engine, run_id)]
        if while_dead:
            while_dead(run_id)
        second = WorkerProcess(_env(crash_schema, executor_id), tmp_path / "w2.log")
        second.wait_ready()
        run = wait_terminal(worker.run_store, tenant, run_id, timeout=90)
        assert run["status"] == status, (run["error"], (tmp_path / "w2.log").read_text())
        time.sleep(0.5)  # recovery adds nothing once terminal
        return run_id, run, step_names(crash_schema, run_id)
    finally:
        first.kill()
        if second is not None:
            second.stop()
        if dbos_client is not None:
            dbos_client.destroy()


def _clean_events(worker, run_id):
    events = event_rows(worker.engine, run_id)
    keys = [e[1] for e in events]
    assert [e[0] for e in events] == list(range(len(events)))  # gap-free
    assert len(set(keys)) == len(keys) and keys.count("end") == 1 and keys[-1] == "end"
    return {e[1]: e[3] for e in events}


def test_crash_at_a_tick_replays_it_without_a_second_tick_or_a_double_iteration(
        worker, tenant, defs, crash_schema, tmp_path):
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    spec = _wf(_loop(_body(agent_id), max_iterations=3, on_max_iterations="exit"))
    run_id, run, names = _crash(worker, tenant, crash_schema, tmp_path, spec, "loop:lp.1:tick")
    data = _clean_events(worker, run_id)
    assert names.count(TICK) == 3  # ticks 0, 1, 2: the crashed tick is recorded once, not re-run
    assert _iteration_starts(data) == [0, 1, 2]
    assert worker.tickets.call_count(tenant, f"{run_id}:") == 3


def _crash_with_a_cancel_while_dead(worker, tenant, crash_schema, tmp_path):
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    spec = _wf(_loop(_body(agent_id), max_iterations=6))

    def cancel(run_id):
        worker.event_log.append(tenant, run_id, "cancel", "cancel_requested", {"requested_by": "test"})

    run_id, run, names = _crash(worker, tenant, crash_schema, tmp_path, spec, "loop:lp.1:tick",
                                while_dead=cancel, status="failed")
    return run_id, _clean_events(worker, run_id), names


def test_a_cancel_arriving_while_the_worker_is_dead_does_not_change_the_recorded_tick(
        worker, tenant, defs, crash_schema, tmp_path):
    """The tick before iteration 1 was recorded `cancel_requested: false`. The cancel event is
    written after the crash: replay still starts iteration 1 (the same decision) and nothing runs
    after it: no third iteration, no second ticket, one tick re-used."""
    run_id, data, names = _crash_with_a_cancel_while_dead(worker, tenant, crash_schema, tmp_path)
    assert _iteration_starts(data) == [0, 1]
    assert "loop:lp.2:started" not in data and names.count(TICK) <= 3
    assert worker.tickets.call_count(tenant, f"{run_id}:") == 1  # iteration 1's agent run was refused


def test_a_cancel_arriving_while_the_worker_is_dead_ends_the_run_run_cancelled(
        worker, tenant, defs, crash_schema, tmp_path):
    """The replayed iteration's child run is refused (ParentRunStopped, the parent has `cancel`).
    The run must still end `run_cancelled`, not an internal error."""
    run_id, data, names = _crash_with_a_cancel_while_dead(worker, tenant, crash_schema, tmp_path)
    assert data["end"]["error_code"] == "run_cancelled", data["end"]
    # The same events as a cooperative cancel: the loop step fails run_cancelled, then `end`.
    assert data["end"]["step_id"] == "lp" and data["step:lp:failed"]["error_code"] == "run_cancelled"
    assert data["loop:lp.1:failed"]["error_code"] == "run_cancelled" and "step:lp:completed" not in data


def test_a_cost_cap_decision_replays_identically_and_bills_nothing_twice(
        worker, tenant, defs, crash_schema, tmp_path):
    """The tick before iteration 2 read USD 0.04 (the cap): the worker dies, recovery replays that
    recorded reading and fails the step the same way, with no third agent run."""
    agent_id = publish_agent(worker.engine, tenant, agent_spec(model="priced"))
    spec = _wf(_loop([_ask(agent_id)], max_iterations=10), limits={"max_cost_usd": "0.04"})
    run_id, run, names = _crash(worker, tenant, crash_schema, tmp_path, spec, "loop:lp.2:tick", status="failed")
    data = _clean_events(worker, run_id)
    assert data["end"]["error_code"] == "run_budget_exceeded"
    assert (data["end"]["limit"], data["end"]["value"]) == ("max_cost_usd", "0.04")
    assert Decimal(data["end"]["spent_usd"]) == Decimal("0.04") == worker.run_store.run_tree_cost(tenant, run_id)
    assert _iteration_starts(data) == [0, 1] and names.count(TICK) == 3
    assert len(worker.run_store.list_runs(tenant, parent_run_id=run_id)) == 2


def test_a_crash_at_a_top_level_agent_tick_runs_the_agent_once(worker, tenant, defs, crash_schema, tmp_path):
    agent_id = publish_agent(worker.engine, tenant, agent_spec(model="priced"))
    spec = _wf(_ask(agent_id, "a", nxt="b"), _ask(agent_id, "b"), limits={"max_cost_usd": "5"})
    run_id, run, names = _crash(worker, tenant, crash_schema, tmp_path, spec, "step:b:tick")
    _clean_events(worker, run_id)
    assert names.count(TICK) == 2
    assert len(worker.run_store.list_runs(tenant, parent_run_id=run_id)) == 2
    assert worker.run_store.run_tree_cost(tenant, run_id) == Decimal("0.04")


def test_cancel_wins_over_deadline_when_step_begin_replays_after_a_crash(
        worker, tenant, defs, crash_schema, tmp_path):
    """The step-start event committed but its DBOS step did not. While the worker is down the
    cancel lands and the recorded deadline expires; replay re-executes step-begin and must choose
    cancellation without adding another durable operation or event."""
    workflow_id, version_id = publish_workflow(worker.engine, tenant, _wf(_tool()))
    executor_id = f"tk-{uuid.uuid4().hex[:8]}"
    env = {
        "EAP_CRASH_AT": "after_event_write",
        "EAP_CRASH_EVENT_KEY": "step:x:started",
        "EAP_WORKFLOW_MAX_RUN_SECONDS": "1",
    }
    first = WorkerProcess(_env(crash_schema, executor_id, **env), tmp_path / "deadline-w1.log")
    second = dbos_client = None
    try:
        first.wait_ready()
        dbos_client = DBOSClient(system_database_url=DBOS_DB_URL, dbos_system_schema=crash_schema)
        run_id = enqueue_workflow_run(
            dbos_client,
            worker.run_store,
            tenant_id=tenant,
            workflow_id=workflow_id,
            version_id=version_id,
            input_value={},
            app_version=CRASH_APP_VERSION,
        )
        assert first.wait_exit() == 137, (tmp_path / "deadline-w1.log").read_text()
        worker.event_log.append(tenant, run_id, "cancel", "cancel_requested", {"requested_by": "test"})
        time.sleep(1.1)  # cross the run's recorded one-second deadline while no worker is running
        second = WorkerProcess(_env(crash_schema, executor_id), tmp_path / "deadline-w2.log")
        second.wait_ready()
        run = wait_terminal(worker.run_store, tenant, run_id, timeout=90)
        assert run["status"] == "failed", (run["error"], (tmp_path / "deadline-w2.log").read_text())
        data = _clean_events(worker, run_id)
        assert data["step:x:failed"]["error_code"] == "run_cancelled"
        assert data["end"]["error_code"] == "run_cancelled"
        assert step_names(crash_schema, run_id).count(BEGIN) == 1
    finally:
        first.kill()
        if second is not None:
            second.stop()
        if dbos_client is not None:
            dbos_client.destroy()
