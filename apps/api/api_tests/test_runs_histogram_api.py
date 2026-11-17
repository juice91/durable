"""GET /api/v1/runs/histogram: runs per hour or day, zero-filled, same filters as GET /runs."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from .run_helpers import requires_pg
from .test_runs import (  # noqa: F401 - fixtures
    _no_hang,
    deltas,
    fake_dbos,
    make_agent,
    pg,
    start,
    stream_settings,
    tenant_of,
)

pytestmark = requires_pg

BASE = datetime(2026, 11, 1, tzinfo=timezone.utc)


def _runs_at(client, app, h, agent_id: str, times: list[datetime], failed: tuple[int, ...] = ()) -> list[str]:
    ids = []
    for i, when in enumerate(times):
        run_id = str(uuid.uuid4())
        assert start(client, h, agent_id, run_id).status_code == 201
        with app.state.database_engine.begin() as conn:
            conn.execute(
                text("UPDATE runs SET created_at = :t, status = :s WHERE id = :r"),
                {"t": when, "s": "failed" if i in failed else "completed", "r": run_id},
            )
        ids.append(run_id)
    return ids


def _hist(client, h, **params):
    r = client.get("/api/v1/runs/histogram", params=params, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def test_histogram_buckets_are_zero_filled_and_hourly_under_three_days(pg, tenant_of):
    client, app = pg
    _, h = tenant_of()
    agent_id = make_agent(client, h)
    times = [BASE + timedelta(hours=1, minutes=5), BASE + timedelta(hours=1, minutes=50), BASE + timedelta(hours=30)]
    _runs_at(client, app, h, agent_id, times, failed=(1,))
    body = _hist(client, h, **{"from": BASE.isoformat(), "to": (BASE + timedelta(hours=48)).isoformat()})
    assert set(body) == {"interval", "buckets"} and body["interval"] == "hour"
    buckets = body["buckets"]
    assert len(buckets) == 48
    assert buckets[0] == {"start": BASE.isoformat(), "runs": 0, "failed": 0}
    assert buckets[1] == {"start": (BASE + timedelta(hours=1)).isoformat(), "runs": 2, "failed": 1}
    assert buckets[30]["runs"] == 1 and buckets[30]["failed"] == 0
    assert sum(b["runs"] for b in buckets) == 3
    starts = [datetime.fromisoformat(b["start"]) for b in buckets]
    assert all(b - a == timedelta(hours=1) for a, b in zip(starts, starts[1:]))


def test_histogram_day_buckets_over_three_days_and_default_range(pg, tenant_of):
    client, app = pg
    _, h = tenant_of()
    agent_id = make_agent(client, h)
    now = datetime.now(timezone.utc)
    _runs_at(client, app, h, agent_id, [BASE + timedelta(days=1, hours=3), BASE + timedelta(days=5)])
    body = _hist(client, h, **{"from": BASE.isoformat(), "to": (BASE + timedelta(days=7)).isoformat()})
    assert body["interval"] == "day" and len(body["buckets"]) == 7
    assert [b["runs"] for b in body["buckets"]] == [0, 1, 0, 0, 0, 1, 0]
    default = _hist(client, h)  # the last 7 days
    assert default["interval"] == "day" and sum(b["runs"] for b in default["buckets"]) == 0
    assert datetime.fromisoformat(default["buckets"][-1]["start"]).date() == now.date()
    exactly_three_days = _hist(client, h, **{"from": BASE.isoformat(), "to": (BASE + timedelta(hours=72)).isoformat()})
    assert exactly_three_days["interval"] == "day"
    just_under = _hist(client, h, **{"from": BASE.isoformat(), "to": (BASE + timedelta(hours=71)).isoformat()})
    assert just_under["interval"] == "hour"


def test_histogram_takes_the_runs_filters_and_is_tenant_scoped(pg, tenant_of):
    client, app = pg
    _, h = tenant_of()
    _, other = tenant_of()
    agent_a, agent_b = make_agent(client, h), make_agent(client, h)
    window = {"from": BASE.isoformat(), "to": (BASE + timedelta(hours=6)).isoformat()}
    _runs_at(client, app, h, agent_a, [BASE + timedelta(hours=1)], failed=(0,))
    _runs_at(client, app, h, agent_b, [BASE + timedelta(hours=2)])
    total = lambda body: sum(b["runs"] for b in body["buckets"])  # noqa: E731
    assert total(_hist(client, h, **window)) == 2
    assert total(_hist(client, h, agent_id=agent_a, **window)) == 1
    assert total(_hist(client, h, status="failed", **window)) == 1
    assert total(_hist(client, h, status="completed,failed", **window)) == 2
    assert total(_hist(client, h, kind="tool", **window)) == 0
    assert total(_hist(client, other, **window)) == 0


def test_histogram_rejects_a_bad_range(pg, tenant_of):
    client, _ = pg
    _, h = tenant_of()
    same = BASE.isoformat()
    assert client.get("/api/v1/runs/histogram", params={"from": same, "to": same}, headers=h).status_code == 422
    huge = {"from": BASE.isoformat(), "to": (BASE + timedelta(days=800)).isoformat()}
    assert client.get("/api/v1/runs/histogram", params=huge, headers=h).status_code == 422


def test_histogram_filters_by_q_and_metadata(pg, tenant_of):
    client, app = pg
    _, h = tenant_of()
    agent_id = make_agent(client, h)
    window = {"from": BASE.isoformat(), "to": (BASE + timedelta(hours=6)).isoformat()}
    ids = _runs_at(client, app, h, agent_id, [BASE + timedelta(hours=1), BASE + timedelta(hours=2)])
    with app.state.database_engine.begin() as conn:
        conn.execute(text("UPDATE runs SET input = 'printer on fire' WHERE id = :r"), {"r": ids[0]})
        conn.execute(text("UPDATE runs SET input = 'billing question', request_context = CAST(:c AS jsonb) WHERE id = :r"),
                     {"r": ids[1], "c": '{"team": "ops"}'})
    total = lambda body: sum(b["runs"] for b in body["buckets"])  # noqa: E731
    assert total(_hist(client, h, q="PRINTER", **window)) == 1
    assert total(_hist(client, h, q="nothing like it", **window)) == 0
    assert total(_hist(client, h, metadata="team=ops", **window)) == 1
    assert total(_hist(client, h, metadata="team=dev", **window)) == 0
    assert client.get("/api/v1/runs/histogram", params={"metadata": "no-equals", **window}, headers=h).status_code == 422
    same = client.get("/api/v1/runs", params={"metadata": "team=ops"}, headers=h).json()["items"]
    assert [i["run_id"] for i in same] == [ids[1]]  # GET /runs reads the same filter
