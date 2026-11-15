"""AC.17 against Postgres: the `judge` operation, the `eval` kind filter (eval case runs no longer
count as `workflow`), the fallback block of the summary, and TraceReader's `waiting_approval`.

Seed (one tenant, plus another tenant's copy of the fallback data that must never show up):

  a1  agent, top-level   model:0 (test:b, 10/5, $0.001, fell back from test:a)
                         model:0:failed:0 (test:a, 4/0, $0.0004)
                         model:1 (test:a, 20/2, $0.002, chain, no fallback)
  w1  workflow           w1c agent child: model:0 (test:a, 7/1, $0.0007)
  e1  eval               judge:k1:0 (test:judge, 30/6, $0.003)
                         e1c agent child (case:k1): model:0 (test:a, 9/3, $0.0009)
  x1  agent, failed      run_failed fallback_exhausted
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from eap_run_store.metrics import FALLBACK_EXHAUSTED_RUN_ERROR, OPERATIONS, MetricsQuery, MetricsStore
from eap_run_store.trace import TraceReader

BASE = datetime(2026, 5, 1, tzinfo=timezone.utc)
AT = BASE + timedelta(hours=1)


def _run(conn, tenant, kind, *, parent=None, step=None, status="completed", model=None, wid=None):
    run_id = str(uuid.uuid4())
    agent = None if kind == "workflow" else str(uuid.uuid4())
    conn.execute(
        text(
            "INSERT INTO runs (id, tenant_id, kind, status, created_at, updated_at, finished_at, agent_id,"
            " agent_version, workflow_id, workflow_version, eval_suite_id, parent_run_id, parent_step_id,"
            " model_id, model_overridden, input)"
            " VALUES (:id, :t, :k, :s, :c, :c, :f, :a, :av, :w, :wv, :es, :p, :ps, :m, false, 'hi')"
        ),
        {"id": run_id, "t": tenant, "k": kind, "s": status, "c": AT, "f": AT + timedelta(seconds=2),
         "a": agent, "av": None if agent is None else 1, "w": wid if kind == "workflow" else None,
         "wv": 1 if kind == "workflow" else None, "es": str(uuid.uuid4()) if kind == "eval" else None,
         "p": parent, "ps": step, "m": model},
    )
    return run_id


def _req(conn, tenant, run_id, key, model, inp, out, cost):
    conn.execute(
        text(
            "INSERT INTO run_model_requests (tenant_id, run_id, request_key, model_id, input_tokens, output_tokens,"
            " cache_read_tokens, cache_write_tokens, usage_measured, cost_usd, price_source, created_at, operation)"
            " VALUES (:t, :r, :k, :m, :i, :o, 0, 0, true, :c, 'override', :at, 'chat')"
        ),
        {"t": tenant, "r": run_id, "k": key, "m": model, "i": inp, "o": out, "c": Decimal(cost), "at": AT},
    )


def _event(conn, tenant, run_id, seq, key, etype, data, ts=AT):
    conn.execute(
        text(
            "INSERT INTO execution_events (tenant_id, run_id, seq, event_key, type, data, ts)"
            " VALUES (:t, :r, :s, :k, :ty, CAST(:d AS jsonb), :ts)"
        ),
        {"t": tenant, "r": run_id, "s": seq, "k": key, "ty": etype, "d": json.dumps(data), "ts": ts},
    )


def _fallback_runs(conn, tenant):
    a1 = _run(conn, tenant, "agent", model="test:a")
    _req(conn, tenant, a1, "model:0", "test:b", 10, 5, "0.001")
    _req(conn, tenant, a1, "model:0:failed:0", "test:a", 4, 0, "0.0004")
    _req(conn, tenant, a1, "model:1", "test:a", 20, 2, "0.002")
    _event(conn, tenant, a1, 1, "model:0:final", "model_message", {
        "model_request": 0, "answering_model": "test:b", "requested_model_id": "test:a", "fallback_index": 1,
        "failed_attempts": [{"model_id": "test:a", "outcome": "error", "error_type": "ModelHTTPError"}]})
    _event(conn, tenant, a1, 2, "model:1:final", "model_message", {
        "model_request": 1, "answering_model": "test:a", "requested_model_id": "test:a", "fallback_index": 0,
        "failed_attempts": []})
    x1 = _run(conn, tenant, "agent", status="failed", model="test:a")
    conn.execute(text("UPDATE runs SET error = :e WHERE id = :r"), {"e": FALLBACK_EXHAUSTED_RUN_ERROR, "r": x1})
    _event(conn, tenant, x1, 1, "end", "run_failed", {"error_code": "fallback_exhausted", "cause": "model_http_error"})
    return a1, x1


@pytest.fixture
def seeded(engine):
    tenant = f"test_tenant_{uuid.uuid4().hex[:8]}"
    other = f"test_tenant_{uuid.uuid4().hex[:8]}"
    with engine.begin() as conn:
        a1, x1 = _fallback_runs(conn, tenant)
        w1 = _run(conn, tenant, "workflow", wid=str(uuid.uuid4()))
        w1c = _run(conn, tenant, "agent", parent=w1, step="s1", model="test:a")
        _req(conn, tenant, w1c, "model:0", "test:a", 7, 1, "0.0007")
        e1 = _run(conn, tenant, "eval", model="test:a")
        _req(conn, tenant, e1, "judge:k1:0", "test:judge", 30, 6, "0.003")
        e1c = _run(conn, tenant, "agent", parent=e1, step="case:k1", model="test:a")
        _req(conn, tenant, e1c, "model:0", "test:a", 9, 3, "0.0009")
        _fallback_runs(conn, other)
        # The other tenant has more fallback activity than this one: a leak would change every count.
        o2 = _run(conn, other, "agent", model="test:a")
        for n in range(3):
            _req(conn, other, o2, f"model:{n}", "test:b", 1, 1, "0.0001")
            _req(conn, other, o2, f"model:{n}:failed:0", "test:a", 1, 0, "0.0001")
    yield tenant, {"a1": a1, "x1": x1, "w1": w1, "w1c": w1c, "e1": e1, "e1c": e1c, "other": other}
    with engine.begin() as conn:
        for t in (tenant, other):
            conn.execute(text("DELETE FROM pending_approvals WHERE tenant_id = :t"), {"t": t})
            conn.execute(text("DELETE FROM execution_events WHERE tenant_id = :t"), {"t": t})
            conn.execute(text("DELETE FROM run_model_requests WHERE tenant_id = :t"), {"t": t})
            conn.execute(text("DELETE FROM runs WHERE tenant_id = :t AND parent_run_id IS NOT NULL"), {"t": t})
            conn.execute(text("DELETE FROM runs WHERE tenant_id = :t"), {"t": t})


def _q(tenant, **kw):
    return MetricsQuery(tenant_id=tenant, start=BASE, end=BASE + timedelta(days=1), **kw)


def test_judge_is_its_own_operation_and_operations_sum_to_totals(engine, seeded):
    tenant, _ = seeded
    store = MetricsStore(engine)
    cur = store.summary(_q(tenant))["current"]
    assert "judge" in OPERATIONS
    ops = cur["operations"]
    assert ops["judge"]["requests"] == 1 and ops["judge"]["input_tokens"] == 30
    assert ops["chat"]["requests"] == 5  # a1 x3 (a failed attempt stays chat), w1c, e1c; no judge row
    assert sum(ops[o]["requests"] for o in OPERATIONS) == cur["requests"] == 6
    assert sum(ops[o]["cost_usd"] for o in OPERATIONS) == cur["cost_usd"]
    judge = store.summary(_q(tenant, operation="judge"))["current"]
    assert judge["requests"] == 1 and judge["runs"] == 1  # the eval run
    chat = store.summary(_q(tenant, operation="chat"))["current"]
    assert chat["requests"] == 5
    items = {i["key"]: i for i in store.breakdown(_q(tenant), "operation", sort="requests")}
    assert items["judge"]["requests"] == 1 and items["chat"]["requests"] == 5


def test_eval_kind_counts_eval_case_runs_and_workflow_no_longer_does(engine, seeded):
    tenant, _ = seeded
    store = MetricsStore(engine)
    ev = store.summary(_q(tenant, kind="eval"))["current"]
    assert ev["runs"] == 1
    assert ev["requests"] == 2  # the judge row and the case run's request
    assert ev["input_tokens"] == 39
    wf = store.summary(_q(tenant, kind="workflow"))["current"]
    assert wf["runs"] == 1 and wf["requests"] == 1 and wf["input_tokens"] == 7
    agent = store.summary(_q(tenant, kind="agent"))["current"]
    assert agent["requests"] == 3  # a1 only: children of workflow and eval runs are not `agent`


def test_usage_by_answering_model(engine, seeded):
    tenant, _ = seeded
    items = {i["key"]: i for i in MetricsStore(engine).breakdown(_q(tenant), "model", sort="requests")}
    assert items["test:b"]["requests"] == 1  # the answering model of the fallen-back request
    assert items["test:a"]["requests"] == 4  # incl. the failed attempt, attributed to the model that failed
    assert items["test:judge"]["requests"] == 1


def _fb(chat=0, fell=0, billed=0, exhausted=0, rate=None):
    return {"chat_requests": chat, "fell_back": fell, "billed_failed_attempts": billed, "exhausted_runs": exhausted,
            "fallback_rate": rate}


def test_fallback_block(engine, seeded):
    """Review 2026-11-15 definition: requests not answered by the primary, out of every agent chat
    request (a1 x2, the workflow step's and the eval case's), with exhausted runs counted."""
    tenant, ids = seeded
    store = MetricsStore(engine)
    assert store.summary(_q(tenant))["current"]["fallback"] == _fb(4, 1, 1, 1, 0.4)
    assert store.summary(_q(tenant, operation="embedding"))["current"]["fallback"] == _fb()
    assert store.summary(_q(tenant, operation="chat"))["current"]["fallback"] == _fb(4, 1, 1, 1, 0.4)
    assert store.summary(_q(tenant, model="test:zzz"))["current"]["fallback"] == _fb()
    assert store.summary(_q(tenant, model="test:a"))["current"]["fallback"] == _fb(4, 1, 1, 1, 0.4)
    assert store.summary(_q(tenant, kind="workflow"))["current"]["fallback"] == _fb(1, 0, 0, 0, 0.0)
    assert store.summary(_q(tenant, kind="eval"))["current"]["fallback"] == _fb(1, 0, 0, 0, 0.0)
    assert store.summary(_q(tenant, kind="agent"))["current"]["fallback"] == _fb(2, 1, 1, 1, 2 / 3)
    assert store.summary(_q(tenant, agent_id=str(uuid.uuid4())))["current"]["fallback"] == _fb()
    prev = store.summary(_q(tenant), compare_previous=True)["previous"]["fallback"]
    assert prev == _fb()


def test_only_exhausted_runs_give_a_full_rate(engine, seeded):
    tenant, ids = seeded
    store = MetricsStore(engine)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM run_model_requests WHERE tenant_id = :t"), {"t": tenant})
    assert store.summary(_q(tenant))["current"]["fallback"] == _fb(0, 0, 0, 1, 1.0)


def test_fallback_is_tenant_scoped(engine, seeded):
    """Both tenants have fallback data, and different amounts: each sees exactly its own."""
    tenant, ids = seeded
    store = MetricsStore(engine)
    assert store.summary(_q(tenant))["current"]["fallback"] == _fb(4, 1, 1, 1, 0.4)
    # other: a1 copy (2 chat, 1 fell back, 1 billed) + o2 (3 chat, all fell back, 3 billed) + x1 copy.
    assert store.summary(_q(ids["other"]))["current"]["fallback"] == _fb(5, 4, 4, 1, 5 / 6)


def test_trace_reader_shows_waiting_approval(engine, seeded):
    tenant, ids = seeded
    run_id = ids["a1"]
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        conn.execute(text("UPDATE runs SET status = 'running', finished_at = NULL, heartbeat_at = :h WHERE id = :r"),
                     {"h": now, "r": run_id})
        _event(conn, tenant, run_id, 3, "tool:c1:call", "tool_call", {"tool_call_id": "c1", "tool_name": "send"}, now)
        _event(conn, tenant, run_id, 4, "tool:c1:approval:waiting", "tool_approval_waiting",
               {"tool_call_id": "c1", "tool_name": "send", "deadline": (now + timedelta(hours=1)).isoformat()}, now)
        conn.execute(
            text(
                "INSERT INTO pending_approvals (tenant_id, run_id, kind, approval_key, root_run_id, tool_name,"
                " requested_at, deadline) VALUES (:t, :r, 'tool', 'c1', :r, 'send', :n, :d)"
            ),
            {"t": tenant, "r": run_id, "n": now, "d": now + timedelta(hours=1)},
        )
    trace = TraceReader(engine).read(tenant, run_id)
    assert trace["run"]["display_status"] == "waiting_approval"
    assert trace["spans"][0]["status"] == "waiting"
    [wait] = [s for s in trace["spans"] if s["kind"] == "tool_approval"]
    assert wait["status"] == "waiting" and wait["parent_id"] == f"{run_id}:tool:c1"
    # Another tenant sees nothing.
    assert TraceReader(engine).read(f"test_tenant_{uuid.uuid4().hex[:8]}", run_id) is None


def test_eval_trace_nests_case_run_and_judge(engine, seeded):
    tenant, ids = seeded
    with engine.begin() as conn:
        _event(conn, tenant, ids["e1"], 1, "case:k1", "eval_case_result",
               {"case_id": "k1", "position": 0, "status": "passed", "child_run_id": ids["e1c"]})
    trace = TraceReader(engine).read(tenant, ids["e1"])
    case = next(s for s in trace["spans"] if s["kind"] == "eval_case")
    judge = next(s for s in trace["spans"] if s["kind"] == "judge")
    child = next(s for s in trace["spans"] if s["kind"] == "child_run")
    assert judge["parent_id"] == case["id"] and child["parent_id"] == case["id"]
    assert trace["totals"]["judge_requests"] == 1 and trace["totals"]["chat_requests"] == 1


def test_mark_running_records_a_missing_model_only(engine, seeded):
    """AC.17 review: a child run created without run options gets the spec's model at start, so its
    fallback answers count; a recorded model (API run, override) is never replaced."""
    from eap_run_store import RunStore

    tenant, ids = seeded
    store = RunStore(engine)
    with engine.begin() as conn:
        conn.execute(text("UPDATE runs SET model_id = NULL, status = 'queued' WHERE id = :r"), {"r": ids["e1c"]})
        conn.execute(text("UPDATE runs SET status = 'queued' WHERE id = :r"), {"r": ids["w1c"]})
    assert store.mark_running(tenant, uuid.UUID(ids["e1c"]), model_id="test:spec")
    assert store.mark_running(tenant, uuid.UUID(ids["w1c"]), model_id="test:spec")
    with engine.connect() as conn:
        got = dict(conn.execute(
            text("SELECT id, model_id FROM runs WHERE id IN (:a, :b)"), {"a": ids["e1c"], "b": ids["w1c"]}
        ).all())
    assert got == {ids["e1c"]: "test:spec", ids["w1c"]: "test:a"}
