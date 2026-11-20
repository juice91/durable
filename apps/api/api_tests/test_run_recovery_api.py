"""Recovered runs: `recovery_attempts` / `recovered` on the run JSON and `recovered_runs` in the Metrics summary.

The DBOS system database is replaced by a fake client inside the real DBOSRunLauncher; its `list_workflows`
answers like DBOS's WorkflowStatus rows (workflow_id, recovery_attempts) and records every call."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import text

from .run_helpers import requires_pg
from .test_metrics_api import _run
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

BASE = datetime(2026, 3, 1, tzinfo=timezone.utc)


def _install(fake_dbos, attempts: dict[str, int]):
    calls: list[dict] = []

    def list_workflows(**kw):
        calls.append(kw)
        ids = kw.get("workflow_ids")
        rows = [SimpleNamespace(workflow_id=k, recovery_attempts=v) for k, v in attempts.items()]
        return [r for r in rows if ids is None or r.workflow_id in ids]

    fake_dbos.list_workflows = list_workflows
    return calls


def _start(client, h, agent_id):
    run_id = str(uuid.uuid4())
    assert start(client, h, agent_id, run_id).status_code == 201
    return run_id


def test_run_json_has_recovery_fields(pg, tenant_of, fake_dbos):
    client, _ = pg
    _, h = tenant_of()
    agent_id = make_agent(client, h)
    plain, resumed, unknown = (_start(client, h, agent_id) for _ in range(3))
    _install(fake_dbos, {plain: 1, resumed: 3})  # attempts=1 means never recovered; attempts=3 means 2 restarts
    for run_id, attempts in ((plain, 1), (resumed, 3), (unknown, 0)):
        body = client.get(f"/api/v1/runs/{run_id}", headers=h).json()
        restarts = max(attempts - 1, 0)
        assert body["recovery_attempts"] == attempts
        assert body["recovered"] == (attempts > 1)
        assert body["restarts"] == restarts


def test_listing_asks_dbos_once_per_page(pg, tenant_of, fake_dbos):
    client, _ = pg
    _, h = tenant_of()
    agent_id = make_agent(client, h)
    ids = [_start(client, h, agent_id) for _ in range(4)]
    calls = _install(fake_dbos, {ids[1]: 2})  # 2 means 1 restart, so recovered
    items = {i["run_id"]: i for i in client.get("/api/v1/runs", headers=h).json()["items"]}
    assert len(calls) == 1 and set(calls[0]["workflow_ids"]) == set(ids)
    assert items[ids[1]]["recovered"] is True and items[ids[1]]["recovery_attempts"] == 2 and items[ids[1]]["restarts"] == 1
    assert all(not items[i]["recovered"] for i in ids if i != ids[1])


def test_run_pages_survive_an_unreachable_dbos(pg, tenant_of, fake_dbos):
    client, _ = pg
    _, h = tenant_of()
    run_id = _start(client, h, make_agent(client, h))

    def boom(**_):
        raise ConnectionError("system database unreachable")

    fake_dbos.list_workflows = boom
    body = client.get(f"/api/v1/runs/{run_id}", headers=h).json()
    assert (body["recovery_attempts"], body["recovered"], body["restarts"]) == (0, False, 0)


def test_metrics_counts_recovered_runs_in_range_and_tenant(pg, tenant_of, fake_dbos):
    client, app = pg
    tenant, h = tenant_of()
    other, h_other = tenant_of()
    a = make_agent(client, h)
    hr = lambda n: BASE + timedelta(hours=n)  # noqa: E731
    with app.state.database_engine.begin() as conn:
        inside = _run(conn, tenant, agent_id=a, created=hr(1), latency_s=1)
        plain = _run(conn, tenant, agent_id=a, created=hr(2), latency_s=1)  # attempts=1 => not recovered
        outside = _run(conn, tenant, agent_id=a, created=hr(-30))  # last event before the range
        theirs = _run(conn, other, agent_id=a, created=hr(3), latency_s=1)
        child = _run(conn, tenant, agent_id=a, created=hr(4), parent=inside)
        conn.execute(text("UPDATE runs SET updated_at = :t WHERE id = :r"), {"t": hr(-29), "r": outside})
    calls = _install(
        fake_dbos,
        {inside: 2, plain: 1, outside: 4, theirs: 5, f"{inside}:seg:0": 2, child: 2},  # attempts=2 means 1 restart
    )
    params = {"from": BASE.isoformat(), "to": (BASE + timedelta(days=2)).isoformat(), "compare": "previous"}
    body = client.get("/api/v1/metrics/summary", params=params, headers=h).json()
    assert body["current"]["recovered_runs"] == 1  # only `inside` (attempts=2 => recovered), not plain (attempts=1)
    assert body["previous"]["recovered_runs"] == 1  # `outside`: its last event is in the previous period
    assert client.get("/api/v1/metrics/summary", params=params, headers=h_other).json()["current"]["recovered_runs"] == 1
    assert all("workflow_ids" not in c for c in calls)
