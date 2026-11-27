"""Inbound webhooks under the real policy model (docs/iam.md, "Inbound webhook signatures" and docs/webhooks.md): who
may manage a trigger, who may approve its secrets, whose identity a delivery's run has, and that a tenant's trigger is
nobody else's. The real `OpenFgaPolicy` over a server that evaluates the shipped model (see test_iam_p2b_wave1)."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from sqlalchemy import text

from eap_api.deps import iam_of

from .conftest import get_auth_header
from .iam_model_engine import DenyAll, ModelServer
from .run_helpers import requires_pg
from .test_webhooks_p1 import (  # noqa: F401 - `_env` and `world` are fixtures
    INPUT_MAP,
    PAYLOAD_SCHEMA,
    World,
    _env,
    body_of,
    signed,
    world,
)

pytestmark = requires_pg

ROLES = {"ann": {"admin"}, "eve": {"editor"}, "vic": {"viewer"}}  # zed is no member
BASE = "/api/v1/webhook-triggers"


class Fga:
    """The world of test_webhooks_p1 with `openfga` selected: a published workflow and a secret made by the admin."""

    def __init__(self, w: World) -> None:
        self.w = w
        server = ModelServer()
        self.policy = server.policy()
        self.policy.roles = {k: frozenset(v) for k, v in ROLES.items()}
        self.use(self.policy)
        self.tenant, _ = w.tenant()
        self.workflow = w.workflow(self.h("ann"))
        r = w.client.put("/api/v1/secrets/hook-secret", json={"value": "sentinel-webhook-secret-7f3a9c"}, headers=self.h("ann"))
        assert r.status_code == 200, r.text

    def use(self, engine: Any) -> None:
        self.w.app.state.iam = dataclasses.replace(iam_of(self.w.app), policy=engine)

    def h(self, user: str, tenant: str | None = None) -> dict[str, str]:
        return get_auth_header(tenant or self.tenant, user)

    def body(self, **over: Any) -> dict[str, Any]:
        return {
            "name": "push", "workflow_id": self.workflow, "version": 1, "preset": "github", "secrets": ["secret:hook-secret"],
            "actions": {"start": {"payload_schema": PAYLOAD_SCHEMA, "input_map": INPUT_MAP}},
        } | over

    def create(self, user: str = "eve", **over: Any):
        return self.w.client.post(BASE, json=self.body(**over), headers=self.h(user))

    def act(self, user: str, trigger: dict[str, Any], verb: str, json: dict[str, Any] | None = None):
        return self.w.client.post(f"{BASE}/{trigger['id']}/{verb}", json=json, headers=self.h(user))

    def enable(self, user: str, trigger: dict[str, Any]):
        return self.act(user, trigger, "enable", {"config_hash": trigger["config_hash"], "confirm": True})

    def get(self, user: str, path: str = ""):
        return self.w.client.get(f"{BASE}{path}", headers=self.h(user))

    def secret_names(self) -> set[str]:
        return {s["name"] for s in self.w.client.get("/api/v1/secrets", headers=self.h("ann")).json()}


@pytest.fixture
def fga(world):  # noqa: F811
    return Fga(world)


def test_viewers_read_editors_manage_non_members_see_nothing(fga: Fga):
    created = fga.create("eve")
    assert created.status_code == 201, created.text
    t = created.json()
    assert fga.create("vic", name="v").status_code == 403
    assert fga.create("zed", name="z").status_code == 404  # the workflow itself is not there for them
    assert [i["id"] for i in fga.get("vic").json()["items"]] == [t["id"]]
    assert fga.get("vic", f"/{t['id']}").status_code == 200
    assert fga.get("zed").json()["items"] == []
    for path in (f"/{t['id']}", f"/{t['id']}/events"):
        r = fga.get("zed", path)
        assert r.status_code == 404 and r.json()["code"] == "webhook_trigger_not_found"
    for verb, body in (("enable-request", None), ("disable", None), ("archive", None), ("rotate", {"retire": "secret:hook-secret"}),
                       ("enable", {"config_hash": t["config_hash"], "confirm": True})):
        assert fga.act("vic", t, verb, body).status_code == 403, verb
        assert fga.act("zed", t, verb, body).status_code == 404, verb
    r = fga.w.client.patch(f"{BASE}/{t['id']}", json={"description": "x"}, headers=fga.h("vic"))
    assert r.status_code == 403
    assert fga.get("ann", f"/{t['id']}").json()["status"] == "disabled"  # nothing changed
    assert fga.act("eve", t, "archive").status_code == 200  # `edit` deletes


def test_only_an_admin_approves_the_secret_a_trigger_verifies_with(fga: Fga):
    t = fga.create("eve").json()
    assert fga.act("eve", t, "enable-request").status_code == 200
    r = fga.enable("eve", t)
    assert r.status_code == 403 and r.json()["code"] == "grant_approval_required" and "hook-secret" in r.json()["detail"]
    r = fga.act("eve", t, "decision", {"decision": "approve", "config_hash": t["config_hash"]})
    assert r.status_code == 403 and r.json()["code"] == "grant_approval_required"
    assert fga.get("eve", f"/{t['id']}").json()["status"] == "pending_enable"
    assert fga.enable("ann", t).status_code == 200
    assert fga.act("eve", t, "disable").status_code == 200  # closing the door needs no admin


def test_adding_a_secret_to_an_enabled_trigger_is_an_approval(fga: Fga):
    assert fga.w.client.put("/api/v1/secrets/second", json={"value": "another-value"}, headers=fga.h("ann")).status_code == 200
    t = fga.create("eve").json()
    # While disabled an editor prepares it: nothing accepts the new secret until an admin enables.
    assert fga.act("eve", t, "rotate", {"add": "secret:second"}).status_code == 200
    assert fga.act("eve", t, "rotate", {"retire": "secret:second"}).status_code == 200
    t = fga.get("ann", f"/{t['id']}").json()
    assert fga.enable("ann", t).status_code == 200
    r = fga.act("eve", t, "rotate", {"add": "secret:second"})
    assert r.status_code == 403 and r.json()["code"] == "grant_approval_required"
    assert [s["ref"] for s in fga.get("ann", f"/{t['id']}").json()["secrets"]] == ["secret:hook-secret"]
    assert fga.act("ann", t, "rotate", {"add": "secret:second"}).status_code == 200


def test_generating_a_secret_is_creating_one_and_its_creator_owns_it(fga: Fga):
    body = {k: v for k, v in fga.body(name="gen").items() if k != "secrets"} | {"generate": True}
    before = fga.secret_names()
    r = fga.w.client.post(BASE, json=body, headers=fga.h("eve"))
    assert r.status_code == 403 and fga.secret_names() == before
    assert fga.w.client.get(BASE, headers=fga.h("ann")).json()["items"] == []
    r = fga.w.client.post(BASE, json=body, headers=fga.h("ann"))
    assert r.status_code == 201, r.text
    assert fga.secret_names() - before == {"webhook-gen"}  # registered with the engine: it is visible and manageable
    t = fga.create("eve", name="other").json()
    r = fga.act("eve", t, "rotate", {"generate": True})
    assert r.status_code == 403 and fga.secret_names() - before == {"webhook-gen"}


def test_lists_show_only_the_triggers_and_deliveries_of_readable_workflows(fga: Fga):
    from eap_iam import Grant, ResourceRef
    import asyncio

    other = fga.w.client.post(
        "/api/v1/workflows", headers=fga.h("ann"), json={"spec": {
            "name": "Other", "input_schema": {"type": "object"}, "start": "a",
            "steps": [{"id": "a", "type": "tool", "tool_id": "clock.now", "input": {}}]}},
    ).json()["id"]
    assert fga.w.client.post(f"/api/v1/workflows/{other}/versions", headers=fga.h("ann")).status_code == 201
    mine = fga.create("ann", name="mine").json()
    theirs = fga.create("ann", name="theirs", workflow_id=other, actions={"start": {"payload_schema": PAYLOAD_SCHEMA, "input_map": {}}}).json()
    for t in (mine, theirs):
        assert fga.enable("ann", t).status_code == 200
        body = body_of()
        assert fga.w.post(t["id"], body, signed(body)).status_code == 202
    # zed is no member of the tenant but was granted `viewer` on one workflow.
    asyncio.run(fga.policy.grant(Grant("zed", "viewer", ResourceRef("workflow", fga.workflow, fga.tenant))))
    assert [i["id"] for i in fga.get("zed").json()["items"]] == [mine["id"]]
    deliveries = fga.w.client.get("/api/v1/webhook-deliveries", headers=fga.h("zed")).json()["items"]
    assert {d["trigger_id"] for d in deliveries} == {mine["id"]}
    all_rows = fga.w.client.get("/api/v1/webhook-deliveries", headers=fga.h("ann")).json()["items"]
    hidden = next(d["id"] for d in all_rows if d["trigger_id"] == theirs["id"])
    assert fga.w.client.get(f"/api/v1/webhook-deliveries/{hidden}", headers=fga.h("zed")).status_code == 404
    assert fga.w.client.get(f"/api/v1/webhook-deliveries?trigger_id={theirs['id']}", headers=fga.h("zed")).status_code == 404


def test_a_delivery_runs_as_the_trigger_for_nobody_and_is_audited_by_its_name(fga: Fga):
    t = fga.create("eve").json()
    assert fga.enable("ann", t).status_code == 200
    # The hook route asks the policy engine nothing: the approved trigger is the authority.
    fga.use(DenyAll())
    body = body_of()
    assert fga.w.post(t["id"], body, signed(body)).status_code == 202
    (run,) = fga.w.runs(fga.tenant)
    assert run["principal"] == f"webhook:{t['id']}" and run["started_by_kind"] == "webhook" and run["on_behalf_of"] is None
    assert run["trigger_id"] == t["id"]
    with fga.w.app.state.database_engine.connect() as conn:
        roles = conn.execute(text("SELECT actor_roles FROM runs WHERE tenant_id = :t"), {"t": fga.tenant}).scalar_one()
        rows = conn.execute(text(
            "SELECT actor_kind, actor_subject, on_behalf_of, resource_kind, resource_id, run_id, detail FROM audit_log"
            " WHERE tenant_id = :t AND action = 'webhook_run_started'"), {"t": fga.tenant}).mappings().all()
    assert roles is None
    (row,) = rows
    assert row["actor_kind"] == "webhook" and row["actor_subject"] == t["id"] and row["on_behalf_of"] is None
    assert (row["resource_kind"], row["resource_id"], row["run_id"]) == ("webhook-triggers", t["id"], run["id"])
    assert row["detail"] == {"name": t["name"]}
    assert fga.w.post(t["id"], body, signed(body)).status_code == 200  # a duplicate starts nothing and logs nothing
    with fga.w.app.state.database_engine.connect() as conn:
        count = conn.execute(text(
            "SELECT count(*) FROM audit_log WHERE tenant_id = :t AND action = 'webhook_run_started'"), {"t": fga.tenant}).scalar_one()
    assert count == 1


def test_an_admin_of_another_tenant_cannot_reach_a_trigger_and_its_run_stays_home(fga: Fga):
    t = fga.create("eve").json()
    assert fga.enable("ann", t).status_code == 200
    other, _ = fga.w.tenant()
    for method, path in (("get", f"{BASE}/{t['id']}"), ("get", f"{BASE}/{t['id']}/events"), ("post", f"{BASE}/{t['id']}/disable"),
                         ("post", f"{BASE}/{t['id']}/archive"), ("post", f"{BASE}/{t['id']}/rotate")):
        kwargs = {"json": {"retire": "secret:hook-secret"}} if path.endswith("rotate") else {}
        assert getattr(fga.w.client, method)(path, headers=fga.h("ann", other), **kwargs).status_code == 404, path
    body = body_of()
    assert fga.w.post(t["id"], body, signed(body)).status_code == 202
    assert len(fga.w.runs(fga.tenant)) == 1 and fga.w.runs(other) == []
    assert fga.w.client.get("/api/v1/webhook-deliveries", headers=fga.h("ann", other)).json()["items"] == []
    (delivery,) = fga.w.client.get("/api/v1/webhook-deliveries", headers=fga.h("ann")).json()["items"]
    r = fga.w.client.get(f"/api/v1/webhook-deliveries/{delivery['id']}", headers=fga.h("ann", other))
    assert r.status_code == 404
