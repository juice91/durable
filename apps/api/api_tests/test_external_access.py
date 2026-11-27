"""External access management (design: external agents inbound, sections 3, 4 and 9): clients, run keys, exports and
the enable card, over the people routes. Real Postgres and identity chain."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from eap_api.app import create_app
from eap_api.external_settings import ExternalConfigError

from .external_world import BASE, NOISY_STEPS, PUBLIC, World, external_env, use_engine, world  # noqa: F401
from .run_helpers import PG_URL, requires_pg
from .test_webhooks_p1 import flat_routes

pytestmark = requires_pg


def setup(world: World):
    tenant, headers = world.tenant()
    return tenant, headers, world.workflow(headers)


# -------------------------------------------------------------------------------------------------- clients and keys


def test_clients_are_created_listed_and_disabled_with_if_match(world: World):
    _t, headers, _w = setup(world)
    made = world.client_record(headers, "claude-code", description="the laptop")
    assert made["enabled"] is True and made["revision"] == 1 and made["keys"] == []
    dup = world.client.post(f"{BASE}/clients", json={"name": "claude-code"}, headers=headers)
    assert dup.status_code == 409 and dup.json()["code"] == "external_client_name_taken"
    got = world.client.get(f"{BASE}/clients/{made['id']}", headers=headers)
    assert got.status_code == 200 and got.headers["etag"] == '"1"'
    off = world.client.patch(f"{BASE}/clients/{made['id']}", json={"enabled": False}, headers={**headers, "If-Match": '"1"'})
    assert off.status_code == 200 and off.json()["enabled"] is False and off.headers["etag"] == '"2"'
    stale = world.client.patch(f"{BASE}/clients/{made['id']}", json={"enabled": True}, headers={**headers, "If-Match": '"1"'})
    assert stale.status_code == 412 and stale.json()["current"]["enabled"] is False
    assert [c["name"] for c in world.client.get(f"{BASE}/clients", headers=headers).json()["items"]] == ["claude-code"]


@pytest.mark.parametrize("body", [
    {}, {"name": ""}, {"name": "x" * 101}, {"name": "a", "extra": 1}, {"name": "a", "max_active_runs": -1},
    {"name": "a", "max_active_runs": 51}, {"name": "a", "max_runs_per_day": 10001},
])
def test_a_bad_client_is_refused(world: World, body):
    _t, headers, _w = setup(world)
    assert world.client.post(f"{BASE}/clients", json=body, headers=headers).status_code == 422


def test_a_run_key_is_shown_once_and_follows_the_key_rules(world: World):
    _t, headers, workflow = setup(world)
    client = world.client_record(headers)
    made = world.key(headers, client["id"], [workflow])
    assert made["key"].startswith("pos_") and made["scopes"] == ["run"] and made["workflow_ids"] == [workflow]
    expires = datetime.fromisoformat(made["expires_at"])
    assert timedelta(days=89) < expires - datetime.now(timezone.utc) <= timedelta(days=90)
    listed = world.client.get(f"{BASE}/clients/{client['id']}", headers=headers).json()
    assert [k["id"] for k in listed["keys"]] == [made["id"]] and "key" not in listed["keys"][0]
    too_long = (datetime.now(timezone.utc) + timedelta(days=366)).isoformat()
    for body in ({"name": "k", "workflow_ids": []}, {"name": "k"}, {"name": "k", "workflow_ids": [str(uuid.uuid4())]},
                 {"name": "k", "workflow_ids": [workflow], "expires_at": too_long},
                 {"name": "k", "workflow_ids": [workflow], "scopes": ["manage"]}):
        r = world.client.post(f"{BASE}/clients/{client['id']}/keys", json=body, headers=headers)
        assert r.status_code == 422, (body, r.text)
    assert world.client.post(f"{BASE}/clients/{uuid.uuid4()}/keys", json={"name": "k", "workflow_ids": [workflow]},
                             headers=headers).status_code == 404
    assert world.client.delete(f"{BASE}/clients/{client['id']}/keys/{made['id']}", headers=headers).status_code == 204
    assert world.client.get(f"{BASE}/clients/{client['id']}", headers=headers).json()["keys"][0]["status"] == "revoked"


def test_the_general_key_routes_never_make_a_run_key(world: World):
    _t, headers, workflow = setup(world)
    r = world.client.post("/api/v1/api-keys", json={"name": "k", "scopes": ["run"], "workflow_ids": [workflow]}, headers=headers)
    assert r.status_code == 422
    r = world.client.post("/api/v1/iam/me/api-keys", json={"name": "k", "scopes": ["run"]}, headers=headers)
    assert r.status_code in (403, 422)


def test_a_run_key_is_refused_everywhere_else(world: World):
    live = world.live()
    bearer = {"Authorization": f"Bearer {live.key}"}
    for method, path, body in (
        ("get", "/api/v1/workflows", None), ("get", f"/api/v1/workflows/{live.workflow}", None),
        ("get", "/api/v1/runs", None), ("get", "/api/v1/api-keys", None), ("get", "/api/v1/iam/capabilities", None),
        ("get", f"{BASE}/exports", None), ("post", f"{BASE}/clients", {"name": "x"}),
        ("post", "/api/v1/otlp/v1/traces", {}),
        ("post", f"/api/v1/ag-ui/workflows/{live.workflow}", {}),
        ("post", "/api/v1/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
    ):
        kwargs = {} if body is None else {"json": body}
        r = getattr(world.client, method)(path, headers={**bearer, "Accept": "application/json, text/event-stream"}, **kwargs)
        assert r.status_code in (401, 403), (path, r.status_code, r.text)


# -------------------------------------------------------------------------------------------------- exports


def test_an_export_is_created_disabled_with_what_the_card_shows(world: World):
    _t, headers, workflow = setup(world)
    client = world.client_record(headers, "codex")
    key = world.key(headers, client["id"], [workflow])
    made = world.export(headers, workflow, tags=["reports"], examples=['{"q": "today"}'])
    assert made["enabled"] is False and made["version"] == "latest" and made["resolved_version"] == 1
    assert made["result"] == "output" and made["client_inputs"] is False and made["max_runtime_seconds"] == 3600
    assert made["require_idempotency_key"] is False  # a read-only workflow
    assert made["input_schema"]["required"] == ["q"] and made["approval_hash"] and made["warnings"] == []
    assert made["keys"] == [{"id": key["id"], "prefix": key["prefix"], "name": "k", "status": "active",
                             "client_id": client["id"], "client_name": "codex"}]
    assert made["workflow_name"].startswith("wf-")
    listed = world.client.get(f"{BASE}/exports", headers=headers).json()["items"]
    assert [e["name"] for e in listed] == ["run_report"]
    settings = world.client.get(BASE, headers=headers).json()
    assert settings == {"enabled": True, "base_url": PUBLIC, "grants_enforced": True, "can_manage": True,
                        "max_runtime_seconds": 14 * 24 * 3600}


def test_side_effects_require_an_idempotency_key_and_are_warned_about(world: World):
    _t, headers = world.tenant()
    workflow = world.workflow(headers, steps=NOISY_STEPS)
    made = world.export(headers, workflow)
    assert made["require_idempotency_key"] is True
    assert [w["code"] for w in made["warnings"]] == ["external_side_effects_without_approval"]
    # the admin may still say otherwise
    other = world.export(headers, workflow, name="loose", require_idempotency_key=False)
    assert other["require_idempotency_key"] is False


@pytest.mark.parametrize("over, status, code", [
    ({"name": "Bad Name"}, 422, "name_invalid"),
    ({"name": "x" * 49}, 422, "name_invalid"),
    ({"title": ""}, 422, "title_invalid"),
    ({"protocols": []}, 422, "protocols_invalid"),
    ({"protocols": ["grpc"]}, 422, "protocols_invalid"),
    ({"result": "everything"}, 422, "result_invalid"),
    ({"version": 7}, 404, "version_not_found"),
    ({"version": "first"}, 422, "version_invalid"),
    ({"max_runtime_seconds": 0}, 422, "max_runtime_invalid"),
    ({"max_runtime_seconds": 15 * 24 * 3600}, 422, "max_runtime_invalid"),
    ({"workflow_id": "00000000-0000-0000-0000-000000000000"}, 404, "workflow_not_found"),
    ({"tags": ["x"] * 21}, 422, "tags_invalid"),
    ({"surprise": 1}, 422, "body_invalid"),
])
def test_a_bad_export_is_refused(world: World, over, status, code):
    _t, headers, workflow = setup(world)
    r = world.client.post(f"{BASE}/exports", json=world.export_body(workflow, **over), headers=headers)
    assert r.status_code == status and r.json()["code"] == code, r.text


def test_another_tenants_workflow_cannot_be_exported_or_reached(world: World):
    _a, mine, workflow = setup(world)
    _b, theirs = world.tenant()
    r = world.client.post(f"{BASE}/exports", json=world.export_body(workflow), headers=theirs)
    assert r.status_code == 404
    export = world.export(mine, workflow)
    client = world.client_record(mine)
    for method, path in (("get", f"{BASE}/exports/{export['id']}"), ("patch", f"{BASE}/exports/{export['id']}"),
                         ("post", f"{BASE}/exports/{export['id']}/disable"), ("get", f"{BASE}/clients/{client['id']}"),
                         ("post", f"{BASE}/clients/{client['id']}/cancel-runs")):
        kwargs = {"json": {"title": "t"}} if method == "patch" else {}
        assert getattr(world.client, method)(path, headers=theirs, **kwargs).status_code == 404, path
    assert world.client.get(f"{BASE}/exports", headers=theirs).json()["items"] == []
    r = world.client.post(f"{BASE}/clients/{client['id']}/keys", json={"name": "k", "workflow_ids": [workflow]}, headers=theirs)
    assert r.status_code == 404


def test_enabling_echoes_the_hash_of_what_the_card_showed(world: World):
    _t, headers, workflow = setup(world)
    export = world.export(headers, workflow)
    url = f"{BASE}/exports/{export['id']}/enable"
    r = world.client.post(url, json={"approved_hash": "stale", "confirm": True}, headers=headers)
    assert r.status_code == 409 and r.json()["code"] == "approved_hash_mismatch"
    r = world.client.post(url, json={"approved_hash": export["approval_hash"]}, headers=headers)
    assert r.status_code == 422 and r.json()["code"] == "confirm_required"
    on = world.enable(headers, export)
    assert on.status_code == 200, on.text
    body = on.json()
    assert body["enabled"] is True and body["enabled_by"] == "user:alice" and body["disabled_reason"] is None
    off = world.client.post(f"{BASE}/exports/{export['id']}/disable", headers=headers).json()
    assert off["enabled"] is False and off["enabled_by"] is None


def test_a_publish_between_the_card_and_the_click_is_refused(world: World):
    _t, headers, workflow = setup(world)
    export = world.export(headers, workflow)
    world.publish(headers, workflow, input_schema={"type": "object"})
    r = world.enable(headers, export)
    assert r.status_code == 409 and r.json()["code"] == "approved_hash_mismatch"


@pytest.mark.parametrize("name, value, code", [
    ("EAP_EXTERNAL_BASE_URL", "", "external_base_url_unset"),
    ("EAP_AGENT_GRANTS_ENFORCE", "0", "grants_not_enforced"),
])
def test_nothing_is_enabled_without_a_public_address_and_enforced_grants(world: World, external_env, name, value, code):
    _t, headers, workflow = setup(world)
    export = world.export(headers, workflow)  # drafting is always possible
    external_env.setenv(name, value)
    r = world.enable(headers, export)
    assert r.status_code == 409 and r.json()["code"] == code, r.text


def test_an_engine_that_keeps_no_grants_cannot_enable(world: World):
    from eap_api.deps import iam_of

    _t, headers, workflow = setup(world)
    export = world.export(headers, workflow)
    use_engine(world.app, iam_of(world.app).policy._inner)
    r = world.enable(headers, export)
    assert r.status_code == 409 and r.json()["code"] == "grants_not_enforced"


@pytest.mark.parametrize("change", [
    {"title": "New title"}, {"description": "other"}, {"tags": ["x"]}, {"examples": ["{}"]}, {"protocols": ["mcp", "a2a"]},
    {"result": "status_only"}, {"max_runtime_seconds": 60}, {"require_idempotency_key": True}, {"client_inputs": True},
    {"version": 1}, {"name": "renamed"},
])
def test_any_change_to_the_contract_disables_the_export(world: World, change):
    live = world.live()
    r = world.client.patch(f"{BASE}/exports/{live.export['id']}", json=change,
                           headers={**live.headers, "If-Match": f'"{live.export["revision"]}"'})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enabled"] is False and body["disabled_reason"] == "export_changed"
    assert body["approval_hash"] != live.export["approval_hash"] or change == {"version": 1}


def test_a_change_that_changes_nothing_keeps_it_enabled_and_a_stale_write_is_refused(world: World):
    live = world.live()
    url = f"{BASE}/exports/{live.export['id']}"
    same = world.client.patch(url, json={"title": live.export["title"]}, headers=live.headers)
    assert same.status_code == 200 and same.json()["enabled"] is True
    stale = world.client.patch(url, json={"title": "x"}, headers={**live.headers, "If-Match": '"1"'})
    assert stale.status_code == 412


def test_a_latest_export_is_disabled_by_a_publish_that_changes_the_contract(world: World):
    live = world.live()
    world.publish(live.headers, live.workflow)  # same input schema and side effects: still enabled
    assert world.client.get(f"{BASE}/exports/{live.export['id']}", headers=live.headers).json()["enabled"] is True
    published = world.publish(live.headers, live.workflow, steps=NOISY_STEPS)
    assert "external_side_effects_without_approval" in [w["code"] for w in published["warnings"]]
    got = world.client.get(f"{BASE}/exports/{live.export['id']}", headers=live.headers).json()
    assert got["enabled"] is False and got["disabled_reason"] == "export_contract_changed" and got["resolved_version"] == 3


# -------------------------------------------------------------------------------------------------- roles


ROLES = {"ann": {"admin"}, "eve": {"editor"}, "vic": {"viewer"}}


def test_admins_manage_editors_read_viewers_neither_under_the_policy_model(world: World):
    from .iam_model_engine import ModelServer

    server = ModelServer()
    policy = server.policy()
    policy.roles = {k: frozenset(v) for k, v in ROLES.items()}
    use_engine(world.app, policy)
    tenant, _ = world.tenant()
    h = {user: {"Authorization": f"Bearer dev:{tenant}:{user}"} for user in (*ROLES, "zed")}
    workflow = world.workflow(h["ann"])
    client = world.client_record(h["ann"])
    export = world.export(h["ann"], workflow)
    for user, read in (("eve", 200), ("vic", 403), ("zed", 404)):
        for path in ("/clients", "/exports", f"/exports/{export['id']}", f"/clients/{client['id']}"):
            assert world.client.get(f"{BASE}{path}", headers=h[user]).status_code == read, (user, path)
    assert world.client.get(BASE, headers=h["eve"]).json()["can_manage"] is False
    for user in ("eve", "vic", "zed"):
        for method, path, body in (
            ("post", "/clients", {"name": "x"}), ("post", f"/clients/{client['id']}/keys", {"name": "k", "workflow_ids": [workflow]}),
            ("post", "/exports", world.export_body(workflow, name="other")), ("patch", f"/exports/{export['id']}", {"title": "t"}),
            ("post", f"/exports/{export['id']}/enable", {"approved_hash": export["approval_hash"], "confirm": True}),
            ("post", f"/exports/{export['id']}/disable", None), ("post", f"/clients/{client['id']}/cancel-runs", None),
        ):
            kwargs = {} if body is None else {"json": body}
            r = getattr(world.client, method)(f"{BASE}{path}", headers=h[user], **kwargs)
            assert r.status_code in (403, 404), (user, path, r.status_code)
    # the policy model keeps no agent grants in this fake unless it says so: enabling still needs them
    assert "agent_grants" in policy.features
    assert world.enable(h["ann"], export).status_code == 200


def test_only_a_person_enables(world: World):
    """An Architect run and an API key reach no people route; the service refuses a non-person all the same."""
    from eap_iam import Principal
    from sqlalchemy.orm import Session

    from eap_api.service_deps.external_access import external_access_service_of
    from eap_api.services.errors import Forbidden

    tenant, headers, workflow = setup(world)
    export = world.export(headers, workflow)
    run_token = Principal(tenant_id=tenant, kind="run", subject="architect-run:x", provider="architect-run-token",
                          on_behalf_of="alice", roles=frozenset({"admin"}), scopes=frozenset({"manage"}))
    with Session(world.engine) as session:
        service = external_access_service_of(world.app, session, run_token)
        with pytest.raises(Forbidden) as exc:
            service.enable_export(tenant, "architect", export["id"], approved_hash=export["approval_hash"], confirm=True)
    assert exc.value.code == "human_required"


# -------------------------------------------------------------------------------------------------- the public prefix


def test_nothing_for_people_lives_under_the_external_prefix(world: World):
    paths = [path for path, _methods, _route in flat_routes(world.app)]
    assert any(p.startswith(BASE) for p in paths)
    # Only the external agents' surfaces (test_external_mcp.py, test_external_a2a.py): the prefix an Access bypass opens.
    a2a = "/api/v1/external/a2a/{endpoint_id}"
    assert sorted(p for p in paths if p == "/api/v1/external" or p.startswith("/api/v1/external/")) == sorted([
        "/api/v1/external/mcp", f"{a2a}/rpc", f"{a2a}/.well-known/agent-card.json", f"{a2a}/skills/{{skill}}/rpc",
        f"{a2a}/skills/{{skill}}/.well-known/agent-card.json",
    ])


def test_the_surface_cannot_be_switched_on_under_the_forgeable_dev_bearer(external_env):
    external_env.setenv("EAP_EXTERNAL_ACCESS_ENABLED", "true")
    with pytest.raises(ExternalConfigError):
        with TestClient(create_app(database_url=PG_URL)):
            pass


def test_a_base_url_that_is_not_https_refuses_to_start(external_env):
    external_env.setenv("EAP_EXTERNAL_BASE_URL", "http://durable.example.test")
    with pytest.raises(ExternalConfigError):
        with TestClient(create_app(database_url=PG_URL)):
            pass


def test_every_management_write_is_audited_without_a_key(world: World):
    live = world.live()
    rows = world.sql("SELECT action, actor_subject, outcome, detail::text AS detail FROM audit_log WHERE tenant_id = :t",
                     t=live.tenant)
    actions = {r["action"] for r in rows}
    assert {"create_external_client", "create_external_client_key", "create_workflow_export", "enable_workflow_export"} <= actions
    assert not any(live.key in (r["detail"] or "") for r in rows)
