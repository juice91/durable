"""GET /api/v1/runs: the tenant's runs newest first, created_at cursor, agent/status filters."""

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

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _start_runs(client, app, h, agent_id: str, n: int, offset: int = 0) -> list[str]:
    """Start n runs with distinct, increasing created_at (BASE + offset + i minutes); oldest first."""
    ids = []
    for i in range(n):
        run_id = str(uuid.uuid4())
        assert start(client, h, agent_id, run_id).status_code == 201
        with app.state.database_engine.begin() as conn:
            conn.execute(
                text("UPDATE runs SET created_at = :t WHERE id = :r"),
                {"t": BASE + timedelta(minutes=offset + i), "r": run_id},
            )
        ids.append(run_id)
    return ids


def _list(client, h, **params):
    r = client.get("/api/v1/runs", params=params, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def test_newest_first_with_full_run_json(pg, tenant_of):
    client, app = pg
    _, h = tenant_of()
    agent_id = make_agent(client, h)
    ids = _start_runs(client, app, h, agent_id, 3)
    body = _list(client, h)
    assert [i["run_id"] for i in body["items"]] == ids[::-1]
    assert body["next_before"] is None
    item = body["items"][0]
    assert item == client.get(f"/api/v1/runs/{ids[-1]}", headers=h).json()
    assert {"trace_id", "conversation_id", "turn", "status", "created_at"} <= set(item)


def test_cursor_pages_through_everything_once(pg, tenant_of):
    client, app = pg
    _, h = tenant_of()
    agent_id = make_agent(client, h)
    ids = _start_runs(client, app, h, agent_id, 5)
    seen, before, pages = [], None, 0
    while True:
        params = {"limit": 2} | ({"before": before} if before else {})
        body = _list(client, h, **params)
        seen += [i["run_id"] for i in body["items"]]
        pages += 1
        before = body["next_before"]
        if before is None:
            break
        assert before == body["items"][-1]["created_at"]
    assert seen == ids[::-1]
    assert pages == 3


def test_filters_by_agent_and_status(pg, tenant_of):
    client, app = pg
    tenant, h = tenant_of()
    a1, a2 = make_agent(client, h), make_agent(client, h)
    r1 = _start_runs(client, app, h, a1, 2)
    r2 = _start_runs(client, app, h, a2, 1, offset=10)
    with app.state.database_engine.begin() as conn:
        conn.execute(text("UPDATE runs SET status = 'completed' WHERE id = :r"), {"r": r1[0]})
    assert [i["run_id"] for i in _list(client, h, agent_id=a1)["items"]] == r1[::-1]
    assert [i["run_id"] for i in _list(client, h, agent_id=a2)["items"]] == r2
    assert [i["run_id"] for i in _list(client, h, status="completed")["items"]] == [r1[0]]
    assert [i["run_id"] for i in _list(client, h, status="queued", agent_id=a1)["items"]] == [r1[1]]
    assert _list(client, h, agent_id=str(uuid.uuid4()))["items"] == []


def test_other_tenants_runs_never_appear(pg, tenant_of):
    client, app = pg
    _, h1 = tenant_of()
    _, h2 = tenant_of()
    mine = _start_runs(client, app, h1, make_agent(client, h1), 2)
    theirs_agent = make_agent(client, h2)
    theirs = _start_runs(client, app, h2, theirs_agent, 2)
    assert {i["run_id"] for i in _list(client, h1, limit=200)["items"]} == set(mine)
    assert {i["run_id"] for i in _list(client, h2, limit=200)["items"]} == set(theirs)
    # Filtering by another tenant's agent id finds nothing, not their runs.
    assert _list(client, h1, agent_id=theirs_agent)["items"] == []


def test_limit_bounds(pg, tenant_of):
    client, app = pg
    _, h = tenant_of()
    _start_runs(client, app, h, make_agent(client, h), 2)
    for bad in (0, -1, 201):
        assert client.get("/api/v1/runs", params={"limit": bad}, headers=h).status_code == 422
    assert len(_list(client, h, limit=1)["items"]) == 1
    assert _list(client, h, limit=1)["next_before"] is not None
    assert len(_list(client, h, limit=200)["items"]) == 2
    assert client.get("/api/v1/runs", params={"before": "not-a-date"}, headers=h).status_code == 422


def test_requires_auth(pg):
    client, _ = pg
    assert client.get("/api/v1/runs").status_code == 401
