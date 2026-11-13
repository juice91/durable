"""P0.21: cross-tenant isolation over every API endpoint added or changed in milestone P0.

Two tenants with interleaved data of the same shape (agents with two published versions and a
draft, a conversation with a turn, an API-started run, a seeded run with a child run, usage rows
and processor results). Tenant B then calls every P0 endpoint with tenant A's ids. Each call must
answer 404 or an empty result, and must change nothing: every table the route could touch is
snapshotted for both tenants before and after B's requests, and the launcher must not have
enqueued anything. B's own results must be exactly B's.

Routes: GET /metrics/summary|timeseries|breakdown, GET /runs/{id}/trace, GET /logs?run_id=,
GET /processor-results, GET /agents/{id}/versions/compare, POST /agents/{id}/versions/{v}/restore,
GET /models, GET /agents/{id}/request-context and POST .../validate, GET|PATCH /conversations[/{id}],
POST /agents/{id}/runs and POST /conversations/{id}/runs with the P0.8 run options, GET /runs with
the P0.6 filters.

Not covered: POST /processors/test. It is stateless (it runs the deterministic processors on the
sample in the request body, reads no tenant data and stores nothing), so there is no other tenant's
id to try and nothing to change.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text

from eap_api.routes.logs import LokiEntry, get_log_source
from eap_run_store import EventLog, RunStore

from .run_helpers import requires_pg
from .test_workflow_runs_api import _no_hang, api, fake  # noqa: F401 - fixtures

pytestmark = requires_pg

SPEC_V1 = {
    "name": "Support",
    "instructions": "Help {{context.customer}}.",
    "model": {"provider": "test", "name": "echo"},
    "context_fields": [
        {"name": "customer", "type": "string", "required": True, "max_length": 20},
        {"name": "plan", "type": "enum", "values": ["free", "pro"]},
    ],
}
SPEC_V2 = {**SPEC_V1, "instructions": "Help {{context.customer}} politely."}
SPEC_DRAFT = {**SPEC_V1, "instructions": "Help {{context.customer}} politely and briefly."}
CONTEXT = {"customer": "Ada", "plan": "pro"}

# Usage model per tenant, so model filters can name the other tenant's model.
MODEL = {"A": "test:alpha", "B": "test:beta"}
# Seeded tokens and cost per tenant (B's are what B's metrics must show, exactly).
USAGE = {"A": (1000, 2000, Decimal("7")), "B": (3, 4, Decimal("0.25"))}

# Tables a P0 route could touch, per tenant, in a stable order.
SNAPSHOT_QUERIES = {
    "runs": "SELECT to_jsonb(x) FROM runs x WHERE tenant_id = :t ORDER BY id",
    "agents": "SELECT to_jsonb(x) FROM agents x WHERE tenant_id = :t ORDER BY id",
    "agent_versions": "SELECT to_jsonb(x) FROM agent_versions x WHERE tenant_id = :t ORDER BY agent_id, version",
    "conversations": "SELECT to_jsonb(x) FROM conversations x WHERE tenant_id = :t ORDER BY id",
    "execution_events": "SELECT to_jsonb(x) FROM execution_events x WHERE tenant_id = :t ORDER BY run_id, seq",
    "run_model_requests": (
        "SELECT to_jsonb(x) FROM run_model_requests x WHERE tenant_id = :t ORDER BY run_id, request_key"
    ),
}


@dataclass
class Tenant:
    name: str
    headers: dict[str, str]
    agent_id: str = ""
    conversation_id: str = ""
    conversation_title: str = ""
    turn_run_id: str = ""  # the conversation's first turn (API)
    api_run_id: str = ""  # POST /agents/{id}/runs (API)
    root_run_id: str = ""  # seeded, completed, with usage and a processor result
    child_run_id: str = ""  # seeded child of root_run_id, with usage
    run_ids: set[str] = field(default_factory=set)


class World:
    def __init__(self, api, fake) -> None:
        self.client, self.app, self.tenant_of = api
        self.fake = fake
        self.engine = self.app.state.database_engine
        self.store, self.log = RunStore(self.engine), EventLog(self.engine)
        self.start = datetime.now(UTC) - timedelta(hours=1)
        self.a = self._tenant("A")
        self.b = self._tenant("B")
        # Interleave: each step runs for A then B, so ids and timestamps alternate between tenants.
        for t in (self.a, self.b):
            self._agent(t)
        for t in (self.a, self.b):
            self._conversation(t)
        for t in (self.a, self.b):
            self._api_run(t)
        for t in (self.a, self.b):
            self._seeded_runs(t)
        for t in (self.a, self.b):
            self._processor_result(t)
        self.end = datetime.now(UTC) + timedelta(hours=1)

    @property
    def window(self) -> dict[str, str]:
        return {"from": self.start.isoformat(), "to": self.end.isoformat()}

    def _tenant(self, label: str) -> Tenant:
        name, headers = self.tenant_of()
        return Tenant(name=name, headers=headers)

    def _agent(self, t: Tenant) -> None:
        c, h = self.client, t.headers
        r = c.post("/api/v1/agents", json={"spec": SPEC_V1}, headers=h)
        assert r.status_code == 201, r.text
        t.agent_id = r.json()["id"]
        assert c.post(f"/api/v1/agents/{t.agent_id}/versions", headers=h).status_code == 201
        assert c.patch(f"/api/v1/agents/{t.agent_id}", json={"spec": SPEC_V2}, headers=h).status_code == 200
        assert c.post(f"/api/v1/agents/{t.agent_id}/versions", headers=h).status_code == 201
        # A dirty draft: a restore without discard_draft would be refused, with it would replace it.
        assert c.patch(f"/api/v1/agents/{t.agent_id}", json={"spec": SPEC_DRAFT}, headers=h).status_code == 200

    def _conversation(self, t: Tenant) -> None:
        t.conversation_title = f"Chat of {t.name}"
        r = self.client.post(
            f"/api/v1/agents/{t.agent_id}/conversations", json={"title": t.conversation_title}, headers=t.headers
        )
        assert r.status_code == 201, r.text
        t.conversation_id = r.json()["id"]
        t.turn_run_id = str(uuid.uuid4())
        r = self.client.post(
            f"/api/v1/conversations/{t.conversation_id}/runs",
            json={"run_id": t.turn_run_id, "input": "hi", "request_context": CONTEXT},
            headers=t.headers,
        )
        assert r.status_code == 201, r.text
        t.run_ids.add(t.turn_run_id)

    def _api_run(self, t: Tenant) -> None:
        t.api_run_id = str(uuid.uuid4())
        r = self.client.post(
            f"/api/v1/agents/{t.agent_id}/runs",
            json={"run_id": t.api_run_id, "input": "hello", "request_context": CONTEXT,
                  "model": {"provider": "test", "name": "history"}},
            headers=t.headers,
        )
        assert r.status_code == 201, r.text
        t.run_ids.add(t.api_run_id)

    def _seeded_runs(self, t: Tenant) -> None:
        label = "A" if t is self.a else "B"
        root, child = uuid.uuid4(), uuid.uuid4()
        self.store.create_run(t.name, uuid.UUID(t.agent_id), 1, "seeded", root)
        self.store.create_child_agent_run(t.name, uuid.UUID(t.agent_id), 1, "child", child, root, "s1")
        inp, out, cost = USAGE[label]
        # Root: one request with the tenant's whole usage; child: one unpriced zero request.
        for run_id, (i, o, c) in ((root, (inp, out, cost)), (child, (0, 0, None))):
            usage = {"request_key": "model:0", "model_id": MODEL[label], "provider_model_name": label.lower(),
                     "input_tokens": i, "output_tokens": o, "cache_read_tokens": 0, "cache_write_tokens": 0,
                     "usage_measured": True, "cost_usd": c, "price_source": "override" if c is not None else None}
            self.log.append(t.name, run_id, "model:0:final", "model_message", {"model_request": 0, "text": "x"}, usage)
        with self.engine.begin() as conn:
            conn.execute(
                text("UPDATE runs SET status = 'completed', model_id = :m, finished_at = created_at + interval '2 s'"
                     " WHERE tenant_id = :t AND id IN (:r, :c)"),
                {"t": t.name, "m": MODEL[label], "r": str(root), "c": str(child)},
            )
        t.root_run_id, t.child_run_id = str(root), str(child)
        t.run_ids |= {t.root_run_id, t.child_run_id}

    def _processor_result(self, t: Tenant) -> None:
        data = {"phase": "input", "index": 0, "processor_type": "pii_redact", "action": "redact",
                "match_counts": {"email": 1}}
        with self.engine.begin() as conn:
            seq = conn.execute(
                text("SELECT COALESCE(MAX(seq), -1) + 1 FROM execution_events WHERE run_id = :r"),
                {"r": t.root_run_id},
            ).scalar_one()
            conn.execute(
                text("INSERT INTO execution_events (tenant_id, run_id, seq, event_key, type, data, ts)"
                     " VALUES (:t, :r, :s, 'processor:input:0', 'processor_result', CAST(:d AS JSONB), now())"),
                {"t": t.name, "r": t.root_run_id, "s": seq, "d": json.dumps(data)},
            )

    # ---- helpers

    def snapshot(self) -> dict[str, dict[str, list[Any]]]:
        out: dict[str, dict[str, list[Any]]] = {}
        with self.engine.connect() as conn:
            for t in (self.a, self.b):
                out[t.name] = {
                    table: [row[0] for row in conn.execute(text(sql), {"t": t.name})]
                    for table, sql in SNAPSHOT_QUERIES.items()
                }
        return out

    def get(self, path: str, t: Tenant | None = None, **params: Any):
        return self.client.get(f"/api/v1{path}", params=params or None, headers=(t or self.b).headers)

    def b_runs_in_db(self) -> int:
        """Tenant B's top-level runs (what B's run counts must equal)."""
        with self.engine.connect() as conn:
            return conn.execute(
                text("SELECT count(*) FROM runs WHERE tenant_id = :t AND parent_run_id IS NULL"), {"t": self.b.name}
            ).scalar_one()


@pytest.fixture
def world(api, fake):  # noqa: F811 - fixtures
    return World(api, fake)


@pytest.fixture
def unchanged(world):
    """Asserts, after the test body, that no tenant's rows changed and nothing was enqueued."""
    before = world.snapshot()
    calls = len(world.fake.calls)
    yield world
    after = world.snapshot()
    for tenant in before:
        for table in before[tenant]:
            assert after[tenant][table] == before[tenant][table], f"{table} of {tenant} changed"
    assert len(world.fake.calls) == calls, "a run was enqueued"


def _not_found(r) -> None:
    assert r.status_code == 404, (r.status_code, r.text)


# --------------------------------------------------------------------------- P0.6 metrics


def test_metrics_summary_shows_only_the_callers_tenant(unchanged):
    w = unchanged
    inp, out, cost = USAGE["B"]
    expected_runs = w.b_runs_in_db()
    assert expected_runs == 3  # turn, API run, seeded root (the child run is not counted)
    body = w.get("/metrics/summary", **w.window, compare="previous").json()
    cur = body["current"]
    assert cur["runs"] == expected_runs and cur["completed"] == 1
    assert (cur["input_tokens"], cur["output_tokens"]) == (inp, out)
    assert Decimal(cur["cost_usd"]) == cost and cur["requests"] == 2 and cur["unpriced_requests"] == 1
    assert body["previous"]["runs"] == 0 and body["previous"]["input_tokens"] == 0
    # With A's agent or A's model as the filter, B sees nothing at all.
    for params in ({"agent_id": w.a.agent_id}, {"model": MODEL["A"]}, {"model": MODEL["A"], "agent_id": w.a.agent_id}):
        cur = w.get("/metrics/summary", **w.window, **params).json()["current"]
        assert cur["runs"] == 0 and cur["completed"] == 0, params
        assert cur["input_tokens"] == 0 and cur["output_tokens"] == 0 and cur["requests"] == 0, params
        assert Decimal(cur["cost_usd"]) == 0, params
    # B's own model filter gives B's usage exactly; A sees A's own numbers (sanity of the seed).
    cur = w.get("/metrics/summary", **w.window, model=MODEL["B"]).json()["current"]
    assert (cur["input_tokens"], cur["output_tokens"], Decimal(cur["cost_usd"])) == (inp, out, cost)
    cur = w.get("/metrics/summary", w.a, **w.window).json()["current"]
    assert (cur["input_tokens"], cur["output_tokens"], Decimal(cur["cost_usd"])) == USAGE["A"]


def test_metrics_timeseries_and_breakdown_show_only_the_callers_tenant(unchanged):
    w = unchanged
    inp, out, cost = USAGE["B"]
    buckets = w.get("/metrics/timeseries", **w.window, interval="hour").json()["buckets"]
    assert sum(b["runs"] for b in buckets) == w.b_runs_in_db()
    assert sum(b["input_tokens"] for b in buckets) == inp and sum(b["output_tokens"] for b in buckets) == out
    assert sum(Decimal(b["cost_usd"]) for b in buckets) == cost
    for params in ({"agent_id": w.a.agent_id}, {"model": MODEL["A"]}):
        buckets = w.get("/metrics/timeseries", **w.window, interval="hour", **params).json()["buckets"]
        assert buckets and all(b["runs"] == 0 and b["input_tokens"] == 0 and b["requests"] == 0 for b in buckets)
        assert all(Decimal(b["cost_usd"]) == 0 for b in buckets)

    by_agent = w.get("/metrics/breakdown", **w.window, by="agent").json()["items"]
    assert [(i["key"], i["label"]) for i in by_agent] == [(w.b.agent_id, "Support")]
    assert by_agent[0]["input_tokens"] == inp and Decimal(by_agent[0]["cost_usd"]) == cost
    by_model = w.get("/metrics/breakdown", **w.window, by="model", sort="tokens").json()["items"]
    assert {i["key"] for i in by_model} <= {MODEL["B"], "test:echo", "test:history"}
    assert MODEL["A"] not in {i["key"] for i in by_model}
    assert sum(i["input_tokens"] for i in by_model) == inp
    for by in ("agent", "model"):
        for params in ({"agent_id": w.a.agent_id}, {"model": MODEL["A"]}):
            items = w.get("/metrics/breakdown", **w.window, by=by, **params).json()["items"]
            assert all(i["runs"] == 0 and i["input_tokens"] == 0 and i["requests"] == 0 for i in items), (by, params)
            assert w.a.agent_id not in {i["key"] for i in items} and MODEL["A"] not in {i["key"] for i in items}


# --------------------------------------------------------------------------- P0.7 trace


def test_trace_of_another_tenants_run_or_child_is_404(unchanged):
    w = unchanged
    for run_id in (w.a.root_run_id, w.a.child_run_id, w.a.api_run_id, w.a.turn_run_id):
        r = w.get(f"/runs/{run_id}/trace")
        _not_found(r)
        assert r.json()["detail"] == "Run not found"
    # B's own trace is B's run and B's child only.
    body = w.get(f"/runs/{w.b.root_run_id}/trace").json()
    span_text = json.dumps(body["spans"])
    assert f"run:{w.b.child_run_id}" in {s["id"] for s in body["spans"]}
    assert all(run_id not in span_text for run_id in w.a.run_ids)
    assert body["totals"]["input_tokens"] == USAGE["B"][0]
    # A sees its own child nested, and never B's.
    a_ids = {s["id"] for s in w.get(f"/runs/{w.a.root_run_id}/trace", w.a).json()["spans"]}
    assert f"run:{w.a.child_run_id}" in a_ids and f"run:{w.b.child_run_id}" not in a_ids


# --------------------------------------------------------------------------- P0.4 logs


class RecordingLoki:
    """Returns one record per tenant for every query; records the queries."""

    def __init__(self, entries: list[LokiEntry]) -> None:
        self.entries = entries
        self.queries: list[str] = []

    def query_range(self, query: str, *, start_ns: int, end_ns: int, limit: int) -> list[LokiEntry]:
        self.queries.append(query)
        return list(self.entries)


def test_logs_of_another_tenants_run_are_404_and_loki_is_never_queried(unchanged):
    w = unchanged
    now_ns = int(datetime.now(UTC).timestamp() * 1e9)

    def entry(t: Tenant, offset: int) -> LokiEntry:
        return LokiEntry(ts_ns=now_ns - offset, line=f"line of {t.name}", labels={
            "service_name": "eap-worker", "severity_text": "INFO",
            "eap_tenant_id": t.name, "eap_run_id": t.root_run_id,
        })

    loki = RecordingLoki([entry(w.a, 1000), entry(w.b, 2000)])
    w.app.dependency_overrides[get_log_source] = lambda: loki
    try:
        for run_id in (w.a.root_run_id, w.a.child_run_id, w.a.api_run_id):
            r = w.get("/logs", run_id=run_id)
            _not_found(r)
            r = w.get("/logs", run_id=run_id, q="line of", level="info")
            _not_found(r)
        assert loki.queries == []
        # B's own run: Loki is asked for B's tenant only, and only B's record comes back even though
        # the source also returned A's.
        items = w.get("/logs", run_id=w.b.root_run_id).json()["items"]
        assert [i["message"] for i in items] == [f"line of {w.b.name}"]
        assert len(loki.queries) == 1 and w.b.name in loki.queries[0] and w.a.name not in loki.queries[0]
        items = w.get("/logs").json()["items"]
        assert [i["message"] for i in items] == [f"line of {w.b.name}"]
    finally:
        w.app.dependency_overrides.pop(get_log_source, None)


# --------------------------------------------------------------------------- P0.9 processor results


def test_processor_results_are_tenant_scoped(unchanged):
    w = unchanged
    body = w.get("/processor-results").json()
    assert [i["run_id"] for i in body["items"]] == [w.b.root_run_id]
    assert body["items"][0]["agent_id"] == w.b.agent_id
    for params in ({"agent_id": w.a.agent_id}, {"run_id": w.a.root_run_id},
                   {"agent_id": w.a.agent_id, "type": "pii_redact", "action": "redact", "phase": "input"}):
        assert w.get("/processor-results", **params).json() == {"items": [], "next_before": None}, params
    # A cursor taken from A's listing only names a position: B still gets B's rows only.
    a_page = w.get("/processor-results", w.a, limit=1).json()
    assert [i["run_id"] for i in a_page["items"]] == [w.a.root_run_id] and a_page["next_before"]
    paged = w.get("/processor-results", before=a_page["next_before"]).json()["items"]
    assert {i["run_id"] for i in paged} <= {w.b.root_run_id}
    # The catalog is not tenant data: identical for both tenants.
    assert w.get("/processors").json() == w.get("/processors", w.a).json()


# --------------------------------------------------------------------------- P0.3 compare and restore


def test_compare_and_restore_of_another_tenants_agent_are_404(unchanged):
    w = unchanged
    a = w.a.agent_id
    for base, target in (("1", "2"), ("1", "draft"), ("draft", "2"), ("2", "1")):
        r = w.get(f"/agents/{a}/versions/compare", base=base, target=target)
        _not_found(r)
        assert r.json()["detail"] == "Agent not found"
    for body in (None, {"discard_draft": False}, {"discard_draft": True}):
        r = w.client.post(f"/api/v1/agents/{a}/versions/1/restore", json=body, headers=w.b.headers)
        _not_found(r)
        assert r.json()["detail"] == "Agent not found"
    # The neighbouring version routes agree (publish, list, get, draft edit).
    _not_found(w.client.post(f"/api/v1/agents/{a}/versions", headers=w.b.headers))
    _not_found(w.get(f"/agents/{a}/versions"))
    _not_found(w.get(f"/agents/{a}/versions/1"))
    _not_found(w.get(f"/agents/{a}"))
    _not_found(w.client.patch(f"/api/v1/agents/{a}", json={"spec": SPEC_V1}, headers=w.b.headers))
    # B's own compare works and shows B's agent.
    r = w.get(f"/agents/{w.b.agent_id}/versions/compare", base="1", target="draft")
    assert r.status_code == 200 and r.json()["agent_id"] == w.b.agent_id


# --------------------------------------------------------------------------- P0.8 models and request context


def test_models_are_platform_wide_and_leak_nothing_tenant_specific(unchanged):
    w = unchanged
    ra, rb = w.get("/models", w.a), w.get("/models")
    assert ra.status_code == rb.status_code == 200
    assert ra.json() == rb.json() and ra.json()["items"]
    raw = rb.text
    for value in (w.a.name, w.b.name, w.a.agent_id, w.b.agent_id, MODEL["A"], MODEL["B"], "Support"):
        assert value not in raw


def test_request_context_of_another_tenants_agent_is_404(unchanged):
    w = unchanged
    a = w.a.agent_id
    for params in ({}, {"version": "1"}, {"version": "2"}, {"version": "draft"}):
        r = w.get(f"/agents/{a}/request-context", **params)
        _not_found(r)
        assert r.json()["detail"] == "Agent not found"
    for body in ({"request_context": CONTEXT}, {"version": "draft", "request_context": CONTEXT},
                 {"version": 1, "request_context": {}}, {"request_context": {"bogus": 1}}):
        r = w.client.post(f"/api/v1/agents/{a}/request-context/validate", json=body, headers=w.b.headers)
        _not_found(r)
        assert r.json()["detail"] == "Agent not found"
    # B's own agent answers.
    assert w.get(f"/agents/{w.b.agent_id}/request-context").json()["agent_id"] == w.b.agent_id


# --------------------------------------------------------------------------- P0.5 conversations


def test_conversations_list_get_and_rename_are_tenant_scoped(unchanged):
    w = unchanged
    body = w.get("/conversations").json()
    assert [c["id"] for c in body["items"]] == [w.b.conversation_id]
    assert body["items"][0]["last_turn_run_id"] == w.b.turn_run_id
    assert w.get("/conversations", agent_id=w.a.agent_id).json() == {"items": [], "next_before": None}
    # A cursor taken from A's listing only names a position: B still gets B's rows only.
    a_page = w.get("/conversations", w.a, limit=1).json()
    assert [c["id"] for c in a_page["items"]] == [w.a.conversation_id] and a_page["next_before"]
    for params in ({}, {"agent_id": w.a.agent_id}, {"agent_id": w.b.agent_id}):
        items = w.get("/conversations", before=a_page["next_before"], **params).json()["items"]
        assert {c["id"] for c in items} <= {w.b.conversation_id}, params
    r = w.get(f"/conversations/{w.a.conversation_id}")
    _not_found(r)
    assert r.json()["detail"] == "Conversation not found"
    r = w.client.patch(f"/api/v1/conversations/{w.a.conversation_id}", json={"title": "pwned"}, headers=w.b.headers)
    _not_found(r)
    assert w.get(f"/conversations/{w.a.conversation_id}", w.a).json()["title"] == w.a.conversation_title


# --------------------------------------------------------------------------- P0.8 run start with options


def test_run_start_on_another_tenants_agent_or_conversation_is_404(unchanged):
    w = unchanged
    bodies = [
        {"input": "x"},
        {"input": "x", "request_context": CONTEXT},
        {"input": "x", "model": {"provider": "test", "name": "history"}},
        {"input": "x", "version": "draft", "request_context": CONTEXT},
        {"input": "x", "version": 1, "request_context": CONTEXT, "model": {"provider": "test", "name": "echo"}},
    ]
    for body in bodies:
        r = w.client.post(f"/api/v1/agents/{w.a.agent_id}/runs", json={**body, "run_id": str(uuid.uuid4())},
                          headers=w.b.headers)
        _not_found(r)
        assert r.json()["detail"] == "Agent not found"
        r = w.client.post(f"/api/v1/conversations/{w.a.conversation_id}/runs",
                          json={**body, "run_id": str(uuid.uuid4())}, headers=w.b.headers)
        _not_found(r)
        assert r.json()["detail"] == "Conversation not found"
        # A's own run_id on A's agent or conversation: still 404, never a retry of A's run.
        for path, run_id in ((f"/agents/{w.a.agent_id}/runs", w.a.api_run_id),
                             (f"/conversations/{w.a.conversation_id}/runs", w.a.turn_run_id)):
            _not_found(w.client.post(f"/api/v1{path}", json={**body, "run_id": run_id}, headers=w.b.headers))


def test_reusing_another_tenants_run_id_never_touches_their_run(unchanged):
    w = unchanged
    # B's own agent and conversation, but A's run ids (every A run, including the child run).
    for run_id in sorted(w.a.run_ids):
        for body in ({"input": "hello", "request_context": CONTEXT, "model": {"provider": "test", "name": "history"}},
                     {"input": "hello", "version": "draft", "request_context": CONTEXT},
                     {"input": "seeded", "version": 1, "request_context": CONTEXT}):
            r = w.client.post(f"/api/v1/agents/{w.b.agent_id}/runs", json={**body, "run_id": run_id},
                              headers=w.b.headers)
            assert r.status_code == 409, (run_id, r.text)
            assert r.json()["detail"] == "run_id is already in use"
    # A's runs are still A's, as they were.
    for run_id in w.a.run_ids:
        assert w.get(f"/runs/{run_id}", w.a).json()["run_id"] == run_id


def test_turn_with_another_tenants_run_id_never_touches_their_run(world):
    w = world
    # B's seeded turn is still queued (a new turn would be refused as busy), so B uses a fresh
    # conversation; it is created before the snapshot.
    r = w.client.post(f"/api/v1/agents/{w.b.agent_id}/conversations", json={"title": "fresh"}, headers=w.b.headers)
    assert r.status_code == 201
    conv = r.json()["id"]
    before = w.snapshot()
    calls = len(w.fake.calls)
    for run_id in sorted(w.a.run_ids):
        r = w.client.post(f"/api/v1/conversations/{conv}/runs",
                          json={"run_id": run_id, "input": "hi", "request_context": CONTEXT}, headers=w.b.headers)
        assert r.status_code == 409, (run_id, r.text)
        assert r.json()["detail"] == "run_id is already in use"
    assert w.snapshot() == before and len(w.fake.calls) == calls
    assert w.get(f"/conversations/{conv}").json()["turn_count"] == 0


# --------------------------------------------------------------------------- P0.6 run list filters


def test_run_list_filters_never_show_another_tenants_runs(unchanged):
    w = unchanged
    b_top = {w.b.turn_run_id, w.b.api_run_id, w.b.root_run_id}
    items = w.get("/runs").json()["items"]
    assert {i["run_id"] for i in items} == b_top
    cases = [
        ({"model": MODEL["A"]}, set()),
        ({"model": MODEL["B"]}, {w.b.root_run_id}),
        ({"provider": "test"}, b_top),
        ({"status": "completed"}, {w.b.root_run_id}),
        ({"status": "queued,completed"}, b_top),
        ({"status": ["queued", "completed"], **w.window}, b_top),
        ({"model": MODEL["A"], "status": "completed", **w.window}, set()),
        ({"model": "test:history", "status": "queued", **w.window}, {w.b.api_run_id}),
        ({"agent_id": w.a.agent_id}, set()),
        ({"agent_id": w.a.agent_id, "model": MODEL["A"], "status": "completed", **w.window}, set()),
        ({"parent_run_id": w.a.root_run_id}, set()),
        ({"parent_run_id": w.a.root_run_id, "model": MODEL["A"]}, set()),
        ({"parent_run_id": w.b.root_run_id}, {w.b.child_run_id}),
    ]
    for params, expected in cases:
        r = w.get("/runs", **params)
        assert r.status_code == 200, (params, r.text)
        assert {i["run_id"] for i in r.json()["items"]} == expected, params
    # A cursor from A's list only names a position.
    a_page = w.get("/runs", w.a, limit=1, **w.window).json()
    assert a_page["next_before"]
    items = w.get("/runs", before=a_page["next_before"], **w.window).json()["items"]
    assert {i["run_id"] for i in items} <= b_top
    # A's runs, child runs and events are 404 for B on the per-run routes.
    for run_id in w.a.run_ids:
        _not_found(w.get(f"/runs/{run_id}"))
        _not_found(w.get(f"/runs/{run_id}/events"))
