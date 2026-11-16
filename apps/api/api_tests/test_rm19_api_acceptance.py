"""RM.19 API acceptance: the operation partition over HTTP (random rows, decimal strings), filters combined
with kind / agent / model, the previous period, timeseries, by=operation, 422s, cross-tenant, and the
trace endpoint for every new span kind (allowlist, stable ids, totals equal the usage rows)."""

from __future__ import annotations

import random
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from .run_helpers import requires_pg
from .test_runs import (  # noqa: F401 - fixtures
    SPEC,
    _no_hang,
    deltas,
    fake_dbos,
    pg,
    start,
    stream_settings,
    tenant_of,
)

pytestmark = requires_pg

API = "/api/v1"
BASE = datetime(2026, 7, 1, tzinfo=timezone.utc)
AGENT_A, AGENT_B = str(uuid.uuid4()), str(uuid.uuid4())
WINDOW = {"from": BASE.isoformat(), "to": (BASE + timedelta(days=1)).isoformat()}
OPS = ("chat", "moderation", "observer", "reflector", "embedding", "judge")  # AC.17 added judge
KEYS = [
    ("model:{n}", "chat"), ("moderation:input:{n}", "chat"), ("memory:observe:{n}", "chat"),
    ("memory:reflect:{n}", "chat"), ("embed:retrieve:c{n}", "embedding"), ("embed:recall:{n}", "embedding"),
    ("embed:index:{n}", "embedding"), ("embed:batch:{n}", "embedding"), ("embed:recall:{n}", "chat"),
    ("moderation:x{n}", "embedding"), ("other:{n}", "chat"), ("judge:{n}", "chat"), ("judge:x{n}", "embedding"),
]


def op_of(key, op):
    if op == "embedding":
        return "embedding"
    for prefix, name in (("moderation:", "moderation"), ("memory:observe:", "observer"), ("memory:reflect:", "reflector"),
                         ("judge:", "judge")):
        if key.startswith(prefix):
            return name
    return "chat"


def is_memory(key, op):
    return key.startswith(("memory:observe:", "memory:reflect:")) if op == "chat" else key.startswith(("embed:recall:", "embed:index:"))


def seed_run(conn, tenant, kind="tool", agent=None, model=None, kb=None, at=None):
    run_id, at = str(uuid.uuid4()), at or BASE + timedelta(hours=1)
    target = {"tool": ("tool_id", "'echo'"), "agent": ("agent_id, agent_version", "'" + str(agent) + "', 1"),
              "kb_ingest": ("knowledge_base_id", f"'{kb}'")}[kind]
    conn.execute(text(
        f"INSERT INTO runs (id, tenant_id, kind, status, created_at, updated_at, finished_at, model_overridden, model_id, {target[0]})"
        f" VALUES (:id, :t, :k, 'completed', :c, :c, :c, false, :m, {target[1]})"), {"id": run_id, "t": tenant, "k": kind, "c": at, "m": model})
    return run_id


def seed_req(conn, tenant, run_id, key, op, inp=1, out=0, cost=None, model="test:m", at=None):
    conn.execute(text(
        "INSERT INTO run_model_requests (tenant_id, run_id, request_key, model_id, input_tokens, output_tokens,"
        " cache_read_tokens, cache_write_tokens, usage_measured, cost_usd, price_source, created_at, operation)"
        " VALUES (:t, :r, :k, :m, :i, :o, 0, 0, true, :c, :src, :at, :op)"),
        {"t": tenant, "r": run_id, "k": key, "m": model, "i": inp, "o": out, "c": cost,
         "src": None if cost is None else "override", "at": at or BASE + timedelta(hours=1), "op": op})


def get(client, h, path, **params):
    r = client.get(f"{API}/metrics/{path}", params={**WINDOW, **params}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.parametrize("seed", range(4))
def test_partition_over_http_with_decimal_strings(pg, tenant_of, seed):
    client, app = pg
    (tenant, h), (other, _) = tenant_of(), tenant_of()
    rng, rows = random.Random(seed), []
    with app.state.database_engine.begin() as conn:
        runs = [seed_run(conn, tenant) for _ in range(3)]
        for n in range(rng.randint(5, 30)):
            tmpl, op = rng.choice(KEYS)
            row = (tmpl.format(n=n), op, rng.randint(0, 999), rng.randint(0, 99),
                   None if rng.random() < 0.2 else Decimal(rng.randint(0, 99999)) / Decimal(10**6))
            seed_req(conn, tenant, rng.choice(runs), *row)
            rows.append(row)
        o = seed_run(conn, other)
        for i, (tmpl, op) in enumerate(KEYS):
            seed_req(conn, other, o, tmpl.format(n=500 + i), op, 8888, 1, Decimal("88"))
    cur = get(client, h, "summary")["current"]
    ops = cur["operations"]
    assert set(ops) == {*OPS, "memory"}
    cost = sum((r[4] or Decimal(0) for r in rows), Decimal(0))
    assert cur["requests"] == len(rows) and cur["input_tokens"] == sum(r[2] for r in rows)
    assert isinstance(cur["cost_usd"], str) and Decimal(cur["cost_usd"]) == cost
    for field, idx in (("input_tokens", 2), ("output_tokens", 3)):
        assert sum(ops[o][field] for o in OPS) == cur[field]
        for o in OPS:
            assert ops[o][field] == sum(r[idx] for r in rows if op_of(r[0], r[1]) == o)
    assert sum((Decimal(ops[o]["cost_usd"]) for o in OPS), Decimal(0)) == cost
    for o in OPS:
        assert ops[o]["requests"] == sum(op_of(r[0], r[1]) == o for r in rows)
    mem = [r for r in rows if is_memory(r[0], r[1])]
    assert ops["memory"]["input_tokens"] == sum(r[2] for r in mem)
    recall_index = [r for r in rows if r[1] == "embedding" and r[0].startswith(("embed:recall:", "embed:index:"))]
    assert ops["memory"]["input_tokens"] == ops["observer"]["input_tokens"] + ops["reflector"]["input_tokens"] + sum(r[2] for r in recall_index)
    for o in (*OPS, "memory"):
        f = get(client, h, "summary", operation=o)["current"]
        assert (f["requests"], f["input_tokens"], f["cost_usd"]) == (ops[o]["requests"], ops[o]["input_tokens"], ops[o]["cost_usd"])
    items = get(client, h, "breakdown", by="operation", limit=10)["items"]
    assert sum(i["input_tokens"] for i in items) == cur["input_tokens"]
    assert all(i["input_tokens"] < 8888 for i in items)


@pytest.fixture
def world(pg, tenant_of):
    client, app = pg
    (t, h), (other, h_other) = tenant_of(), tenant_of()
    h1, h5, prev, kb = BASE + timedelta(hours=1), BASE + timedelta(hours=5), BASE - timedelta(hours=3), str(uuid.uuid4())
    with app.state.database_engine.begin() as conn:
        conn.execute(text("INSERT INTO knowledge_bases (id, tenant_id, name, embedding_model, dimensions, chunking)"
                          " VALUES (:id, :t, 'Docs', 'test:hash', 1024, '{}'::jsonb)"), {"id": kb, "t": t})
        a = seed_run(conn, t, "agent", AGENT_A, "a:m", at=h1)
        b = seed_run(conn, t, "agent", AGENT_B, "b:m", at=h5)
        tool, ing = seed_run(conn, t, at=h1), seed_run(conn, t, "kb_ingest", kb=kb, at=h1)
        old = seed_run(conn, t, "agent", AGENT_A, "a:m", at=prev)
        f = seed_run(conn, other, "agent", AGENT_A, "a:m", at=h1)
        seed_req(conn, t, a, "model:0", "chat", 10, 2, Decimal("0.01"), "a:m", h1)
        seed_req(conn, t, a, "memory:observe:1", "chat", 20, 4, Decimal("0.02"), "a:m", h1)
        seed_req(conn, t, a, "embed:recall:1", "embedding", 3, 0, Decimal("0.0003"), "a:emb", h1)
        seed_req(conn, t, b, "model:0", "chat", 11, 1, Decimal("0.011"), "b:m", h5)
        seed_req(conn, t, b, "embed:retrieve:c", "embedding", 5, 0, Decimal("0.0005"), "b:emb", h5)
        seed_req(conn, t, tool, "embed:retrieve:c", "embedding", 6, 0, Decimal("0.0006"), "a:emb", h1)
        seed_req(conn, t, ing, "embed:batch:0", "embedding", 100, 0, Decimal("0.1"), "a:emb", h1)
        seed_req(conn, t, old, "memory:reflect:1", "chat", 40, 8, Decimal("0.04"), "a:m", prev)
        seed_req(conn, t, old, "embed:batch:0", "embedding", 50, 0, Decimal("0.05"), "a:emb", prev)
        seed_req(conn, other, f, "embed:batch:0", "embedding", 9999, 0, Decimal("9"), "a:emb", h1)
        seed_req(conn, other, f, "memory:observe:1", "chat", 9999, 0, Decimal("9"), "a:m", h1)
    yield client, h, h_other
    with app.state.database_engine.begin() as conn:
        conn.execute(text("DELETE FROM run_model_requests WHERE tenant_id = :t"), {"t": t})
        conn.execute(text("DELETE FROM runs WHERE kind = 'kb_ingest' AND tenant_id = :t"), {"t": t})
        conn.execute(text("DELETE FROM knowledge_bases WHERE tenant_id = :t"), {"t": t})


@pytest.mark.parametrize(("params", "want"), [
    ({"operation": "embedding", "kind": "kb_ingest"}, (1, 100, "0.1000000000")),
    ({"operation": "embedding", "kind": "tool"}, (1, 6, "0.0006000000")),
    ({"operation": "embedding", "kind": "agent", "agent_id": AGENT_B}, (1, 5, "0.0005000000")),
    ({"operation": "memory", "agent_id": AGENT_A}, (2, 23, "0.0203000000")),
    ({"operation": "observer", "model": "a:m"}, (1, 20, "0.0200000000")),
    ({"operation": "observer", "model": "b:m"}, (0, 0, "0.0000000000")),
    ({"operation": "embedding", "provider": "b"}, (1, 5, "0.0005000000")),
    ({"kind": "kb_ingest"}, (1, 100, "0.1000000000")),
])
def test_operation_with_kind_agent_model_provider(world, params, want):
    client, h, _ = world
    body = get(client, h, "summary", **params)
    assert body["filters"]["operation"] == params.get("operation")
    c = body["current"]
    assert (c["requests"], c["input_tokens"], c["cost_usd"]) == want


def test_previous_period_timeseries_and_breakdown(world):
    client, h, _ = world
    body = get(client, h, "summary", operation="embedding", compare="previous")
    assert (body["current"]["requests"], body["previous"]["requests"], body["previous"]["input_tokens"]) == (4, 1, 50)
    assert body["previous"]["operations"]["embedding"]["cost_usd"] == "0.0500000000"
    series = get(client, h, "timeseries", operation="embedding", kind="agent", interval="hour")["buckets"]
    assert {b["start"][:13]: b["requests"] for b in series if b["requests"]} == {"2026-07-01T01": 1, "2026-07-01T05": 1}
    for kw, want in (({}, {"chat": 2, "observer": 1, "embedding": 4}), ({"kind": "agent"}, {"chat": 2, "observer": 1, "embedding": 2}),
                     ({"operation": "memory"}, {"observer": 1, "embedding": 1}), ({"model": "a:emb"}, {"embedding": 3})):
        items = get(client, h, "breakdown", by="operation", **kw)["items"]
        assert {i["key"]: i["requests"] for i in items} == want, kw


@pytest.mark.parametrize(("path", "params"), [
    ("summary", {"operation": "nope"}), ("summary", {"operation": "Embedding"}), ("summary", {"operation": ""}),
    ("timeseries", {"operation": "tool", "interval": "hour"}), ("breakdown", {"by": "tool"}),
    ("breakdown", {"by": "operation", "operation": "x"}), ("summary", {"kind": "bogus"}),
])
def test_bad_values_are_422(world, path, params):
    client, h, _ = world
    assert client.get(f"{API}/metrics/{path}", params={**WINDOW, **params}, headers=h).status_code == 422


def test_other_tenants_usage_in_no_operation_total_or_breakdown(world):
    client, h, h_other = world
    for op in (None, *OPS, "memory"):
        c = get(client, h, "summary", **({"operation": op} if op else {}))["current"]
        assert c["input_tokens"] < 9999 and all(v["input_tokens"] < 9999 for v in c["operations"].values())
    for by in ("operation", "model", "agent"):
        assert all(i["input_tokens"] < 9999 for i in get(client, h, "breakdown", by=by)["items"])
    mine = get(client, h_other, "summary")["current"]["operations"]
    assert (mine["embedding"]["input_tokens"], mine["observer"]["input_tokens"], mine["chat"]["requests"]) == (9999, 9999, 0)


def _usage(key, op, inp, out, cost, model="test:m"):
    return {"request_key": key, "model_id": model, "provider_model_name": None, "input_tokens": inp, "output_tokens": out,
            "cache_read_tokens": 0, "cache_write_tokens": 0, "usage_measured": True, "cost_usd": Decimal(cost),
            "price_source": "override", "operation": op}


SRC = {"n": 1, "title": "Install", "url": "https://docs.example/install", "ref": "https://docs.example/install",
       "kb_id": "kb1", "document_id": "d1", "document_key": "install", "chunk_id": "ch1", "doc_version": 1, "score": 0.9}
BAD = {"zz_injected": "INJECTED-TEXT", "content": "INJECTED-TEXT", "text": "INJECTED-TEXT"}
CHUNK = "CHUNK-BODY-TEXT"


def _agent_events(tenant, run_id, log):
    rid = uuid.UUID(run_id)
    emb = {"model_id": "test:hash", "inputs": 1, "dimensions": 1024, "input_type": "query", "requests": 1}
    log.append(tenant, rid, "start", "run_started", {"input": "hi"})
    log.append(tenant, rid, "embed:recall:1", "embedding", {**emb, "input_tokens": 3, "cost_usd": "0.00003", "stage": "recall",
               "hits": 1, "turns": 1, "groups": 1, "messages": 2, **BAD}, _usage("embed:recall:1", "embedding", 3, 0, "0.00003", "test:hash"))
    log.append(tenant, rid, "tool:c1:call", "tool_call", {"tool_call_id": "c1", "tool_name": "retrieve", "args": {"search_query": "install"}})
    log.append(tenant, rid, "embed:retrieve:c1", "embedding", {**emb, "input_tokens": 2, "cost_usd": "0.00002"},
               _usage("embed:retrieve:c1", "embedding", 2, 0, "0.00002", "test:hash"))
    log.append(tenant, rid, "tool:c1:result", "tool_result", {"tool_call_id": "c1", "tool_name": "retrieve", "status": "ok",
               "content": f"[1] Install\n> {CHUNK}", "sources": [{**SRC, **BAD, "chunk_text": CHUNK}]})
    log.append(tenant, rid, "tool:c2:call", "tool_call", {"tool_call_id": "c2", "tool_name": "update_working_memory", "args": {"fields": ["name"]}})
    log.append(tenant, rid, "memory:wm:c2", "memory_updated", {"scope": "resource", "format": "json", "version": 2,
               "changed_keys": ["name"], "tool_call_id": "c2", "redacted": 0, **BAD})
    log.append(tenant, rid, "tool:c2:result", "tool_result", {"tool_call_id": "c2", "tool_name": "update_working_memory", "status": "ok", "content": "ok"})
    # embed:recall: belongs to the in-turn work (it reads the user text before the model request).
    # Loops D12 drops non-post-turn events after `end`, so write this before the turn ends.
    log.append(tenant, rid, "embed:recall:2:failed", "embedding", {"stage": "recall", "status": "failed", "error_code": "embedding_timeout", "model_id": "test:hash"})
    log.append(tenant, rid, "end", "run_completed", {"output": "done"})
    log.append(tenant, rid, "embed:index:1", "embedding", {**emb, "input_tokens": 9, "cost_usd": "0.00009", "stage": "index", "turn": 1,
               "inserted": 1, "existing": 0, "redacted": 0}, _usage("embed:index:1", "embedding", 9, 0, "0.00009", "test:hash"))
    log.append(tenant, rid, "memory:observe:1", "memory_updated", {"kind": "observation", "stage": "observe", "covers_from_turn": 0,
               "covers_to_turn": 1, "approx_tokens": 10, "notes": 2, "redacted": 0, **BAD}, _usage("memory:observe:1", "chat", 50, 20, "0.002"))
    log.append(tenant, rid, "memory:reflect:1:failed", "memory_observe_failed", {"stage": "reflect", "error_code": "overlap", **BAD})
    log.append(tenant, rid, "embed:index:2:failed", "embedding", {"stage": "index", "status": "failed", "error_code": "embedding_timeout", "model_id": "test:hash"})
    return 3 + 2 + 9 + 50  # input tokens of the four usage rows


def test_trace_endpoint_new_span_kinds_totals_allowlist_and_stable_ids(pg, tenant_of):
    from eap_run_store import EventLog

    client, app = pg
    (tenant, h), (_, h_other) = tenant_of(), tenant_of()
    agent = client.post(f"{API}/agents", json={"spec": SPEC}, headers=h).json()["id"]
    assert client.post(f"{API}/agents/{agent}/versions", headers=h).status_code == 201
    run_id = str(uuid.uuid4())
    assert start(client, h, agent, run_id).status_code == 201
    tokens = _agent_events(tenant, run_id, EventLog(app.state.database_engine))
    first = client.get(f"{API}/runs/{run_id}/trace", headers=h)
    assert first.status_code == 200, first.text
    body = first.json()
    kinds = {s["id"].removeprefix(f"{run_id}:"): s for s in body["spans"]}
    assert [kinds[k]["kind"] for k in ("tool:c1", "embed:retrieve:c1", "embed:recall:1", "embed:index:1", "memory:wm:c2",
            "memory:observe:1", "memory:reflect:1:failed", "embed:recall:2:failed", "embed:index:2:failed")] == [
        "retrieve", "embedding", "memory_recall", "memory_index", "working_memory", "observer", "reflector", "memory_recall", "memory_index"]
    assert kinds["embed:retrieve:c1"]["parent_id"] == kinds["tool:c1"]["id"]
    assert kinds["memory:wm:c2"]["parent_id"] == kinds["tool:c2"]["id"]
    assert [kinds[k]["status"] for k in ("embed:recall:2:failed", "embed:index:2:failed", "memory:reflect:1:failed")] == ["error"] * 3
    for k, s in kinds.items():
        if s["kind"] in ("memory_recall", "memory_index", "observer", "reflector", "working_memory", "embedding"):
            assert "INJECTED-TEXT" not in str(s), k
    hit = kinds["tool:c1"]["attributes"]["hits"][0]
    assert hit == SRC and CHUNK not in str(kinds["tool:c1"]["attributes"])
    t = body["totals"]
    assert (t["embedding_requests"], t["embedding_input_tokens"], t["memory_requests"], t["input_tokens"]) == (3, 14, 1, tokens)
    assert Decimal(t["embedding_cost_usd"]) == Decimal("0.00014") and Decimal(t["cost_usd"]) == Decimal("0.00214")
    m = client.get(f"{API}/metrics/summary", headers=h).json()["current"]  # the default window ends now
    assert (m["requests"], m["input_tokens"]) == (t["model_requests"], t["input_tokens"])
    assert Decimal(m["cost_usd"]) == Decimal(t["cost_usd"]) and m["operations"]["embedding"]["input_tokens"] == 14
    again = client.get(f"{API}/runs/{run_id}/trace", headers=h).json()
    assert [s["id"] for s in again["spans"]] == [s["id"] for s in body["spans"]]
    assert client.get(f"{API}/runs/{run_id}/trace", headers=h_other).status_code == 404


def test_kb_ingest_trace_over_http(pg, tenant_of):
    from eap_run_store import EventLog

    client, app = pg
    (tenant, h), (_, h_other) = tenant_of(), tenant_of()
    kb = str(uuid.uuid4())
    try:
        with app.state.database_engine.begin() as conn:
            conn.execute(text("INSERT INTO knowledge_bases (id, tenant_id, name, embedding_model, dimensions, chunking)"
                              " VALUES (:id, :t, 'Docs', 'test:hash', 1024, '{}'::jsonb)"), {"id": kb, "t": tenant})
            run_id = seed_run(conn, tenant, "kb_ingest", kb=kb, at=datetime.now(timezone.utc))
        log, rid = EventLog(app.state.database_engine), uuid.UUID(run_id)
        log.append(tenant, rid, "start", "run_started", {"kind": "kb_ingest", "mode": "ingest", "knowledge_base_id": kb})
        log.append(tenant, rid, "chunk:0", "ingest_progress", {"phase": "chunked", "generation": 1, "embedded": 0, "total": 3, "pending_tokens": 900, **BAD})
        log.append(tenant, rid, "embed:batch:0", "ingest_progress", {"model_id": "test:hash", "inputs": 3, "input_tokens": 900, "cost_usd": "0.0009",
                   "dimensions": 1024, "phase": "embedding", "batch": 0, "chunks": 3, "embedded": 3, "total": 3},
                   _usage("embed:batch:0", "embedding", 900, 0, "0.0009", "test:hash"))
        log.append(tenant, rid, "finalize:0", "ingest_progress", {"phase": "finalized", "generation": 1, "document_id": "d1", "doc_version": 1})
        log.append(tenant, rid, "end", "run_completed", {"output": "{}"})
        body = client.get(f"{API}/runs/{run_id}/trace", headers=h)
        assert body.status_code == 200, body.text
        spans = body.json()["spans"]
        assert spans[0]["kind"] == "run" and spans[0]["attributes"]["knowledge_base_id"] == kb
        assert [s["kind"] for s in spans[1:]] == ["ingest", "embedding", "ingest"]
        assert "INJECTED-TEXT" not in str(spans)
        t = body.json()["totals"]
        assert (t["embedding_requests"], t["embedding_input_tokens"], t["embedding_cost_usd"]) == (1, 900, "0.0009000000")
        assert client.get(f"{API}/runs/{run_id}/trace", headers=h_other).status_code == 404
    finally:
        with app.state.database_engine.begin() as conn:
            for table in ("execution_events", "run_model_requests"):
                conn.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tenant})
            conn.execute(text("DELETE FROM runs WHERE tenant_id = :t AND kind = 'kb_ingest'"), {"t": tenant})
            conn.execute(text("DELETE FROM knowledge_bases WHERE tenant_id = :t"), {"t": tenant})
