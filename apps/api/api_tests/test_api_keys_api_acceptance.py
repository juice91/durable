"""AGUI.3 acceptance: /api/v1/api-keys and agui_principal through HTTP. One 401 answer for every bad
key, scopes and allowlists, tenant isolation, immediate revoke, management routes dev-only (Q2),
and the plaintext only in the create response."""

from __future__ import annotations

import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import APIRouter
from sqlalchemy import text

from eap_api.auth import AgUiPrincipalDep, require_agui_agent, require_agui_workflow
from eap_run_store import api_keys as store_mod

from .conftest import create_agent, get_auth_header
from .test_workflows_smoke import _spec

KEYS = "/api/v1/api-keys"


def _probes() -> APIRouter:
    router = APIRouter()

    @router.get("/api/v1/ag-ui/_probe/agents/{agent_id}")
    async def agent(agent_id: str, principal: AgUiPrincipalDep) -> dict:
        require_agui_agent(principal, agent_id)
        return {"tenant_id": principal.tenant_id, "kind": principal.kind}

    @router.get("/api/v1/ag-ui/_probe/workflows/{workflow_id}")
    async def workflow(workflow_id: str, principal: AgUiPrincipalDep) -> dict:
        require_agui_workflow(principal, workflow_id)
        return {"tenant_id": principal.tenant_id, "kind": principal.kind}

    return router


@pytest.fixture
def c(client):
    client.app.include_router(_probes())
    return client


def _dev(tenant, user="alice"):
    return get_auth_header(tenant, user)


def _bearer(key):
    return {"Authorization": f"Bearer {key}"}


def _create(c, tenant, **body):
    r = c.post(KEYS, json={"name": "k", **body}, headers=_dev(tenant))
    assert r.status_code == 201, r.text
    return r.json()


def _agent_url(agent_id):
    return f"/api/v1/ag-ui/_probe/agents/{agent_id}"


def _wf_url(workflow_id):
    return f"/api/v1/ag-ui/_probe/workflows/{workflow_id}"


def _workflow(c, tenant, agent_id):
    r = c.post("/api/v1/workflows", json={"spec": _spec(agent_id)}, headers=_dev(tenant))
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _sql(c, sql, **params):
    with c.app.state.database_engine.begin() as conn:
        conn.execute(text(sql), params)


def _secret(key):
    return key.split("_", 2)[2]


def test_plaintext_appears_only_in_the_create_response_and_not_in_the_database(c, tenant):
    body = _create(c, tenant)
    key, secret = body["key"], _secret(body["key"])
    assert body["key"].startswith(body["prefix"]) and len(secret) == 43
    with c.app.state.database_engine.connect() as conn:
        rows = conn.execute(text("SELECT * FROM api_keys WHERE tenant_id = :t"), {"t": tenant}).mappings().all()
    assert key not in repr(rows) and secret not in repr(rows)
    stored_hash = rows[0]["secret_hash"]
    seen = [
        c.get(KEYS, headers=_dev(tenant)),
        c.get(KEYS, headers=_dev(tenant)),
        c.delete(f"{KEYS}/{body['id']}", headers=_dev(tenant)),
        c.get(KEYS, headers=_dev(tenant)),
    ]
    for r in seen:
        assert key not in r.text and secret not in r.text and stored_hash not in r.text
        assert "secret" not in r.text and '"key"' not in r.text and "secret_hash" not in r.text
    assert c.get(KEYS, headers=_dev(tenant)).json()["items"][0]["status"] == "revoked"


def test_no_log_record_or_error_body_contains_the_key(c, tenant, caplog):
    caplog.set_level(logging.DEBUG)
    body = _create(c, tenant)
    key, secret = body["key"], _secret(body["key"])
    agent_id = create_agent(c, tenant, "alice")
    bodies = [
        c.get(_agent_url(agent_id), headers=_bearer(key)),
        c.get(_agent_url(agent_id), headers=_bearer(key[:-1] + ("A" if key[-1] != "A" else "B"))),
        c.get(KEYS, headers=_bearer(key)),
        c.post(KEYS, json={"name": "x"}, headers=_bearer(key)),
        c.delete(f"{KEYS}/{body['id']}", headers=_dev(tenant)),
        c.get(_agent_url(agent_id), headers=_bearer(key)),
        c.post(KEYS, json={"name": "", "bogus": key}, headers=_dev(tenant)),
    ]
    for r in bodies:
        assert key not in r.text and secret not in r.text
        assert key not in repr(r.headers) and secret not in repr(r.headers)
    assert key not in caplog.text and secret not in caplog.text


def test_every_bad_key_gets_the_identical_401(c, tenant):
    agent_id = create_agent(c, tenant, "alice")
    good = _create(c, tenant)
    key, secret = good["key"], _secret(good["key"])
    revoked = _create(c, tenant)
    assert c.delete(f"{KEYS}/{revoked['id']}", headers=_dev(tenant)).status_code == 204
    expired = _create(c, tenant, expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
    _sql(c, "UPDATE api_keys SET expires_at = now() - interval '1 hour' WHERE id = :i", i=expired["id"])
    bad = {
        "wrong_secret": f"pos_{good['id']}_{'A' * 43}",
        "wrong_id": f"pos_{'e' * 24}_{secret}",
        "malformed": "pos_nonsense",
        "prefix_only": "pos_",
        "short_secret": f"pos_{good['id']}_{secret[:10]}",
        "revoked": revoked["key"],
        "expired": expired["key"],
    }
    seen = {}
    for name, token in bad.items():
        r = c.get(_agent_url(agent_id), headers=_bearer(token))
        seen[name] = (r.status_code, r.json(), r.headers.get("www-authenticate"))
    assert all(v[0] == 401 for v in seen.values())
    assert len({repr(v) for v in seen.values()}) == 1, seen
    assert next(iter(seen.values()))[1] == {"detail": "Invalid API key"}
    assert c.get(_agent_url(agent_id), headers=_bearer(key)).status_code == 200


def test_http_bad_key_paths_each_do_one_constant_time_compare(c, tenant, monkeypatch):
    agent_id = create_agent(c, tenant, "alice")
    good = _create(c, tenant)
    revoked = _create(c, tenant)
    c.delete(f"{KEYS}/{revoked['id']}", headers=_dev(tenant))
    calls = []
    real = store_mod.hmac.compare_digest
    monkeypatch.setattr(store_mod.hmac, "compare_digest", lambda a, b: calls.append(1) or real(a, b))
    tokens = [good["key"], f"pos_{good['id']}_{'A' * 43}", f"pos_{'e' * 24}_{'A' * 43}", "pos_x", revoked["key"]]
    for t in tokens:
        calls.clear()
        c.get(_agent_url(agent_id), headers=_bearer(t))
        assert len(calls) == 1, t


def test_scopes_a_key_without_agui_run_is_403_with_a_fixed_message(c, tenant):
    agent_id = create_agent(c, tenant, "alice")
    k = _create(c, tenant)
    _sql(c, "UPDATE api_keys SET scopes = '[\"other\"]'::jsonb WHERE id = :i", i=k["id"])
    r = c.get(_agent_url(agent_id), headers=_bearer(k["key"]))
    assert r.status_code == 403 and r.json() == {"detail": "API key lacks the agui:run scope"}
    assert k["key"] not in r.text


def test_agent_allowlist_other_agent_is_the_same_404_as_a_missing_agent(c, tenant):
    mine, other = create_agent(c, tenant, "alice"), create_agent(c, tenant, "alice")
    k = _create(c, tenant, agent_ids=[mine])
    assert c.get(_agent_url(mine), headers=_bearer(k["key"])).status_code == 200
    outside = c.get(_agent_url(other), headers=_bearer(k["key"]))
    assert outside.status_code == 404 and outside.json() == {"detail": "Agent not found"}
    assert c.get(_agent_url("does-not-exist"), headers=_bearer(k["key"])).json() == outside.json()


def test_an_agent_only_key_cannot_reach_a_workflow_route_when_workflows_are_empty(c, tenant):
    agent_id = create_agent(c, tenant, "alice")
    wf = _workflow(c, tenant, agent_id)
    k = _create(c, tenant, agent_ids=[agent_id], workflow_ids=[])
    assert c.get(_agent_url(agent_id), headers=_bearer(k["key"])).status_code == 200
    r = c.get(_wf_url(wf), headers=_bearer(k["key"]))
    assert r.status_code == 404 and r.json() == {"detail": "Workflow not found"}
    w = _create(c, tenant, agent_ids=[], workflow_ids=[wf])
    assert c.get(_wf_url(wf), headers=_bearer(w["key"])).status_code == 200
    denied = c.get(_agent_url(agent_id), headers=_bearer(w["key"]))
    assert denied.status_code == 404 and denied.json() == {"detail": "Agent not found"}


def test_allowlists_fail_closed_on_the_dimension_left_out(c, tenant):
    """Neither list: tenant-wide. Either list alone: the other dimension allows nothing (stored as
    []). Both lists: exactly those ids (review MAJOR 1)."""
    a1, a2 = create_agent(c, tenant, "alice"), create_agent(c, tenant, "alice")
    wf1, wf2 = _workflow(c, tenant, a1), _workflow(c, tenant, a1)
    urls = (_agent_url(a1), _agent_url(a2), _wf_url(wf1), _wf_url(wf2))

    def codes(key: dict) -> list[int]:
        return [c.get(u, headers=_bearer(key["key"])).status_code for u in urls]

    neither = _create(c, tenant)
    assert (neither["agent_ids"], neither["workflow_ids"]) == (None, None)
    assert codes(neither) == [200, 200, 200, 200]

    agent_only = _create(c, tenant, agent_ids=[a1])
    assert (agent_only["agent_ids"], agent_only["workflow_ids"]) == ([a1], [])
    assert codes(agent_only) == [200, 404, 404, 404]

    workflow_only = _create(c, tenant, workflow_ids=[wf1])
    assert (workflow_only["agent_ids"], workflow_only["workflow_ids"]) == ([], [wf1])
    assert codes(workflow_only) == [404, 404, 200, 404]

    both = _create(c, tenant, agent_ids=[a2], workflow_ids=[wf2])
    assert (both["agent_ids"], both["workflow_ids"]) == ([a2], [wf2])
    assert codes(both) == [404, 200, 404, 200]

    listed = {k["id"]: k for k in c.get(KEYS, headers=_dev(tenant)).json()["items"]}
    assert listed[agent_only["id"]]["workflow_ids"] == [] and listed[workflow_only["id"]]["agent_ids"] == []


def test_allowlists_must_name_this_tenants_agents_and_workflows(c, tenant):
    other = f"t{uuid.uuid4().hex[:10]}"
    foreign_agent = create_agent(c, other, "bob")
    foreign_wf = _workflow(c, other, foreign_agent)
    for body in ({"agent_ids": [foreign_agent]}, {"workflow_ids": [foreign_wf]}):
        r = c.post(KEYS, json={"name": "x", **body}, headers=_dev(tenant))
        assert r.status_code == 422 and "unknown" in r.json()["detail"]
    assert c.get(KEYS, headers=_dev(tenant)).json()["items"] == []


def test_a_key_is_bound_to_its_tenant(c, tenant):
    other = f"t{uuid.uuid4().hex[:10]}"
    k = _create(c, tenant)
    r = c.get(_agent_url("any"), headers=_bearer(k["key"]))
    assert r.status_code == 200 and r.json()["tenant_id"] == tenant  # authenticated, as the key's tenant
    probe_ok = c.get(_agent_url(create_agent(c, tenant, "alice")), headers=_bearer(k["key"]))
    assert probe_ok.json()["tenant_id"] == tenant
    assert c.get(KEYS, headers=_dev(other)).json()["items"] == []
    listed = c.get(KEYS, headers=_dev(tenant)).json()["items"]
    assert [i["id"] for i in listed] == [k["id"]]


def test_delete_unknown_foreign_or_malformed_is_the_same_404(c, tenant):
    other = f"t{uuid.uuid4().hex[:10]}"
    k = _create(c, tenant)
    answers = set()
    for key_id in (k["id"], "0" * 24, "nope", "A" * 24, "x" * 64):
        r = c.delete(f"{KEYS}/{key_id}", headers=_dev(other))
        assert r.status_code == 404, key_id
        answers.add(r.text)
    assert len(answers) == 1
    assert c.delete(f"{KEYS}/{'0' * 24}", headers=_dev(tenant)).status_code == 404
    assert c.delete(f"{KEYS}/{'x' * 65}", headers=_dev(tenant)).status_code in (404, 422)
    # The foreign delete changed nothing.
    assert c.get(_agent_url("a"), headers=_bearer(k["key"])).status_code == 200  # still authenticates


def test_revoke_is_effective_on_the_very_next_request(c, tenant):
    agent_id = create_agent(c, tenant, "alice")
    k = _create(c, tenant)
    for _ in range(3):
        assert c.get(_agent_url(agent_id), headers=_bearer(k["key"])).status_code == 200
    assert c.delete(f"{KEYS}/{k['id']}", headers=_dev(tenant)).status_code == 204
    r = c.get(_agent_url(agent_id), headers=_bearer(k["key"]))
    assert r.status_code == 401 and r.json() == {"detail": "Invalid API key"}


def test_list_is_newest_first_with_status_and_no_secret_fields(c, tenant):
    a, b = _create(c, tenant, name="a"), _create(c, tenant, name="b")
    c.delete(f"{KEYS}/{a['id']}", headers=_dev(tenant))
    items = c.get(KEYS, headers=_dev(tenant)).json()["items"]
    assert [i["id"] for i in items] == [b["id"], a["id"]]
    assert [i["status"] for i in items] == ["active", "revoked"]
    for item in items:
        assert set(item) == {"id", "name", "prefix", "status", "scopes", "agent_ids", "workflow_ids",
                             "allowed_origins", "created_by", "created_at", "expires_at", "revoked_at",
                             "last_used_at", "owner_subject", "owner_email"}


def test_management_routes_refuse_a_key_and_a_missing_header(c, tenant):
    k = _create(c, tenant)
    for call in (
        lambda h: c.get(KEYS, headers=h),
        lambda h: c.post(KEYS, json={"name": "x"}, headers=h),
        lambda h: c.delete(f"{KEYS}/{k['id']}", headers=h),
        lambda h: c.get("/api/v1/agents", headers=h),
    ):
        assert call(_bearer(k["key"])).status_code == 401
        assert call({}).status_code == 401
    assert c.get(_agent_url("a"), headers={}).status_code == 401
    # The refused DELETE did not revoke it.
    assert c.get(_agent_url("a"), headers=_bearer(k["key"])).status_code == 200


def test_agui_principal_accepts_the_dev_bearer_and_a_key(c, tenant):
    agent_id = create_agent(c, tenant, "alice")
    k = _create(c, tenant)
    dev = c.get(_agent_url(agent_id), headers=_dev(tenant))
    key = c.get(_agent_url(agent_id), headers=_bearer(k["key"]))
    assert dev.json() == {"tenant_id": tenant, "kind": "dev"}
    assert key.json() == {"tenant_id": tenant, "kind": "api_key"}
    lower = c.get(_agent_url(agent_id), headers={"Authorization": f"bearer {k['key']}"})
    assert lower.status_code == 200
    assert c.get(_agent_url(agent_id), headers={"Authorization": "Bearer dev:bad"}).status_code == 401
    assert c.get(_agent_url(agent_id), headers={"Authorization": f"Basic {k['key']}"}).status_code == 401


def test_without_dev_mode_a_key_works_but_the_dev_bearer_and_management_do_not(c, tenant, monkeypatch):
    agent_id = create_agent(c, tenant, "alice")
    k = _create(c, tenant)
    monkeypatch.delenv("EAP_AUTH_MODE")
    assert c.get(_agent_url(agent_id), headers=_bearer(k["key"])).status_code == 200
    assert c.get(_agent_url(agent_id), headers=_dev(tenant)).status_code == 401
    # Management stays dev-only (Q2): with dev mode off current_principal fails closed with 401
    # (AGUI.9; before it, the configuration RuntimeError, a 500), for the dev bearer and for a key
    # alike; never a 2xx.
    for headers in (_dev(tenant), _bearer(k["key"])):
        for response in (c.get(KEYS, headers=headers), c.post(KEYS, json={"name": "x"}, headers=headers)):
            assert response.status_code == 401
            assert response.json() == {"detail": "dev bearer authentication is not enabled"}
    # A bad key is still 401 with the key message, not the dev-mode one.
    bad = c.get(_agent_url(agent_id), headers=_bearer(f"pos_{'a' * 24}_{'B' * 43}"))
    assert bad.status_code == 401 and bad.json() == {"detail": "Invalid API key"}


def test_a_key_is_not_a_dev_bearer_even_in_dev_mode(c, tenant):
    k = _create(c, tenant)
    assert c.get("/api/v1/agents", headers=_bearer(k["key"])).status_code == 401
    assert c.get(KEYS, headers=_bearer(k["key"])).status_code == 401


def test_create_validation_and_body_limits(c, tenant):
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    ok = _create(c, tenant, expires_at=future, allowed_origins=["HTTPS://App.Example:443"], scopes=["agui:run"])
    assert ok["allowed_origins"] == ["https://app.example"] and ok["expires_at"] is not None
    for body in (
        {"name": ""}, {"name": "x" * 101}, {"scopes": []}, {"scopes": ["root"]}, {"expires_at": "2001-01-01T00:00:00Z"},
        {"expires_at": "2099-01-01T00:00:00"}, {"allowed_origins": ["https://a.example/p"]}, {"unknown": 1},
        {"agent_ids": [""]},
    ):
        r = c.post(KEYS, json={"name": "k", **body}, headers=_dev(tenant))
        assert r.status_code == 422, (body, r.status_code, r.text)
        assert "key" not in r.json() and "pos_" not in r.text
    big = c.post(KEYS, content=b'{"name":"' + b"x" * 70000 + b'"}',
                 headers={**_dev(tenant), "Content-Type": "application/json"})
    assert big.status_code == 413
    assert len(c.get(KEYS, headers=_dev(tenant)).json()["items"]) == 1


def test_active_key_cap_is_409_and_revoking_frees_a_slot(c, tenant, monkeypatch):
    monkeypatch.setattr(store_mod, "MAX_ACTIVE_API_KEYS_PER_TENANT", 2)
    a = _create(c, tenant)
    _create(c, tenant)
    r = c.post(KEYS, json={"name": "third"}, headers=_dev(tenant))
    assert r.status_code == 409 and "key" not in r.json()
    c.delete(f"{KEYS}/{a['id']}", headers=_dev(tenant))
    assert c.post(KEYS, json={"name": "third"}, headers=_dev(tenant)).status_code == 201


def test_concurrent_creates_give_unique_ids_and_working_keys(c, tenant):
    agent_id = create_agent(c, tenant, "alice")

    def make(i):
        r = c.post(KEYS, json={"name": f"k{i}"}, headers=_dev(tenant))
        return r.status_code, r.json()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(make, range(16)))
    assert {s for s, _ in results} == {201}
    assert len({b["id"] for _, b in results}) == 16 and len({b["key"] for _, b in results}) == 16
    assert all(c.get(_agent_url(agent_id), headers=_bearer(b["key"])).status_code == 200 for _, b in results[:4])
    assert len(c.get(KEYS, headers=_dev(tenant)).json()["items"]) == 16


def test_concurrent_revokes_all_204_and_the_key_is_dead(c, tenant):
    agent_id = create_agent(c, tenant, "alice")
    k = _create(c, tenant)
    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(lambda _: c.delete(f"{KEYS}/{k['id']}", headers=_dev(tenant)).status_code, range(8)))
    assert codes == [204] * 8
    assert c.get(_agent_url(agent_id), headers=_bearer(k["key"])).status_code == 401
