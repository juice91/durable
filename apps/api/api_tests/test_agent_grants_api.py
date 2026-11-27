"""Agent-bound grants (docs/iam.md, "Agent and workflow grants"): the grant API, and the publish that newly uses a server or
a secret. The real `OpenFgaPolicy` over `ModelServer` (admin ann, editor eve, viewer vic), against the real API on Postgres."""

from __future__ import annotations

import uuid

import pytest
from eap_secrets import KEY_ENV, generate_key
from sqlalchemy import text

from .iam_model_engine import AllowAll
from .test_iam_p2b_wave1 import API, World
from .test_iam_p2b_wave1 import world as _wave1_world  # noqa: F401
from .test_workflow_runs_api import AGENT_SPEC

SECRET = "fake-not-a-real-credential-0001"


@pytest.fixture
def world(_wave1_world):  # noqa: F811
    """Wave 1's world (admin ann, editor eve, viewer vic), with the secrets and servers these tests leave removed."""
    yield _wave1_world
    with _wave1_world.app.state.database_engine.begin() as conn:
        for table in ("tenant_secrets", "mcp_server_secrets", "mcp_servers"):
            conn.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": _wave1_world.tenant})


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv(KEY_ENV, generate_key())


def _server(w: World, name: str = "tickets") -> str:
    r = w.client.post(f"{API}/mcp-servers", json={"name": name, "url": "https://example.com/mcp"}, headers=w.h("ann"))
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _secret(w: World, name: str = "crm_token") -> None:
    assert w.client.put(f"{API}/secrets/{name}", json={"value": SECRET}, headers=w.h("ann")).status_code == 200


def _agent(w: World, *, server: str | None = "tickets", user: str = "ann") -> str:
    spec = {**AGENT_SPEC, "name": f"A{uuid.uuid4().hex[:6]}"}
    if server:
        spec["mcp_tools"] = [{"server": server, "tool": "list"}]
    r = w.client.post(f"{API}/agents", json={"spec": spec}, headers=w.h(user))
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _workflow(w: World, *, user: str = "ann") -> str:
    spec = {
        "name": f"W{uuid.uuid4().hex[:6]}", "input_schema": {"type": "object"}, "start": "post",
        "steps": [{
            "id": "post", "type": "tool", "tool_id": "http.request",
            "input": {"method": "POST", "url": "https://example.com/hook", "headers": {"Authorization": "secret:crm_token"}},
        }],
    }
    r = w.client.post(f"{API}/workflows", json={"spec": spec}, headers=w.h(user))
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _audit(w: World, action: str) -> list[dict]:
    with w.app.state.database_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT actor_subject, resource_kind, resource_id, detail FROM audit_log WHERE tenant_id = :t AND action = :a"),
            {"t": w.tenant, "a": action},
        ).mappings().all()
    return [dict(r) for r in rows]


def test_only_an_admin_grants_or_revokes_and_each_change_is_audited(world: World):
    world.use(world.engine)
    server_id = _server(world)
    _secret(world)
    agent_id = _agent(world)
    url = f"{API}/agents/{agent_id}"

    assert world.client.get(f"{url}/access", headers=world.h("vic")).json()["grants"] == []  # a viewer reads
    body = {"kind": "mcp_server", "name": "tickets"}
    assert world.client.post(f"{url}/grants", json=body, headers=world.h("eve")).status_code == 403  # an editor may not
    assert world.client.post(f"{url}/grants", json=body, headers=world.h("vic")).status_code == 403
    assert world.client.post(f"{url}/grants", json=body, headers=world.h("zed")).status_code == 404  # a non-member reads nothing
    assert world.client.post(f"{url}/grants", json=body, headers=world.h("ann")).status_code == 200
    assert world.client.post(f"{url}/grants", json={"kind": "secret", "name": "crm_token"}, headers=world.h("ann")).status_code == 200
    assert world.client.post(f"{url}/grants", json={"kind": "secret", "name": "nope"}, headers=world.h("ann")).status_code == 404

    access = world.client.get(f"{url}/access", headers=world.h("eve")).json()
    assert access["supported"] and access["can_manage"] is False and access["options"] is None
    assert [(g["kind"], g["name"]) for g in access["grants"]] == [("mcp_server", "tickets"), ("secret", "crm_token")]
    assert access["grants"][0]["id"] == server_id and access["grants"][0]["granted_by"] == "ann"
    assert world.client.get(f"{url}/access", headers=world.h("ann")).json()["can_manage"] is True

    assert world.client.delete(f"{url}/grants?kind=mcp_server&name=tickets", headers=world.h("eve")).status_code == 403
    assert world.client.delete(f"{url}/grants?kind=mcp_server&name=tickets", headers=world.h("ann")).status_code == 204
    names = [g["name"] for g in world.client.get(f"{url}/access", headers=world.h("ann")).json()["grants"]]
    assert names == ["crm_token"]

    granted, revoked = _audit(world, "grant_agent_use"), _audit(world, "revoke_agent_use")
    assert sorted(r["detail"]["name"] for r in granted) == ["mcp_server:tickets", "secret:crm_token"]
    assert [r["detail"]["name"] for r in revoked] == ["mcp_server:tickets"]
    assert {r["actor_subject"] for r in granted + revoked} == {"ann"} and granted[0]["resource_id"] == agent_id


def test_a_publish_that_newly_uses_a_server_is_an_admins_approval_and_that_approval_is_the_grant(world: World):
    world.use(world.engine)
    _server(world)
    agent_id = _agent(world, user="eve")
    url = f"{API}/agents/{agent_id}"

    card = world.client.get(f"{url}/publish-grants", headers=world.h("eve")).json()
    assert card["newly_uses"] == {"mcp_servers": ["tickets"], "secrets": []} and card["can_approve"] is False
    assert "newly uses MCP server `tickets`" in card["message"] and "Approving grants them" in card["message"]
    assert world.client.get(f"{url}/publish-grants", headers=world.h("ann")).json()["can_approve"] is True

    refused = world.client.post(f"{url}/versions", headers=world.h("eve"))  # a non-admin cannot approve such a publish
    assert refused.status_code == 403 and "an admin must approve" in refused.json()["detail"]
    assert world.client.get(f"{url}/versions", headers=world.h("ann")).json() == []  # nothing was published

    done = world.client.post(f"{url}/versions", headers=world.h("ann"))
    assert done.status_code == 201, done.text
    access = world.client.get(f"{url}/access", headers=world.h("eve")).json()
    assert [(g["kind"], g["name"], g["used_by_latest"]) for g in access["grants"]] == [("mcp_server", "tickets", True)]
    (row,) = _audit(world, "grant_agent_use")
    assert row["detail"] == {"name": "mcp_server:tickets", "reason": "published version 1"} and row["actor_subject"] == "ann"

    # A version that uses nothing new publishes as before, by an editor too.
    assert world.client.get(f"{url}/publish-grants", headers=world.h("eve")).json()["newly_uses"] == {"mcp_servers": [], "secrets": []}
    patched = world.client.patch(f"{url}", json={"spec": {**AGENT_SPEC, "name": "Renamed", "mcp_tools": [{"server": "tickets", "tool": "list"}], "tools": ["clock.now"]}}, headers=world.h("eve"))
    assert patched.status_code == 200, patched.text
    assert world.client.post(f"{url}/versions", headers=world.h("eve")).status_code == 201

    # Dropping the server leaves the grant in place, marked unused by the latest version (revoking is never automatic).
    world.client.patch(f"{url}", json={"spec": {**AGENT_SPEC, "name": "Renamed"}}, headers=world.h("eve"))
    assert world.client.post(f"{url}/versions", headers=world.h("eve")).status_code == 201
    access = world.client.get(f"{url}/access", headers=world.h("eve")).json()
    assert [(g["name"], g["used_by_latest"]) for g in access["grants"]] == [("tickets", False)]


def test_a_workflow_publish_grants_the_secrets_its_steps_name_to_the_workflow(world: World):
    world.use(world.engine)
    _secret(world)
    workflow_id = _workflow(world, user="eve")
    url = f"{API}/workflows/{workflow_id}"
    card = world.client.get(f"{url}/publish-grants", headers=world.h("eve")).json()
    assert card["newly_uses"]["secrets"] == ["crm_token"] and card["can_approve"] is False
    refused = world.client.post(f"{url}/versions", headers=world.h("eve"))
    assert refused.status_code == 403 and "secret `crm_token`" in refused.json()["detail"]
    assert world.client.post(f"{url}/versions", headers=world.h("ann")).status_code == 201
    access = world.client.get(f"{url}/access", headers=world.h("vic")).json()
    assert [(g["kind"], g["name"]) for g in access["grants"]] == [("secret", "crm_token")]
    plain = {"name": "Plain", "input_schema": {"type": "object"}, "start": "a",
             "steps": [{"id": "a", "type": "tool", "tool_id": "clock.now", "input": {}}]}
    assert world.client.patch(url, json={"spec": plain}, headers=world.h("eve")).status_code == 200
    assert world.client.post(f"{url}/versions", headers=world.h("eve")).status_code == 201  # nothing new: an editor publishes
    assert world.client.delete(f"{url}/grants?kind=secret&name=crm_token", headers=world.h("ann")).status_code == 204
    # Restoring version 1 newly uses the secret again: the same rule applies.
    assert world.client.post(f"{url}/versions/1/restore", json={}, headers=world.h("eve")).status_code == 403
    assert world.client.post(f"{url}/versions/1/restore", json={}, headers=world.h("ann")).status_code == 201
    assert [g["name"] for g in world.client.get(f"{url}/access", headers=world.h("ann")).json()["grants"]] == ["crm_token"]


def test_an_engine_without_grants_publishes_as_before_and_the_section_says_it_is_unavailable(world: World):
    world.use(AllowAll())
    _agent(world, server=None)
    agent_id = world.client.get(f"{API}/agents", headers=world.h("ann")).json()[0]["id"]
    url = f"{API}/agents/{agent_id}"
    assert world.client.post(f"{url}/versions", headers=world.h("eve")).status_code == 201
    access = world.client.get(f"{url}/access", headers=world.h("ann")).json()
    assert access["supported"] is False and access["grants"] == []
    assert world.client.get(f"{url}/publish-grants", headers=world.h("eve")).json()["newly_uses"] == {"mcp_servers": [], "secrets": []}
    r = world.client.post(f"{url}/grants", json={"kind": "secret", "name": "x"}, headers=world.h("ann"))
    assert r.status_code == 503 and r.json()["detail"].startswith("the active policy engine keeps no per-agent grants")


# ------------------------------------------------------------------------------ the publish card of an approval


def _approval_service(world: World, user: str, monkeypatch, outcome: str = "recorded"):
    """An ApprovalService for `user` whose stored approval is the Architect's publish of the agent, and a `decide` that records
    whether the grant was already written when the run was woken."""
    from types import SimpleNamespace

    from eap_iam import Principal
    from sqlalchemy.orm import Session

    from eap_api.services import approvals as module
    from eap_api.services.access import Access
    from eap_api.services.authz import AuthzService

    agent_id = _agent(world, user="eve")
    row = {
        "run_id": uuid.uuid4(), "kind": "tool", "approval_key": "call_1", "root_run_id": uuid.uuid4(), "agent_id": agent_id,
        "workflow_id": None, "tool_name": "architect__publish_agent", "args": {"id": agent_id}, "prompt": None, "status": "pending",
        "requested_at": None, "deadline": None, "decided_at": None, "decision": None, "decided_by": None, "run_status": "waiting",
    }

    class Store:
        def get(self, *a):
            return row

        def run_target(self, *a):
            return (agent_id, None)

    class Events:
        def get_event(self, *a):
            return None

    seen: list[bool] = []

    def fake_decide(*a, **k):
        tuples = [t for t in world.server.tuples if t[1] == "granted_to"]
        seen.append(bool(tuples))
        return SimpleNamespace(outcome=outcome, notified=True, waiting=True, recorded_decision=lambda: None)

    monkeypatch.setattr(module, "decide", fake_decide)
    principal = Principal(tenant_id=world.tenant, kind="user", subject=user, provider="t", roles=frozenset({"admin"} if user == "ann" else {"editor"}))
    session = Session(world.app.state.database_engine)
    access = Access(AuthzService(world.engine), principal)
    return module.ApprovalService(Store(), Events(), SimpleNamespace(), access, session), agent_id, seen, session


def test_the_card_lists_the_new_use_and_only_an_admin_can_approve_it(world: World, monkeypatch):
    from eap_api.services.errors import Forbidden

    world.use(world.engine)
    _server(world)
    service, agent_id, seen, session = _approval_service(world, "eve", monkeypatch)
    world.grant("eve", "approver", "agent", agent_id)
    item = service.get(world.tenant, uuid.uuid4(), "tool", "call_1")
    assert item["grants"]["newly_uses"] == {"mcp_servers": ["tickets"], "secrets": []} and item["grants"]["can_approve"] is False
    with pytest.raises(Forbidden) as refused:
        service.decide_tool(world.tenant, "eve", uuid.uuid4(), "call_1", "approved", None)
    assert refused.value.code == "grant_approval_required" and "an admin must approve" in refused.value.message
    assert seen == [] and not [t for t in world.server.tuples if t[1] == "granted_to"]  # the run was never woken, nothing granted
    # Denying a publish is open to the approver: it grants nothing.
    service.decide_tool(world.tenant, "eve", uuid.uuid4(), "call_1", "denied", None)
    assert not [t for t in world.server.tuples if t[1] == "granted_to"]
    session.close()


def test_an_admins_approval_writes_the_grant_before_the_run_wakes_and_takes_it_back_when_not_recorded(world: World, monkeypatch):
    world.use(world.engine)
    _server(world)
    service, agent_id, seen, session = _approval_service(world, "ann", monkeypatch)
    item = service.get(world.tenant, uuid.uuid4(), "tool", "call_1")
    assert item["grants"]["can_approve"] is True
    service.decide_tool(world.tenant, "ann", uuid.uuid4(), "call_1", "approved", None)
    assert seen == [True]  # the grant existed when the run was woken
    assert [t[0] for t in world.server.tuples if t[1] == "granted_to"] == [f"agent:{world.tenant}/{agent_id}"]
    assert [r["detail"]["reason"] for r in _audit(world, "grant_agent_use")] == ["approved publish"]
    session.close()

    # The decision lost a race (already decided): the approval granted nothing.
    for t in [t for t in world.server.tuples if t[1] == "granted_to"]:
        world.server.tuples.discard(t)
    service2, _, seen2, session2 = _approval_service(world, "ann", monkeypatch, outcome="exists")
    from eap_run_store.definition_errors import DefinitionError

    with pytest.raises(DefinitionError):
        service2.decide_tool(world.tenant, "ann", uuid.uuid4(), "call_1", "approved", None)
    assert seen2 == [True] and not [t for t in world.server.tuples if t[1] == "granted_to"]
    session2.close()
