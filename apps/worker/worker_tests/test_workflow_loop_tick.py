"""LP.11: the loop tick (`eap.workflow_loop_tick`), the run-tree cost cap and the cooperative cancel.

The tick runs before every loop or foreach iteration starts and, for a spec with `limits`, before
every top-level agent and evaluate step. Its decision (cancel, then deadline, then the cost cap) is
workflow code over the recorded tick, so a replay decides identically. `test:priced` costs USD
0.02 per request.
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

import pytest

from eap_schemas.limits import RunBudgetExceeded
from eap_schemas.workflow_scope import EMPTY_SCOPE, child_scope
from eap_schemas.workflows import WorkflowSpec
from eap_worker import workflow_run as wr
from eap_worker.errors import RunFailure, WorkflowStepError
from worker_tests.conftest import APP_VERSION, event_rows, wait_terminal
from worker_tests.test_conversation import step_names
from worker_tests.workflow_run_helpers import (
    agent_spec,
    canonical,
    cleanup_definitions,
    enqueue_workflow_run,
    publish_agent,
    publish_workflow,
    wait_event,
)

EVENT = "eap.append_event"
BEGIN = "eap.workflow_step_begin"
TICK = "eap.workflow_loop_tick"
AGENT = ["eap.workflow_resolve_agent", "eap.agent_run", "DBOS.getResult"]


def _ref(path):
    return {"$ref": path}


def _wf(*steps, **extra):
    return {"name": "Tick", "input_schema": {"type": "object"}, "start": steps[0]["id"], "steps": list(steps), **extra}


def _tool(sid="x", nxt=None):
    return {"id": sid, "type": "tool", "tool_id": "clock.now", "input": {}, "next": nxt}


def _ask(agent_id, sid="ask", prompt="hi", nxt=None):
    return {"id": sid, "type": "agent", "agent_id": agent_id, "input": {"p": prompt}, "next": nxt}


def _loop(body, *, lid="lp", max_iterations=5, **kw):
    kw.setdefault("until", {"op": "eq", "left": _ref(f"loop.{lid}.index"), "right": 1000})  # never by condition
    return {"id": lid, "type": "loop", "max_iterations": max_iterations,
            "body": {"start": body[0]["id"], "steps": body}, **kw}


def _foreach(body, *, sid="each", max_items=5, **kw):
    return {"id": sid, "type": "foreach", "items": _ref("input.items"), "max_items": max_items,
            "body": {"start": body[0]["id"], "steps": body}, **kw}


# ---------------------------------------------------------------------------- the decision (pure)


def _interp(limits=None, *, started_at=1000.0, max_run_seconds=60):
    spec = WorkflowSpec.model_validate(_wf(_loop([_tool()]), **({"limits": limits} if limits else {})))
    return wr._Interpreter(
        "00000000-0000-0000-0000-000000000001", "t", spec, {}, {},
        {"started_at": started_at, "max_run_seconds": max_run_seconds}, None, workflow_version_id="v",
    )


def _fake_tick(monkeypatch, *, now=1001.0, cancel=False, cost="0"):
    calls = []

    async def tick(run_id, tenant_id, scope_text, step_id, with_cost):
        calls.append((scope_text, step_id, with_cost))
        return {"now": now, "cancel_requested": cancel, "cost_usd": cost if with_cost else None}

    monkeypatch.setattr(wr, "loop_tick_step", tick)
    monkeypatch.setattr(wr, "crash_point", lambda *a, **k: None)
    return calls


def _decide(interp):
    return asyncio.run(interp._tick("lp", child_scope(EMPTY_SCOPE, "lp", 0)))


def test_tick_passes_under_the_cap_and_asks_for_cost_only_with_one(monkeypatch):
    calls = _fake_tick(monkeypatch, cost="0.0499")
    _decide(_interp({"max_cost_usd": "0.05"}))
    _decide(_interp())
    assert calls == [("lp.0", "lp", True), ("lp.0", "lp", False)]


@pytest.mark.parametrize("cost", ["0.05", "0.050000", "0.07"])
def test_tick_at_or_over_the_cap_raises_run_budget_exceeded(monkeypatch, cost):
    _fake_tick(monkeypatch, cost=cost)
    with pytest.raises(RunBudgetExceeded) as raised:
        _decide(_interp({"max_cost_usd": "0.05"}))
    assert raised.value.data() == {"limit": "max_cost_usd", "value": "0.05", "spent_usd": str(Decimal(cost))}
    failure = wr.classify(raised.value)
    assert failure.code == "run_budget_exceeded" and failure.data["limit"] == "max_cost_usd"


def test_tick_cancel_wins_over_deadline_and_cap(monkeypatch):
    _fake_tick(monkeypatch, now=5000.0, cancel=True, cost="9")
    with pytest.raises(WorkflowStepError) as raised:
        _decide(_interp({"max_cost_usd": "0.05"}))
    assert (raised.value.code, raised.value.step_id) == ("run_cancelled", "lp")


def test_tick_deadline_wins_over_the_cap(monkeypatch):
    _fake_tick(monkeypatch, now=1061.0, cost="9")
    with pytest.raises(WorkflowStepError) as raised:
        _decide(_interp({"max_cost_usd": "0.05"}))
    assert raised.value.code == "workflow_deadline_exceeded"
    _fake_tick(monkeypatch, now=1060.0)  # exactly max_run_seconds old: still allowed
    _decide(_interp())


def test_step_begin_cancel_wins_over_the_early_deadline(monkeypatch):
    """The pre-execution deadline gate must use the cancel snapshot recorded by step-begin."""
    interp = _interp()

    async def begin(*args, **kwargs):
        return {"now": 1061.0, "cancel_requested": True}

    async def append(*args, **kwargs):
        return 0

    monkeypatch.setattr(wr, "step_begin_step", begin)
    monkeypatch.setattr(wr, "append_event_step", append)
    with pytest.raises(wr.StepFailed) as raised:
        asyncio.run(interp.run_step(interp.spec.steps[0]))
    assert raised.value.failure.code == "run_cancelled"


def test_old_float_step_begin_checkpoint_keeps_its_recorded_deadline_decision(monkeypatch):
    """A pre-fix replay returns the recorded float and must not gain a new cancel read or step."""
    interp = _interp()

    async def old_begin(*args, **kwargs):
        return 1061.0

    async def append(*args, **kwargs):
        return 0

    monkeypatch.setattr(wr, "step_begin_step", old_begin)
    monkeypatch.setattr(wr, "append_event_step", append)
    with pytest.raises(wr.StepFailed) as raised:
        asyncio.run(interp.run_step(interp.spec.steps[0]))
    assert raised.value.failure.code == "workflow_deadline_exceeded"


def test_step_begin_cancel_inside_the_deadline_still_reaches_the_tick(monkeypatch):
    """Cancel does not cut a step short unless that same snapshot is already past the deadline.
    The tick remains the cooperative cancel inside the deadline."""
    interp = _interp()
    calls = []

    async def begin(*args, **kwargs):
        return {"now": 1001.0, "cancel_requested": True}

    async def tick(*args, **kwargs):
        calls.append(args)
        raise wr.WorkflowStepError("run_cancelled", "lp")

    async def append(*args, **kwargs):
        return 0

    monkeypatch.setattr(wr, "step_begin_step", begin)
    monkeypatch.setattr(wr, "loop_tick_step", tick)
    monkeypatch.setattr(wr, "append_event_step", append)
    monkeypatch.setattr(wr, "crash_point", lambda *a, **k: None)
    with pytest.raises(wr.StepFailed) as raised:
        asyncio.run(interp.run_step(interp.spec.steps[0]))
    assert calls, "the deadline gate must not fail the step before its tick"
    assert raised.value.failure.code == "run_cancelled"


def test_failure_results_carry_data_only_when_there_is_some():
    plain = RunFailure("failed", "input_ref_missing", "WorkflowStepError", "m")
    assert wr._failure_result(plain, "x") == {
        "status": "failed", "error": "WorkflowStepError: m", "error_code": "input_ref_missing", "step_id": "x"}
    budget = wr._exception_result(RunBudgetExceeded("0.06", "0.05"))
    assert budget["error_code"] == "run_budget_exceeded" and budget["data"]["spent_usd"] == "0.06"
    assert wr._result_failure(budget).data == budget["data"]


# ---------------------------------------------------------------------------- on the worker


@pytest.fixture
def defs(worker, tenant):
    yield
    cleanup_definitions(worker.engine, tenant)


def _start(worker, client, tenant, spec, input_value, *, before=None):
    workflow_id, version_id = publish_workflow(worker.engine, tenant, spec)
    run_id = uuid.uuid4()
    if before is not None:
        worker.run_store.create_workflow_run(tenant, workflow_id, 1, canonical(input_value), run_id)
        before(run_id)
    return enqueue_workflow_run(
        client, worker.run_store, tenant_id=tenant, workflow_id=workflow_id, version_id=version_id,
        input_value=input_value, app_version=APP_VERSION, run_id=run_id,
    )


def _events(worker, run_id):
    return {k: (t, d) for _, k, t, d in event_rows(worker.engine, run_id)}


def _iteration_starts(events, loop_id="lp"):
    return sorted(int(k.split(".")[1].split(":")[0]) for k in events
                  if k.startswith(f"loop:{loop_id}.") and k.endswith(":started") and ":step:" not in k)


@pytest.mark.parametrize("cap, iterations", [("0.05", 3), ("0.04", 2)], ids=["over", "at-the-cap"])
def test_cost_capped_loop_stops_before_the_iteration_at_the_cap(
        worker, client, worker_settings, tenant, defs, cap, iterations):
    agent_id = publish_agent(worker.engine, tenant, agent_spec(model="priced"))
    spec = _wf(_loop([_ask(agent_id)]), limits={"max_cost_usd": cap})
    run_id = _start(worker, client, tenant, spec, {})
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "failed" and "cost limit" in run["error"], run["error"]
    events = _events(worker, run_id)
    assert _iteration_starts(events) == list(range(iterations))
    for key in ("step:lp:failed", "end"):
        data = events[key][1]
        assert data["error_code"] == "run_budget_exceeded", key
        assert (data["limit"], data["value"]) == ("max_cost_usd", cap)
        assert Decimal(data["spent_usd"]) == Decimal("0.02") * iterations
    assert events["end"][1]["step_id"] == "lp"
    names = step_names(worker_settings.dbos_system_schema, run_id)
    per = [TICK, "eap.workflow_iteration", "DBOS.getResult", EVENT]
    assert names == ["eap.start_workflow_run", EVENT, BEGIN, *per * iterations, TICK, EVENT, EVENT, "eap.finish_run"]


def test_top_level_agents_are_ticked_only_with_limits(worker, client, worker_settings, tenant, defs):
    agent_id = publish_agent(worker.engine, tenant, agent_spec(model="priced"))
    spec = _wf(_ask(agent_id, "a", nxt="b"), _ask(agent_id, "b"), limits={"max_cost_usd": "0.02"})
    run_id = _start(worker, client, tenant, spec, {})
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "failed", run
    events = _events(worker, run_id)
    assert events["step:b:failed"][1]["error_code"] == "run_budget_exceeded"
    assert "child_run_id" in events["step:b:started"][1]  # step b started, its agent never ran
    assert len(worker.run_store.list_runs(tenant, parent_run_id=run_id)) == 1
    assert step_names(worker_settings.dbos_system_schema, run_id) == [
        "eap.start_workflow_run", EVENT, BEGIN, TICK, *AGENT, EVENT, BEGIN, TICK, EVENT, EVENT, "eap.finish_run"]
    # The same spec without `limits` keeps the pre-LP.11 layout and runs both agents.
    free_id = _start(worker, client, tenant, _wf(_ask(agent_id, "a", nxt="b"), _ask(agent_id, "b")), {})
    assert wait_terminal(worker.run_store, tenant, free_id)["status"] == "completed"
    assert step_names(worker_settings.dbos_system_schema, free_id) == [
        "eap.start_workflow_run", EVENT, BEGIN, *AGENT, EVENT, BEGIN, *AGENT, EVENT, EVENT, "eap.finish_run"]


def _cancel_requested(worker, tenant):
    def write(run_id):
        worker.event_log.append(tenant, run_id, "cancel", "cancel_requested", {"requested_by": "test"})

    return write


@pytest.mark.parametrize("kind", ["loop", "foreach"])
def test_a_cancel_requested_before_the_first_tick_starts_no_iteration(worker, client, tenant, defs, kind):
    """The cooperative cancel (no API terminal write, no DBOS cancel): the worker ends the run
    `run_cancelled` through its own events."""
    step = _loop([_tool()]) if kind == "loop" else _foreach([_tool()], concurrency=2)
    run_id = _start(worker, client, tenant, _wf(step), {"items": [1, 2, 3]}, before=_cancel_requested(worker, tenant))
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "failed", run
    events = _events(worker, run_id)
    sid = step["id"]
    assert _iteration_starts(events, sid) == []
    assert events[f"step:{sid}:failed"][1]["error_code"] == "run_cancelled"
    assert events["end"][1]["error_code"] == "run_cancelled" and events["end"][1]["step_id"] == sid
    assert [k for _, k, _, _ in event_rows(worker.engine, run_id)].count("end") == 1


def test_a_cancel_requested_mid_loop_stops_it_within_one_iteration(worker, client, tenant, defs):
    spec = _wf(_loop([_tool()], max_iterations=100))
    run_id = _start(worker, client, tenant, spec, {})
    wait_event(worker.engine, run_id, "loop:lp.2:started")
    _cancel_requested(worker, tenant)(run_id)
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "failed", run
    events = _events(worker, run_id)
    starts = _iteration_starts(events)
    assert 3 <= len(starts) < 100 and starts == list(range(len(starts)))
    for i in starts:  # the iteration in flight finished; none started after the tick saw the cancel
        assert f"loop:lp.{i}:completed" in events
    cancel_seq = next(seq for seq, k, _, _ in event_rows(worker.engine, run_id) if k == "cancel")
    late = [k for seq, k, _, _ in event_rows(worker.engine, run_id)
            if seq > cancel_seq and k.endswith(":started") and ":step:" not in k]
    assert len(late) <= 1  # at most the iteration whose tick ran before the cancel was written
    assert events["end"][1]["error_code"] == "run_cancelled"


def test_a_cancel_landing_between_the_tick_and_the_child_run_ends_the_run_run_cancelled(
        worker, client, tenant, defs, monkeypatch):
    """The live race: the tick before iteration 1 reads no cancel, then the cancel lands before the
    iteration's agent step creates its child run. The child is refused (ParentRunStopped) and the
    run ends exactly as a cooperative cancel: `run_cancelled` on the loop step and `end`."""
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    original_tick = wr.loop_tick_step
    reads = []

    async def tick(run_id, tenant_id, scope_text, step_id, with_cost):
        found = await original_tick(run_id, tenant_id, scope_text, step_id, with_cost)
        reads.append(run_id)
        if len(reads) == 2:  # the tick before iteration 1 has read "no cancel"
            worker.event_log.append(tenant_id, run_id, "cancel", "cancel_requested", {"requested_by": "test"})
        return found

    monkeypatch.setattr(wr, "loop_tick_step", tick)
    run_id = _start(worker, client, tenant, _wf(_loop([_ask(agent_id)], max_iterations=6)), {})
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "failed", run
    events = _events(worker, run_id)
    assert _iteration_starts(events) == [0, 1]
    assert events["loop:lp.1:failed"][1]["error_code"] == "run_cancelled"
    assert events["step:lp:failed"][1]["error_code"] == "run_cancelled"
    assert events["end"][1]["error_code"] == "run_cancelled" and events["end"][1]["step_id"] == "lp"
    assert [k for _, k, _, _ in event_rows(worker.engine, run_id)].count("end") == 1
    assert len(worker.run_store.list_runs(tenant, parent_run_id=run_id)) == 1  # iteration 1's was refused


@pytest.mark.parametrize("end, expected", [
    (None, ("run_cancelled", None, None)),
    ({"error_code": "run_cancelled", "status": "failed", "error": "WorkflowStepError: the run was cancelled"},
     ("run_cancelled", "failed", "WorkflowStepError: the run was cancelled")),
    ({"error_code": "run_budget_exceeded", "status": "failed", "error": "RunBudgetExceeded: the run reached its cost limit"},
     ("run_budget_exceeded", "failed", "RunBudgetExceeded: the run reached its cost limit")),
    ({"error_code": "workflow_deadline_exceeded", "status": "failed"}, ("workflow_deadline_exceeded", "failed", None)),
    ({"error_code": "side_effect_outcome_unknown", "status": "needs_attention"},
     ("side_effect_outcome_unknown", "needs_attention", None)),
], ids=["cancel-only", "api-cancel", "cost-cap", "deadline", "needs-attention"])
def test_a_refused_child_run_fails_the_step_with_the_parents_stop_reason(worker, tenant, defs, end, expected):
    """ParentRunStopped maps to the run's own stop: its `end` when it has one (so a cost cap or a
    deadline keeps its code), else `run_cancelled` for a cancel request."""
    workflow_id, _ = publish_workflow(worker.engine, tenant, _wf(_tool()))
    run_id = uuid.uuid4()
    worker.run_store.create_workflow_run(tenant, workflow_id, 1, canonical({}), run_id)
    worker.event_log.append(tenant, run_id, "cancel", "cancel_requested", {"requested_by": "test"})
    if end is not None:
        worker.event_log.append(tenant, run_id, "end", "run_failed", end)
    got = wr.parent_stop_failure(worker, tenant, str(run_id))
    assert (got["code"], got["status"], got["error"]) == expected
    assert "budget" not in got  # no AC.5 data in these ends
    failure = wr.classify(WorkflowStepError(got["code"], "ask", got["status"], got["error"]))
    assert failure.code == expected[0] and failure.status == (expected[1] or "failed")


def test_a_refused_child_after_the_cost_cap_keeps_ac5_data(worker, tenant, defs):
    """An `end` that failed on the cost cap with AC.5's {limit, value, spent_usd}: the refused
    step raises RunBudgetExceeded, so its step_failed carries the same data."""
    workflow_id, _ = publish_workflow(worker.engine, tenant, _wf(_tool()))
    run_id = uuid.uuid4()
    worker.run_store.create_workflow_run(tenant, workflow_id, 1, canonical({}), run_id)
    worker.event_log.append(tenant, run_id, "end", "run_failed", {
        "error_code": "run_budget_exceeded", "status": "failed",
        "error": "RunBudgetExceeded: the run reached its cost limit",
        "limit": "max_cost_usd", "value": "0.05", "spent_usd": "0.06",
    })
    got = wr.parent_stop_failure(worker, tenant, str(run_id))
    assert got["code"] == "run_budget_exceeded" and got["budget"] == {"value": "0.05", "spent_usd": "0.06"}
    failure = wr.classify(RunBudgetExceeded(got["budget"]["spent_usd"], got["budget"]["value"]))
    assert failure.code == "run_budget_exceeded"
    assert failure.data == {"limit": "max_cost_usd", "value": "0.05", "spent_usd": "0.06"}


@pytest.mark.parametrize("mode", ["fail", "collect"])
def test_foreach_collect_never_collects_the_cost_cap(worker, client, tenant, defs, mode):
    """Under `on_item_error: collect` (as under `fail`) the cap stops the foreach (no new starts,
    the started items awaited) and fails the step and the run; it is never an item's error entry."""
    agent_id = publish_agent(worker.engine, tenant, agent_spec(model="priced"))
    ask = {"id": "ask", "type": "agent", "agent_id": agent_id, "input": {"p": _ref("loop.each.item")}}
    spec = _wf(_foreach([ask], on_item_error=mode), limits={"max_cost_usd": "0.03"})
    run_id = _start(worker, client, tenant, spec, {"items": ["a", "b", "c", "d"]})
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "failed", run
    events = _events(worker, run_id)
    assert _iteration_starts(events, "each") == [0, 1]
    assert events["step:each:failed"][1]["error_code"] == "run_budget_exceeded"
    assert events["end"][1]["error_code"] == "run_budget_exceeded"
    assert "step:each:completed" not in events



def _tick_fault(monkeypatch, worker, tenant, *, at, fault):
    """Wrap the tick: on call `at` (1-based), `db_error` raises what a lost connection raises
    (sqlalchemy OperationalError, classified `internal_error`) and `cancel` writes the run's
    `cancel_requested` event just before the real tick reads it. Deterministic, no timing."""
    from sqlalchemy.exc import OperationalError

    real = wr.loop_tick_step
    calls = {"n": 0}

    async def tick(run_id, tenant_id, scope_text, step_id, with_cost):
        calls["n"] += 1
        if calls["n"] == at:
            if fault == "db_error":
                raise OperationalError("SELECT 1", {}, ConnectionResetError("connection reset by peer"))
            await asyncio.to_thread(
                worker.event_log.append, tenant, run_id, "cancel", "cancel_requested", {"requested_by": "test"})
        return await real(run_id, tenant_id, scope_text, step_id, with_cost)

    monkeypatch.setattr(wr, "loop_tick_step", tick)
    return calls


@pytest.mark.parametrize("mode", ["fail", "collect"])
@pytest.mark.parametrize("at", [1, 2], ids=["initial-window", "refill"])
@pytest.mark.parametrize("fault, code", [("db_error", "internal_error"), ("cancel", "run_cancelled")])
def test_a_failed_foreach_tick_fails_the_run_and_is_never_an_item_entry(
        monkeypatch, worker, client, tenant, defs, mode, at, fault, code):
    """A tick failure belongs to no item: under `collect` as under `fail` it stops the foreach and
    fails the run with its own code, so a foreach never completes with fewer entries than items
    (3 items in, 2 entries out was the LP.11 review's blocking bug)."""
    calls = _tick_fault(monkeypatch, worker, tenant, at=at, fault=fault)
    spec = _wf(_foreach([_tool()], concurrency=1, on_item_error=mode))
    run_id = _start(worker, client, tenant, spec, {"items": ["a", "b", "c"]})
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "failed", run
    events = _events(worker, run_id)
    assert _iteration_starts(events, "each") == list(range(at - 1))  # no item starts after the failed tick
    for index in _iteration_starts(events, "each"):
        assert f"loop:each.{index}:completed" in events  # started items were awaited
    assert "step:each:completed" not in events
    assert events["step:each:failed"][1]["error_code"] == code
    assert events["end"][1]["error_code"] == code
    assert calls["n"] == at


@pytest.mark.parametrize("mode", ["fail", "collect"])
def test_a_foreach_without_tick_faults_accounts_for_every_item(monkeypatch, worker, client, tenant, defs, mode):
    """The control: the same foreach with healthy ticks gives one entry per item."""
    calls = _tick_fault(monkeypatch, worker, tenant, at=0, fault="db_error")
    spec = _wf(_foreach([_tool()], concurrency=1, on_item_error=mode))
    run_id = _start(worker, client, tenant, spec, {"items": ["a", "b", "c"]})
    assert wait_terminal(worker.run_store, tenant, run_id)["status"] == "completed"
    events = _events(worker, run_id)
    assert len(events["step:each:completed"][1]["output"]) == 3
    assert calls["n"] == 3  # one tick per start under concurrency 1

# ---------------------------------------------------------------------------- iteration spans (LP.9 wiring)


@pytest.fixture
def spans(monkeypatch):
    """Telemetry on for this process: iteration spans go to an in-memory exporter."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    import eap_observability

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(eap_observability._state, "tracer_provider", provider)
    yield exporter
    provider.shutdown()


def _iteration_spans(exporter, run_id):
    return sorted(
        (s for s in exporter.get_finished_spans()
         if s.name == "eap.loop_iteration" and s.attributes.get("eap.run_id") == str(run_id)),
        key=lambda s: (s.attributes["eap.loop_scope"]),
    )


def test_one_iteration_span_per_iteration(worker, client, tenant, defs, spans):
    from opentelemetry.trace import StatusCode

    spec = _wf(_loop([_tool("x", "y"), _tool("y")], max_iterations=2, on_max_iterations="exit"))
    run_id = _start(worker, client, tenant, spec, {})
    assert wait_terminal(worker.run_store, tenant, run_id)["status"] == "completed"
    found = _iteration_spans(spans, run_id)
    assert [s.attributes["eap.loop_scope"] for s in found] == ["lp.0", "lp.1"]
    for i, s in enumerate(found):
        a = s.attributes
        assert (a["eap.iteration_index"], a["eap.loop_kind"], a["eap.workflow_step_id"]) == (i, "loop", "lp")
        assert (a["eap.iteration_status"], a["eap.body_steps"]) == ("completed", 2)
        assert s.status.status_code == StatusCode.OK


def test_a_nested_cost_cap_stop_ends_the_outer_iteration_span_as_an_error(worker, client, tenant, defs, spans):
    from opentelemetry.trace import StatusCode

    agent_id = publish_agent(worker.engine, tenant, agent_spec(model="priced"))
    inner = _loop([_ask(agent_id)], lid="in")
    spec = _wf(_loop([inner], lid="out", max_iterations=2), limits={"max_cost_usd": "0.03"})
    run_id = _start(worker, client, tenant, spec, {})
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "failed", run
    by_scope = {s.attributes["eap.loop_scope"]: s for s in _iteration_spans(spans, run_id)}
    assert sorted(by_scope) == ["out.0", "out.0/in.0", "out.0/in.1"]  # in.2's tick hit the cap
    outer = by_scope["out.0"]
    assert outer.status.status_code == StatusCode.ERROR
    assert (outer.attributes["eap.iteration_status"], outer.attributes["eap.error_code"]) == (
        "failed", "run_budget_exceeded")
    assert by_scope["out.0/in.1"].status.status_code == StatusCode.OK
    assert by_scope["out.0/in.1"].attributes["eap.loop_depth"] == 2
