"""The Access tab's API and "why can't I?" (docs/iam.md, "The Access tab and explain"): who holds which relation on a
resource, admin-only grants to a member or a role, and a decision explained to the caller only. The real `OpenFgaPolicy`
over `ModelServer` (admin ann, editor eve, viewer vic; zed is no member), against the real API on Postgres."""

from __future__ import annotations

import dataclasses
import uuid

import pytest
from eap_iam import Member, PolicyUnavailable, Principal, ResourceRef, UseGrant
from eap_secrets import KEY_ENV, generate_key
from fastapi import Request

from eap_api.auth import iam_principal
from eap_api.deps import iam_of

from .iam_model_engine import AllowAll, DenyAll
from .test_agent_grants_api import _agent, _audit, _secret, _server, _workflow
from .test_agent_grants_api import world as _grants_world  # noqa: F401
from .test_iam_p2b_wave1 import API, World
from .test_iam_p2b_wave1 import world as _wave1_world  # noqa: F401

IAM = f"{API}/iam"


class Members:
    """A membership source whose user ids make the subjects `zitadel:<id>`."""

    name, mutable, configured = "zitadel", True, True

    def __init__(self) -> None:
        self.members = [Member(id="vic-id", email="vic@example.test", roles=("viewer",))]

    async def list_members(self, tenant_id: str) -> list[Member]:
        return self.members

    async def roles_of(self, tenant_id: str, subject: str) -> frozenset[str]:
        return frozenset()


@pytest.fixture
def world(_grants_world):  # noqa: F811
    w = _grants_world
    w.app.state.iam = dataclasses.replace(iam_of(w.app), membership=Members())
    w.engine.roles["zitadel:vic-id"] = frozenset({"viewer"})
    w.use(w.engine)

    # In production the roles are on the principal (the identity provider's claim) and travel with it; the dev bearer
    # carries none, so give it the same table the engine double applies.
    async def with_roles(request: Request) -> Principal:
        return w.engine._with_roles(await iam_principal(request))

    w.app.dependency_overrides[iam_principal] = with_roles
    yield w
    w.app.dependency_overrides.pop(iam_principal)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv(KEY_ENV, generate_key())


def _resource(w: World, kind: str) -> str:
    if kind == "agent":
        return _agent(w, server=None)
    if kind == "workflow":
        return _workflow(w)
    if kind == "secret":
        _secret(w)
        return "crm_token"
    if kind == "mcp_server":
        return _server(w)
    r = w.client.post(f"{API}/knowledge-bases", json={"name": f"KB{uuid.uuid4().hex[:6]}", "embedding_model": "test:hash"}, headers=w.h("ann"))
    assert r.status_code == 201, r.text
    return r.json()["id"]


KINDS = ["agent", "workflow", "knowledge_base", "secret", "mcp_server"]
RELATIONS = {
    "agent": ["owner", "editor", "runner", "approver", "viewer"],
    "workflow": ["owner", "editor", "runner", "approver", "viewer"],
    "knowledge_base": ["owner", "editor", "runner", "viewer"],
    "secret": ["owner"],
    "mcp_server": ["owner"],
}


@pytest.mark.parametrize("kind", KINDS)
def test_a_reader_sees_who_holds_which_relation_and_only_an_admin_may_change_it(world: World, kind):
    rid = _resource(world, kind)
    url = f"{IAM}/access/{kind}/{rid}"
    admin = world.client.get(url, headers=world.h("ann"))
    assert admin.status_code == 200, admin.text
    body = admin.json()
    assert body["supported"] is True and body["can_manage"] is True
    assert [r["relation"] for r in body["relations"]] == RELATIONS[kind]
    assert {"relation": "owner", "subject_type": "user", "subject": "ann"} in body["grants"]
    for reader in ("eve", "vic"):
        seen = world.client.get(url, headers=world.h(reader))
        assert seen.status_code == 200 and seen.json()["can_manage"] is False and seen.json()["grants"] == body["grants"]
    assert world.client.get(url, headers=world.h("zed")).status_code == 404
    grant = {"relation": "owner", "member_id": "vic-id"}
    for refused in ("eve", "vic"):
        r = world.client.post(f"{url}/grants", json=grant, headers=world.h(refused))
        assert r.status_code == 403, r.text
        assert world.client.delete(f"{url}/grants?relation=owner&subject_type=user&subject=ann", headers=world.h(refused)).status_code == 403
    assert world.client.post(f"{url}/grants", json=grant, headers=world.h("zed")).status_code == 404

    assert world.client.post(f"{url}/grants", json=grant, headers=world.h("ann")).status_code == 200
    assert world.client.post(f"{url}/grants", json=grant, headers=world.h("ann")).status_code == 200  # idempotent
    stored = ("user:zitadel%3Avic-id", "owner", f"{kind}:{world.tenant}/{rid}")
    assert stored in world.server.tuples
    listed = world.client.get(url, headers=world.h("ann")).json()["grants"]
    assert {"relation": "owner", "subject_type": "user", "subject": "zitadel:vic-id"} in listed
    gone = world.client.delete(f"{url}/grants?relation=owner&subject_type=user&subject=zitadel:vic-id", headers=world.h("ann"))
    assert gone.status_code == 204 and stored not in world.server.tuples

    granted, revoked = _audit(world, "grant_access"), _audit(world, "revoke_access")
    assert [r["detail"]["name"] for r in granted] == ["owner user:zitadel:vic-id"] * 2
    assert [r["detail"]["name"] for r in revoked] == ["owner user:zitadel:vic-id"]
    assert {(r["actor_subject"], r["resource_kind"], r["resource_id"]) for r in granted + revoked} == {("ann", kind, rid)}


def test_a_role_grant_gives_everyone_with_the_role_the_relation_on_that_workflow_only(world: World):
    one, two = _resource(world, "workflow"), _resource(world, "workflow")
    url = f"{IAM}/access/workflow/{one}"
    explain = lambda wid: world.client.get(f"{IAM}/explain?kind=workflow&id={wid}&action=run", headers=world.h("vic")).json()  # noqa: E731
    assert explain(one)["decisions"][0]["allowed"] is False
    assert world.client.post(f"{url}/grants", json={"relation": "runner", "role": "viewer"}, headers=world.h("ann")).status_code == 200
    assert explain(one)["decisions"][0]["allowed"] is True and explain(two)["decisions"][0]["allowed"] is False
    assert {"relation": "runner", "subject_type": "role", "subject": "viewer"} in world.client.get(url, headers=world.h("vic")).json()["grants"]
    assert world.client.delete(f"{url}/grants?relation=runner&subject_type=role&subject=viewer", headers=world.h("ann")).status_code == 204
    assert explain(one)["decisions"][0]["allowed"] is False
    assert [r["detail"]["name"] for r in _audit(world, "grant_access")] == ["runner role:viewer"]


@pytest.mark.parametrize(
    "kind,body",
    [
        ("agent", {"relation": "owner", "role": "editor"}),  # an owner is a person
        ("agent", {"relation": "runner", "member_id": "nobody"}),  # not a member
        ("agent", {"relation": "granted_to", "member_id": "vic-id"}),
        ("agent", {"relation": "runner"}),  # neither
        ("agent", {"relation": "runner", "member_id": "vic-id", "role": "viewer"}),  # both
        ("agent", {"relation": "runner", "role": "owner"}),
        ("secret", {"relation": "runner", "member_id": "vic-id"}),
        ("agent", {"relation": "runner", "subject": "zitadel:vic-id"}),  # a raw subject is not accepted on a grant
    ],
)
def test_a_grant_the_model_or_the_tenant_cannot_hold_is_refused_and_nothing_is_written(world: World, kind, body):
    rid = _resource(world, kind)
    before = set(world.server.tuples)
    r = world.client.post(f"{IAM}/access/{kind}/{rid}/grants", json=body, headers=world.h("ann"))
    assert r.status_code == 422, r.text
    assert world.server.tuples == before and _audit(world, "grant_access") == []


def test_an_unknown_resource_or_kind_is_not_found_even_for_an_admin(world: World):
    for kind in KINDS:
        rid = str(uuid.uuid4()) if kind != "secret" else "no_such_secret"
        assert world.client.get(f"{IAM}/access/{kind}/{rid}", headers=world.h("ann")).status_code == 404
        r = world.client.post(f"{IAM}/access/{kind}/{rid}/grants", json={"relation": "owner", "member_id": "vic-id"}, headers=world.h("ann"))
        assert r.status_code == 404
    assert world.client.get(f"{IAM}/access/api_key/x", headers=world.h("ann")).status_code == 422
    assert world.client.get(f"{IAM}/access/tenant/{world.tenant}", headers=world.h("ann")).status_code == 422
    assert not [t for t in world.server.tuples if t[1] != "tenant" and "no_such" in t[2]]


def test_a_secret_lists_the_agents_and_workflows_granted_to_use_it_that_the_caller_may_read(world: World):
    rid = _resource(world, "secret")
    agent_id = _resource(world, "agent")
    import asyncio

    asyncio.run(world.engine.grant_use(UseGrant(ResourceRef("agent", agent_id, world.tenant), ResourceRef("secret", rid, world.tenant))))
    body = world.client.get(f"{IAM}/access/secret/{rid}", headers=world.h("vic")).json()
    assert body["used_by"] == [{"kind": "agent", "id": agent_id}]
    assert world.client.get(f"{IAM}/access/agent/{agent_id}", headers=world.h("vic")).json()["used_by"] == []


def test_without_grants_the_section_says_it_is_off_and_writes_answer_503(world: World):
    rid = _resource(world, "agent")
    world.use(AllowAll())
    body = world.client.get(f"{IAM}/access/agent/{rid}", headers=world.h("eve")).json()
    assert body["supported"] is False and body["grants"] == [] and body["relations"] == [] and body["can_manage"] is False
    r = world.client.post(f"{IAM}/access/agent/{rid}/grants", json={"relation": "runner", "role": "viewer"}, headers=world.h("ann"))
    assert r.status_code == 503 and r.json()["code"] == "grants_unavailable"
    why = world.client.get(f"{IAM}/explain?kind=agent&id={rid}&action=run", headers=world.h("vic")).json()
    assert why["supported"] is False and why["decisions"][0]["allowed"] is True


# --------------------------------------------------------------------------------------------------------------- explain


def test_explain_says_what_is_missing_and_what_to_ask_for_and_never_who_else_holds_it(world: World):
    rid = _resource(world, "workflow")
    world.client.post(f"{IAM}/access/workflow/{rid}/grants", json={"relation": "approver", "member_id": "vic-id"}, headers=world.h("ann"))
    r = world.client.get(f"{IAM}/explain?kind=workflow&id={rid}&action=run&action=read&action=approve", headers=world.h("vic"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["supported"] is True and body["roles"] == ["viewer"]
    run, read, approve = body["decisions"]
    assert run == {"action": "run", "allowed": False, "reason": "no_permission",
                   "needed": {"roles": ["admin", "editor"], "relations": ["owner", "editor", "runner"]}}
    assert read["allowed"] is True and read["reason"] == "allowed"
    assert approve["allowed"] is False and approve["needed"] == {"roles": ["admin"], "relations": ["approver"]}
    assert "ann" not in r.text and "zitadel:vic-id" not in r.text


def test_explain_tells_a_non_member_so_and_answers_the_same_for_a_resource_that_does_not_exist(world: World):
    rid = _resource(world, "agent")
    real = world.client.get(f"{IAM}/explain?kind=agent&id={rid}&action=run", headers=world.h("zed")).json()
    assert real["roles"] == [] and real["decisions"][0]["reason"] == "not_a_member"
    ghost = world.client.get(f"{IAM}/explain?kind=agent&id={uuid.uuid4()}&action=run", headers=world.h("zed")).json()
    assert ghost["decisions"] == real["decisions"]
    create = world.client.get(f"{IAM}/explain?kind=secret&action=create", headers=world.h("eve")).json()
    assert create["decisions"][0] == {"action": "create", "allowed": False, "reason": "no_permission", "needed": {"roles": ["admin"], "relations": []}}
    for bad in ("kind=nothing&action=run", "kind=agent&action=Run%20Me", "kind=agent", f"kind=agent&id={rid}&action=fly"):
        r = world.client.get(f"{IAM}/explain?{bad}", headers=world.h("ann"))
        assert r.status_code == 422, (bad, r.text)


class Down(DenyAll):
    name, features = "down", frozenset({"grants", "explain"})

    async def check(self, principal, action, resource):
        raise PolicyUnavailable("openfga POST check -> 500")


def test_explain_reports_an_unavailable_engine_instead_of_failing(world: World):
    world.use(Down())
    r = world.client.get(f"{IAM}/explain?kind=agent&id={uuid.uuid4()}&action=run", headers=world.h("ann"))
    assert r.status_code == 200 and r.json()["decisions"][0]["reason"] == "engine_unavailable"
    assert r.json()["decisions"][0]["allowed"] is False and "500" not in r.text


def test_a_refusal_names_the_action_and_resource_so_studio_can_explain_it(world: World):
    rid, wid = _resource(world, "agent"), _resource(world, "workflow")
    assert world.client.post(f"{API}/workflows/{wid}/versions", headers=world.h("ann")).status_code == 201
    assert world.client.post(f"{API}/agents/{rid}/versions", headers=world.h("ann")).status_code == 201
    vic = world.h("vic")
    run_id = str(uuid.uuid4())
    for method, url, body, refused in [
        ("post", f"{API}/agents/{rid}/versions", None, {"action": "publish", "kind": "agent", "id": rid}),
        ("post", f"{API}/agents/{rid}/runs", {"input": "hi", "run_id": run_id}, {"action": "run", "kind": "agent", "id": rid}),
        ("post", f"{API}/agents/{rid}/conversations", {}, {"action": "run", "kind": "agent", "id": rid}),
        ("post", f"{API}/workflows/{wid}/runs", {"input": {}, "run_id": run_id}, {"action": "run", "kind": "workflow", "id": wid}),
        ("put", f"{API}/secrets/new_one", {"value": "fake-not-a-real-credential"}, {"action": "create", "kind": "secret", "id": None}),
    ]:
        r = world.client.request(method.upper(), url, json=body, headers=vic)
        assert r.status_code == 403, (url, r.text)
        assert r.json()["refused"] == refused, (url, r.json())
    hidden = world.client.get(f"{API}/agents/{rid}", headers=world.h("zed"))
    assert hidden.status_code == 404 and "refused" not in hidden.json()
