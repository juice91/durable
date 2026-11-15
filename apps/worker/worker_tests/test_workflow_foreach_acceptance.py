"""LP.10 acceptance (tester): foreach on the in-process worker and real DBOS.

The implementer's test_workflow_foreach_run.py stays as is. This file adds: the measured window
(1, 3, 8; never more in flight than `concurrency`; item-ordered output under reversed latencies),
fail/collect with an error, a needs_attention and a cancelled item, the caps, nesting, the
references-only checkpoints and the layout of existing workflows. Crash and cancel are in the
second half.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator

import psycopg
import pytest
from sqlalchemy import text

from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from eap_worker.models import _last_prompt
from worker_tests.conftest import APP_VERSION, DBOS_DB_URL, event_rows, wait_terminal
from worker_tests.test_conversation import step_names
from worker_tests.test_crash_recovery import crash_schema  # noqa: F401 - fixture
from worker_tests.test_workflow_foreach_run import AWAIT, BEGIN, EVENT, START, _events, _foreach, _ref, _wf
from worker_tests.workflow_run_helpers import (
    agent_spec,
    cleanup_definitions,
    enqueue_workflow_run,
    publish_agent,
    publish_workflow,
)


@pytest.fixture
def defs(worker, tenant):
    yield
    cleanup_definitions(worker.engine, tenant)
    with worker.engine.begin() as conn:
        conn.execute(text("DELETE FROM side_effects WHERE tenant_id = :t"), {"t": tenant})


class Probe:
    """Counts model requests in flight (one per iteration's agent step) and their finish order."""

    def __init__(self, total: int, reverse: bool = True, base: float = 0.4, step: float = 0.1) -> None:
        self.total, self.reverse, self.base, self.step = total, reverse, base, step
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.finished: list[str] = []

    def delay(self, prompt: str) -> float:
        n = int(prompt[1:]) if prompt[:1] == "i" and prompt[1:].isdigit() else 0
        return self.base + self.step * ((self.total - n) if self.reverse else n)

    def install(self, monkeypatch) -> None:
        probe = self

        def build() -> FunctionModel:
            async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
                prompt = _last_prompt(messages)
                with probe.lock:
                    probe.active += 1
                    probe.max_active = max(probe.max_active, probe.active)
                try:
                    await asyncio.sleep(probe.delay(prompt))
                finally:
                    with probe.lock:
                        probe.active -= 1
                        probe.finished.append(prompt)
                yield f"Echo: {prompt}"

            def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
                return ModelResponse(parts=[TextPart(f"Echo: {_last_prompt(messages)}")])

            return FunctionModel(respond, stream_function=stream, model_name="scripted-echo")

        monkeypatch.setattr("eap_worker.models.scripted_echo_model", build)


def _ask(agent_id, ref="loop.each.item", sid="ask"):
    return {"id": sid, "type": "agent", "agent_id": agent_id, "input": {"p": _ref(ref)}}


def _start(worker, client, tenant, spec, input_value, run_id=None):
    workflow_id, version_id = publish_workflow(worker.engine, tenant, spec)
    return enqueue_workflow_run(
        client, worker.run_store, tenant_id=tenant, workflow_id=workflow_id, version_id=version_id,
        input_value=input_value, app_version=APP_VERSION, run_id=run_id,
    )


def _run(worker, client, tenant, spec, input_value, **kw):
    run_id = _start(worker, client, tenant, spec, input_value, **kw)
    return run_id, wait_terminal(worker.run_store, tenant, run_id, timeout=90)


# ---------------------------------------------------------------------------- the window
@pytest.mark.parametrize("concurrency", [1, 3, 8])
def test_window_is_measured_and_output_follows_item_order(
    worker, client, tenant, defs, monkeypatch, concurrency
):
    """Never more than `concurrency` iterations in flight, the window fills, and the output is in
    item order although (reversed latencies) item 0 finishes last."""
    count = 10
    probe = Probe(count)
    probe.install(monkeypatch)
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    spec = _wf(_foreach([_ask(agent_id)], concurrency=concurrency, max_items=count))
    items = [f"i{n}" for n in range(count)]
    run_id, run = _run(worker, client, tenant, spec, {"items": items})
    assert run["status"] == "completed", run["error"]
    assert json.loads(run["output"]) == [f"Echo: {i}" for i in items]
    assert probe.max_active == concurrency, (probe.max_active, concurrency)
    if concurrency == 1:
        assert probe.finished == items
    else:
        assert probe.finished != items  # items did finish out of order
    rows = event_rows(worker.engine, run_id)
    assert [r[0] for r in rows] == list(range(len(rows)))
    done = [r[1] for r in rows if r[1].startswith("loop:each.") and r[1].endswith(":completed")
            and ":step:" not in r[1]]
    assert done == [f"loop:each.{n}:completed" for n in range(count)]  # recorded in item order
    assert [r[1] for r in rows].count("end") == 1


def test_parent_operation_order_is_independent_of_latency(worker, client, worker_settings, tenant, defs, monkeypatch):
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    spec = _wf(_foreach([_ask(agent_id)], concurrency=3, max_items=6))
    items = [f"i{n}" for n in range(6)]
    layouts = []
    for reverse in (True, False):
        probe = Probe(6, reverse=reverse, base=0.2)
        probe.install(monkeypatch)
        run_id, run = _run(worker, client, tenant, spec, {"items": items})
        assert run["status"] == "completed", run["error"]
        assert json.loads(run["output"]) == [f"Echo: {i}" for i in items]
        layouts.append(step_names(worker_settings.dbos_system_schema, run_id))
    assert layouts[0] == layouts[1]
    assert layouts[0] == ["eap.start_workflow_run", EVENT, BEGIN, START, START, START, *AWAIT, START, *AWAIT,
                          START, *AWAIT, START, *AWAIT, *AWAIT, *AWAIT, EVENT, EVENT, "eap.finish_run"]


def test_concurrency_over_the_run_cap_is_refused_before_any_step(worker, client, tenant, defs, monkeypatch):
    """A foreach of concurrency 3 under a recorded run cap of 2 (EAP_WORKFLOW_MAX_RUN_CONCURRENCY) is
    refused before any step (workflow_concurrency_exceeded), never silently narrowed."""
    monkeypatch.setenv("EAP_WORKFLOW_MAX_RUN_CONCURRENCY", "2")
    probe = Probe(4)
    probe.install(monkeypatch)
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    spec = _wf(_foreach([_ask(agent_id)], concurrency=3, max_items=4))
    run_id, run = _run(worker, client, tenant, spec, {"items": ["i0", "i1", "i2", "i3"]})
    assert run["status"] == "failed"
    assert probe.max_active == 0
    assert {k for _, k, _, _ in event_rows(worker.engine, run_id)} <= {"start", "end"}
    assert _events(worker, run_id)["end"][1]["error_code"] == "workflow_concurrency_exceeded"


def test_window_at_exactly_the_run_concurrency_cap(worker, client, tenant, defs, monkeypatch):
    """A concurrency equal to the run cap is admitted and is the most ever in flight (the
    static check makes the runtime min() a defence: a smaller recorded cap refuses the run)."""
    monkeypatch.setenv("EAP_WORKFLOW_MAX_RUN_CONCURRENCY", "2")
    probe = Probe(5, base=0.3)
    probe.install(monkeypatch)
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    spec = _wf(_foreach([_ask(agent_id)], concurrency=2, max_items=5))
    _id, run = _run(worker, client, tenant, spec, {"items": [f"i{n}" for n in range(5)]})
    assert run["status"] == "completed", run["error"]
    assert probe.max_active == 2


# ---------------------------------------------------------------------------- fail vs collect
V_ITEMS = [{"v": "a"}, {}, {"v": "c"}, {}, {"v": "e"}]  # items 1 and 3 fail: input_ref_missing


def test_fail_reports_the_first_failed_item_by_index_and_starts_nothing_after(worker, client, tenant, defs):
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    spec = _wf(_foreach([_ask(agent_id, "loop.each.item.v")], concurrency=3, max_items=5))
    run_id, run = _run(worker, client, tenant, spec, {"items": V_ITEMS})
    assert run["status"] == "failed"
    events = _events(worker, run_id)
    assert events["end"][1]["error_code"] == "input_ref_missing" and events["end"][1]["step_id"] == "each"
    started = {k for k in events if k.startswith("loop:each.") and k.endswith(":started") and ":step:" not in k}
    # window 3: items 0,1,2 start; item 3 starts after item 0 is awaited, item 4 is never started
    # once item 1 (awaited second) has failed.
    assert started == {f"loop:each.{i}:started" for i in range(4)}
    assert "loop:each.4:started" not in events and "step:each:completed" not in events
    for i in range(4):  # every started item was awaited and recorded
        assert f"loop:each.{i}:completed" in events or f"loop:each.{i}:failed" in events
    assert len(children_of(worker, tenant, run_id)) == 2  # items 1 and 3 failed before their agent ran


def children_of(worker, tenant, run_id):
    return worker.run_store.list_runs(tenant, parent_run_id=run_id)


def test_collect_keeps_item_order_across_several_errors(worker, client, tenant, defs, monkeypatch):
    probe = Probe(5, base=0.2)
    probe.install(monkeypatch)
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    spec = _wf(_foreach([_ask(agent_id, "loop.each.item.v")], concurrency=3, max_items=5, on_item_error="collect"))
    items = [{"v": f"i{n}"} if n not in (1, 3) else {} for n in range(5)]
    run_id, run = _run(worker, client, tenant, spec, {"items": items})
    assert run["status"] == "completed", run["error"]
    out = json.loads(run["output"])
    assert [o if isinstance(o, str) else "ERR" for o in out] == ["Echo: i0", "ERR", "Echo: i2", "ERR", "Echo: i4"]
    assert all(set(out[i]) == {"error_code", "error"} for i in (1, 3))
    events = _events(worker, run_id)
    assert events["step:each:completed"][1]["output"] == out
    assert "loop:each.1:failed" in events and "loop:each.3:failed" in events


GUARDED = {"id": "mk", "type": "tool", "tool_id": "tickets.create", "input": {"title": "T", "body": "B"}}


@pytest.mark.parametrize("policy", ["fail", "collect"])
def test_needs_attention_item_is_never_collected(worker, client, tenant, defs, policy):
    """Item 1's side-effect guard is `reserved` with no result, so its tool step is
    side_effect_outcome_unknown. Under collect it still fails the step: an operator must look."""
    import uuid

    run_id = uuid.uuid4()
    with worker.engine.begin() as conn:
        conn.execute(text("INSERT INTO side_effects (tenant_id, key, state, created_at)"
                          " VALUES (:t, :k, 'reserved', now())"), {"t": tenant, "k": f"{run_id}:each.1:mk"})
    spec = _wf(_foreach([GUARDED], concurrency=1, max_items=3, on_item_error=policy))
    _id, run = _run(worker, client, tenant, spec, {"items": [1, 2, 3]}, run_id=run_id)
    assert run["status"] == "needs_attention", run["error"]
    events = _events(worker, run_id)
    assert events["loop:each.1:failed"][1]["status"] == "needs_attention"
    for key in ("step:each:failed", "end"):
        assert events[key][1]["error_code"] == "side_effect_outcome_unknown", key
    assert events["end"][1]["status"] == "needs_attention"
    assert "step:each:completed" not in events and "loop:each.2:started" not in events
    assert worker.tickets.call_count(tenant, f"{run_id}:") == 1  # item 0 only; item 1 never called


@pytest.fixture
def gate(monkeypatch):
    from worker_tests.cancel_acceptance_helpers import Gate, gated_echo_model

    g = Gate()
    monkeypatch.setattr("eap_worker.models.scripted_echo_model", gated_echo_model(g))
    yield g
    g.release.set()


@pytest.mark.parametrize("policy", ["fail", "collect"])
def test_a_cancelled_item_wins_over_another_item_failure(worker, client, tenant, defs, gate, policy):
    """Item 0 fails (input_ref_missing) and item 1's child is cancelled: the step ends
    run_cancelled, whichever of the two the parent saw first, with no error entry."""
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    spec = _wf(_foreach([_ask(agent_id, "loop.each.item.v")], concurrency=2, on_item_error=policy))
    run_id = _start(worker, client, tenant, spec, {"items": [{}, {"v": "b"}, {"v": "c"}]})
    assert gate.entered.wait(60)
    client.cancel_workflow(f"{run_id}:loop:each.1")
    gate.release.set()
    run = wait_terminal(worker.run_store, tenant, run_id, timeout=90)
    assert run["status"] == "failed"
    events = _events(worker, run_id)
    assert events["loop:each.0:failed"][1]["error_code"] == "input_ref_missing"
    assert events["loop:each.1:failed"][1]["error_code"] == "run_cancelled"
    for key in ("step:each:failed", "end"):
        assert events[key][1]["error_code"] == "run_cancelled", key
    assert "step:each:completed" not in events
    if policy == "fail":  # under collect item 2 starts once item 0 (an entry) is awaited, before the cancel
        assert "loop:each.2:started" not in events


# ---------------------------------------------------------------------------- caps
def test_item_count_cap_is_exact_and_starts_nothing(worker, client, tenant, defs):
    tool = {"id": "x", "type": "tool", "tool_id": "clock.now", "input": {}}
    spec = _wf(_foreach([tool], max_items=3, concurrency=3))
    _id, ok = _run(worker, client, tenant, spec, {"items": ["s1", "s2", "s3"]})
    assert ok["status"] == "completed", ok["error"]
    secret = "SECRET-ITEM-77"
    run_id, run = _run(worker, client, tenant, spec, {"items": [secret, "b", "c", "d"]})
    assert run["status"] == "failed"
    events = _events(worker, run_id)
    assert events["end"][1]["error_code"] == "loop_items_exceeded"
    assert not any(k.startswith("loop:") for k in events)
    failures = json.dumps([events["step:each:failed"], events["end"]])  # `start` holds the input
    assert secret not in failures and secret not in (run["error"] or "")
    assert not children_of(worker, tenant, run_id)


def test_output_cap_is_exact_and_leaks_no_output(worker, client, tenant, defs, monkeypatch):
    from eap_worker.workflow_eval import json_size

    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    spec = _wf(_foreach([_ask(agent_id)], concurrency=2))
    size = json_size(["Echo: SECRETAA", "Echo: SECRETBB"])
    monkeypatch.setenv("EAP_WORKFLOW_MAX_LOOP_OUTPUT_BYTES", str(size))
    _id, ok = _run(worker, client, tenant, spec, {"items": ["SECRETAA", "SECRETBB"]})
    assert ok["status"] == "completed", ok["error"]
    monkeypatch.setenv("EAP_WORKFLOW_MAX_LOOP_OUTPUT_BYTES", str(size - 1))
    run_id, run = _run(worker, client, tenant, spec, {"items": ["SECRETAA", "SECRETBB"]})
    assert run["status"] == "failed"
    events = _events(worker, run_id)
    assert events["step:each:failed"][1]["error_code"] == "step_output_too_large"
    assert events["end"][1]["error_code"] == "step_output_too_large"
    assert "step:each:completed" not in events
    for key in ("step:each:failed", "end"):
        assert "SECRET" not in json.dumps(events[key][1]), key
    assert "SECRET" not in (run["error"] or "")


# ---------------------------------------------------------------------------- nesting
def test_loop_inside_a_foreach(worker, client, tenant, defs):
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    ask = {"id": "ask", "type": "agent", "agent_id": agent_id, "input": {"p": _ref("loop.each.item")}}
    inner = {"id": "lp", "type": "loop", "max_iterations": 3,
             "until": {"op": "eq", "left": _ref("loop.lp.index"), "right": 1},
             "body": {"start": "ask", "steps": [ask]}}
    spec = _wf(_foreach([inner], concurrency=2, max_items=3))
    run_id, run = _run(worker, client, tenant, spec, {"items": ["x", "y", "z"]})
    assert run["status"] == "completed", run["error"]
    out = json.loads(run["output"])
    assert [(o["iterations"], o["exited"], o["last"]) for o in out] == [
        (2, "condition", f"Echo: {i}") for i in "xyz"]
    events = _events(worker, run_id)
    for i in range(3):
        for j in range(2):
            data = events[f"loop:each.{i}/lp.{j}:step:ask:completed"][1]
            assert data["scope"] == [{"loop": "each", "index": i}, {"loop": "lp", "index": j}]
    rows = event_rows(worker.engine, run_id)
    assert [r[0] for r in rows] == list(range(len(rows)))
    assert len({r[1] for r in rows}) == len(rows)  # every key unique
    ids = sorted(c["parent_step_id"] for c in children_of(worker, tenant, run_id))
    assert ids == sorted(f"each.{i}/lp.{j}/ask" for i in range(3) for j in range(2))


def test_foreach_inside_a_foreach_window_is_the_product(worker, client, tenant, defs, monkeypatch):
    probe = Probe(0, base=0.4)
    probe.install(monkeypatch)
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    inner = _foreach([_ask(agent_id, "loop.inner.item")], sid="inner", items="loop.outer.item", concurrency=2,
                     max_items=3)
    spec = _wf(_foreach([inner], sid="outer", items="input.groups", concurrency=2, max_items=3))
    groups = [["a1", "a2", "a3"], ["b1", "b2"], ["c1"]]
    run_id, run = _run(worker, client, tenant, spec, {"groups": groups})
    assert run["status"] == "completed", run["error"]
    assert json.loads(run["output"]) == [[f"Echo: {m}" for m in g] for g in groups]
    assert probe.max_active <= 4  # 2 outer x 2 inner
    rows = event_rows(worker.engine, run_id)
    assert [r[0] for r in rows] == list(range(len(rows)))
    assert len({r[1] for r in rows}) == len(rows) and [r[1] for r in rows].count("end") == 1


def test_run_concurrency_product_over_the_cap_is_refused_before_any_step(worker, client, tenant, defs):
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    inner = _foreach([_ask(agent_id, "loop.inner.item")], sid="inner", items="loop.outer.item", concurrency=3,
                     max_items=3)
    spec = _wf(_foreach([inner], sid="outer", items="input.groups", concurrency=3, max_items=3))
    run_id, run = _run(worker, client, tenant, spec, {"groups": [["a"], ["b"], ["c"]]})
    assert run["status"] == "failed"
    assert _events(worker, run_id)["end"][1]["error_code"] == "workflow_concurrency_exceeded"
    assert not children_of(worker, tenant, run_id)


# ---------------------------------------------------------------------------- references only
def _dbos_bytes(schema, run_id):
    """Everything DBOS stored for the run's parent and its iteration children, minus the root's
    own input and output: {(workflow, table): [decoded bytes]}."""
    import base64

    out: dict[tuple[str, str], list[bytes]] = {}

    def add(key, value):
        if value is None:
            return
        try:
            out.setdefault(key, []).append(base64.b64decode(value))
        except Exception:  # noqa: BLE001 - a non-base64 value is kept as raw text
            out.setdefault(key, []).append(str(value).encode())

    with psycopg.connect(DBOS_DB_URL, connect_timeout=5) as conn:
        ops = conn.execute(
            f'SELECT workflow_uuid, output FROM "{schema}".operation_outputs'
            " WHERE workflow_uuid = %s OR workflow_uuid LIKE %s", (str(run_id), f"{run_id}:loop:%")).fetchall()
        for wf, output in ops:
            add((wf, "op"), output)
        kids = conn.execute(
            f'SELECT s.workflow_uuid, coalesce(i.inputs, s.inputs), s.output FROM "{schema}".workflow_status s'
            f' LEFT JOIN "{schema}".workflow_input i USING (workflow_uuid) WHERE s.workflow_uuid LIKE %s',
            (f"{run_id}:loop:%",)).fetchall()
        for wf, inputs, output in kids:
            add((wf, "inputs"), inputs)
            add((wf, "output"), output)
    return out


def test_checkpoints_hold_references_not_items_or_outputs(worker, client, worker_settings, tenant, defs):
    """A list of large items: the iterations' arguments and results and the parent's operation
    outputs stay reference-sized, and the same size as for tiny items."""
    schema = worker_settings.dbos_system_schema
    tool = {"id": "x", "type": "tool", "tool_id": "clock.now", "input": {}}
    spec = _wf(_foreach([tool], concurrency=4, max_items=20))
    sizes = {}
    for label, width in (("small", 4), ("large", 20_000)):
        items = [f"{label}-{n:02d}-" + "Q" * width for n in range(20)]
        run_id, run = _run(worker, client, tenant, spec, {"items": items})
        assert run["status"] == "completed", run["error"]
        stored = _dbos_bytes(schema, run_id)
        marker = f"{label}-05-".encode() + b"Q" * min(width, 8)
        assert not any(marker in blob for blobs in stored.values() for blob in blobs), "an item was checkpointed"
        with psycopg.connect(DBOS_DB_URL, connect_timeout=5) as conn:  # the method can see an item: the run input
            root = conn.execute(
                f'SELECT coalesce(i.inputs, s.inputs) FROM "{schema}".workflow_status s LEFT JOIN'
                f' "{schema}".workflow_input i USING (workflow_uuid) WHERE s.workflow_uuid = %s',
                (str(run_id),)).fetchone()[0]
        import base64

        assert marker in base64.b64decode(root)
        sizes[label] = sum(len(b) for blobs in stored.values() for b in blobs)
        assert max(len(b) for key, blobs in stored.items() if key[1] == "inputs" for b in blobs) < 2048
    assert sizes["large"] <= sizes["small"] * 1.1 + 512, sizes  # independent of the item size
    assert sizes["large"] < 200 * 1024


# ---------------------------------------------------------------------------- existing workflows
def test_loop_free_workflows_keep_their_golden_step_lists(worker, client, worker_settings, tenant, defs):
    from worker_tests.test_workflow_loops_golden import GOLDEN_DEMO
    from worker_tests.workflow_run_helpers import demo_spec

    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    run_id, run = _run(worker, client, tenant, demo_spec(agent_id), {"q": "hello"})
    assert run["status"] == "completed", run["error"]
    assert step_names(worker_settings.dbos_system_schema, run_id) == GOLDEN_DEMO


def _while_until(kind):
    tool = {"id": "x", "type": "tool", "tool_id": "clock.now", "input": {}}
    cond = {"op": "lt", "left": _ref("loop.lp.index"), "right": 2}
    body = {"start": "x", "steps": [tool]}
    if kind == "while":
        return _wf({"id": "lp", "type": "loop", "max_iterations": 5, "while": cond, "body": body})
    return _wf({"id": "lp", "type": "loop", "max_iterations": 5, "body": body,
                "until": {"op": "eq", "left": _ref("loop.lp.index"), "right": 1}})


@pytest.mark.parametrize("kind", ["while", "until"])
def test_while_and_until_loops_keep_their_step_lists(worker, client, worker_settings, tenant, defs, kind):
    run_id, run = _run(worker, client, tenant, _while_until(kind), {})
    assert run["status"] == "completed", run["error"]
    assert json.loads(run["output"])["iterations"] == 2
    schema = worker_settings.dbos_system_schema
    per = ["eap.workflow_iteration", "DBOS.getResult", EVENT]  # the layout recorded at HEAD (LP.7)
    assert step_names(schema, run_id) == ["eap.start_workflow_run", EVENT, BEGIN, *per, *per, EVENT, EVENT,
                                          "eap.finish_run"]
    assert step_names(schema, f"{run_id}:loop:lp.0") == [
        EVENT, BEGIN, "eap.check_tool", "eap.workflow_tool_call", EVENT]
    keys = [k for _, k, _, _ in event_rows(worker.engine, run_id)]
    assert keys[:3] == ["start", "step:lp:started", "loop:lp.0:started"] and keys[-2:] == ["step:lp:completed", "end"]


# ---------------------------------------------------------------------------- crash recovery
N = 5
CRASH_BODY = [
    {"id": "ask", "type": "agent", "agent_id": "", "input": {"p": _ref("loop.each.item")}, "next": "mk"},
    {"id": "mk", "type": "tool", "tool_id": "tickets.create", "input": {"title": _ref("steps.ask.output"), "body": "B"}},
]


def _crash_spec(worker, tenant, **kw):
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    body = [{**CRASH_BODY[0], "agent_id": agent_id}, CRASH_BODY[1]]
    return _wf(_foreach(body, concurrency=3, max_items=N, **kw))


def _crash_run(worker, tenant, crash_schema, tmp_path, crash_env, spec, status="completed"):
    import uuid

    from dbos import DBOSClient
    from worker_tests.test_crash_recovery import APP_VERSION as CRASH_APP_VERSION
    from worker_tests.test_crash_recovery import WorkerProcess, _env

    workflow_id, version_id = publish_workflow(worker.engine, tenant, spec)
    executor_id = f"fe-{uuid.uuid4().hex[:8]}"
    first = WorkerProcess(_env(crash_schema, executor_id, **crash_env), tmp_path / "w1.log")
    second = dbos_client = None
    try:
        first.wait_ready()
        dbos_client = DBOSClient(system_database_url=DBOS_DB_URL, dbos_system_schema=crash_schema)
        run_id = enqueue_workflow_run(dbos_client, worker.run_store, tenant_id=tenant, workflow_id=workflow_id,
                                      version_id=version_id, input_value={"items": [f"i{n}" for n in range(N)]},
                                      app_version=CRASH_APP_VERSION)
        assert first.wait_exit() == 137, (tmp_path / "w1.log").read_text()
        assert "end" not in [e[1] for e in event_rows(worker.engine, run_id)]
        second = WorkerProcess(_env(crash_schema, executor_id), tmp_path / "w2.log")
        second.wait_ready()
        run = wait_terminal(worker.run_store, tenant, run_id, timeout=90)
        assert run["status"] == status, (run["error"], (tmp_path / "w2.log").read_text())
        import time

        time.sleep(0.6)  # a second look: recovery must add nothing once terminal
        return run_id, run
    finally:
        first.kill()
        if second is not None:
            second.stop()
        if dbos_client is not None:
            dbos_client.destroy()


def _expected_keys():
    keys = {"start", "step:each:started", "step:each:completed", "end"}
    for i in range(N):
        keys |= {f"loop:each.{i}:started", f"loop:each.{i}:completed",
                 *(f"loop:each.{i}:step:{s}:{e}" for s in ("ask", "mk") for e in ("started", "completed"))}
    return keys


@pytest.mark.parametrize("crash_env", [
    pytest.param({"EAP_CRASH_AT": "workflow_mid_step", "EAP_CRASH_EVENT_KEY": "loop:each.1:step:mk:started"},
                 id="in-flight-iteration"),
    pytest.param({"EAP_CRASH_AT": "after_event_write", "EAP_CRASH_EVENT_KEY": "loop:each.0:completed"},
                 id="parent-after-first-await"),
    pytest.param({"EAP_CRASH_AT": "after_event_write", "EAP_CRASH_EVENT_KEY": "loop:each.3:completed"},
                 id="parent-mid-window"),
    # The refill (LP.10 review): item 3's start is recorded after awaiting item 0, then the parent
    # dies before awaiting item 1; recovery replays the start (same workflow id) and runs item 3 once.
    pytest.param({"EAP_CRASH_AT": "foreach_refill", "EAP_CRASH_EVENT_KEY": "loop:each.3:refill"},
                 id="parent-after-refill-start"),
    pytest.param({"EAP_CRASH_AT": "after_event_write", "EAP_CRASH_EVENT_KEY": "loop:each.4:completed"},
                 id="after-last-item-before-the-step-completes"),
])
def test_crash_recovery_runs_each_item_once_in_order(worker, tenant, defs, crash_schema, tmp_path, crash_env):
    spec = _crash_spec(worker, tenant)
    run_id, run = _crash_run(worker, tenant, crash_schema, tmp_path, crash_env, spec)
    events = event_rows(worker.engine, run_id)
    keys = [e[1] for e in events]
    assert [e[0] for e in events] == list(range(len(events)))  # gap-free seq
    assert len(set(keys)) == len(keys), "a duplicate event"
    assert set(keys) == _expected_keys() and keys.count("end") == 1 and keys[-1] == "end"
    seq = {e[1]: e[0] for e in events}
    assert [seq[f"loop:each.{i}:completed"] for i in range(N)] == sorted(seq[f"loop:each.{i}:completed"] for i in range(N))
    assert seq["step:each:completed"] < seq["end"]
    data = {e[1]: e[3] for e in events}
    out = json.loads(run["output"])
    assert len(out) == N and len({o["ticket_id"] for o in out}) == N
    assert out == [data[f"loop:each.{i}:step:mk:completed"]["output"] for i in range(N)]  # item order
    assert data["step:each:completed"]["output"] == out
    from worker_tests.test_workflow_run_acceptance import child_runs

    kids = child_runs(worker.engine, run_id)
    assert [(k[1], k[2]) for k in kids] == [(f"each.{i}/ask", "completed") for i in range(N)]  # one per item
    assert worker.tickets.call_count(tenant, f"{run_id}:") == N
    assert worker.tickets.ticket_count(tenant, f"{run_id}:") == N


def test_crash_after_a_side_effecting_item_call_needs_attention_once_even_under_collect(
        worker, tenant, defs, crash_schema, tmp_path):
    spec = _crash_spec(worker, tenant, on_item_error="collect")
    env = {"EAP_CRASH_AT": "after_workflow_tool_call", "EAP_CRASH_EVENT_KEY": "loop:each.2:step:mk:call"}
    run_id, run = _crash_run(worker, tenant, crash_schema, tmp_path, env, spec, status="needs_attention")
    events = event_rows(worker.engine, run_id)
    assert [e[0] for e in events] == list(range(len(events)))
    keys = [e[1] for e in events]
    assert len(set(keys)) == len(keys) and keys.count("end") == 1 and keys[-1] == "end"
    data = {e[1]: e[3] for e in events}
    for key in ("loop:each.2:step:mk:failed", "loop:each.2:failed", "step:each:failed", "end"):
        assert data[key]["error_code"] == "side_effect_outcome_unknown", key
    assert data["end"]["status"] == "needs_attention" and "step:each:completed" not in data
    assert worker.tickets.call_count(tenant, f"{run_id}:") == worker.tickets.ticket_count(tenant, f"{run_id}:") <= N
    with worker.engine.connect() as conn:
        guard = conn.execute(text("SELECT state FROM side_effects WHERE tenant_id = :t AND key = :k"),
                             {"t": tenant, "k": f"{run_id}:each.2:mk"}).scalar()
    assert guard == "reserved"  # never called again


# ---------------------------------------------------------------------------- cancel (LP.8 API)
def test_cancel_mid_window_stops_children_starts_nothing_and_keeps_usage(
        worker, client, worker_settings, tenant, defs, monkeypatch):
    import uuid

    from worker_tests.cancel_acceptance_helpers import assert_cancelled_tree, snapshot, stays, tree_ids, usage_rows
    from worker_tests.conftest import wait_for
    from worker_tests.test_workflow_run_acceptance import auth_header, workflow_api
    from worker_tests.workflow_run_helpers import wait_event

    monkeypatch.setenv("EAP_WORKFLOW_LOOPS_PREVIEW", "1")
    release, parked = threading.Event(), []

    def build() -> FunctionModel:  # i0 answers at once (its usage is billed); the others park
        async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
            prompt = _last_prompt(messages)
            if prompt != "i0":
                parked.append(prompt)
                while not release.is_set():
                    await asyncio.sleep(0.05)
            yield f"Echo: {prompt}"

        def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            raise AssertionError("streamed only")

        return FunctionModel(respond, stream_function=stream, model_name="scripted-echo")

    monkeypatch.setattr("eap_worker.models.scripted_echo_model", build)
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    wf, _ = publish_workflow(worker.engine, tenant, _wf(_foreach([_ask(agent_id)], concurrency=3, max_items=8)))
    run_id = str(uuid.uuid4())
    try:
        with workflow_api(worker_settings.dbos_system_schema, APP_VERSION) as api:
            r = api.post(f"/api/v1/workflows/{wf}/runs", headers=auth_header(tenant),
                         json={"input": {"items": [f"i{n}" for n in range(8)]}, "run_id": run_id})
            assert r.status_code == 202, r.text
            wait_event(worker.engine, run_id, "loop:each.3:started")  # i0 awaited, the window moved on
            wait_for(lambda: len(parked) >= 3, what="items 1-3 parked in the model")
            ids = tree_ids(worker.engine, run_id)
            kids = {c["parent_step_id"]: str(c["id"]) for c in worker.run_store.list_runs(tenant, parent_run_id=run_id)}
            assert sorted(kids) == [f"each.{i}/ask" for i in range(4)]
            billed = usage_rows(worker.engine, kids["each.0/ask"])
            assert billed >= 1
            cancelled = api.post(f"/api/v1/runs/{run_id}/cancel", headers=auth_header(tenant, "alice"))
            assert cancelled.status_code == 202, cancelled.text
            assert cancelled.json()["error_code"] == "run_cancelled"
            parked_ids = [i for i in ids if i in {kids[f"each.{n}/ask"] for n in (1, 2, 3)} | {run_id}]
            assert_cancelled_tree(worker.engine, worker.run_store, tenant, parked_ids)
            assert worker.run_store.get_run(tenant, uuid.UUID(kids["each.0/ask"]))["status"] == "completed"
            with psycopg.connect(DBOS_DB_URL, connect_timeout=5) as conn:
                rows = conn.execute(
                    f'SELECT workflow_uuid, status FROM "{worker_settings.dbos_system_schema}".workflow_status'
                    " WHERE workflow_uuid LIKE %s", (f"{run_id}:loop:%",)).fetchall()
            status = {w.rsplit(".", 1)[1]: s for w, s in rows}
            assert status == {"0": "SUCCESS", "1": "CANCELLED", "2": "CANCELLED", "3": "CANCELLED"}, status
            before = snapshot(worker.engine, [run_id])
            release.set()  # the parked requests finish after the cancel
            assert stays(lambda: snapshot(worker.engine, [run_id]) == before
                         and not any(k.startswith(f"loop:each.{n}:") for n in range(4, 8)
                                     for _, k, _, _ in event_rows(worker.engine, run_id)))
            ends_ = [e for e in event_rows(worker.engine, run_id) if e[1] == "end"]
            assert len(ends_) == 1 and ends_[0][3]["error_code"] == "run_cancelled"
            for n in (1, 2, 3):  # usage of the requests finished after the cancel is kept
                wait_for(lambda n=n: usage_rows(worker.engine, kids[f"each.{n}/ask"]) >= 1, timeout=30,
                         what=f"usage row of item {n}")
            assert usage_rows(worker.engine, kids["each.0/ask"]) == billed
    finally:
        release.set()


# ---------------------------------------------------------------------------- through the API
def test_foreach_runs_end_to_end_through_the_api_with_the_flag_on_and_is_refused_off(
        worker, worker_settings, tenant, defs, monkeypatch):
    import uuid

    from worker_tests.test_workflow_run_acceptance import auth_header, workflow_api

    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    spec = _wf(_foreach([_ask(agent_id)], concurrency=4, max_items=6))
    headers = auth_header(tenant)
    with workflow_api(worker_settings.dbos_system_schema, APP_VERSION) as api:
        created = api.post("/api/v1/workflows", json={"spec": spec}, headers=headers)
        assert created.status_code == 201, created.text
        wid = created.json()["id"]
        monkeypatch.delenv("EAP_WORKFLOW_LOOPS_PREVIEW", raising=False)
        refused = api.post(f"/api/v1/workflows/{wid}/versions", headers=headers)
        assert refused.status_code == 422 and "EAP_WORKFLOW_LOOPS_PREVIEW=1" in refused.json()["detail"]
        monkeypatch.setenv("EAP_WORKFLOW_LOOPS_PREVIEW", "1")
        assert api.post(f"/api/v1/workflows/{wid}/versions", headers=headers).status_code == 201
        items = [f"i{n}" for n in range(6)]
        started = api.post(f"/api/v1/workflows/{wid}/runs", json={"input": {"items": items}}, headers=headers)
        assert started.status_code == 202, started.text
        run_id = uuid.UUID(started.json()["run_id"])
        run = wait_terminal(worker.run_store, tenant, run_id, timeout=90)
        assert run["status"] == "completed", run["error"]
        assert json.loads(run["output"]) == [f"Echo: {i}" for i in items]
        rows = event_rows(worker.engine, run_id)
        assert [r[0] for r in rows] == list(range(len(rows))) and [r[1] for r in rows].count("end") == 1
        listed = api.get(f"/api/v1/runs/{run_id}/events", headers=headers)
        assert listed.status_code == 200, listed.text
        api_events = listed.json()
        assert [e["seq"] for e in api_events] == list(range(len(api_events))) and len(api_events) == len(rows)
        types = [e["type"] for e in api_events]
        assert types.count("loop_iteration_completed") == 6 and types[-1] == "run_completed"
        monkeypatch.delenv("EAP_WORKFLOW_LOOPS_PREVIEW")  # off again: run start is refused, no new run
        again = api.post(f"/api/v1/workflows/{wid}/runs", json={"input": {"items": items}}, headers=headers)
        assert again.status_code == 422, again.text
