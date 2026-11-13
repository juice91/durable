"""P0.6: GET /metrics/summary|timeseries|breakdown and the run-list drill-down filters.

One seeded dataset with hand-computed answers (window [BASE, BASE + 2 days), UTC):

  r1  agent A1  completed        created +1h   latency 1 s   requests test:a (10/5, $0.001) and (20/10, $0.002)
  r2  agent A1  failed, blocked  created +2h   latency 3 s   request  test:b unmeasured, unpriced
  w1  workflow  completed        created +3h   latency 10 s  (no requests itself)
  c1    child of w1, agent A2    created +3h                 request  test:a (100/50, $0.01)
  r3  agent A2  needs_attention  created +26h  latency 5 s
  r4  agent A2  running          created +27h
  r5  agent A1  completed        created -1h   latency 2 s   request  test:a (7/7, $0.0007)  [previous period]
  tenant B: one completed run in the window with a $1 request (must never show up for A).
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from .run_helpers import requires_pg
from .test_runs import (  # noqa: F401 - fixtures
    _no_hang,
    deltas,
    fake_dbos,
    make_agent,
    pg,
    stream_settings,
    tenant_of,
)

pytestmark = requires_pg

BASE = datetime(2026, 3, 1, tzinfo=timezone.utc)
WINDOW = {"from": BASE.isoformat(), "to": (BASE + timedelta(days=2)).isoformat()}


def _run(conn, tenant, *, kind="agent", agent_id=None, status="completed", created, latency_s=None, parent=None):
    run_id = str(uuid.uuid4())
    values = {
        "id": run_id, "t": tenant, "kind": kind, "status": status, "created": created,
        "finished": created + timedelta(seconds=latency_s) if latency_s is not None else None,
        "agent_id": agent_id, "agent_version": 1 if kind == "agent" else None,
        "workflow_id": str(uuid.uuid4()) if kind == "workflow" else None,
        "workflow_version": 1 if kind == "workflow" else None,
        "parent": parent, "step": "s1" if parent else None,
    }
    conn.execute(
        text(
            "INSERT INTO runs (id, tenant_id, kind, status, created_at, updated_at, finished_at, agent_id,"
            " agent_version, workflow_id, workflow_version, parent_run_id, parent_step_id, model_overridden)"
            " VALUES (:id, :t, :kind, :status, :created, :created, :finished, :agent_id, :agent_version,"
            " :workflow_id, :workflow_version, :parent, :step, false)"
        ),
        values,
    )
    return run_id


def _request(conn, tenant, run_id, key, model, inp, out, cost, created, measured=True):
    conn.execute(
        text(
            "INSERT INTO run_model_requests (tenant_id, run_id, request_key, model_id, input_tokens, output_tokens,"
            " cache_read_tokens, cache_write_tokens, usage_measured, cost_usd, price_source, created_at)"
            " VALUES (:t, :r, :k, :m, :i, :o, 0, 0, :measured, :c, :src, :created)"
        ),
        {"t": tenant, "r": run_id, "k": key, "m": model, "i": inp, "o": out, "measured": measured,
         "c": cost, "src": "override" if cost is not None else None, "created": created},
    )


@pytest.fixture
def seeded(pg, tenant_of):
    client, app = pg
    tenant, h = tenant_of()
    other, h_other = tenant_of()
    a1, a2 = make_agent(client, h), make_agent(client, h)
    ids: dict[str, str] = {}
    with app.state.database_engine.begin() as conn:
        hr = lambda n: BASE + timedelta(hours=n)  # noqa: E731
        ids["r1"] = _run(conn, tenant, agent_id=a1, created=hr(1), latency_s=1)
        _request(conn, tenant, ids["r1"], "model:0", "test:a", 10, 5, Decimal("0.001"), hr(1))
        _request(conn, tenant, ids["r1"], "model:1", "test:a", 20, 10, Decimal("0.002"), hr(1))
        ids["r2"] = _run(conn, tenant, agent_id=a1, status="failed", created=hr(2), latency_s=3)
        _request(conn, tenant, ids["r2"], "model:0", "test:b", 0, 0, None, hr(2), measured=False)
        conn.execute(
            text(
                "INSERT INTO execution_events (tenant_id, run_id, seq, event_key, type, data, ts)"
                " VALUES (:t, :r, 0, 'end', 'run_failed', CAST(:d AS jsonb), :ts)"
            ),
            {"t": tenant, "r": ids["r2"], "d": '{"status": "failed", "error_code": "input_blocked"}', "ts": hr(2)},
        )
        ids["w1"] = _run(conn, tenant, kind="workflow", created=hr(3), latency_s=10)
        ids["c1"] = _run(conn, tenant, agent_id=a2, created=hr(3), latency_s=4, parent=ids["w1"])
        _request(conn, tenant, ids["c1"], "model:0", "test:a", 100, 50, Decimal("0.01"), hr(3))
        ids["r3"] = _run(conn, tenant, agent_id=a2, status="needs_attention", created=hr(26), latency_s=5)
        ids["r4"] = _run(conn, tenant, agent_id=a2, status="running", created=hr(27))
        ids["r5"] = _run(conn, tenant, agent_id=a1, created=hr(-1), latency_s=2)
        _request(conn, tenant, ids["r5"], "model:0", "test:a", 7, 7, Decimal("0.0007"), hr(-1))
        b1 = _run(conn, other, agent_id=a1, created=hr(1), latency_s=1)  # another tenant, same agent id
        _request(conn, other, b1, "model:0", "test:a", 1000, 1000, Decimal("1"), hr(1))
    return client, app, h, h_other, a1, a2, ids


def test_summary_with_previous_period(seeded):
    client, _, h, _, *_ = seeded
    r = client.get("/api/v1/metrics/summary", params={**WINDOW, "compare": "previous"}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    cur = body["current"]
    assert {k: cur[k] for k in ("runs", "completed", "failed", "needs_attention", "in_progress", "blocked")} == {
        "runs": 5, "completed": 2, "failed": 1, "needs_attention": 1, "in_progress": 1, "blocked": 1,
    }
    assert cur["error_rate"] == pytest.approx(0.25)
    # Latencies 1, 3, 5, 10 s (child run c1 excluded): linear interpolation.
    assert cur["latency_p50_ms"] == pytest.approx(4000)
    assert cur["latency_p95_ms"] == pytest.approx(9250)
    assert {k: cur[k] for k in ("requests", "input_tokens", "output_tokens", "cost_usd", "unpriced_requests",
                                "unmeasured_requests")} == {
        "requests": 4, "input_tokens": 130, "output_tokens": 65, "cost_usd": "0.0130000000",
        "unpriced_requests": 1, "unmeasured_requests": 1,
    }
    prev = body["previous"]
    assert (prev["runs"], prev["completed"], prev["requests"], prev["input_tokens"], prev["cost_usd"]) == (
        1, 1, 1, 7, "0.0007000000"
    )
    assert prev["latency_p50_ms"] == pytest.approx(2000)
    assert body["previous_from"] == (BASE - timedelta(days=2)).isoformat()
    assert body["previous_to"] == BASE.isoformat()

    without = client.get("/api/v1/metrics/summary", params=WINDOW, headers=h).json()
    assert without["previous"] is None and without["current"] == cur


def test_summary_filters(seeded):
    client, _, h, _, a1, a2, _ = seeded

    def cur(**params):
        r = client.get("/api/v1/metrics/summary", params={**WINDOW, **params}, headers=h)
        assert r.status_code == 200, r.text
        return r.json()["current"]

    wf = cur(kind="workflow")
    assert (wf["runs"], wf["requests"], wf["input_tokens"], wf["cost_usd"]) == (1, 1, 100, "0.0100000000")
    agent2 = cur(agent_id=a2)  # runs: its top-level runs; usage: also its workflow-step requests
    assert (agent2["runs"], agent2["requests"], agent2["cost_usd"]) == (2, 1, "0.0100000000")
    b = cur(model="test:b")
    assert (b["runs"], b["failed"], b["requests"], b["unpriced_requests"], b["cost_usd"]) == (1, 1, 1, 1, "0.0000000000")
    a = cur(model="test:a")  # r1 directly, w1 through its child
    assert (a["runs"], a["requests"], a["input_tokens"]) == (2, 3, 130)
    prov = cur(provider="test")
    assert (prov["runs"], prov["requests"]) == (3, 4)
    assert cur(provider="tes")["requests"] == 0  # a prefix of a provider is not that provider
    assert cur(provider="t%")["runs"] == 0  # LIKE wildcards are literal


def test_timeseries_zero_filled(seeded):
    client, _, h, *_ = seeded
    r = client.get("/api/v1/metrics/timeseries", params={**WINDOW, "interval": "day"}, headers=h)
    assert r.status_code == 200, r.text
    buckets = r.json()["buckets"]
    assert [b["start"] for b in buckets] == [BASE.isoformat(), (BASE + timedelta(days=1)).isoformat()]
    assert [b["runs"] for b in buckets] == [3, 2]
    assert [b["requests"] for b in buckets] == [4, 0]
    assert [b["cost_usd"] for b in buckets] == ["0.0130000000", "0.0000000000"]
    assert buckets[1]["latency_p50_ms"] == pytest.approx(5000)

    hourly = client.get(
        "/api/v1/metrics/timeseries",
        params={"from": BASE.isoformat(), "to": (BASE + timedelta(hours=5)).isoformat(), "metric": ["runs", "failed"]},
        headers=h,
    ).json()
    assert hourly["interval"] == "hour"
    assert [(b["runs"], b["failed"]) for b in hourly["buckets"]] == [(0, 0), (1, 0), (1, 1), (1, 0), (0, 0)]
    assert set(hourly["buckets"][0]) == {"start", "runs", "failed"}
    assert hourly["buckets"][0]["start"] == BASE.isoformat()


def test_breakdowns(seeded):
    client, _, h, _, a1, a2, _ = seeded

    def items(**params):
        r = client.get("/api/v1/metrics/breakdown", params={**WINDOW, **params}, headers=h)
        assert r.status_code == 200, r.text
        return r.json()["items"]

    by_cost = items(by="agent", sort="cost")
    assert [i["key"] for i in by_cost] == [a2, a1]
    assert [i["label"] for i in by_cost] == ["Support triage", "Support triage"]  # current agent names
    assert (by_cost[0]["runs"], by_cost[0]["requests"], by_cost[0]["cost_usd"]) == (2, 1, "0.0100000000")
    assert (by_cost[1]["runs"], by_cost[1]["failed"], by_cost[1]["requests"], by_cost[1]["cost_usd"]) == (
        2, 1, 3, "0.0030000000"
    )
    assert [i["key"] for i in items(by="agent", sort="failed", limit=1)] == [a1]

    models = items(by="model", sort="requests")
    assert [(m["key"], m["label"], m["requests"], m["runs"], m["completed"], m["failed"]) for m in models] == [
        ("test:a", "test:a", 3, 2, 2, 0),
        ("test:b", "test:b", 1, 1, 0, 1),
    ]
    assert models[0]["input_tokens"] == 130 and models[1]["unpriced_requests"] == 1


def test_tenant_isolation(seeded):
    client, _, _, h_other, a1, *_ = seeded
    s = client.get("/api/v1/metrics/summary", params=WINDOW, headers=h_other).json()["current"]
    assert (s["runs"], s["requests"], s["cost_usd"]) == (1, 1, "1.0000000000")
    # Tenant B filtering by tenant A's agent id sees only its own run with that id, never A's.
    by_agent = client.get("/api/v1/metrics/breakdown", params={**WINDOW, "by": "agent"}, headers=h_other).json()
    assert [(i["key"], i["label"], i["runs"]) for i in by_agent["items"]] == [(a1, None, 1)]


@pytest.mark.parametrize(
    ("path", "params", "detail"),
    [
        ("summary", {"from": "2026-01-01T00:00:00Z", "to": "2026-04-02T00:00:00Z"}, "the range must be at most 90 days"),
        ("summary", {"from": "2026-01-02T00:00:00Z", "to": "2026-01-01T00:00:00Z"}, "from must be before to"),
        ("timeseries", {"from": "2026-01-01T00:00:00Z", "to": "2026-01-31T00:00:00Z", "interval": "hour"}, None),
        ("timeseries", {"interval": "week"}, None),
        ("timeseries", {"metric": "nope"}, None),
        ("breakdown", {"by": "agent", "limit": 51}, None),
        ("breakdown", {"by": "tool"}, None),
        ("summary", {"kind": "nope"}, None),
        ("summary", {"from": "0001-01-01", "compare": "previous"}, "from and to must not be before 1970-01-01"),
        ("summary", {"from": "0001-01-01T00:00:00+01:00"}, "from and to must not be before 1970-01-01"),
        ("summary", {"to": "0001-01-01"}, "from and to must not be before 1970-01-01"),
        ("timeseries", {"from": "1969-12-31T23:59:59Z"}, "from and to must not be before 1970-01-01"),
        ("breakdown", {"by": "agent", "from": "0001-01-01"}, "from and to must not be before 1970-01-01"),
    ],
)
def test_bounds_are_422(pg, tenant_of, path, params, detail):
    client, _ = pg
    _, h = tenant_of()
    r = client.get(f"/api/v1/metrics/{path}", params=params, headers=h)
    assert r.status_code == 422, r.text
    if detail is not None:
        assert r.json()["detail"] == detail
    if params.get("interval") == "hour":
        assert "at most 400" in r.json()["detail"]


def test_requires_auth(pg):
    client, _ = pg
    assert client.get("/api/v1/metrics/summary").status_code == 401


def test_statement_timeout(pg, tenant_of):
    from eap_run_store import MetricsQueryTimeout, MetricsStore

    client, app = pg
    _, h = tenant_of()
    store = MetricsStore(app.state.database_engine, statement_timeout_ms=50)
    with pytest.raises(MetricsQueryTimeout):
        with store._session() as session:
            session.execute(text("SELECT pg_sleep(1)"))
    with store._session() as session:
        assert session.execute(text("SHOW statement_timeout")).scalar_one() == "50ms"

    class Slow(MetricsStore):
        def summary(self, *a, **k):
            raise MetricsQueryTimeout(50)

    app.state.metrics_store = Slow(app.state.database_engine)
    r = client.get("/api/v1/metrics/summary", headers=h)
    assert r.status_code == 503
    assert r.json()["detail"] == "Metrics query timed out; narrow the time range or the filters"


def test_run_list_drill_down(seeded):
    client, _, h, _, a1, *_ = seeded

    def ids_of(**params):
        r = client.get("/api/v1/runs", params=params, headers=h)
        assert r.status_code == 200, r.text
        return {i["run_id"] for i in r.json()["items"]}

    ids = seeded[-1]
    assert ids_of(**WINDOW, status="failed,needs_attention") == {ids["r2"], ids["r3"]}
    assert ids_of(**WINDOW, status=["failed", "needs_attention"]) == {ids["r2"], ids["r3"]}
    assert ids_of(**WINDOW, model="test:a") == {ids["r1"], ids["w1"]}
    assert ids_of(**WINDOW, provider="test") == {ids["r1"], ids["r2"], ids["w1"]}
    assert ids_of(**WINDOW, agent_id=a1) == {ids["r1"], ids["r2"]}
    assert ids_of(**{"from": (BASE + timedelta(hours=26)).isoformat(), "to": (BASE + timedelta(hours=27)).isoformat()}) == {
        ids["r3"]
    }
    assert ids["r5"] in ids_of(status="completed")  # filters are optional
    item = next(i for i in client.get("/api/v1/runs", params=WINDOW, headers=h).json()["items"] if i["run_id"] == ids["r1"])
    assert {"input_tokens", "output_tokens", "cost_usd", "started_at", "finished_at"} <= set(item)
    bad = client.get("/api/v1/runs", params={"from": WINDOW["to"], "to": WINDOW["from"]}, headers=h)
    assert bad.status_code == 422 and bad.json()["detail"] == "from must be before to"
    many = client.get("/api/v1/runs", params={"status": ",".join(f"s{i}" for i in range(11))}, headers=h)
    assert many.status_code == 422


def test_queries_use_the_tenant_indexes(pg, tenant_of):
    """With realistic statistics (5000 runs and 20000 requests of another tenant, ANALYZEd), the
    run side scans a tenant-prefixed runs index and the usage side the 0008 request indexes."""
    from sqlalchemy import select
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.orm import aliased

    from eap_run_store import MetricsQuery, RunModel
    from eap_run_store.metrics import _run_columns, _run_where, _usage_columns, _usage_from, _usage_source

    _, app = pg
    tenant, _ = tenant_of()
    noise, _ = tenant_of()
    engine = app.state.database_engine
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO runs (id, tenant_id, kind, status, created_at, updated_at, finished_at, agent_id,"
                " agent_version, model_overridden)"
                " SELECT gen_random_uuid()::text, :t, 'agent', 'completed', :b - g * interval '1 hour',"
                " :b - g * interval '1 hour', :b, gen_random_uuid()::text, 1, false FROM generate_series(1, 5000) g"
            ),
            {"t": noise, "b": BASE},
        )
        conn.execute(
            text(
                "INSERT INTO run_model_requests (tenant_id, run_id, request_key, model_id, input_tokens,"
                " output_tokens, cache_read_tokens, cache_write_tokens, usage_measured, cost_usd, created_at)"
                " SELECT r.tenant_id, r.id, 'model:' || k, 'test:' || (k % 7), 1, 1, 0, 0, true, 0.001, r.created_at"
                " FROM runs r CROSS JOIN generate_series(0, 3) k WHERE r.tenant_id = :t"
            ),
            {"t": noise},
        )
        conn.execute(text("ANALYZE runs"))
        conn.execute(text("ANALYZE run_model_requests"))

    q = MetricsQuery(tenant, BASE, BASE + timedelta(days=2))
    r = aliased(RunModel)
    runs_sql = select(*_run_columns(q, r)).select_from(r).where(*_run_where(q, r))
    req, rr, conds = _usage_from(q)
    usage_sql = select(*_usage_columns(req)).select_from(_usage_source(q, req, rr)).where(*conds)
    qm = MetricsQuery(tenant, BASE, BASE + timedelta(days=2), model="test:a")
    req_m, rr_m, conds_m = _usage_from(qm)
    model_sql = select(*_usage_columns(req_m)).select_from(_usage_source(qm, req_m, rr_m)).where(*conds_m)
    qa = MetricsQuery(tenant, BASE, BASE + timedelta(days=2), agent_id=str(uuid.uuid4()))
    req_a, rr_a, conds_a = _usage_from(qa)
    agent_sql = select(*_usage_columns(req_a)).select_from(_usage_source(qa, req_a, rr_a)).where(*conds_a)

    def plan(stmt) -> str:
        """The plan with sequential scans disabled. That makes the assertion "an index CAN serve
        every table access" deterministic, whatever rows other tests in the same schema just wrote
        (the planner still falls back to a Seq Scan when no usable index exists). Statistics are
        refreshed first so the plan reflects the current tables."""
        compiled = stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
        with engine.begin() as conn:
            conn.execute(text("ANALYZE runs, run_model_requests, execution_events"))
            conn.execute(text("SET LOCAL enable_seqscan = off"))
            return "\n".join(row[0] for row in conn.execute(text(f"EXPLAIN {compiled}")))

    runs_plan = plan(runs_sql)
    assert re.search(r"Index (Only )?Scan using ix_runs_tenant_\w+ on runs", runs_plan), runs_plan
    assert "Seq Scan" not in runs_plan, runs_plan
    # The `blocked` EXISTS (tenant_id, run_id, type, error_code) is index-served: by
    # ix_execution_events_tenant_run (tenant_id, run_id), ix_execution_events_tenant_type_ts
    # (tenant_id, type, …), the (run_id, seq) primary key or uq_run_event_key (run_id, event_key).
    # Which one wins depends on statistics (an empty tenant makes them cost the same), so assert
    # only that it is one of those, never a scan. No index is missing.
    events = [line for line in runs_plan.splitlines() if " on execution_events" in line]
    event_indexes = (
        "ix_execution_events_tenant_run|ix_execution_events_tenant_type_ts|execution_events_pkey|uq_run_event_key"
    )
    assert events and all(
        re.search(rf"(Bitmap Heap Scan|Index (Only )?Scan using ({event_indexes}))", line) for line in events
    ), runs_plan
    if any("Bitmap Heap Scan on execution_events" in line for line in events):
        assert re.search(rf"Bitmap Index Scan on ({event_indexes})", runs_plan), runs_plan
    usage_plan = plan(usage_sql)
    assert "ix_run_model_requests_tenant_" in usage_plan and "Seq Scan" not in usage_plan, usage_plan
    model_plan = plan(model_sql)
    # The queried tenant has no rows, so both tenant-prefixed request indexes cost the same here.
    assert "ix_run_model_requests_tenant_" in model_plan and "Seq Scan" not in model_plan, model_plan
    # Without an agent or kind filter the usage side never touches runs; with one, the run side of
    # the join is bounded by the window's end and is never a scan of the tenant's whole history.
    assert " runs " not in usage_plan and " runs " not in model_plan, (usage_plan, model_plan)
    agent_plan = plan(agent_sql)
    assert "ix_run_model_requests_tenant_" in agent_plan and "Seq Scan" not in agent_plan, agent_plan
    assert "created_at < '2026-03-03" in agent_plan, agent_plan


def test_usage_join_only_when_needed():
    """Review fix: the request -> run join exists only for agent/kind filters and groupings, and
    is always bounded by `runs.created_at < end`."""
    from sqlalchemy import select

    from eap_run_store import MetricsQuery
    from eap_run_store.metrics import _usage_columns, _usage_from, _usage_source

    def sql(q, need_run=False) -> str:
        req, rr, conds = _usage_from(q)
        return str(select(*_usage_columns(req)).select_from(_usage_source(q, req, rr, need_run=need_run)).where(*conds))

    end = BASE + timedelta(days=1)
    plain = MetricsQuery("t", BASE, end, model="test:a", provider="test")
    assert "JOIN runs" not in sql(plain)
    for q in (MetricsQuery("t", BASE, end, agent_id="a"), MetricsQuery("t", BASE, end, kind="workflow")):
        assert "JOIN runs" in sql(q) and "runs_1.created_at < " in sql(q)
    assert "JOIN runs" in sql(plain, need_run=True) and "runs_1.created_at < " in sql(plain, need_run=True)


def test_run_list_model_filter_runs_under_the_statement_timeout(seeded):
    """Review fix: GET /runs with model/provider runs under SET LOCAL statement_timeout (default
    5000 ms); without them no timeout is set; a timeout answers 503."""
    from sqlalchemy import event

    from eap_run_store import MetricsQueryTimeout, RunStore

    client, app, h, *_ = seeded
    ids = seeded[-1]
    engine = app.state.database_engine
    seen: list[str] = []

    def capture(conn, cursor, statement, *args):
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        tenant = app.state.test_tenants[0]
        store = RunStore(engine)
        assert {r["id"] for r in store.list_runs(tenant, model="test:b")} == {ids["r2"]}
        assert any("SET LOCAL statement_timeout = 5000" in s for s in seen)
        seen.clear()
        store.list_runs(tenant, provider="test", statement_timeout_ms=1234)
        assert any("SET LOCAL statement_timeout = 1234" in s for s in seen)
        seen.clear()
        store.list_runs(tenant, status=["failed"])
        assert not any("statement_timeout" in s for s in seen)
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    class Slow(RunStore):
        def list_runs(self, *a, **k):
            raise MetricsQueryTimeout(5000)

    real = app.state.run_stores
    app.state.run_stores = (Slow(engine), real[1])
    try:
        r = client.get("/api/v1/runs", params={"model": "test:a"}, headers=h)
    finally:
        app.state.run_stores = real
    assert r.status_code == 503
    assert r.json()["detail"] == "Run list query timed out; narrow the time range or the filters"
