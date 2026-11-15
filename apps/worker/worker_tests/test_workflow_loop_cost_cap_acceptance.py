"""LP.11 acceptance (tester): the run-tree cost cap. `test:priced` costs USD 0.02 a request."""

from __future__ import annotations

from decimal import Decimal

import pytest

from worker_tests.conftest import wait_terminal
from worker_tests.test_workflow_loop_tick import _ask, _events, _foreach, _iteration_starts, _loop, _ref, _start, _tool, _wf, defs  # noqa: F401
from worker_tests.workflow_run_helpers import agent_spec, publish_agent


def _children(worker, tenant, run_id):
    return worker.run_store.list_runs(tenant, parent_run_id=run_id)


def _assert_cap(events, cap, spent):
    for key in ("step:lp:failed", "end"):
        data = events[key][1]
        assert data["error_code"] == "run_budget_exceeded", key
        assert (data["limit"], data["value"]) == ("max_cost_usd", cap), key
        assert Decimal(data["spent_usd"]) == Decimal(spent), key


def test_cost_across_nested_child_runs_reaches_the_cap_and_nothing_starts_after(worker, client, tenant, defs):
    """The cap sums the whole run tree: iteration child runs and the agent runs below them."""
    agent_id = publish_agent(worker.engine, tenant, agent_spec(model="priced"))
    spec = _wf(_loop([{**_ask(agent_id), "next": "ask2"}, _ask(agent_id, sid="ask2")], max_iterations=20), limits={"max_cost_usd": "0.07"})
    run_id = _start(worker, client, tenant, spec, {})
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "failed" and "cost limit" in run["error"], run["error"]
    events = _events(worker, run_id)
    assert _iteration_starts(events) == [0, 1]  # 0.04 after one iteration, 0.08 after two: stop
    _assert_cap(events, "0.07", "0.08")
    assert "loop:lp.2:started" not in events and "step:lp:completed" not in events
    assert len(_children(worker, tenant, run_id)) == 4  # no agent run after the cap


def test_cost_cap_in_a_foreach_window_starts_no_further_items(worker, client, tenant, defs):
    agent_id = publish_agent(worker.engine, tenant, agent_spec(model="priced"))
    ask = {"id": "ask", "type": "agent", "agent_id": agent_id, "input": {"p": _ref("loop.each.item")}}
    spec = _wf(_foreach([ask], concurrency=1, max_items=6), limits={"max_cost_usd": "0.05"})
    run_id = _start(worker, client, tenant, spec, {"items": list("abcdef")})
    run = wait_terminal(worker.run_store, tenant, run_id)
    events = _events(worker, run_id)
    assert run["status"] == "failed"
    assert _iteration_starts(events, "each") == [0, 1, 2]
    assert events["end"][1]["error_code"] == "run_budget_exceeded"
    assert Decimal(events["end"][1]["spent_usd"]) == Decimal("0.06")


def test_unpriced_usage_counts_as_zero_in_the_run_tree_cost_the_tick_reads(worker, client, tenant, defs):
    import asyncio
    import uuid

    from eap_worker.workflow_run import loop_tick_step

    agent_id = publish_agent(worker.engine, tenant, agent_spec(model="priced"))
    spec = _wf(_loop([_ask(agent_id)], max_iterations=2, on_max_iterations="exit"), limits={"max_cost_usd": "5"})
    run_id = _start(worker, client, tenant, spec, {})
    assert wait_terminal(worker.run_store, tenant, run_id)["status"] == "completed"
    usage = {"request_key": "model:99", "model_id": "x:unpriced", "provider_model_name": None, "input_tokens": 9,
             "output_tokens": 9, "cache_read_tokens": 0, "cache_write_tokens": 0, "usage_measured": True,
             "cost_usd": None, "price_source": None}
    before = worker.run_store.run_tree_cost(tenant, run_id)
    assert before == Decimal("0.04")
    worker.event_log.append(tenant, run_id, "unpriced", "model_message", {}, usage)  # a NULL-cost row in the tree
    assert worker.run_store.run_tree_cost(tenant, run_id) == before
    tick = asyncio.run(loop_tick_step(str(run_id), tenant, "lp.9", "lp", True))
    assert Decimal(tick["cost_usd"]) == before and tick["cancel_requested"] is False


def test_a_loop_free_tool_loop_never_trips_the_cap(worker, client, tenant, defs):
    spec = _wf(_loop([_tool()], max_iterations=3, on_max_iterations="exit"), limits={"max_cost_usd": "0.0001"})
    run_id = _start(worker, client, tenant, spec, {})
    assert wait_terminal(worker.run_store, tenant, run_id)["status"] == "completed"


def test_the_cap_stops_a_top_level_agent_after_a_loop_spent_it(worker, client, tenant, defs):
    agent_id = publish_agent(worker.engine, tenant, agent_spec(model="priced"))
    loop = _loop([_ask(agent_id)], max_iterations=2, on_max_iterations="exit", next="after")
    spec = _wf(loop, {"id": "after", "type": "agent", "agent_id": agent_id, "input": {"p": "z"}},
               limits={"max_cost_usd": "0.04"})
    run_id = _start(worker, client, tenant, spec, {})
    run = wait_terminal(worker.run_store, tenant, run_id)
    events = _events(worker, run_id)
    assert run["status"] == "failed"
    assert events["step:after:failed"][1]["error_code"] == "run_budget_exceeded"
    assert events["end"][1]["step_id"] == "after" and events["end"][1]["limit"] == "max_cost_usd"
    assert len(_children(worker, tenant, run_id)) == 2  # the top-level agent never ran


@pytest.mark.parametrize("cap", ["0.0001"])
def test_a_cap_below_one_request_stops_before_the_second_iteration(worker, client, tenant, defs, cap):
    agent_id = publish_agent(worker.engine, tenant, agent_spec(model="priced"))
    run_id = _start(worker, client, tenant, _wf(_loop([_ask(agent_id)]), limits={"max_cost_usd": cap}), {})
    assert wait_terminal(worker.run_store, tenant, run_id)["status"] == "failed"
    assert _iteration_starts(_events(worker, run_id)) == [0]
