"""LP.11 acceptance (tester): where the tick runs and in what order it decides.

Pure part: the decision order over every combination of cancel, deadline and cap. Worker part:
the parent's step layout (a tick before every iteration, once per initial foreach window, before
top-level agent steps only with `limits`, never in a loop body) and that a loop-free workflow
without limits has no tick at all.
"""

from __future__ import annotations

import itertools

import pytest

from eap_schemas.limits import RunBudgetExceeded
from eap_worker.errors import WorkflowStepError
from worker_tests.conftest import wait_terminal
from worker_tests.test_conversation import step_names
from worker_tests.test_workflow_loop_tick import (
    AGENT, BEGIN, EVENT, TICK, _ask, _decide, _fake_tick, _foreach, _interp, _loop, _start, _tool, _wf, defs,  # noqa: F401
)
from worker_tests.workflow_run_helpers import agent_spec, publish_agent

START = "eap.start_workflow_run"
ITER = "eap.workflow_iteration"


@pytest.mark.parametrize("cancel, late, over", list(itertools.product([False, True], repeat=3)))
def test_decision_order_over_every_combination(monkeypatch, cancel, late, over):
    _fake_tick(monkeypatch, now=1100.0 if late else 1001.0, cancel=cancel, cost="1" if over else "0")
    expected = ("run_cancelled" if cancel else "workflow_deadline_exceeded" if late
                else "run_budget_exceeded" if over else None)
    if expected is None:
        _decide(_interp({"max_cost_usd": "0.5"}))
        return
    with pytest.raises((WorkflowStepError, RunBudgetExceeded)) as raised:
        _decide(_interp({"max_cost_usd": "0.5"}))
    code = getattr(raised.value, "code", None) or "run_budget_exceeded"
    assert code == expected


def _names(worker, worker_settings, client, tenant, spec, value):
    run_id = _start(worker, client, tenant, spec, value)
    assert wait_terminal(worker.run_store, tenant, run_id)["status"] == "completed"
    return step_names(worker_settings.dbos_system_schema, run_id)


@pytest.mark.parametrize("limits", [None, {"max_cost_usd": "5"}], ids=["no-limits", "limits"])
def test_a_tick_runs_before_every_iteration_and_none_after_the_last(
        worker, client, worker_settings, tenant, defs, limits):
    spec = _wf(_loop([_tool()], max_iterations=3, on_max_iterations="exit"), **({"limits": limits} if limits else {}))
    names = _names(worker, worker_settings, client, tenant, spec, {})
    per = [TICK, ITER, "DBOS.getResult", EVENT]
    assert names == [START, EVENT, BEGIN, *per * 3, EVENT, EVENT, "eap.finish_run"]


@pytest.mark.parametrize("c, n, ticks", [(1, 4, 4), (3, 5, 3), (5, 3, 1)])
def test_foreach_ticks_once_per_initial_window_and_once_per_refill(
        worker, client, worker_settings, tenant, defs, c, n, ticks):
    spec = _wf(_foreach([_tool()], concurrency=c, max_items=n))
    names = _names(worker, worker_settings, client, tenant, spec, {"items": list(range(n))})
    assert names.count(TICK) == ticks, names
    assert names.index(TICK) == names.index(BEGIN) + 1  # the first tick precedes every start
    assert names.count(ITER) == n


def test_agent_steps_inside_a_loop_body_add_no_parent_tick_for_limits(
        worker, client, worker_settings, tenant, defs):
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    spec = _wf(_loop([_ask(agent_id)], max_iterations=2, on_max_iterations="exit"), limits={"max_cost_usd": "5"})
    names = _names(worker, worker_settings, client, tenant, spec, {})
    assert names.count(TICK) == 2 and AGENT[1] not in names  # body agents run in the iteration workflows


def test_only_agent_steps_are_ticked_at_the_top_level_and_in_a_parallel_branch(
        worker, client, worker_settings, tenant, defs):
    from eap_worker.workflow_run import branch_workflow_id

    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    spec = _wf(
        {"id": "fan", "type": "parallel", "branches": ["a", "t"], "join": "j"},
        {"id": "a", "type": "agent", "agent_id": agent_id, "input": {"p": "x"}, "next": "j"},
        {"id": "t", "type": "tool", "tool_id": "clock.now", "input": {}, "next": "j"},
        _tool("j"), limits={"max_cost_usd": "5"})
    run_id = _start(worker, client, tenant, spec, {})
    assert wait_terminal(worker.run_store, tenant, run_id)["status"] == "completed"
    schema = worker_settings.dbos_system_schema
    assert TICK not in step_names(schema, run_id)  # the parent: no agent step of its own
    assert TICK in step_names(schema, branch_workflow_id(str(run_id), "fan", "a"))
    assert TICK not in step_names(schema, branch_workflow_id(str(run_id), "fan", "t"))
