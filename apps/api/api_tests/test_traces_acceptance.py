"""P0.7 acceptance: trace tenant isolation, span / event / child caps, payload truncation, totals."""

from __future__ import annotations

import json
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from .run_helpers import requires_pg
from .test_workflow_runs_api import _no_hang, api, fake  # noqa: F401 - fixtures

pytestmark = requires_pg


class Ctx:
    def __init__(self, api):
        from eap_run_store import EventLog, RunStore

        self.client, self.app, self.tenant_of = api
        self.engine = self.app.state.database_engine
        self.store, self.log = RunStore(self.engine), EventLog(self.engine)
        self.tenant, self.h = self.tenant_of()

    def run(self, tenant=None, parent=None, step="s1") -> uuid.UUID:
        run_id = uuid.uuid4()
        if parent is None:
            self.store.create_run(tenant or self.tenant, uuid.uuid4(), 1, "hello", run_id)
        else:
            self.store.create_child_agent_run(tenant or self.tenant, uuid.uuid4(), 1, "hi", run_id, parent, step)
        return run_id

    def model(self, run_id, n, inp, out, cost, tenant=None):
        usage = {"request_key": f"model:{n}", "model_id": "test:echo", "provider_model_name": "echo",
                 "input_tokens": inp, "output_tokens": out, "cache_read_tokens": 0, "cache_write_tokens": 0,
                 "usage_measured": True, "cost_usd": cost, "price_source": "override" if cost is not None else None}
        self.log.append(tenant or self.tenant, run_id, f"model:{n}:final", "model_message",
                        {"model_request": n, "text": f"answer {n}"}, usage)

    def tool(self, run_id, call_id, args, content="ok"):
        t = self.tenant
        self.log.append(t, run_id, f"tool:{call_id}:call", "tool_call",
                        {"tool_call_id": call_id, "tool_name": "clock.now", "args": args})
        self.log.append(t, run_id, f"tool:{call_id}:result", "tool_result",
                        {"tool_call_id": call_id, "status": "ok", "content": content})

    def trace(self, run_id, h=None):
        r = self.client.get(f"/api/v1/runs/{run_id}/trace", headers=h or self.h)
        assert r.status_code == 200, r.text
        return r.json()


@pytest.fixture
def ctx(api):
    return Ctx(api)


def test_other_tenants_runs_and_children_are_404_and_never_nested(ctx):
    root = ctx.run()
    child = ctx.run(parent=root)
    ctx.model(child, 0, 4, 2, Decimal("0.00001"))
    other, h2 = ctx.tenant_of()
    for run_id in (root, child):  # a foreign run and a foreign child run look like an unknown run
        r = ctx.client.get(f"/api/v1/runs/{run_id}/trace", headers=h2)
        assert r.status_code == 404 and r.json()["detail"] == "Run not found"
    assert ctx.client.get(f"/api/v1/runs/{uuid.uuid4()}/trace", headers=h2).status_code == 404
    assert ctx.client.get(f"/api/v1/runs/{root}/trace", headers={**h2, "Authorization": "Bearer junk"}).status_code == 401
    # The owner sees the child nested; the foreign tenant cannot plant a run under it.
    assert any(s["id"] == f"run:{child}" for s in ctx.trace(root)["spans"])
    forged = uuid.uuid4()
    with pytest.raises(IntegrityError):
        with ctx.engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO runs (id, tenant_id, kind, status, agent_id, agent_version, parent_run_id, parent_step_id,"
                " created_at, updated_at) VALUES (:id, :t, 'agent', 'completed', :a, 1, :p, 's1', now(), now())"),
                {"id": str(forged), "t": other, "a": str(uuid.uuid4()), "p": str(root)})
    spans = ctx.trace(root)["spans"]
    assert {s["id"] for s in spans if s["kind"] == "child_run"} == {f"run:{child}"}
    assert ctx.trace(root)["totals"]["runs"] == 2
    # Tenant B's own trace of its own run never includes A's usage.
    mine = ctx.run(tenant=other)
    ctx.model(mine, 0, 1, 1, Decimal("0.5"), tenant=other)
    body = ctx.trace(mine, h2)
    assert body["totals"]["input_tokens"] == 1 and body["totals"]["cost_usd"] == "0.5000000000"
    assert all(str(root) not in s["id"] and str(child) not in s["id"] for s in body["spans"])


def test_span_cap_keeps_the_first_spans_in_tree_order(ctx, monkeypatch):
    from eap_run_store import trace

    monkeypatch.setattr(trace, "MAX_SPANS", 5)
    root = ctx.run()
    for n in range(8):
        ctx.model(root, n, 1, 1, Decimal("0.001"))
    body = ctx.trace(root)
    assert len(body["spans"]) == 5 and body["limits"]["max_spans"] == 5
    assert body["truncated"]["spans"] is True
    assert body["spans"][0]["kind"] == "run" and body["spans"][0]["parent_id"] is None
    assert [s["id"] for s in body["spans"][1:]] == [f"{root}:model:{n}" for n in range(4)]
    assert body["totals"]["model_requests"] == 8  # totals come from every request, not just the kept spans
    ids = {s["id"] for s in body["spans"]}
    assert all(s["parent_id"] in ids for s in body["spans"][1:])
    monkeypatch.setattr(trace, "MAX_SPANS", 9)  # exactly the span count: nothing is cut
    full = ctx.trace(root)
    assert len(full["spans"]) == 9 and full["truncated"]["spans"] is False


def test_event_budget_covers_the_whole_trace_and_child_cap(ctx, monkeypatch):
    from eap_run_store import trace

    root = ctx.run()
    children = [ctx.run(parent=root, step=f"s{i}") for i in range(3)]
    for n in range(3):
        ctx.model(root, n, 1, 1, Decimal("0.001"))
    for c in children:
        ctx.model(c, 0, 1, 1, Decimal("0.001"))
    clean = ctx.trace(root)
    assert clean["truncated"] == {"spans": False, "events": False, "child_runs": False, "payload_fields": 0}
    assert clean["limits"]["max_trace_events"] == 4 * clean["limits"]["max_spans"] == 8000
    monkeypatch.setattr(trace, "MAX_TRACE_EVENTS", 3)  # the root's own 3 events use the whole budget
    cut = ctx.trace(root)
    assert cut["limits"]["max_trace_events"] == 3 and cut["truncated"]["child_runs"] is True
    assert cut["truncated"]["events"] is False and cut["totals"]["runs"] == 1
    assert not any(s["kind"] == "child_run" for s in cut["spans"])
    monkeypatch.setattr(trace, "MAX_TRACE_EVENTS", 2)  # even the root's events do not fit
    short = ctx.trace(root)
    assert short["truncated"]["events"] is True and short["truncated"]["child_runs"] is True
    assert sum(1 for s in short["spans"] if s["kind"] == "model_request") == 2
    monkeypatch.setattr(trace, "MAX_TRACE_EVENTS", 5)  # root (3) + first child (1) + second child (1)
    mid = ctx.trace(root)
    assert mid["truncated"]["child_runs"] is True and mid["totals"]["runs"] == 3
    assert [s["id"] for s in mid["spans"] if s["kind"] == "child_run"] == [f"run:{c}" for c in children[:2]]
    monkeypatch.setattr(trace, "MAX_TRACE_EVENTS", 6)  # all events of all four runs fit exactly
    assert ctx.trace(root)["truncated"] == clean["truncated"]
    monkeypatch.setattr(trace, "MAX_CHILD_RUNS", 2)
    capped = ctx.trace(root)
    assert capped["truncated"]["child_runs"] is True and capped["totals"]["runs"] == 3
    assert capped["limits"]["max_child_runs"] == 2


def test_payload_truncation_flags(ctx, monkeypatch):
    from eap_run_store import trace

    root = ctx.run()
    big = {"blob": "x" * 20_000}
    ctx.tool(root, "big", big, content="y" * 30_000)
    ctx.tool(root, "small", {"a": 1})
    body = ctx.trace(root)
    spans = {s["id"]: s for s in body["spans"]}
    big_span, small_span = spans[f"{root}:tool:big"], spans[f"{root}:tool:small"]
    size_in, size_out = len(json.dumps(big).encode()), 30_000
    assert big_span["truncated"] == {"input": size_in, "output": size_out}
    assert isinstance(big_span["input"], str) and len(big_span["input"].encode()) <= trace.FIELD_MAX_BYTES
    assert big_span["input"].startswith('{"blob": "xxx') and big_span["output"] == "y" * trace.FIELD_MAX_BYTES
    assert small_span["truncated"] == {} and small_span["input"] == {"a": 1} and small_span["output"] == "ok"
    assert body["truncated"]["payload_fields"] == 2 and body["limits"]["field_max_bytes"] == 8192
    # Once the trace-wide payload budget is spent, later fields shrink to a preview (never below 256 bytes).
    monkeypatch.setattr(trace, "PAYLOAD_BUDGET_BYTES", 1000)
    ctx.tool(root, "mid", {"v": "z" * 3000})
    tight = {s["id"]: s for s in ctx.trace(root)["spans"]}[f"{root}:tool:mid"]
    assert tight["truncated"]["input"] == len(json.dumps({"v": "z" * 3000}).encode())
    assert len(tight["input"].encode()) <= 1000


def _sums(body):
    models = [s for s in body["spans"] if s["kind"] == "model_request"]
    return models, {k: sum(m["attributes"]["usage"][k] for m in models) for k in ("input_tokens", "output_tokens")}


def test_totals_equal_the_sum_of_the_model_spans(ctx):
    root = ctx.run()
    c1, c2 = ctx.run(parent=root, step="a"), ctx.run(parent=root, step="b")
    ctx.model(root, 0, 10, 5, Decimal("0.001"))
    ctx.model(root, 1, 20, 7, Decimal("0.0025"))
    ctx.model(c1, 0, 100, 50, Decimal("0.01"))
    ctx.model(c2, 0, 1000, 500, Decimal("0.1"))
    ctx.tool(root, "t1", {})
    body = ctx.trace(root)
    models, sums = _sums(body)
    t = body["totals"]
    assert len(models) == t["model_requests"] == 4 and t["runs"] == 3 and t["tool_calls"] == 1
    assert (t["input_tokens"], t["output_tokens"]) == (sums["input_tokens"], sums["output_tokens"]) == (1130, 562)
    assert Decimal(t["cost_usd"]) == sum(Decimal(m["attributes"]["cost_usd"]) for m in models) == Decimal("0.1135")
    assert (t["unpriced_requests"], t["unrecorded_requests"]) == (0, 0)
    assert {m["id"] for m in models} == {f"{root}:model:0", f"{root}:model:1", f"{c1}:model:0", f"{c2}:model:0"}
    # One unpriced request: the total cost is null (never a partial sum); tokens still add up.
    ctx.model(c2, 1, 3, 2, None)
    body = ctx.trace(root)
    models, sums = _sums(body)
    t = body["totals"]
    assert t["cost_usd"] is None and t["unpriced_requests"] == 1 and t["model_requests"] == 5
    assert (t["input_tokens"], t["output_tokens"]) == (sums["input_tokens"], sums["output_tokens"]) == (1133, 564)
    assert [m["attributes"]["cost_usd"] for m in models].count(None) == 1
    # A run whose model events predate usage has no usage: null, not zero.
    old = ctx.run()
    ctx.log.append(ctx.tenant, old, "model:0:final", "model_message", {"model_request": 0, "text": "old"})
    legacy = ctx.trace(old)
    assert legacy["totals"]["input_tokens"] is None and legacy["totals"]["cost_usd"] is None
    assert legacy["totals"]["unrecorded_requests"] == 1
    assert [m["attributes"]["usage"] for m in legacy["spans"] if m["kind"] == "model_request"] == [None]
