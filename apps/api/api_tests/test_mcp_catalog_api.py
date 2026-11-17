"""The mirrored MCP catalog (read-only for tenants) and installing from it."""

from __future__ import annotations

import json

from sqlalchemy import text

from eap_schemas import CatalogMeta, CatalogNetwork

from .mcp_runtime_helpers import BASE, CATALOG, make_entry, requires_pg, runtime_world, _secrets_key  # noqa: F401

pytestmark = requires_pg


def test_catalog_search_pages_and_hides_hidden_entries(runtime_world):
    client, app, _fake, tenant_of = runtime_world
    _t, h = tenant_of()
    token = "zq" + make_entry().name[-8:]
    a, b, c = (make_entry(name=f"io.example/{token}-{i}", description=f"{token} number {i}") for i in "abc")
    app.state.seed(a, b, c)
    r = client.get(CATALOG, params={"q": token, "limit": 2}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert [i["name"] for i in body["items"]] == [a.name, b.name]
    assert body["next_after"] == b.name
    page2 = client.get(CATALOG, params={"q": token, "limit": 2, "after": body["next_after"]}, headers=h).json()
    assert [i["name"] for i in page2["items"]] == [c.name] and page2["next_after"] is None
    first = body["items"][0]
    assert first["local"] is True and first["remote"] is False
    assert first["packages"][0]["environment_variables"][0] == {
        "name": "API_TOKEN", "description": None, "required": True, "secret": True}
    with app.state.database_engine.begin() as conn:
        conn.execute(text("UPDATE mcp_catalog_entries SET hidden = true WHERE name = :n"), {"n": a.name})
    names = [i["name"] for i in client.get(CATALOG, params={"q": token}, headers=h).json()["items"]]
    assert a.name not in names
    assert client.get(f"{CATALOG}/{a.name}", headers=h).status_code == 200  # an installed server keeps resolving


def test_catalog_source_fields_filter_and_toolhive_first(runtime_world):
    client, app, _fake, tenant_of = runtime_world
    _t, h = tenant_of()
    token = "th" + make_entry().name[-8:]
    off = make_entry(name=f"io.example/{token}-a", description=token)
    meta = CatalogMeta(
        tier="Official", tags=("browser", "web"), tools=("navigate", "click"), tool_count=2,
        network=CatalogNetwork(allow_host=(), allow_port=(443,), insecure_allow_all=True))
    th = make_entry(name=f"io.github.stacklok/{token}-z", description=token, source="toolhive", meta=meta)
    quiet = make_entry(name=f"io.github.stacklok/{token}-y", description=token, source="toolhive", meta=CatalogMeta())
    app.state.seed(off, th, quiet)
    items = client.get(CATALOG, params={"q": token}, headers=h).json()["items"]
    assert [i["name"] for i in items] == [quiet.name, th.name, off.name]  # ToolHive first, then official
    by = {i["name"]: i for i in items}
    assert by[th.name]["source"] == "toolhive" and by[th.name]["tier"] == "Official"
    assert by[th.name]["tags"] == ["browser", "web"] and by[th.name]["tool_count"] == 2
    assert by[th.name]["network"] == {"allow_host": [], "allow_port": [443], "insecure_allow_all": True}
    assert by[quiet.name]["network"] is None and by[quiet.name]["tier"] is None and by[quiet.name]["tool_count"] == 0
    assert by[off.name]["source"] == "official" and by[off.name]["tags"] == [] and by[off.name]["network"] is None
    only = client.get(CATALOG, params={"q": token, "source": "toolhive"}, headers=h).json()["items"]
    assert [i["name"] for i in only] == [quiet.name, th.name]
    only = client.get(CATALOG, params={"q": token, "source": "official"}, headers=h).json()["items"]
    assert [i["name"] for i in only] == [off.name]
    page1 = client.get(CATALOG, params={"q": token, "limit": 2}, headers=h).json()
    page2 = client.get(CATALOG, params={"q": token, "limit": 2, "after": page1["next_after"]}, headers=h).json()
    assert [i["name"] for i in page1["items"] + page2["items"]] == [quiet.name, th.name, off.name]
    assert client.get(CATALOG, params={"source": "nope"}, headers=h).status_code == 422
    got = client.get(f"{CATALOG}/{th.name}", headers=h).json()
    assert got["source"] == "toolhive" and got["tool_count"] == 2


def test_catalog_entry_by_name_with_a_slash_and_unknown_is_404(runtime_world):
    client, app, _fake, tenant_of = runtime_world
    _t, h = tenant_of()
    entry = make_entry()
    app.state.seed(entry)
    r = client.get(f"{CATALOG}/{entry.name}", headers=h)
    assert r.status_code == 200 and r.json()["name"] == entry.name and r.json()["version"] == "1.2.3"
    assert client.get(f"{CATALOG}/io.example/nope", headers=h).status_code == 404
    assert client.get(CATALOG).status_code == 401


def test_from_catalog_seals_secret_env_and_stores_plain_env(runtime_world):
    client, app, fake, tenant_of = runtime_world
    tenant, h = tenant_of()
    entry = make_entry()
    app.state.seed(entry)
    secret = "sntl-very-secret-value-123"
    r = client.post(f"{BASE}/from-catalog", headers=h, json={
        "catalog_name": entry.name, "name": "my-example", "env": {"API_TOKEN": secret, "REGION": "eu-west-1"}})
    assert r.status_code == 201, r.text
    body = r.json()
    assert secret not in r.text
    assert body["runtime"] == "managed" and body["url"] is None
    assert body["catalog_name"] == entry.name and body["runtime_state"] == "starting"
    assert body["env_names"] == ["API_TOKEN", "REGION"] and body["header_names"] == []
    # Started through the adapter, with the secret only in secret_refs.
    (spec,) = fake.specs
    assert dict(spec.secret_refs) == {"API_TOKEN": secret}
    assert spec.env["REGION"] == "eu-west-1" and "API_TOKEN" not in spec.env
    assert spec.env["MCP_PACKAGE"] == "@example/mcp-server@1.2.3"
    with app.state.database_engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT kind, header_name, ciphertext FROM mcp_server_secrets WHERE tenant_id = :t"), {"t": tenant}).all()
        assert [(r.kind, r.header_name) for r in rows] == [("env", "API_TOKEN")]
        assert secret.encode() not in bytes(rows[0].ciphertext)
        rt = conn.execute(text("SELECT package, state, catalog_name FROM mcp_runtimes WHERE tenant_id = :t"),
                          {"t": tenant}).one()
        assert secret not in json.dumps(rt.package) and rt.package["env"]["REGION"] == "eu-west-1"
        assert rt.catalog_name == entry.name
    # Env secrets never show as headers, and the headers filter never touches them.
    got = client.get(f"{BASE}/{body['id']}", headers=h).json()
    assert got["header_names"] == [] and got["env_names"] == ["API_TOKEN", "REGION"]
    listed = client.get(BASE, headers=h).json()
    assert listed[0]["header_names"] == [] and listed[0]["runtime_state"] == "starting"


def test_from_catalog_validates_env_and_package(runtime_world):
    client, app, fake, tenant_of = runtime_world
    _t, h = tenant_of()
    entry = make_entry()
    app.state.seed(entry)
    post = lambda **b: client.post(f"{BASE}/from-catalog", headers=h, json={"catalog_name": entry.name, **b})  # noqa: E731
    assert post(env={}).status_code == 422  # API_TOKEN is required
    assert post(env={"API_TOKEN": "x", "NOT_DECLARED": "y"}).status_code == 422
    assert post(env={"API_TOKEN": "x"}, package_index=3).status_code == 422
    assert post(env={"API_TOKEN": "x"}, name="bad name").status_code == 422
    assert client.post(f"{BASE}/from-catalog", headers=h, json={"catalog_name": "io.example/none"}).status_code == 404
    assert fake.specs == []
    ok = post(env={"API_TOKEN": "x"}, name="dup")
    assert ok.status_code == 201
    assert post(env={"API_TOKEN": "x"}, name="dup").status_code == 409


def test_a_start_failure_leaves_a_failed_runtime_without_exception_text(runtime_world):
    client, app, fake, tenant_of = runtime_world
    _t, h = tenant_of()
    entry = make_entry()
    app.state.seed(entry)
    fake.fail_start = True
    r = client.post(f"{BASE}/from-catalog", headers=h, json={"catalog_name": entry.name, "env": {"API_TOKEN": "x"}})
    assert r.status_code == 201
    assert r.json()["runtime_state"] == "failed" and r.json()["runtime_error"] == "start_failed: RuntimeError"
    assert "abc123" not in r.text


def test_remote_entries_register_without_a_runtime(runtime_world):
    client, app, fake, tenant_of = runtime_world
    tenant, h = tenant_of()
    entry = make_entry(packages=[], remotes=[{"type": "streamable-http", "url": "https://mcp.example.com/mcp"}])
    app.state.seed(entry)
    assert client.get(f"{CATALOG}/{entry.name}", headers=h).json()["remote"] is True
    r = client.post(f"{BASE}/from-catalog", headers=h, json={
        "catalog_name": entry.name, "name": "remote-one", "headers": {"Authorization": "Bearer sntl-remote"}})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["runtime"] == "external" and body["url"] == "https://mcp.example.com/mcp"
    assert body["runtime_state"] is None and body["header_names"] == ["authorization"]
    assert fake.specs == []
    with app.state.database_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM mcp_runtimes WHERE tenant_id = :t"), {"t": tenant}).scalar() == 0
    sse = make_entry(packages=[], remotes=[{"type": "sse", "url": "https://mcp.example.com/sse"}])
    app.state.seed(sse)
    assert client.post(f"{BASE}/from-catalog", headers=h, json={"catalog_name": sse.name}).status_code == 422


def test_dangerous_env_names_are_denied(runtime_world):
    client, app, fake, tenant_of = runtime_world
    _t, h = tenant_of()
    for name in ("NODE_OPTIONS", "LD_PRELOAD", "LD_LIBRARY_PATH", "PATH", "PYTHONPATH", "PYTHONSTARTUP", "HOME", "DYLD_INSERT_LIBRARIES"):
        entry = make_entry(packages=[{
            "registry_type": "npm", "identifier": "@example/mcp-server", "version": "1.2.3", "transport": "stdio",
            "environment_variables": [{"name": name, "required": False, "secret": False}],
        }])
        app.state.seed(entry)
        r = client.post(f"{BASE}/from-catalog", headers=h, json={"catalog_name": entry.name, "env": {name: "x"}})
        assert r.status_code == 422 and "env_name_denied" in r.json()["detail"], (name, r.text)
        assert not fake.specs
