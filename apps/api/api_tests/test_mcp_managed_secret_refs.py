"""Secret input (E1): managed MCP servers take tenant secret refs; updates re-project and restart."""

from __future__ import annotations

import logging

from sqlalchemy import text

from .mcp_runtime_helpers import BASE, make_entry, requires_pg, runtime_world, _secrets_key  # noqa: F401

pytestmark = requires_pg

API = "/api/v1"
V1 = "fake-not-a-real-credential-v1"
V2 = "fake-not-a-real-credential-v2"


def put(client, h, name, value):
    return client.put(f"{API}/secrets/{name}", headers=h, json={"value": value})


def install(client, app, h, env, **over):
    entry = make_entry(**over)
    app.state.seed(entry)
    return client.post(f"{BASE}/from-catalog", headers=h, json={"catalog_name": entry.name, "env": env})


def cleanup(app, tenant):
    with app.state.database_engine.begin() as conn:
        conn.execute(text("DELETE FROM tenant_secrets WHERE tenant_id = :t"), {"t": tenant})


def test_install_with_a_ref_projects_the_resolved_value_and_lists_only_the_ref(runtime_world, caplog):
    caplog.set_level(logging.DEBUG)
    client, app, fake, tenant_of = runtime_world
    tenant, h = tenant_of()
    try:
        assert put(client, h, "slack-token", V1).status_code == 200
        r = install(client, app, h, {"API_TOKEN": "secret:slack-token", "REGION": "eu"})
        assert r.status_code == 201, r.text
        sid = r.json()["id"]
        spec = fake.specs[-1]
        assert dict(spec.secret_refs) == {"API_TOKEN": V1}
        assert "API_TOKEN" not in spec.env and spec.env["REGION"] == "eu"
        body = client.get(f"{BASE}/{sid}/runtime", headers=h).json()
        assert {"name": "API_TOKEN", "ref": "secret:slack-token"} in body["env"]
        assert {"name": "REGION", "ref": None} in body["env"]
        assert V1 not in client.get(f"{BASE}/{sid}/runtime", headers=h).text + r.text
        assert V1 not in caplog.text
        listed = client.get(f"{API}/secrets", headers=h).json()
        assert listed[0]["used_by"] == [{"server_id": sid, "name": r.json()["name"]}]
    finally:
        cleanup(app, tenant)


def test_a_ref_to_a_missing_secret_is_422_and_another_tenants_secret_is_not_visible(runtime_world):
    client, app, fake, tenant_of = runtime_world
    tenant, h = tenant_of()
    other, oh = tenant_of("mallory")
    try:
        assert put(client, oh, "theirs", V1).status_code == 200
        r = install(client, app, h, {"API_TOKEN": "secret:theirs"})
        assert r.status_code == 422 and "theirs" in r.text and V1 not in r.text
        assert fake.specs == []
        assert client.delete(f"{API}/secrets/theirs", headers=h).status_code == 404
    finally:
        cleanup(app, tenant)
        cleanup(app, other)


def test_delete_is_409_while_a_server_uses_the_secret_and_204_after_it_is_repointed(runtime_world):
    client, app, fake, tenant_of = runtime_world
    tenant, h = tenant_of()
    try:
        put(client, h, "a-token", V1)
        put(client, h, "b-token", V2)
        sid = install(client, app, h, {"API_TOKEN": "secret:a-token"}).json()["id"]
        d = client.delete(f"{API}/secrets/a-token", headers=h)
        assert d.status_code == 409
        assert d.json()["code"] == "secret_in_use" and d.json()["servers"][0]["server_id"] == sid
        fake.make_ready()
        client.get(f"{BASE}/{sid}/runtime", headers=h)
        p = client.patch(f"{BASE}/{sid}", headers=h, json={"env": {"API_TOKEN": "secret:b-token"}})
        assert p.status_code == 200, p.text
        assert ("stop", fake.calls[-2][1]) in fake.calls and fake.calls[-1][0] == "start"
        assert dict(fake.specs[-1].secret_refs) == {"API_TOKEN": V2}
        assert client.delete(f"{API}/secrets/a-token", headers=h).status_code == 204
        assert client.delete(f"{API}/secrets/b-token", headers=h).status_code == 409
    finally:
        cleanup(app, tenant)


def test_patch_env_with_a_literal_or_a_missing_ref(runtime_world):
    client, app, fake, tenant_of = runtime_world
    tenant, h = tenant_of()
    try:
        put(client, h, "a-token", V1)
        sid = install(client, app, h, {"API_TOKEN": "secret:a-token"}).json()["id"]
        bad = client.patch(f"{BASE}/{sid}", headers=h, json={"env": {"API_TOKEN": "secret:nope"}})
        assert bad.status_code == 422
        undeclared = client.patch(f"{BASE}/{sid}", headers=h, json={"env": {"OTHER": "x"}})
        assert undeclared.status_code == 422
        p = client.patch(f"{BASE}/{sid}", headers=h, json={"env": {"API_TOKEN": "fake-literal-value-9"}})
        assert p.status_code == 200 and "fake-literal-value-9" not in p.text
        assert dict(fake.specs[-1].secret_refs) == {"API_TOKEN": "fake-literal-value-9"}
        body = client.get(f"{BASE}/{sid}/runtime", headers=h).json()
        assert {"name": "API_TOKEN", "ref": None} in body["env"]
        assert client.get(f"{API}/secrets", headers=h).json()[0]["used_by"] == []
    finally:
        cleanup(app, tenant)


def test_put_re_projects_and_restarts_referencing_servers(runtime_world):
    client, app, fake, tenant_of = runtime_world
    tenant, h = tenant_of()
    try:
        put(client, h, "slack-token", V1)
        sid = install(client, app, h, {"API_TOKEN": "secret:slack-token"}).json()["id"]
        other = install(client, app, h, {"API_TOKEN": "plain-fake-value"}).json()["id"]
        fake.make_ready()
        for s in (sid, other):
            client.get(f"{BASE}/{s}/runtime", headers=h)
        before = len(fake.specs)
        r = put(client, h, "slack-token", V2)
        assert r.status_code == 200 and r.json() == {"restarted": [sid]}
        assert V2 not in r.text
        assert len(fake.specs) == before + 1 and dict(fake.specs[-1].secret_refs) == {"API_TOKEN": V2}
        # A new secret nobody references restarts nothing.
        assert put(client, h, "unused", V1).json() == {"restarted": []}
    finally:
        cleanup(app, tenant)


def test_put_leaves_a_stopped_server_alone(runtime_world):
    client, app, fake, tenant_of = runtime_world
    tenant, h = tenant_of()
    try:
        put(client, h, "slack-token", V1)
        sid = install(client, app, h, {"API_TOKEN": "secret:slack-token"}).json()["id"]
        client.post(f"{BASE}/{sid}/runtime/stop", headers=h)
        n = len(fake.specs)
        assert put(client, h, "slack-token", V2).json() == {"restarted": []}
        assert len(fake.specs) == n
    finally:
        cleanup(app, tenant)
