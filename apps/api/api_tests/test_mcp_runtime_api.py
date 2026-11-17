"""Runtime controls, status, redacted logs and delete for managed MCP servers."""

from __future__ import annotations

from sqlalchemy import text

from .mcp_runtime_helpers import BASE, make_entry, requires_pg, runtime_world, _secrets_key  # noqa: F401

pytestmark = requires_pg

SECRET = "sntl-very-secret-value-123"


def install(client, app, h, **over):
    entry = make_entry(**over)
    app.state.seed(entry)
    r = client.post(f"{BASE}/from-catalog", headers=h, json={
        "catalog_name": entry.name, "env": {"API_TOKEN": SECRET}})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def state(app, sid):
    with app.state.database_engine.connect() as conn:
        return conn.execute(text("SELECT state, last_error FROM mcp_runtimes WHERE server_id = :s"), {"s": sid}).one()


def test_status_reconciles_a_starting_row_with_the_adapter(runtime_world):
    client, app, fake, tenant_of = runtime_world
    _t, h = tenant_of()
    sid = install(client, app, h)
    assert client.get(f"{BASE}/{sid}/runtime", headers=h).json()["state"] == "starting"
    fake.make_ready()
    body = client.get(f"{BASE}/{sid}/runtime", headers=h).json()
    assert body["state"] == "running" and state(app, sid).state == "running"
    assert body["catalog_name"] and set(body) >= {"state", "message", "last_error", "last_used_at", "image", "updated_at"}
    assert "svc.cluster.local" not in str(body)


def test_stop_start_and_restart(runtime_world):
    client, app, fake, tenant_of = runtime_world
    _t, h = tenant_of()
    sid = install(client, app, h)
    fake.make_ready()
    client.get(f"{BASE}/{sid}/runtime", headers=h)
    r = client.post(f"{BASE}/{sid}/runtime/stop", headers=h)
    assert r.status_code == 200 and r.json()["state"] == "stopped" and fake.calls[-1][0] == "stop"
    assert state(app, sid).state == "stopped"
    assert client.post(f"{BASE}/{sid}/runtime/stop", headers=h).json()["state"] == "stopped"  # idempotent
    starts = len(fake.specs)
    r = client.post(f"{BASE}/{sid}/runtime/start", headers=h)
    assert r.json()["state"] == "starting" and len(fake.specs) == starts + 1
    assert dict(fake.specs[-1].secret_refs) == {"API_TOKEN": SECRET}  # decrypted from the sealed row
    assert client.post(f"{BASE}/{sid}/runtime/start", headers=h).json()["state"] == "starting"
    assert len(fake.specs) == starts + 1  # already starting: no second start
    r = client.post(f"{BASE}/{sid}/runtime/restart", headers=h)
    assert r.json()["state"] == "starting" and [c[0] for c in fake.calls[-2:]] == ["stop", "start"]
    assert client.post(f"{BASE}/{sid}/runtime/explode", headers=h).status_code == 422


def test_runtime_routes_reject_external_servers_and_other_tenants(runtime_world):
    client, app, _fake, tenant_of = runtime_world
    _t, h = tenant_of()
    _o, other = tenant_of("mallory")
    ext = client.post(BASE, headers=h, json={"name": "ext", "url": "https://mcp.example.com/mcp"}).json()["id"]
    for method, path in (("GET", "runtime"), ("POST", "runtime/start"), ("GET", "runtime/logs")):
        assert client.request(method, f"{BASE}/{ext}/{path}", headers=h).status_code == 409
    sid = install(client, app, h)
    for method, path in (("GET", "runtime"), ("POST", "runtime/start"), ("POST", "runtime/stop"),
                         ("POST", "runtime/restart"), ("GET", "runtime/logs")):
        assert client.request(method, f"{BASE}/{sid}/{path}", headers=other).status_code == 404, path
    assert client.delete(f"{BASE}/{sid}", headers=other).status_code == 404
    assert state(app, sid).state == "starting"


def test_runtime_logs_are_redacted(runtime_world):
    client, app, fake, tenant_of = runtime_world
    _t, h = tenant_of()
    sid = install(client, app, h)
    fake.log_lines = [
        "server up",
        f"connecting with {SECRET}",
        "token=abc123def and password=hunter2 then API_KEY=zzz9",
        "Secret=top",
        "regular line",
    ]
    r = client.get(f"{BASE}/{sid}/runtime/logs", params={"tail": 10}, headers=h)
    assert r.status_code == 200, r.text
    lines = r.json()["lines"]
    text_ = "\n".join(lines)
    for leaked in (SECRET, "abc123def", "hunter2", "zzz9", "top"):
        assert leaked not in text_, leaked
    assert lines[0] == "server up" and lines[-1] == "regular line" and len(lines) == 5
    assert "token=[redacted]" in lines[2]
    assert client.get(f"{BASE}/{sid}/runtime/logs", params={"tail": 0}, headers=h).status_code == 422
    assert client.get(f"{BASE}/{sid}/runtime/logs", params={"tail": 100000}, headers=h).status_code == 422


def test_delete_removes_runtime_objects_and_rows(runtime_world):
    client, app, fake, tenant_of = runtime_world
    tenant, h = tenant_of()
    sid = install(client, app, h)
    assert client.delete(f"{BASE}/{sid}", headers=h).status_code == 204
    assert fake.objects == set() and fake.calls[-1][0] == "delete"
    with app.state.database_engine.connect() as conn:
        for table in ("mcp_servers", "mcp_runtimes", "mcp_server_secrets"):
            assert conn.execute(text(f"SELECT count(*) FROM {table} WHERE tenant_id = :t"), {"t": tenant}).scalar() == 0


def test_delete_is_idempotent_across_a_crash(runtime_world):
    client, app, fake, tenant_of = runtime_world
    tenant, h = tenant_of()
    sid = install(client, app, h)
    fake.crash_after_delete = True  # the CR is gone, then the call dies before the rows are removed
    first = client.delete(f"{BASE}/{sid}", headers=h)
    assert first.status_code == 502 and "retry" in first.json()["detail"]
    got = client.get(f"{BASE}/{sid}", headers=h)
    assert got.status_code == 200 and got.json()["enabled"] is False  # nothing may start it meanwhile
    # Re-running delete: the CR is already gone, which is fine.
    assert client.delete(f"{BASE}/{sid}", headers=h).status_code == 204
    assert client.get(f"{BASE}/{sid}", headers=h).status_code == 404
    assert fake.objects == set()
    with app.state.database_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM mcp_runtimes WHERE tenant_id = :t"), {"t": tenant}).scalar() == 0


def test_patch_headers_keeps_env_secrets_and_managed_servers_take_no_url_or_headers(runtime_world):
    client, app, _fake, tenant_of = runtime_world
    tenant, h = tenant_of()
    sid = install(client, app, h)
    assert client.patch(f"{BASE}/{sid}", headers=h, json={"headers": {"X-A": "b"}}).status_code == 422
    assert client.patch(f"{BASE}/{sid}", headers=h, json={"url": "https://mcp.example.com/x"}).status_code == 422
    assert client.patch(f"{BASE}/{sid}", headers=h, json={"enabled": False}).json()["env_names"] == ["API_TOKEN"]
    ext = client.post(BASE, headers=h, json={"name": "ext", "url": "https://mcp.example.com/mcp",
                                             "headers": {"X-A": "b"}}).json()["id"]
    with app.state.database_engine.begin() as conn:  # an env secret on the same server must survive a header replace
        conn.execute(text(
            "INSERT INTO mcp_server_secrets (tenant_id, server_id, kind, header_name, ciphertext, key_id) "
            "VALUES (:t, :s, 'env', 'E', 'x', 'k')"), {"t": tenant, "s": ext})
    r = client.patch(f"{BASE}/{ext}", headers=h, json={"headers": {"X-C": "d"}})
    assert r.json()["header_names"] == ["x-c"]
    with app.state.database_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM mcp_server_secrets WHERE server_id = :s AND kind = 'env'"),
                            {"s": ext}).scalar() == 1


def test_test_route_uses_the_managed_endpoint_and_needs_a_running_pod(runtime_world, monkeypatch):
    from eap_api.routes import mcp_servers as routes

    client, app, fake, tenant_of = runtime_world
    _t, h = tenant_of()
    sid = install(client, app, h)
    seen: dict = {}

    async def fake_list(url, headers, policy, timeout):
        seen.update(url=url, headers=headers)
        # the policy is the managed one: only that endpoint passes
        policy.check_url(url)
        from eap_api.mcp_servers.connection import ProbeResult
        return ProbeResult(status="ok", tools=[{"name": "t", "description": None, "input_schema": {}}])

    monkeypatch.setattr(routes, "list_server_tools", fake_list)
    r = client.post(f"{BASE}/{sid}/test", headers=h)
    assert r.json() == {"status": "error", "error_code": "runtime_not_running", "tools": []}
    fake.make_ready()
    client.get(f"{BASE}/{sid}/runtime", headers=h)
    r = client.post(f"{BASE}/{sid}/test", headers=h)
    assert r.json()["status"] == "ok", r.text
    assert seen["url"].startswith("http://") and ".svc.cluster.local" in seen["url"] and seen["headers"] == {}


# --- review follow-ups (task 7b) -------------------------------------------------------------------


def make_running(client, app, fake, h, sid):
    fake.make_ready()
    assert client.get(f"{BASE}/{sid}/runtime", headers=h).json()["state"] == "running"


def test_stop_claims_stopping_then_stopped_and_restart_starts_only_after_stopped(runtime_world):
    client, app, fake, tenant_of = runtime_world
    _t, h = tenant_of()
    sid = install(client, app, h)
    make_running(client, app, fake, h, sid)
    seen: list[str] = []
    fake.on_stop = lambda: seen.append(state(app, sid).state)
    r = client.post(f"{BASE}/{sid}/runtime/stop", headers=h)
    assert r.status_code == 200 and r.json()["state"] == "stopped"
    assert seen == ["stopping"]  # the adapter ran while the row was `stopping`, so a start could not slip in
    seen.clear()
    r = client.post(f"{BASE}/{sid}/runtime/restart", headers=h)
    assert r.json()["state"] == "starting" and seen == ["stopping"]
    assert [c[0] for c in fake.calls[-2:]] == ["stop", "start"]


def test_a_late_stop_does_not_clobber_a_row_that_moved_on(runtime_world):
    client, app, fake, tenant_of = runtime_world
    _t, h = tenant_of()
    sid = install(client, app, h)
    make_running(client, app, fake, h, sid)

    def interfere():  # something moves the row out of `stopping` while the adapter is stopping the pod
        with app.state.database_engine.begin() as conn:
            conn.execute(text("UPDATE mcp_runtimes SET state = 'failed', last_error = 'x' WHERE server_id = :s"), {"s": sid})

    fake.on_stop = interfere
    client.post(f"{BASE}/{sid}/runtime/stop", headers=h)
    assert state(app, sid).state == "failed"  # the final UPDATE is conditional on `stopping`
    starts = len(fake.specs)
    fake.on_stop = interfere
    r = client.post(f"{BASE}/{sid}/runtime/restart", headers=h)
    assert len(fake.specs) == starts  # the stop did not end in `stopped`: no start


def test_adapter_errors_on_start_stop_restart_answer_502_with_a_failed_row(runtime_world):
    client, app, fake, tenant_of = runtime_world
    _t, h = tenant_of()
    sid = install(client, app, h)
    make_running(client, app, fake, h, sid)
    fake.fail_stop = True
    for action in ("stop", "restart"):
        make_running_row(app, sid)
        r = client.post(f"{BASE}/{sid}/runtime/{action}", headers=h)
        assert r.status_code == 502, (action, r.text)
        assert "boom" not in r.text
        assert tuple(state(app, sid)) == ("failed", "stop_failed: RuntimeError")
    fake.fail_stop = False
    fake.fail_start = True
    for action in ("start", "restart"):
        r = client.post(f"{BASE}/{sid}/runtime/{action}", headers=h)
        assert r.status_code == 502, (action, r.text)
        assert "abc123" not in r.text
        assert tuple(state(app, sid)) == ("failed", "start_failed: RuntimeError")


def make_running_row(app, sid):
    with app.state.database_engine.begin() as conn:
        conn.execute(text("UPDATE mcp_runtimes SET state = 'running', last_error = NULL WHERE server_id = :s"), {"s": sid})


def last_used(app, sid):
    with app.state.database_engine.connect() as conn:
        return conn.execute(text("SELECT last_used_at FROM mcp_runtimes WHERE server_id = :s"), {"s": sid}).scalar()


def test_api_starts_and_reconcile_set_last_used_at(runtime_world):
    client, app, fake, tenant_of = runtime_world
    _t, h = tenant_of()
    sid = install(client, app, h)
    assert last_used(app, sid) is not None  # claimed `starting` by the install
    with app.state.database_engine.begin() as conn:
        conn.execute(text("UPDATE mcp_runtimes SET last_used_at = NULL WHERE server_id = :s"), {"s": sid})
    fake.make_ready()
    client.get(f"{BASE}/{sid}/runtime", headers=h)  # reconcile starting -> running
    assert state(app, sid).state == "running" and last_used(app, sid) is not None
    client.post(f"{BASE}/{sid}/runtime/stop", headers=h)
    with app.state.database_engine.begin() as conn:
        conn.execute(text("UPDATE mcp_runtimes SET last_used_at = NULL WHERE server_id = :s"), {"s": sid})
    client.post(f"{BASE}/{sid}/runtime/start", headers=h)
    assert last_used(app, sid) is not None


def _resolve(app, sid, tenant, fake):
    import anyio
    from starlette.requests import Request

    from eap_api.routes import mcp_servers as routes
    from sqlalchemy.orm import Session
    from eap_secrets.outbound import OutboundPolicy

    with Session(app.state.database_engine) as db:
        server = db.get(routes.McpServerModel, sid)
        target = routes._target(db, server)
    request = Request({"type": "http", "app": app, "headers": []})

    async def public(host, port):
        return ["93.184.215.14"]

    return anyio.run(routes._resolve_target, request, tenant, sid, target, fake, OutboundPolicy(resolver=public))


def test_idle_is_resolved_like_running_and_touches_last_used_at(runtime_world):
    client, app, fake, tenant_of = runtime_world
    tenant, h = tenant_of()
    sid = install(client, app, h)
    make_running(client, app, fake, h, sid)
    with app.state.database_engine.begin() as conn:
        conn.execute(text("UPDATE mcp_runtimes SET state = 'idle', last_used_at = NULL WHERE server_id = :s"), {"s": sid})
    url, _policy, code = _resolve(app, sid, tenant, fake)
    assert code is None and url and ".svc.cluster.local" in url
    assert last_used(app, sid) is not None
    with app.state.database_engine.begin() as conn:
        conn.execute(text("UPDATE mcp_runtimes SET state = 'running', last_used_at = NULL WHERE server_id = :s"), {"s": sid})
    assert _resolve(app, sid, tenant, fake)[2] is None and last_used(app, sid) is not None


def test_resolve_target_is_scoped_to_the_tenant(runtime_world):
    client, app, fake, tenant_of = runtime_world
    tenant, h = tenant_of()
    sid = install(client, app, h)
    make_running(client, app, fake, h, sid)
    assert _resolve(app, sid, "someone-else", fake)[2] == "runtime_missing"


def test_runtime_logs_mask_the_other_assignment_shapes(runtime_world):
    client, app, fake, tenant_of = runtime_world
    _t, h = tenant_of()
    sid = install(client, app, h)
    fake.log_lines = [
        "auth token: abc111xyz",
        'json {"password":"p4ss w0rd","user":"u"}',
        "run --api-key cli222key --verbose",
        "Authorization: Bearer eyJhbGciOi.payload.sig",
        "secret = 'two words 333' done",
        "plain line",
    ]
    r = client.get(f"{BASE}/{sid}/runtime/logs", headers=h)
    out = "\n".join(r.json()["lines"])
    for leaked in ("abc111xyz", "p4ss", "w0rd", "cli222key", "eyJhbGciOi", "two words", "333"):
        assert leaked not in out, (leaked, out)
    assert "token: [redacted]" in out and "--verbose" in out and '"user":"u"' in out and "plain line" in out
    assert "Authorization: Bearer [redacted]" in out and "--api-key [redacted]" in out
