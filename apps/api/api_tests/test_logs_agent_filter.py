"""GET /logs?agent_id= (follow-up 2): one agent's records, tenant-scoped, via the agent's run ids."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

from eap_api.routes import logs as logs_route
from eap_api.routes.logs import build_logql
from eap_run_store.store import RunModel

from .conftest import create_agent, get_auth_header, publish_version
from .test_logs import SELECTOR, _entry, fake_loki  # noqa: F401 - fixture

NOW_NS = int(datetime.now(UTC).timestamp() * 1_000_000_000)


def _agent_with_runs(client, tenant: str, runs: int) -> tuple[str, list[str]]:
    agent_id = create_agent(client, tenant, "alice")
    publish_version(client, agent_id, tenant, "alice")
    run_store, _ = client.app.state.run_stores
    ids = []
    for _ in range(runs):
        run_id = uuid.uuid4()
        run_store.create_run(tenant, uuid.UUID(agent_id), 1, "hi", run_id)
        ids.append(str(run_id))
    return agent_id, ids


def _get(client, tenant, **params):
    return client.get("/api/v1/logs", params=params, headers=get_auth_header(tenant, "alice"))


def test_logql_run_ids_become_one_regex_filter_and_must_be_uuids():
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    assert build_logql(tenant_id="acme", run_ids=[a, b]) == f'{SELECTOR} | eap_tenant_id="acme" | eap_run_id=~"({a}|{b})"'
    for bad in ([], ["r-1"], [a.upper()], [f'{a}|.*']):
        with pytest.raises(ValueError):
            build_logql(tenant_id="acme", run_ids=bad)


def test_agent_filter_searches_only_that_agents_runs(client, tenant, fake_loki):
    agent, runs = _agent_with_runs(client, tenant, 2)
    _other, other_runs = _agent_with_runs(client, tenant, 1)
    fake_loki.entries = [
        _entry(NOW_NS - 1, "mine", tenant, runs[0]),
        _entry(NOW_NS - 2, "other agent", tenant, other_runs[0]),  # Loki misbehaving: dropped anyway
        _entry(NOW_NS - 3, "no run", tenant),
    ]
    response = _get(client, tenant, agent_id=agent)
    assert response.status_code == 200
    body = response.json()
    assert [i["message"] for i in body["items"]] == ["mine"]
    assert (body["agent_runs"], body["agent_runs_limited"]) == (2, False)
    query = fake_loki.calls[0]["query"]
    assert f'eap_tenant_id="{tenant}"' in query
    assert all(r in query for r in runs) and other_runs[0] not in query


def test_unknown_or_other_tenants_agent_is_404_and_bad_uuid_is_422(client, tenant, fake_loki):
    agent, _ = _agent_with_runs(client, tenant, 1)
    other_tenant = f"tb{uuid.uuid4().hex[:8]}"
    response = client.get("/api/v1/logs", params={"agent_id": agent}, headers=get_auth_header(other_tenant, "bob"))
    assert (response.status_code, response.json()["detail"]) == (404, "Agent not found")
    assert _get(client, tenant, agent_id=str(uuid.uuid4())).status_code == 404
    assert _get(client, tenant, agent_id="not-a-uuid").status_code == 422
    assert fake_loki.calls == []


def test_agent_without_runs_in_the_window_answers_empty_without_loki(client, tenant, fake_loki):
    agent = create_agent(client, tenant, "alice")
    body = _get(client, tenant, agent_id=agent).json()
    assert body["items"] == [] and body["agent_runs"] == 0 and body["agent_runs_limited"] is False
    assert {"from", "to", "limit"} <= set(body)
    assert fake_loki.calls == []


def test_runs_finished_before_or_created_after_the_window_are_left_out(client, tenant, fake_loki):
    agent, runs = _agent_with_runs(client, tenant, 3)
    run_store, _ = client.app.state.run_stores
    long_ago = datetime.now(UTC) - timedelta(days=3)
    with run_store.Session() as session, session.begin():
        session.execute(
            update(RunModel).where(RunModel.id == runs[0]).values(created_at=long_ago, finished_at=long_ago + timedelta(minutes=1))
        )
    fake_loki.entries = [_entry(NOW_NS - 1, "x", tenant, runs[1])]
    body = _get(client, tenant, agent_id=agent).json()  # default window: the last hour
    assert body["agent_runs"] == 2
    assert runs[0] not in fake_loki.calls[0]["query"]
    # A window that ended before the runs were created matches none of them.
    old = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    older = (datetime.now(UTC) - timedelta(days=2, hours=1)).isoformat()
    body = _get(client, tenant, agent_id=agent, **{"from": older, "to": old}).json()
    assert body["agent_runs"] == 0 and len(fake_loki.calls) == 1


def test_more_runs_than_the_cap_are_limited_to_the_newest(client, tenant, fake_loki, monkeypatch):
    monkeypatch.setattr(logs_route, "MAX_AGENT_RUNS", 2)
    agent, runs = _agent_with_runs(client, tenant, 3)
    body = _get(client, tenant, agent_id=agent).json()
    assert (body["agent_runs"], body["agent_runs_limited"]) == (2, True)
    query = fake_loki.calls[0]["query"]
    assert sum(r in query for r in runs) == 2


def test_agent_and_run_together(client, tenant, fake_loki):
    agent, runs = _agent_with_runs(client, tenant, 1)
    _other, other_runs = _agent_with_runs(client, tenant, 1)
    # A run of another agent matches nothing (Loki is not asked).
    body = _get(client, tenant, agent_id=agent, run_id=other_runs[0]).json()
    assert body["items"] == [] and body["agent_runs"] == 0 and fake_loki.calls == []
    # The agent's own run: the plain run filter.
    fake_loki.entries = [_entry(NOW_NS - 1, "ok", tenant, runs[0])]
    body = _get(client, tenant, agent_id=agent, run_id=runs[0]).json()
    assert [i["message"] for i in body["items"]] == ["ok"] and body["agent_runs"] == 1
    assert f'eap_run_id="{runs[0]}"' in fake_loki.calls[0]["query"]
