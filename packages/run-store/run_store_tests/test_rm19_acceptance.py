"""RM.19 acceptance: the operation partition (property), filter combinations, cross-tenant, and trace building.

Added beside the implementer's test_metrics_operations.py and test_trace_rag_memory.py (kept as they are).
"""

from __future__ import annotations

import json
import random
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from eap_run_store.metrics import OPERATIONS, MetricsQuery, MetricsRangeError, MetricsStore

BASE = datetime(2026, 6, 1, tzinfo=timezone.utc)
METRICS = ("requests", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "cost_usd")

# (key template, operation column): every request-key prefix the worker writes, plus odd combinations
# (an embedding row under a chat prefix and the reverse) the partition must still classify exactly once.
KEYS = [
    ("model:{n}", "chat"), ("step:s1:model:{n}", "chat"), ("moderation:input:{n}", "chat"),
    ("moderation:output:{n}", "chat"), ("memory:observe:{n}", "chat"), ("memory:reflect:{n}", "chat"),
    ("embed:retrieve:c{n}", "embedding"), ("embed:retrieve:c{n}:1", "embedding"), ("embed:recall:{n}", "embedding"),
    ("embed:index:{n}", "embedding"), ("embed:batch:{n}", "embedding"),
    ("embed:recall:{n}", "chat"), ("moderation:x{n}", "embedding"), ("memory:observe:{n}", "embedding"),
    ("other:{n}", "chat"),
]


def expect_op(key: str, op: str) -> str:
    if op == "embedding":
        return "embedding"
    for prefix, name in (("moderation:", "moderation"), ("memory:observe:", "observer"), ("memory:reflect:", "reflector")):
        if key.startswith(prefix):
            return name
    return "chat"


def expect_memory(key: str, op: str) -> bool:
    if op == "chat":
        return key.startswith(("memory:observe:", "memory:reflect:"))
    return key.startswith(("embed:recall:", "embed:index:"))


def insert_run(conn, tenant, status="completed", created=None, kind="tool", agent=None, model=None, kb=None):
    run_id = str(uuid.uuid4())
    created = created or BASE + timedelta(hours=1)
    cols = {"id": run_id, "t": tenant, "k": kind, "s": status, "c": created, "f": created + timedelta(seconds=1)}
    extra = {"tool": ("tool_id", "'echo'"), "agent": ("agent_id, agent_version", ":ag, 1")}.get(kind)
    if kind == "kb_ingest":
        extra = ("knowledge_base_id", ":kb")
        cols["kb"] = kb
    if kind == "agent":
        cols["ag"] = agent
    conn.execute(text(
        f"INSERT INTO runs (id, tenant_id, kind, status, created_at, updated_at, finished_at, model_overridden, model_id,"
        f" {extra[0]}) VALUES (:id, :t, :k, :s, :c, :c, :f, false, :m, {extra[1]})"), {**cols, "m": model})
    return run_id


def insert_req(conn, tenant, run_id, key, op, inp=1, out=0, cost=None, model="test:m", created=None, cache=(0, 0)):
    conn.execute(text(
        "INSERT INTO run_model_requests (tenant_id, run_id, request_key, model_id, input_tokens, output_tokens,"
        " cache_read_tokens, cache_write_tokens, usage_measured, cost_usd, price_source, created_at, operation)"
        " VALUES (:t, :r, :k, :m, :i, :o, :cr, :cw, true, :c, :src, :at, :op)"),
        {"t": tenant, "r": run_id, "k": key, "m": model, "i": inp, "o": out, "cr": cache[0], "cw": cache[1], "c": cost,
         "src": None if cost is None else "override", "at": created or BASE + timedelta(hours=1), "op": op})


def cleanup(engine, *tenants):
    with engine.begin() as conn:
        for t in tenants:
            for table in ("run_model_requests", "runs"):
                conn.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": t})
            conn.execute(text("DELETE FROM knowledge_bases WHERE tenant_id = :t"), {"t": t})


def q_of(tenant, **kw):
    return MetricsQuery(tenant_id=tenant, start=kw.pop("start", BASE), end=kw.pop("end", BASE + timedelta(days=1)), **kw)


def _random_world(engine, seed):
    """Random usage rows over every key prefix in two tenants; returns (tenant, rows) of the first."""
    rng = random.Random(seed)
    tenant, other = f"test_tenant_{uuid.uuid4().hex[:8]}", f"test_tenant_{uuid.uuid4().hex[:8]}"
    rows = []
    with engine.begin() as conn:
        runs = [insert_run(conn, tenant) for _ in range(rng.randint(1, 4))]
        foreign = insert_run(conn, other)
        for n in range(rng.randint(5, 40)):
            tmpl, op = rng.choice(KEYS)
            key = tmpl.format(n=n)
            cost = None if rng.random() < 0.2 else Decimal(rng.randint(0, 99999)) / Decimal(10**6)
            row = dict(key=key, op=op, inp=rng.randint(0, 5000), out=rng.randint(0, 900), cost=cost,
                       cache=(rng.randint(0, 50), rng.randint(0, 50)), model=rng.choice(["test:a", "test:b"]))
            insert_req(conn, tenant, rng.choice(runs), row["key"], row["op"], row["inp"], row["out"], row["cost"],
                       row["model"], cache=row["cache"])
            rows.append(row)
        for i, (tmpl, op) in enumerate(KEYS):  # every prefix again in the other tenant: never counted
            insert_req(conn, other, foreign, tmpl.format(n=100 + i), op, 7777, 7777, Decimal("77"))
    return tenant, other, rows


def _expected(rows, pick=lambda r: True):
    sel = [r for r in rows if pick(r)]
    return {
        "requests": len(sel), "input_tokens": sum(r["inp"] for r in sel), "output_tokens": sum(r["out"] for r in sel),
        "cache_read_tokens": sum(r["cache"][0] for r in sel), "cache_write_tokens": sum(r["cache"][1] for r in sel),
        "cost_usd": sum((r["cost"] or Decimal(0) for r in sel), Decimal(0)),
    }


@pytest.mark.parametrize("seed", range(8))
def test_operations_partition_the_usage_rows_exactly(engine, seed):
    tenant, other, rows = _random_world(engine, seed)
    try:
        store = MetricsStore(engine)
        cur = store.summary(q_of(tenant))["current"]
        ops = cur["operations"]
        assert set(ops) == {*OPERATIONS, "memory"}
        total = _expected(rows)
        for m in METRICS:  # the totals are exactly the rows (decimal costs included), nothing of the other tenant
            assert cur[m] == total[m], m
            assert sum((ops[o][m] for o in OPERATIONS), type(total[m])(0)) == total[m], m
        for o in OPERATIONS:  # disjoint and each equal to its oracle
            want = _expected(rows, lambda r, o=o: expect_op(r["key"], r["op"]) == o)
            assert {m: ops[o][m] for m in METRICS} == want, o
        mem = _expected(rows, lambda r: expect_memory(r["key"], r["op"]))
        assert {m: ops["memory"][m] for m in METRICS} == mem
        recall_index = _expected(rows, lambda r: r["op"] == "embedding" and r["key"].startswith(("embed:recall:", "embed:index:")))
        for m in METRICS:
            assert ops["memory"][m] == ops["observer"][m] + ops["reflector"][m] + recall_index[m]
        for o in (*OPERATIONS, "memory"):  # the filter and the split agree
            filtered = store.summary(q_of(tenant, operation=o))["current"]
            assert {m: filtered[m] for m in METRICS} == {m: ops[o][m] for m in METRICS}, o
    finally:
        cleanup(engine, tenant, other)


@pytest.fixture
def world(engine):
    """Agent A (model a:m), agent B (model b:m), a tool run, a kb_ingest run, last period's runs, a foreign tenant."""
    t, other = f"test_tenant_{uuid.uuid4().hex[:8]}", f"test_tenant_{uuid.uuid4().hex[:8]}"
    h1, h5, prev = BASE + timedelta(hours=1), BASE + timedelta(hours=5), BASE - timedelta(hours=3)
    kb = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO knowledge_bases (id, tenant_id, name, embedding_model, dimensions, chunking)"
            " VALUES (:id, :t, 'Docs', 'test:hash', 1024, '{}'::jsonb)"), {"id": kb, "t": t})
        a = insert_run(conn, t, kind="agent", agent="agent-a", model="a:m", created=h1)
        b = insert_run(conn, t, kind="agent", agent="agent-b", model="b:m", created=h5)
        tool = insert_run(conn, t, created=h1)
        ing = insert_run(conn, t, kind="kb_ingest", kb=kb, created=h1)
        old = insert_run(conn, t, kind="agent", agent="agent-a", model="a:m", created=prev)
        f = insert_run(conn, other, kind="agent", agent="agent-a", model="a:m", created=h1)
        insert_req(conn, t, a, "model:0", "chat", 10, 2, Decimal("0.01"), "a:m", h1)
        insert_req(conn, t, a, "memory:observe:1", "chat", 20, 4, Decimal("0.02"), "a:m", h1)
        insert_req(conn, t, a, "embed:recall:1", "embedding", 3, 0, Decimal("0.0003"), "a:emb", h1)
        insert_req(conn, t, b, "model:0", "chat", 11, 1, Decimal("0.011"), "b:m", h5)
        insert_req(conn, t, b, "embed:retrieve:c", "embedding", 5, 0, Decimal("0.0005"), "b:emb", h5)
        insert_req(conn, t, tool, "embed:retrieve:c", "embedding", 6, 0, Decimal("0.0006"), "a:emb", h1)
        insert_req(conn, t, ing, "embed:batch:0", "embedding", 100, 0, Decimal("0.1"), "a:emb", h1)
        insert_req(conn, t, old, "memory:reflect:1", "chat", 40, 8, Decimal("0.04"), "a:m", prev)
        insert_req(conn, t, old, "embed:batch:0", "embedding", 50, 0, Decimal("0.05"), "a:emb", prev)
        insert_req(conn, other, f, "embed:batch:0", "embedding", 9999, 0, Decimal("9"), "a:emb", h1)
        insert_req(conn, other, f, "memory:observe:1", "chat", 9999, 0, Decimal("9"), "a:m", h1)
    yield t
    cleanup(engine, t, other)


def _sum(store, tenant, **kw):
    s = store.summary(q_of(tenant, **kw))["current"]
    return s["requests"], s["input_tokens"], s["cost_usd"]


@pytest.mark.parametrize(("kw", "want"), [
    (dict(operation="embedding", kind="kb_ingest"), (1, 100, Decimal("0.1"))),
    (dict(operation="embedding", kind="tool"), (1, 6, Decimal("0.0006"))),
    (dict(operation="embedding", kind="agent"), (2, 8, Decimal("0.0008"))),
    (dict(operation="embedding", kind="agent", agent_id="agent-b"), (1, 5, Decimal("0.0005"))),
    (dict(operation="memory", agent_id="agent-a"), (2, 23, Decimal("0.0203"))),
    (dict(operation="observer", model="a:m"), (1, 20, Decimal("0.02"))),
    (dict(operation="observer", model="b:m"), (0, 0, Decimal("0"))),
    (dict(operation="embedding", provider="b"), (1, 5, Decimal("0.0005"))),
    (dict(operation="chat", agent_id="agent-b", model="b:m"), (1, 11, Decimal("0.011"))),
    (dict(operation="chat", kind="kb_ingest"), (0, 0, Decimal("0"))),
    (dict(kind="kb_ingest"), (1, 100, Decimal("0.1"))),
])
def test_operation_combined_with_kind_agent_and_model(engine, world, kw, want):
    assert _sum(MetricsStore(engine), world, **kw) == want


def test_previous_period_timeseries_and_breakdown_under_operation(engine, world):
    store = MetricsStore(engine)
    q = q_of(world, operation="embedding")
    res = store.summary(q, compare_previous=True)
    assert (res["current"]["requests"], res["current"]["input_tokens"]) == (4, 114)
    assert (res["previous"]["requests"], res["previous"]["input_tokens"]) == (1, 50)
    assert res["previous"]["operations"]["embedding"]["cost_usd"] == Decimal("0.05")
    assert res["previous"]["operations"]["reflector"]["requests"] == 0  # filtered out of the previous period too
    buckets = store.timeseries(q_of(world, operation="embedding", kind="agent"), "hour")
    by_hour = {b["start"]: b["requests"] for b in buckets if b["requests"]}
    assert by_hour == {BASE + timedelta(hours=1): 1, BASE + timedelta(hours=5): 1}
    assert sum(b["input_tokens"] for b in buckets) == 8
    series = store.timeseries(q_of(world, operation="memory"), "day")
    assert sum(b["requests"] for b in series) == 2


def test_breakdown_by_operation_under_each_filter(engine, world):
    store = MetricsStore(engine)
    def groups(**kw):
        return {i["key"]: i["requests"] for i in store.breakdown(q_of(world, **kw), "operation", limit=10)}
    assert groups() == {"chat": 2, "observer": 1, "embedding": 4}
    assert groups(kind="agent") == {"chat": 2, "observer": 1, "embedding": 2}
    assert groups(agent_id="agent-a") == {"chat": 1, "observer": 1, "embedding": 1}
    assert groups(model="a:emb") == {"embedding": 3}
    assert groups(operation="memory") == {"observer": 1, "embedding": 1}
    assert groups(operation="embedding", kind="kb_ingest") == {"embedding": 1}
    by_model = {i["key"] for i in store.breakdown(q_of(world, operation="embedding"), "model", limit=10)}
    assert by_model == {"a:emb", "b:emb"}
    by_agent = {i["key"] for i in store.breakdown(q_of(world, operation="observer"), "agent", limit=10)}
    assert by_agent == {"agent-a"}


def test_bad_values_are_rejected(engine, world):
    with pytest.raises(MetricsRangeError):
        q_of(world, operation="Embedding")
    with pytest.raises(MetricsRangeError):
        q_of(world, operation="")
    with pytest.raises(MetricsRangeError):
        MetricsStore(engine).breakdown(q_of(world), "tool", limit=10)
    with pytest.raises(MetricsRangeError):
        MetricsStore(engine).breakdown(q_of(world), "operation", limit=0)


def test_cross_tenant_never_in_any_operation_total_or_breakdown(engine, world):
    store = MetricsStore(engine)
    for op in (None, *OPERATIONS, "memory"):
        s = store.summary(q_of(world, operation=op))["current"]
        assert s["input_tokens"] < 9999 and s["cost_usd"] < 9
        assert all(v["input_tokens"] < 9999 for v in s["operations"].values())
    for by in ("operation", "model", "agent"):
        assert all(i["input_tokens"] < 9999 for i in store.breakdown(q_of(world), by, limit=10))
    assert all(b["input_tokens"] < 9999 for b in store.timeseries(q_of(world, operation="embedding"), "hour"))


# ------------------------------------------------------------------------------ trace building

from eap_run_store import trace as trace_mod  # noqa: E402
from eap_run_store.trace import RunData, TraceInput, build_trace  # noqa: E402

from .test_trace_rag_memory import (  # noqa: E402
    AGENT_EVENTS, CHUNK, KB_EVENTS, REQUESTS, SOURCES, T0, _events, _row, _run,
)

NEW_KINDS = {"retrieve", "embedding", "memory_recall", "memory_index", "observer", "reflector", "working_memory", "ingest"}


def _build(events=AGENT_EVENTS, requests=REQUESTS, **run_kw):
    return build_trace(TraceInput(root=RunData(run=_run(**run_kw), events=_events(events), requests=dict(requests))))


def _spans(trace):
    return {s["id"]: s for s in trace["spans"]}


def test_every_new_span_kind_with_its_parent_and_status():
    spans = _spans(_build())
    got = {i.removeprefix("r1:"): (s["kind"], s["parent_id"], s["status"]) for i, s in spans.items() if i != "run:r1"}
    for key, want in {
        "tool:c1": ("retrieve", "run:r1", "ok"),
        "embed:retrieve:c1": ("embedding", "r1:tool:c1", "ok"),
        "embed:recall:2": ("memory_recall", "run:r1", "ok"),
        "embed:index:2": ("memory_index", "run:r1", "ok"),
        "memory:wm:c2": ("working_memory", "r1:tool:c2", "ok"),
        "tool:c2": ("tool_call", "run:r1", "ok"),
        "memory:observe:2": ("observer", "run:r1", "ok"),
        "memory:reflect:2:failed": ("reflector", "run:r1", "error"),
    }.items():
        assert got[key] == want, key
    ingest = _build(KB_EVENTS, {}, kind="kb_ingest", agent_id=None, agent_version=None, knowledge_base_id="kb1")
    assert {s["kind"] for s in ingest["spans"]} >= {"ingest", "embedding"}
    assert NEW_KINDS <= {s["kind"] for s in _build()["spans"]} | {s["kind"] for s in ingest["spans"]}
    assert NEW_KINDS <= set(trace_mod.SPAN_KINDS)


def test_failed_recall_and_index_are_error_spans_without_usage():
    failed = [
        ("embed:recall:2:failed", "embedding", {"stage": "recall", "status": "failed", "error_code": "embedding_timeout", "model_id": "test:hash"}),
        ("embed:index:2:failed", "embedding", {"stage": "index", "status": "failed", "error_code": "embedding_dimension_mismatch", "model_id": "test:hash"}),
        ("memory:observe:2:failed", "memory_observe_failed", {"stage": "observe", "error_code": "overlap", "covers_from_turn": 0, "covers_to_turn": 2}),
    ]
    trace = _build([AGENT_EVENTS[0], *failed], {})
    spans = _spans(trace)
    recall, index, obs = spans["r1:embed:recall:2:failed"], spans["r1:embed:index:2:failed"], spans["r1:memory:observe:2:failed"]
    assert (recall["kind"], recall["status"], recall["attributes"]["error_code"]) == ("memory_recall", "error", "embedding_timeout")
    assert (index["kind"], index["status"], index["attributes"]["error_code"]) == ("memory_index", "error", "embedding_dimension_mismatch")
    assert (obs["kind"], obs["status"], obs["attributes"]["error_code"]) == ("observer", "error", "overlap")
    assert recall["attributes"]["usage"] is None and recall["attributes"]["cost_usd"] is None
    assert trace["totals"]["embedding_requests"] == 0 and trace["totals"]["model_requests"] == 0


def test_kb_ingest_run_span_and_totals():
    req = {"embed:batch:0": {**_row("embed:batch:0", op="embedding", inp=900, cost="0.0009", model="test:hash"), "run_id": "r1"}}
    trace = _build(KB_EVENTS, req, kind="kb_ingest", agent_id=None, agent_version=None, conversation_id=None,
                   turn=None, knowledge_base_id="kb1")
    root = trace["spans"][0]
    assert root["kind"] == "run" and root["id"] == "run:r1" and root["parent_id"] is None
    assert root["name"] == "knowledge base kb1 ingest"
    assert root["attributes"]["knowledge_base_id"] == "kb1" and root["attributes"]["ingest_mode"] == "ingest"
    phases = [s["name"] for s in trace["spans"] if s["kind"] == "ingest"]
    assert phases == ["chunked", "finalized"]
    assert all(s["start"] == s["end"] for s in trace["spans"] if s["kind"] == "ingest")
    t = trace["totals"]
    assert (t["model_requests"], t["embedding_requests"], t["embedding_input_tokens"], t["tool_calls"]) == (1, 1, 900, 0)
    assert t["cost_usd"] == t["embedding_cost_usd"] == "0.0009"


@pytest.mark.parametrize("seed", range(6))
def test_totals_equal_the_usage_row_sums(seed):
    rng = random.Random(seed)
    rows = {}
    for n in range(rng.randint(1, 25)):
        tmpl, op = rng.choice([k for k in KEYS if not (k[1] == "embedding" and k[0].startswith(("moderation", "memory")))
                               and not (k[1] == "chat" and k[0].startswith("embed:"))])
        key = tmpl.format(n=n)
        cost = None if rng.random() < 0.15 else format(Decimal(rng.randint(0, 99999)) / Decimal(10**6), "f")
        rows[key] = _row(key, op=op, inp=rng.randint(0, 999), out=rng.randint(0, 99), cost=cost)
    t = _build([AGENT_EVENTS[0]], rows)["totals"]
    emb = [r for r in rows.values() if r["operation"] == "embedding"]
    priced = [r["cost_usd"] for r in rows.values() if r["cost_usd"] is not None]
    assert t["model_requests"] == len(rows) and t["embedding_requests"] == len(emb)
    assert t["input_tokens"] == sum(r["input_tokens"] for r in rows.values())
    assert t["output_tokens"] == sum(r["output_tokens"] for r in rows.values())
    assert t["embedding_input_tokens"] == sum(r["input_tokens"] for r in emb)
    assert t["memory_requests"] == sum(k.startswith(("memory:observe:", "memory:reflect:")) for k in rows)
    assert t["moderation_requests"] == sum(k.startswith("moderation:") for k in rows)
    assert t["unpriced_requests"] == len(rows) - len(priced)
    if len(priced) == len(rows):
        assert Decimal(t["cost_usd"]) == sum(priced, Decimal(0))
    else:
        assert t["cost_usd"] is None
    if all(r["cost_usd"] is not None for r in emb):
        assert Decimal(t["embedding_cost_usd"]) == sum((r["cost_usd"] for r in emb), Decimal(0))
    else:
        assert t["embedding_cost_usd"] is None


INJECT = {"zz_injected": "INJECTED-TEXT", "content": "INJECTED-TEXT", "text": "INJECTED-TEXT", "messages_text": "INJECTED-TEXT"}
ALLOWED = {
    *trace_mod.EMBEDDING_ATTRIBUTES, *trace_mod.INGEST_ATTRIBUTES, *trace_mod.OBSERVER_ATTRIBUTES,
    *trace_mod.WORKING_MEMORY_ATTRIBUTES, "event_key", "request_key", "model_id", "provider_model_name", "usage",
    "cost_usd", "price_source", "error_code", "memory_kind", "phase", "stage", "usage_measured", "input_tokens",
}


def test_allowlist_blocks_injected_fields_on_every_new_event():
    events = [(k, t, {**d, **INJECT}) if t in ("embedding", "memory_updated", "memory_observe_failed", "ingest_progress")
              else (k, t, d) for k, t, d in AGENT_EVENTS]
    kb = [(k, t, {**d, **INJECT}) if t == "ingest_progress" else (k, t, d) for k, t, d in KB_EVENTS]
    traces = [_build(events), _build(kb, {}, kind="kb_ingest", agent_id=None, agent_version=None, knowledge_base_id="kb1")]
    seen = set()
    for trace in traces:
        for s in trace["spans"]:
            if s["kind"] in NEW_KINDS - {"retrieve"}:
                seen.add(s["kind"])
                assert "INJECTED-TEXT" not in json.dumps(s, default=str), s["id"]
                assert set(s["attributes"]) <= ALLOWED, (s["id"], set(s["attributes"]) - ALLOWED)
                assert s["input"] is None and s["output"] is None
    assert seen == NEW_KINDS - {"retrieve"}


def test_retrieve_hits_are_allowlisted_and_add_nothing_to_the_tool_output():
    sources = [{**s, **INJECT, "chunk_text": CHUNK, "heading_path": ["A"], "metadata": {"k": "v"}} for s in SOURCES]
    body = f"[1] Install\n> {CHUNK}\n[2] FAQ\n> faq text"  # the numbered sources the model saw (retrieve's real shape)
    events = [(k, t, {**d, "sources": sources, "content": body}) if k == "tool:c1:result" else (k, t, d) for k, t, d in AGENT_EVENTS]
    trace = _build(events)
    (span,) = [s for s in trace["spans"] if s["kind"] == "retrieve"]
    hits = span["attributes"]["hits"]
    assert [set(h) for h in hits] and all(set(h) <= set(trace_mod.HIT_ATTRIBUTES) for h in hits)
    assert hits == SOURCES
    assert "INJECTED-TEXT" not in json.dumps(span["attributes"]) and CHUNK not in json.dumps(span["attributes"])
    # Compared with the same call as a plain tool_call: the span's input and output are what the tool_call showed.
    plain = [(k, t, {**d, "tool_name": "other"}) if t in ("tool_call", "tool_result") and d.get("tool_call_id") == "c1" else (k, t, d)
             for k, t, d in events]
    (tool,) = [s for s in _build(plain)["spans"] if s["id"] == "r1:tool:c1"]
    assert tool["kind"] == "tool_call"
    assert (span["input"], span["output"]) == (tool["input"], tool["output"])
    texts = json.dumps(span["output"])
    for h in hits:  # every title in a hit is something the tool result already carried
        assert h["title"] in texts


def test_span_ids_are_stable_across_rebuilds_and_replayed_events():
    first = [s["id"] for s in _build()["spans"]]
    assert first == [s["id"] for s in _build()["spans"]]
    assert len(first) == len(set(first))
    replay = _events(AGENT_EVENTS)
    replay += [{**e, "seq": e["seq"] + 100} for e in replay if e["type"] in ("embedding", "memory_updated", "tool_call")]
    again = build_trace(TraceInput(root=RunData(run=_run(), events=replay, requests=dict(REQUESTS))))
    assert sorted(s["id"] for s in again["spans"]) == sorted(first)
    assert again["totals"]["embedding_requests"] == _build()["totals"]["embedding_requests"]
