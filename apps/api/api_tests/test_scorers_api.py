"""Reusable scorers (W2.3): CRUD, validation, attachment to agents, tenant isolation."""

from __future__ import annotations

import uuid

from sqlalchemy import text

from .test_evals_api import API, _agent, api, fake, pg  # noqa: F401

CONTAINS = {"kind": "contains", "value": "hello", "case_sensitive": False}


def _create(client, h, name="polite", evaluator=None):
    r = client.post(f"{API}/scorers", json={"name": name, "evaluator": evaluator or CONTAINS}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


def _drop(app, tenant):
    with app.state.database_engine.begin() as conn:
        conn.execute(text("DELETE FROM scorers WHERE tenant_id = :t"), {"t": tenant})


@pg
def test_scorer_crud_and_attachment(api):
    client, app, tenant_of = api
    tenant, h = tenant_of()
    try:
        scorer = _create(client, h)
        assert scorer["evaluator"]["kind"] == "contains" and scorer["agent_ids"] == []
        assert client.post(f"{API}/scorers", json={"name": "polite", "evaluator": CONTAINS}, headers=h).status_code == 409

        r = client.patch(f"{API}/scorers/{scorer['id']}", json={"description": "d", "name": "polite2"}, headers=h)
        assert r.status_code == 200 and r.json()["name"] == "polite2" and r.json()["description"] == "d"

        agent = _agent(client, h)
        r = client.put(f"{API}/agents/{agent}/scorers/{scorer['id']}", json={"sampling_rate": "0.5"}, headers=h)
        assert r.status_code == 200, r.text
        [item] = r.json()["items"]
        assert (item["id"], item["sampling_rate"]) == (scorer["id"], 0.5)
        assert client.get(f"{API}/scorers/{scorer['id']}", headers=h).json()["agent_ids"] == [agent]
        assert [s["id"] for s in client.get(f"{API}/agents/{agent}/scorers", headers=h).json()["items"]] == [scorer["id"]]

        assert client.delete(f"{API}/agents/{agent}/scorers/{scorer['id']}", headers=h).status_code == 204
        assert client.get(f"{API}/agents/{agent}/scorers", headers=h).json()["items"] == []

        client.put(f"{API}/agents/{agent}/scorers/{scorer['id']}", json={}, headers=h)
        assert client.delete(f"{API}/scorers/{scorer['id']}", headers=h).status_code == 204
        assert client.get(f"{API}/agents/{agent}/scorers", headers=h).json()["items"] == []
        assert client.get(f"{API}/scorers/{scorer['id']}", headers=h).status_code == 404
    finally:
        _drop(app, tenant)


@pg
def test_scorer_validation(api):
    client, app, tenant_of = api
    tenant, h = tenant_of()
    try:
        def post(evaluator):
            return client.post(f"{API}/scorers", json={"name": "x", "evaluator": evaluator}, headers=h)

        assert post({"kind": "llm_judge"}).status_code == 422
        assert post({"kind": "equals_expected"}).status_code == 422
        assert post({"kind": "max_duration", "seconds": 1}).status_code == 422
        r = post({"kind": "contains"})
        assert r.status_code == 422 and "value is required" in r.json()["detail"]
        r = post({"kind": "regex", "pattern": "(?P<a>"})
        assert r.status_code == 422 and "(?P<a>" not in r.json()["detail"]
        assert post({"kind": "regex", "pattern": "^ok$"}).status_code == 201
        r = post({"kind": "regex", "pattern": "zq(", "negate": True})
        assert r.status_code == 422 and "zq(" not in r.json()["detail"]
        r = post({"kind": "regex", "pattern": "a", "field": "a..b"})
        assert r.status_code == 422 and "evaluator.field" in r.json()["detail"]
        r = client.post(f"{API}/scorers", headers=h, json={"name": "no-url", "evaluator": {
            "kind": "regex", "pattern": "https?://", "negate": True, "field": "post"}})
        assert r.status_code == 201
        stored = r.json()["evaluator"]
        assert stored["negate"] is True and stored["field"] == "post"
        scorer = _create(client, h, name="y")
        agent = _agent(client, h)
        for rate in ("0", "1.5"):
            r = client.put(f"{API}/agents/{agent}/scorers/{scorer['id']}", json={"sampling_rate": rate}, headers=h)
            assert r.status_code == 422
    finally:
        _drop(app, tenant)


@pg
def test_scorers_are_tenant_scoped(api):
    client, app, tenant_of = api
    t1, h1 = tenant_of("alice")
    t2, h2 = tenant_of("bob")
    try:
        scorer = _create(client, h1)
        agent1, agent2 = _agent(client, h1), _agent(client, h2)
        assert client.get(f"{API}/scorers", headers=h2).json()["items"] == []
        for method, url, kw in (
            ("get", f"/scorers/{scorer['id']}", {}),
            ("patch", f"/scorers/{scorer['id']}", {"json": {"name": "z"}}),
            ("delete", f"/scorers/{scorer['id']}", {}),
            ("put", f"/agents/{agent2}/scorers/{scorer['id']}", {"json": {}}),
            ("put", f"/agents/{agent1}/scorers/{scorer['id']}", {"json": {}}),
            ("get", f"/agents/{agent1}/scorers", {}),
            ("delete", f"/agents/{agent1}/scorers/{scorer['id']}", {}),
            ("get", f"/scorers/{uuid.uuid4()}", {}),
        ):
            r = getattr(client, method)(f"{API}{url}", headers=h2, **kw)
            assert r.status_code == 404, (method, url, r.status_code)
        assert client.get(f"{API}/scorers/{scorer['id']}", headers=h1).status_code == 200
        # The same name is free in another tenant.
        assert client.post(f"{API}/scorers", json={"name": "polite", "evaluator": CONTAINS}, headers=h2).status_code == 201
    finally:
        _drop(app, t1)
        _drop(app, t2)
