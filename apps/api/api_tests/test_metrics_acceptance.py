"""P0.6 acceptance: tenant isolation of every endpoint, previous period, zero fill, limits, sorts, no double count."""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest

from .run_helpers import requires_pg
from .test_metrics_api import BASE, WINDOW, _request, _run
from .test_runs import (  # noqa: F401 - fixtures
    _no_hang, deltas, fake_dbos, make_agent, pg, stream_settings, tenant_of,
)

pytestmark = requires_pg

M = "/api/v1/metrics"
ZERO = "0.0000000000"


def hr(n: float):
    return BASE + timedelta(hours=n)


def get(client, path, h, **params):
    r = client.get(f"{M}/{path}", params=params, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def seed_sorted(conn, tenant, x, y, z):
    """X: 3 runs (1 failed), 1 request 10/10 $0.5; Y: 2 failed runs, 4 requests 1/1 $0.004; Z: 1 run,
    2 requests 500/500 $0.1. Every sort key has a distinct order: runs X,Y,Z; failed Y,X,Z;
    requests Y,Z,X; tokens Z,X,Y; cost X,Z,Y."""
    ids = {}
    for name, agent, status, h in [("x1", x, "completed", 1), ("x2", x, "completed", 2), ("x3", x, "failed", 3),
                                   ("y1", y, "failed", 4), ("y2", y, "failed", 5), ("z1", z, "completed", 6)]:
        ids[name] = _run(conn, tenant, agent_id=agent, status=status, created=hr(h), latency_s=2)
    _request(conn, tenant, ids["x1"], "model:0", "test:m1", 10, 10, Decimal("0.5"), hr(1))
    for run, n in (("y1", 0), ("y1", 1), ("y2", 0), ("y2", 1)):
        _request(conn, tenant, ids[run], f"model:{n}", "test:m1", 1, 1, Decimal("0.004"), hr(4))
    for n in (0, 1):
        _request(conn, tenant, ids["z1"], f"model:{n}", "test:m2", 500, 500, Decimal("0.1"), hr(6))
    return ids


@pytest.fixture
def two_tenants(pg, tenant_of):
    client, app = pg
    a, ha = tenant_of()
    b, hb = tenant_of()
    x, y, z = (make_agent(client, ha) for _ in range(3))
    with app.state.database_engine.begin() as conn:
        a_ids = seed_sorted(conn, a, x, y, z)
        b_ids = {}
        for name, agent in (("x", x), ("y", y), ("z", z)):  # same agent ids, interleaved in time
            b_ids[name] = _run(conn, b, agent_id=agent, created=hr(2 + len(b_ids)), latency_s=9)
            _request(conn, b, b_ids[name], "model:0", "test:m1", 9999, 9999, Decimal("7"), hr(2 + len(b_ids)))
    return client, ha, hb, (x, y, z), a_ids, b_ids


def test_every_endpoint_counts_only_the_callers_tenant(two_tenants):
    client, ha, hb, (x, y, z), a_ids, b_ids = two_tenants
    a = get(client, "summary", ha, **WINDOW)["current"]
    assert (a["runs"], a["completed"], a["failed"], a["requests"]) == (6, 3, 3, 7)
    assert (a["input_tokens"], a["output_tokens"], a["cost_usd"]) == (1014, 1014, "0.7160000000")
    b = get(client, "summary", hb, **WINDOW)["current"]
    assert (b["runs"], b["completed"], b["requests"], b["input_tokens"], b["cost_usd"]) == (3, 3, 3, 29997, "21.0000000000")
    assert b["latency_p50_ms"] == pytest.approx(9000) and a["latency_p50_ms"] == pytest.approx(2000)
    for h, runs, cost in ((ha, 6, Decimal("0.716")), (hb, 3, Decimal(21))):
        buckets = get(client, "timeseries", h, **WINDOW, interval="hour")["buckets"]
        assert sum(k["runs"] for k in buckets) == runs and sum(Decimal(k["cost_usd"]) for k in buckets) == cost
        for by in ("agent", "model"):
            items = get(client, "breakdown", h, **WINDOW, by=by)["items"]
            assert sum(Decimal(i["cost_usd"]) for i in items) == cost
    b_agents = get(client, "breakdown", hb, **WINDOW, by="agent")["items"]
    assert {i["key"] for i in b_agents} == {x, y, z} and all(i["label"] is None for i in b_agents)  # A's agent names
    b_models = get(client, "breakdown", hb, **WINDOW, by="model")["items"]
    assert [(m["key"], m["requests"]) for m in b_models] == [("test:m1", 3)]
    listed = lambda h, **p: {i["run_id"] for i in client.get("/api/v1/runs", params={**WINDOW, **p}, headers=h).json()["items"]}  # noqa: E731
    assert listed(ha) == set(a_ids.values()) and listed(hb) == set(b_ids.values())
    assert listed(ha, model="test:m2") == {a_ids["z1"]} and listed(hb, model="test:m2") == set()
    assert listed(hb, agent_id=x) == {b_ids["x"]}


def test_breakdown_sort_orders_and_limit(two_tenants):
    client, ha, _, (x, y, z), *_ = two_tenants

    def keys(by, sort, **p):
        return [i["key"] for i in get(client, "breakdown", ha, **WINDOW, by=by, sort=sort, **p)["items"]]

    assert keys("agent", "runs") == [x, y, z]  # the default sort is runs
    assert [i["key"] for i in get(client, "breakdown", ha, **WINDOW, by="agent")["items"]] == [x, y, z]
    assert keys("agent", "failed") == [y, x, z]
    assert keys("agent", "requests") == [y, z, x]
    assert keys("agent", "tokens") == [z, x, y]
    assert keys("agent", "cost") == [x, z, y]
    assert keys("agent", "cost", limit=2) == [x, z] and keys("agent", "requests", limit=1) == [y]
    assert keys("model", "requests") == ["test:m1", "test:m2"]
    assert keys("model", "tokens") == ["test:m2", "test:m1"]
    assert keys("model", "cost") == ["test:m1", "test:m2"]
    assert keys("model", "cost", limit=1) == ["test:m1"]
    top = get(client, "breakdown", ha, **WINDOW, by="agent", sort="cost")["items"][0]
    assert (top["runs"], top["failed"], top["requests"], top["cost_usd"]) == (3, 1, 1, "0.5000000000")
    bad = client.get(f"{M}/breakdown", params={**WINDOW, "by": "agent", "sort": "latency"}, headers=ha)
    assert bad.status_code == 422


def test_previous_period_boundaries_filters_and_tenant(pg, tenant_of):
    client, app = pg
    a, ha = tenant_of()
    b, hb = tenant_of()
    x, y = make_agent(client, ha), make_agent(client, ha)
    s = timedelta(seconds=1)
    day2 = timedelta(days=2)
    with app.state.database_engine.begin() as conn:
        # Current [BASE, BASE+2d): a run at BASE counts, a run exactly at BASE+2d does not.
        c1 = _run(conn, a, agent_id=x, created=BASE, latency_s=2)
        _request(conn, a, c1, "model:0", "test:m", 5, 5, Decimal("0.1"), BASE)
        _run(conn, a, agent_id=x, created=BASE + day2 - s, latency_s=4)
        _run(conn, a, agent_id=x, created=BASE + day2, latency_s=100)
        # Previous [BASE-2d, BASE): BASE-2d counts, BASE-2d-1s does not; a run at BASE-1s counts.
        p1 = _run(conn, a, agent_id=x, status="failed", created=BASE - day2, latency_s=1)
        _request(conn, a, p1, "model:0", "test:m", 3, 3, Decimal("0.3"), BASE - day2)
        _run(conn, a, agent_id=x, created=BASE - s, latency_s=6)
        _run(conn, a, agent_id=x, created=BASE - day2 - s, latency_s=50)
        other = _run(conn, a, agent_id=y, created=BASE - day2 + timedelta(hours=1), latency_s=1)
        _request(conn, a, other, "model:0", "test:m", 77, 77, Decimal("9"), BASE - day2 + timedelta(hours=1))
        q = _run(conn, b, agent_id=x, created=BASE - timedelta(hours=1), latency_s=1)  # another tenant
        _request(conn, b, q, "model:0", "test:m", 1000, 1000, Decimal("100"), BASE - timedelta(hours=1))
    body = get(client, "summary", ha, **WINDOW, compare="previous", agent_id=x)
    cur, prev = body["current"], body["previous"]
    assert (cur["runs"], cur["completed"], cur["failed"], cur["requests"], cur["cost_usd"]) == (2, 2, 0, 1, "0.1000000000")
    assert cur["latency_p50_ms"] == pytest.approx(3000) and cur["latency_p95_ms"] == pytest.approx(3900)
    assert (prev["runs"], prev["completed"], prev["failed"], prev["requests"], prev["cost_usd"]) == (2, 1, 1, 1, "0.3000000000")
    assert prev["error_rate"] == pytest.approx(0.5) and prev["latency_p50_ms"] == pytest.approx(3500)
    assert (body["previous_from"], body["previous_to"]) == ((BASE - day2).isoformat(), BASE.isoformat())
    both = get(client, "summary", ha, **WINDOW, compare="previous")["previous"]  # the other agent joins in
    assert (both["runs"], both["requests"], both["cost_usd"]) == (3, 2, "9.3000000000")
    mine_b = get(client, "summary", hb, **WINDOW, compare="previous")
    assert (mine_b["current"]["runs"], mine_b["previous"]["runs"], mine_b["previous"]["cost_usd"]) == (0, 1, "100.0000000000")
    assert client.get(f"{M}/summary", params={**WINDOW, "compare": "next"}, headers=ha).status_code == 422


def test_an_empty_period_is_zero_filled_with_null_latency(pg, tenant_of):
    client, _ = pg
    _, h = tenant_of()
    body = get(client, "summary", h, **WINDOW, compare="previous")
    for side in ("current", "previous"):
        m = body[side]
        assert (m["runs"], m["requests"], m["input_tokens"], m["cost_usd"]) == (0, 0, 0, ZERO)
        assert m["error_rate"] is None and m["latency_p50_ms"] is None and m["latency_p95_ms"] is None
    days = get(client, "timeseries", h, **{"from": BASE.isoformat(), "to": (BASE + timedelta(days=3)).isoformat()},
               interval="day")["buckets"]
    assert [d["start"] for d in days] == [(BASE + timedelta(days=i)).isoformat() for i in range(3)]
    for d in days:
        assert (d["runs"], d["failed"], d["requests"], d["cost_usd"]) == (0, 0, 0, ZERO)
        assert d["latency_p50_ms"] is None and d["error_rate"] is None
    # An unaligned start is truncated to its hour: 00:30 .. +48h touches 49 hour buckets.
    start = BASE + timedelta(minutes=30)
    hours = get(client, "timeseries", h, **{"from": start.isoformat(), "to": (start + timedelta(hours=48)).isoformat()},
                interval="hour")["buckets"]
    assert [x["start"] for x in hours] == [(BASE + timedelta(hours=i)).isoformat() for i in range(49)]
    assert all(x["runs"] == 0 and x["cost_usd"] == ZERO for x in hours)


def _range(start, delta):
    return {"from": start.isoformat(), "to": (start + delta).isoformat()}


def test_range_and_bucket_limits(pg, tenant_of):
    client, _ = pg
    _, h = tenant_of()
    ninety, over = timedelta(days=90), timedelta(days=90, seconds=1)
    for path, extra in (("summary", {}), ("timeseries", {"interval": "day"}), ("breakdown", {"by": "agent"})):
        ok = client.get(f"{M}/{path}", params={**_range(BASE, ninety), **extra}, headers=h)
        assert ok.status_code == 200, (path, ok.text)
        bad = client.get(f"{M}/{path}", params={**_range(BASE, over), **extra}, headers=h)
        assert bad.status_code == 422 and bad.json()["detail"] == "the range must be at most 90 days", path
    assert client.get(f"{M}/summary", params={**_range(BASE, ninety), "compare": "previous"}, headers=h).status_code == 200
    assert len(get(client, "timeseries", h, **_range(BASE, ninety), interval="day")["buckets"]) == 90
    # 400 buckets pass, 401 do not; a start that is not on the hour adds a bucket.
    assert len(get(client, "timeseries", h, **_range(BASE, timedelta(hours=400)), interval="hour")["buckets"]) == 400
    for start, hours in ((BASE, 401), (BASE + timedelta(minutes=30), 400)):
        r = client.get(f"{M}/timeseries", params={**_range(start, timedelta(hours=hours)), "interval": "hour"}, headers=h)
        assert r.status_code == 422 and "at most 400" in r.json()["detail"], r.text
    assert get(client, "timeseries", h, **_range(BASE, timedelta(days=16)))["interval"] == "day"  # default interval
    assert client.get(f"{M}/timeseries", params={**_range(BASE, timedelta(days=17)), "interval": "hour"}, headers=h).status_code == 422
    inverted = client.get(f"{M}/summary", params={"from": WINDOW["to"], "to": WINDOW["from"]}, headers=h)
    assert inverted.status_code == 422 and inverted.json()["detail"] == "from must be before to"
    assert client.get(f"{M}/summary", params={**WINDOW, "agent_id": "nope"}, headers=h).status_code == 422


def test_a_workflow_and_its_child_runs_are_never_counted_twice(pg, tenant_of):
    client, app = pg
    a, h = tenant_of()
    x, y = make_agent(client, h), make_agent(client, h)
    with app.state.database_engine.begin() as conn:
        w1 = _run(conn, a, kind="workflow", created=hr(1), latency_s=10)
        c1 = _run(conn, a, agent_id=x, created=hr(1), latency_s=4, parent=w1)
        c2 = _run(conn, a, agent_id=y, created=hr(1), latency_s=4, parent=w1)
        r1 = _run(conn, a, agent_id=x, created=hr(2), latency_s=2)
        _request(conn, a, c1, "model:0", "test:m1", 100, 50, Decimal("0.01"), hr(1))
        _request(conn, a, c2, "model:0", "test:m2", 200, 100, Decimal("0.02"), hr(1))
        _request(conn, a, r1, "model:0", "test:m1", 10, 5, Decimal("0.04"), hr(2))
    total = get(client, "summary", h, **WINDOW)["current"]
    assert (total["runs"], total["completed"]) == (2, 2)  # w1 and r1: the child runs are not runs here
    assert (total["requests"], total["input_tokens"], total["output_tokens"], total["cost_usd"]) == (3, 310, 155, "0.0700000000")
    wf = get(client, "summary", h, **WINDOW, kind="workflow")["current"]
    ag = get(client, "summary", h, **WINDOW, kind="agent")["current"]
    assert (wf["runs"], wf["requests"], wf["cost_usd"]) == (1, 2, "0.0300000000")
    assert (ag["runs"], ag["requests"], ag["cost_usd"]) == (1, 1, "0.0400000000")
    assert wf["requests"] + ag["requests"] == total["requests"]
    buckets = get(client, "timeseries", h, **WINDOW, interval="hour")["buckets"]
    assert sum(b["runs"] for b in buckets) == 2 and sum(b["requests"] for b in buckets) == 3
    assert sum(b["input_tokens"] for b in buckets) == 310
    by_runs = get(client, "breakdown", h, **WINDOW, by="agent")["items"]
    assert [i["key"] for i in by_runs] == [x]  # sort=runs lists agents with top-level runs: y only ran as a step
    agents = {i["key"]: i for i in get(client, "breakdown", h, **WINDOW, by="agent", sort="cost")["items"]}
    assert (agents[x]["runs"], agents[x]["requests"], agents[x]["cost_usd"]) == (1, 2, "0.0500000000")
    assert (agents[y]["runs"], agents[y]["requests"], agents[y]["cost_usd"]) == (0, 1, "0.0200000000")
    assert sum(i["requests"] for i in agents.values()) == 3
    models = get(client, "breakdown", h, **WINDOW, by="model")["items"]
    assert sum(m["requests"] for m in models) == 3 and sum(Decimal(m["cost_usd"]) for m in models) == Decimal("0.07")
    listed = client.get("/api/v1/runs", params=WINDOW, headers=h).json()["items"]
    assert {i["run_id"] for i in listed} == {w1, r1}  # the list hides the workflow's child runs too
