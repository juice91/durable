"""POST /api/v1/runs/{run_id}/score: enqueue `eap.score_run` for a finished run (202), else 404/409/422."""

from __future__ import annotations

import uuid

from sqlalchemy import text

from .run_helpers import requires_pg
from .test_conversations import finish
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
API = "/api/v1"
CONTAINS = {"kind": "contains", "value": "ticket", "case_sensitive": False}


def _scorer(client, h, name: str) -> str:
    r = client.post(f"{API}/scorers", json={"name": name, "evaluator": CONTAINS}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _drop(app, tenant: str) -> None:
    with app.state.database_engine.begin() as conn:
        conn.execute(text("DELETE FROM scorers WHERE tenant_id = :t"), {"t": tenant})


def _run(client, app, h, status: str | None) -> str:
    agent_id = make_agent(client, h)
    run_id = str(uuid.uuid4())
    assert start(client, h, agent_id, run_id).status_code == 201
    if status is not None:
        finish(app, run_id, status, output="Created ticket TCK-1.")
    return run_id


def test_score_on_demand_enqueues_and_rejects_unfinished_runs(pg, tenant_of, fake_dbos):
    client, app = pg
    tenant, h = tenant_of()
    try:
        s1, s2 = _scorer(client, h, "one"), _scorer(client, h, "two")
        done = _run(client, app, h, "completed")
        queued = len(fake_dbos.calls)
        r = client.post(f"{API}/runs/{done}/score", json={"scorer_ids": [s1, s2]}, headers=h)
        assert r.status_code == 202, r.text
        assert r.json() == {"run_id": done, "scorer_ids": [s1, s2]}
        [(options, args)] = fake_dbos.calls[queued:]
        assert options["workflow_name"] == "eap.score_run" and options["queue_name"] == "eap-agent-runs"
        assert options["workflow_id"] != done and done in options["workflow_id"]  # never the run's own workflow
        assert args == (done, tenant, [s1, s2])

        failed = _run(client, app, h, "failed")  # a finished run, however it ended
        assert client.post(f"{API}/runs/{failed}/score", json={"scorer_ids": [s1]}, headers=h).status_code == 202

        running = _run(client, app, h, None)
        before = len(fake_dbos.calls)
        r = client.post(f"{API}/runs/{running}/score", json={"scorer_ids": [s1]}, headers=h)
        assert r.status_code == 409 and r.json()["detail"] == "run_not_finished"
        assert len(fake_dbos.calls) == before
    finally:
        _drop(app, tenant)


def test_score_on_demand_validates_the_request_and_the_tenant(pg, tenant_of, fake_dbos):
    client, app = pg
    tenant, h = tenant_of()
    other, oh = tenant_of()
    try:
        s1 = _scorer(client, h, "one")
        foreign = _scorer(client, oh, "theirs")
        done = _run(client, app, h, "completed")
        url = f"{API}/runs/{done}/score"
        before = len(fake_dbos.calls)
        assert client.post(url, json={"scorer_ids": []}, headers=h).status_code == 422
        nine = [str(uuid.uuid4()) for _ in range(9)]
        assert client.post(url, json={"scorer_ids": nine}, headers=h).status_code == 422
        assert client.post(url, json={"scorer_ids": ["nope"]}, headers=h).status_code == 422
        assert client.post(url, json={"scorer_ids": [s1], "x": 1}, headers=h).status_code == 422
        assert client.post(url, json={"scorer_ids": [str(uuid.uuid4())]}, headers=h).status_code == 404
        assert client.post(url, json={"scorer_ids": [foreign]}, headers=h).status_code == 404  # another tenant's
        assert client.post(url, json={"scorer_ids": [s1]}, headers=oh).status_code == 404  # another tenant's run
        assert client.post(f"{API}/runs/{uuid.uuid4()}/score", json={"scorer_ids": [s1]}, headers=h).status_code == 404
        assert len(fake_dbos.calls) == before
        r = client.post(url, json={"scorer_ids": [s1, s1]}, headers=h)  # a repeated id counts once
        assert r.status_code == 202 and r.json()["scorer_ids"] == [s1]
    finally:
        _drop(app, tenant)
        _drop(app, other)
