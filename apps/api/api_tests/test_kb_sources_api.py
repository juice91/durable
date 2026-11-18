"""Connections and knowledge-base sources: the routes of the kb source sync (brief §14)."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

import pytest
from sqlalchemy import text

from eap_kb_sources import ConnectionCheck, SourceError
from eap_kb_sources.fake import FakeCatalogAdapter
from eap_kb_sources.registry import register_adapter
from eap_secrets import KEY_ENV, generate_key

from eap_api.launcher import get_run_launcher

from .conftest import get_auth_header
from .run_helpers import PG_URL, requires_pg

pytestmark = requires_pg
API = "/api/v1"
SENTINEL = "sentinel-client-secret-VALUE-0001"
REFS = {"client_id": "secret:conn-id", "client_secret": "secret:conn-secret"}
CONN = {"name": "ti", "kind": "oauth2_client_credentials", "base_url": "https://transact.example.com",
        "config": {"provider": "fake"}, "credential_refs": REFS}


class Launcher:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.fail = False

    def launch_kb_source_sync(self, **kw) -> None:
        self.calls.append(kw)
        if self.fail:
            raise RuntimeError("postgresql://u:pw@db/x")


class Adapter(FakeCatalogAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.sleep = 0.0
        self.result = ConnectionCheck(True)
        self.raises: Exception | None = None

    async def check_connection(self, connection, secrets):
        values = {k: secrets.resolve(ref) for k, ref in connection.secret_refs.items()}
        self.secret_values.append(values)
        if self.sleep:
            await asyncio.sleep(self.sleep)
        if self.raises:
            raise self.raises
        return self.result


@pytest.fixture
def env(client, monkeypatch):
    monkeypatch.setenv(KEY_ENV, generate_key())
    monkeypatch.setenv("EAP_ALLOWED_EMBEDDING_MODELS", "test:*")
    adapter, launcher = Adapter(), Launcher()
    register_adapter("product_catalog", "fake", lambda: adapter)
    client.app.dependency_overrides[get_run_launcher] = lambda: launcher
    tenants: list[str] = []

    def tenant(name="alice"):
        t = f"t{uuid.uuid4().hex[:10]}"
        tenants.append(t)
        return t, get_auth_header(t, name)

    yield client, adapter, launcher, tenant
    client.app.dependency_overrides.pop(get_run_launcher, None)
    with client.app.state.database_engine.begin() as conn:
        for t in tenants:
            conn.execute(text("DELETE FROM execution_events WHERE tenant_id = :t"), {"t": t})
            conn.execute(text("DELETE FROM runs WHERE tenant_id = :t"), {"t": t})
            conn.execute(text("DELETE FROM knowledge_bases WHERE tenant_id = :t"), {"t": t})
            conn.execute(text("DELETE FROM settings_connections WHERE tenant_id = :t"), {"t": t})
            conn.execute(text("DELETE FROM tenant_secrets WHERE tenant_id = :t"), {"t": t})


def mk_conn(client, h, **over):
    r = client.post(f"{API}/settings/connections", json={**CONN, **over}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


def mk_kb(client, h, name="docs"):
    r = client.post(f"{API}/knowledge-bases", json={"name": name, "embedding_model": "test:hash"}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def mk_source(client, h, kb, conn_id, **over):
    body = {"connection_id": conn_id, "name": "parts", "kind": "product_catalog", "provider": "fake", "config": {}}
    r = client.post(f"{API}/knowledge-bases/{kb}/sources", json={**body, **over}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


# ------------------------------------------------------------------------------------ connections


def test_connection_crud_echoes_refs_never_values(env):
    client, _, _, tenant = env
    _, h = tenant()
    made = mk_conn(client, h)
    assert made["credential_refs"] == REFS and made["enabled"] is True and made["last_status"] is None
    assert set(made) >= {"id", "name", "kind", "base_url", "config", "credential_refs", "enabled",
                         "last_checked_at", "last_status", "last_error_code", "created_at", "updated_at"}
    assert client.get(f"{API}/settings/connections", headers=h).json()["items"][0]["id"] == made["id"]
    assert client.get(f"{API}/settings/connections/{made['id']}", headers=h).json()["name"] == "ti"
    patched = client.patch(f"{API}/settings/connections/{made['id']}",
                           json={"enabled": False, "credential_refs": {"client_id": "secret:other"}}, headers=h)
    assert patched.status_code == 200 and patched.json()["enabled"] is False
    assert patched.json()["credential_refs"] == {"client_id": "secret:other"}
    dup = client.post(f"{API}/settings/connections", json=CONN, headers=h)
    assert dup.status_code == 409 and dup.json()["code"] == "connection_name_conflict"
    assert client.delete(f"{API}/settings/connections/{made['id']}", headers=h).status_code == 204
    assert client.get(f"{API}/settings/connections/{made['id']}", headers=h).status_code == 404


@pytest.mark.parametrize("refs", [{"client_secret": SENTINEL}, {"client_secret": "secret:Bad Name"},
                                  {"client_secret": 5}, {"": "secret:a"}])
def test_plaintext_looking_refs_are_refused_without_echo(env, refs, caplog):
    client, _, _, tenant = env
    _, h = tenant()
    caplog.set_level(logging.DEBUG)
    r = client.post(f"{API}/settings/connections", json={**CONN, "credential_refs": refs}, headers=h)
    assert r.status_code == 422 and r.json()["code"] == "secret_ref_required"
    assert SENTINEL not in r.text and SENTINEL not in caplog.text
    made = mk_conn(client, h)
    p = client.patch(f"{API}/settings/connections/{made['id']}", json={"credential_refs": refs}, headers=h)
    assert p.status_code == 422 and p.json()["code"] == "secret_ref_required" and SENTINEL not in p.text


def test_connection_validation(env):
    client, _, _, tenant = env
    _, h = tenant()
    assert client.post(f"{API}/settings/connections", json={**CONN, "kind": "basic"}, headers=h).status_code == 422
    assert client.post(f"{API}/settings/connections", json={**CONN, "base_url": "http://x.example"}, headers=h).status_code == 422
    assert client.post(f"{API}/settings/connections", json={**CONN, "config": {}}, headers=h).status_code == 422  # provider


def test_delete_connection_in_use_is_409(env):
    client, _, _, tenant = env
    _, h = tenant()
    conn = mk_conn(client, h)
    mk_source(client, h, mk_kb(client, h), conn["id"])
    r = client.delete(f"{API}/settings/connections/{conn['id']}", headers=h)
    assert r.status_code == 409 and r.json()["code"] == "connection_in_use"


def test_check_resolves_inside_the_request_and_persists_without_leaking(env, caplog):
    client, adapter, _, tenant = env
    caplog.set_level(logging.DEBUG)
    t, h = tenant()
    for name, value in (("conn-id", "id-value"), ("conn-secret", SENTINEL)):
        assert client.put(f"{API}/secrets/{name}", json={"value": value}, headers=h).status_code == 200
    conn = mk_conn(client, h)
    r = client.post(f"{API}/settings/connections/{conn['id']}/check", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True and r.json()["error_code"] is None and r.json()["checked_at"]
    assert adapter.secret_values[-1] == {"client_id": "id-value", "client_secret": SENTINEL}
    assert SENTINEL not in r.text and SENTINEL not in caplog.text
    stored = client.get(f"{API}/settings/connections/{conn['id']}", headers=h).json()
    assert stored["last_status"] == "ok" and stored["last_checked_at"] and SENTINEL not in str(stored)

    adapter.result = ConnectionCheck(False, "connection_auth_failed")
    bad = client.post(f"{API}/settings/connections/{conn['id']}/check", headers=h).json()
    assert bad["ok"] is False and bad["error_code"] == "connection_auth_failed"
    stored = client.get(f"{API}/settings/connections/{conn['id']}", headers=h).json()
    assert stored["last_status"] == "error" and stored["last_error_code"] == "connection_auth_failed"


def test_check_error_codes(env, monkeypatch):
    client, adapter, _, tenant = env
    _, h = tenant()
    conn = mk_conn(client, h)

    def check():
        r = client.post(f"{API}/settings/connections/{conn['id']}/check", headers=h)
        assert r.status_code == 200, r.text
        return r.json()["error_code"]

    assert check() == "secret_missing"  # no secret stored yet
    client.put(f"{API}/secrets/conn-id", json={"value": "a"}, headers=h)
    client.put(f"{API}/secrets/conn-secret", json={"value": SENTINEL}, headers=h)
    adapter.raises = RuntimeError(f"boom {SENTINEL}")
    assert check() == "connection_response_invalid"
    adapter.raises = SourceError("connection_unreachable", "no route")
    assert check() == "connection_unreachable"
    adapter.raises = None
    monkeypatch.setenv("EAP_KB_SOURCE_CHECK_TIMEOUT_SECONDS", "0.5")
    adapter.sleep = 3
    assert check() == "connection_unreachable"  # bounded
    adapter.sleep = 0
    adapter.result = ConnectionCheck(False, f"leak {SENTINEL}")
    assert check() == "connection_response_invalid"  # only stable codes leave
    client.patch(f"{API}/settings/connections/{conn['id']}", json={"enabled": False}, headers=h)
    assert check() == "connection_disabled"
    assert client.post(f"{API}/settings/connections/{uuid.uuid4()}/check", headers=h).status_code == 404


def test_check_timeout_is_clamped(monkeypatch):
    from eap_kb_sources.check import check_timeout_seconds

    monkeypatch.delenv("EAP_KB_SOURCE_CHECK_TIMEOUT_SECONDS", raising=False)
    assert check_timeout_seconds() == 10
    for raw, want in (("0.1", 0.5), ("99", 30), ("abc", 10), ("5", 5)):
        monkeypatch.setenv("EAP_KB_SOURCE_CHECK_TIMEOUT_SECONDS", raw)
        assert check_timeout_seconds() == want


# -------------------------------------------------------------------------------------- sources


def test_source_crud_and_validation(env):
    client, _, _, tenant = env
    _, h = tenant()
    conn, kb = mk_conn(client, h), None
    kb = mk_kb(client, h)
    src = mk_source(client, h, kb, conn["id"], config={"daily_limit": 5})
    assert src["config"] == {"daily_limit": 5} and src["enabled"] is True and src["provider"] == "fake"
    assert src["last_successful_sync_at"] is None and src["last_checkpoint_summary"] == {"segments": 0, "has_cursor": False}
    assert "last_checkpoint" not in src
    base = f"{API}/knowledge-bases/{kb}/sources"
    assert [s["id"] for s in client.get(base, headers=h).json()["items"]] == [src["id"]]
    assert client.get(f"{base}/{src['id']}", headers=h).json()["name"] == "parts"
    p = client.patch(f"{base}/{src['id']}", json={"name": "renamed", "enabled": False, "config": {"daily_limit": 9}}, headers=h)
    assert p.status_code == 200 and p.json()["name"] == "renamed" and p.json()["enabled"] is False
    assert client.patch(f"{base}/{src['id']}", json={"config": {"daily_limit": -1}}, headers=h).status_code == 422
    bad = client.post(base, json={"connection_id": conn["id"], "name": "x", "kind": "product_catalog",
                                  "provider": "fake", "config": {"daily_limit": "many"}}, headers=h)
    assert bad.status_code == 422 and bad.json()["code"] == "invalid_source_config"
    unknown = client.post(base, json={"connection_id": conn["id"], "name": "y", "kind": "product_catalog",
                                      "provider": "nope", "config": {}}, headers=h)
    assert unknown.status_code == 422 and unknown.json()["code"] == "source_adapter_not_found"
    dup = client.post(base, json={"connection_id": conn["id"], "name": "renamed", "kind": "product_catalog",
                                  "provider": "fake"}, headers=h)
    assert dup.status_code == 409
    missing_conn = client.post(base, json={"connection_id": str(uuid.uuid4()), "name": "z", "kind": "product_catalog",
                                           "provider": "fake"}, headers=h)
    assert missing_conn.status_code == 404
    assert client.get(f"{API}/knowledge-bases/{uuid.uuid4()}/sources", headers=h).status_code == 404
    other_kb = mk_kb(client, h, "other")
    assert client.get(f"{API}/knowledge-bases/{other_kb}/sources/{src['id']}", headers=h).status_code == 404


def test_sync_route_queues_through_the_shared_launch(env):
    client, _, launcher, tenant = env
    t, h = tenant()
    kb = mk_kb(client, h)
    src = mk_source(client, h, kb, mk_conn(client, h)["id"])
    url = f"{API}/knowledge-bases/{kb}/sources/{src['id']}/sync"
    r = client.post(url, json={"mode": "targeted", "part_numbers": ["LM317"]}, headers=h)
    assert r.status_code == 202, r.text
    run_id = r.json()["run_id"]
    assert r.json() == {"run_id": run_id, "status": "queued"}
    (call,) = launcher.calls
    assert str(call["run_id"]) == run_id and call["tenant_id"] == t and call["source_id"] == src["id"]
    row = client.get(f"{API}/runs/{run_id}", headers=h).json()
    assert row["kind"] == "kb_source_sync" and row["status"] == "queued"

    same = client.post(url, json={"mode": "targeted", "part_numbers": ["LM317"], "run_id": run_id}, headers=h)
    assert same.status_code == 202 and same.json()["run_id"] == run_id
    assert client.post(url, json={"mode": "full", "part_numbers": ["x"]}, headers=h).json()["code"] == "part_numbers_not_allowed"
    assert client.post(url, json={"mode": "bogus"}, headers=h).status_code == 422
    assert client.post(url, json={"mode": "targeted", "part_numbers": ["a b"]}, headers=h).status_code == 422
    assert client.post(url, json={"mode": "full"}, headers=h).status_code == 202  # part_numbers default []
    client.patch(f"{API}/knowledge-bases/{kb}/sources/{src['id']}", json={"enabled": False}, headers=h)
    off = client.post(url, json={"mode": "full"}, headers=h)
    assert off.status_code == 409 and off.json()["code"] == "source_disabled"


def test_sync_route_accepts_restart_for_a_walk_only(env):
    client, _, launcher, tenant = env
    _, h = tenant()
    kb = mk_kb(client, h)
    src = mk_source(client, h, kb, mk_conn(client, h)["id"])
    url = f"{API}/knowledge-bases/{kb}/sources/{src['id']}/sync"
    assert client.post(url, json={"mode": "full", "restart": True}, headers=h).status_code == 202
    assert json.loads(launcher.calls[-1]["request_json"]) == {"mode": "full", "part_numbers": [], "restart": True}
    assert client.post(url, json={"mode": "full"}, headers=h).status_code == 202
    assert "restart" not in json.loads(launcher.calls[-1]["request_json"])
    bad = client.post(url, json={"mode": "targeted", "part_numbers": ["LM317"], "restart": True}, headers=h)
    assert bad.status_code == 422 and bad.json()["code"] == "invalid_request"
    assert client.post(url, json={"mode": "full", "restart": "yes"}, headers=h).status_code == 422


def test_sync_enqueue_failure_is_503_and_retry_requeues(env):
    client, _, launcher, tenant = env
    _, h = tenant()
    kb = mk_kb(client, h)
    src = mk_source(client, h, kb, mk_conn(client, h)["id"])
    url = f"{API}/knowledge-bases/{kb}/sources/{src['id']}/sync"
    rid = str(uuid.uuid4())
    launcher.fail = True
    r = client.post(url, json={"mode": "full", "run_id": rid}, headers=h)
    assert r.status_code == 503 and r.json()["run_id"] == rid and "postgresql" not in r.text
    launcher.fail = False
    assert client.post(url, json={"mode": "full", "run_id": rid}, headers=h).status_code == 202


def test_syncs_list_and_report(env):
    client, _, _, tenant = env
    t, h = tenant()
    kb = mk_kb(client, h)
    src = mk_source(client, h, kb, mk_conn(client, h)["id"])
    base = f"{API}/knowledge-bases/{kb}/sources/{src['id']}"
    ids = [client.post(f"{base}/sync", json={"mode": "full"}, headers=h).json()["run_id"] for _ in range(2)]
    listed = client.get(f"{base}/syncs", headers=h).json()["items"]
    assert [s["run_id"] for s in listed] == ids[::-1]
    assert set(listed[0]) >= {"run_id", "source_id", "mode", "status", "counters", "errors", "started_at", "completed_at"}
    one = client.get(f"{base}/syncs/{ids[0]}", headers=h).json()
    assert one["status"] == "running" and one["report"] is None and one["run_status"] == "queued"
    report = {"run_id": ids[0], "status": "completed", "discovered": 3}
    from uuid import UUID

    rs = client.app.state.run_store if hasattr(client.app.state, "run_store") else None
    from eap_run_store import RunStore

    rs = RunStore(client.app.state.database_engine)
    rs.mark_running(t, UUID(ids[0]))
    import json as _json

    rs.finish_with_end(t, UUID(ids[0]), "completed", _json.dumps(report), None, end_data={"report": report})
    done = client.get(f"{base}/syncs/{ids[0]}", headers=h).json()
    assert done["report"] == report and done["run_status"] == "completed"
    assert client.get(f"{base}/syncs/{uuid.uuid4()}", headers=h).status_code == 404
    assert client.get(f"{base}/syncs?limit=1", headers=h).json()["items"][0]["run_id"] == ids[1]
