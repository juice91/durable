"""Secret input (Task A): the tenant secret store routes. Values are write-only."""

from __future__ import annotations

import logging
import uuid

import pytest
from sqlalchemy import text

from eap_secrets import KEY_ENV, generate_key

from .conftest import get_auth_header

FAKE_VALUE = "fake-not-a-real-credential-0001"
API = "/api/v1"


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setenv(KEY_ENV, generate_key())


@pytest.fixture
def tenant_secrets_cleanup(client):
    tenants: list[str] = []
    yield tenants
    with client.app.state.database_engine.begin() as conn:
        for t in tenants:
            conn.execute(text("DELETE FROM tenant_secrets WHERE tenant_id = :t"), {"t": t})


def _tenant(cleanup) -> tuple[str, dict[str, str]]:
    t = f"t{uuid.uuid4().hex[:10]}"
    cleanup.append(t)
    return t, get_auth_header(t, "alice")


def test_put_list_delete_never_returns_the_value(client, keyed, tenant_secrets_cleanup, caplog):
    caplog.set_level(logging.DEBUG)
    _, h = _tenant(tenant_secrets_cleanup)
    r = client.put(f"{API}/secrets/slack-token", json={"value": FAKE_VALUE}, headers=h)
    assert r.status_code == 204 and r.content == b""
    listed = client.get(f"{API}/secrets", headers=h)
    assert listed.status_code == 200
    body = listed.json()
    assert [s["name"] for s in body] == ["slack-token"]
    assert set(body[0]) == {"name", "created_at", "updated_at"}
    assert FAKE_VALUE not in listed.text
    # An upsert keeps one row and moves updated_at.
    assert client.put(f"{API}/secrets/slack-token", json={"value": FAKE_VALUE + "-2"}, headers=h).status_code == 204
    again = client.get(f"{API}/secrets", headers=h).json()
    assert len(again) == 1 and again[0]["created_at"] == body[0]["created_at"]
    assert client.delete(f"{API}/secrets/slack-token", headers=h).status_code == 204
    assert client.get(f"{API}/secrets", headers=h).json() == []
    assert FAKE_VALUE not in caplog.text


def test_value_is_stored_encrypted_and_resolves(client, keyed, tenant_secrets_cleanup):
    from eap_run_store.secrets import SecretStore
    from eap_secrets import load_keyring

    t, h = _tenant(tenant_secrets_cleanup)
    assert client.put(f"{API}/secrets/api.key", json={"value": FAKE_VALUE}, headers=h).status_code == 204
    engine = client.app.state.database_engine
    with engine.connect() as conn:
        raw = conn.execute(text("SELECT ciphertext, created_by FROM tenant_secrets WHERE tenant_id = :t"), {"t": t}).one()
    assert FAKE_VALUE.encode() not in bytes(raw[0]) and raw[1] == "alice"
    store = SecretStore(engine, load_keyring())
    assert store.resolve(t, "api.key") == FAKE_VALUE
    assert store.resolve(t, "missing") is None
    assert store.resolve("another-tenant", "api.key") is None


def test_delete_unknown_is_404_with_code(client, keyed, tenant_secrets_cleanup):
    _, h = _tenant(tenant_secrets_cleanup)
    r = client.delete(f"{API}/secrets/nope", headers=h)
    assert r.status_code == 404 and r.json()["code"] == "secret_not_found"


@pytest.mark.parametrize("name", ["Upper", "1abc", "has space", "x" * 65, "a/b", "a:b"])
def test_invalid_name_is_422(client, keyed, tenant_secrets_cleanup, name):
    _, h = _tenant(tenant_secrets_cleanup)
    r = client.put(f"{API}/secrets/{name.replace(' ', '%20').replace('/', '%2F')}", json={"value": FAKE_VALUE}, headers=h)
    assert r.status_code in (404, 422)
    if r.status_code == 422:
        assert r.json()["code"] == "secret_name_invalid"
    assert FAKE_VALUE not in r.text


@pytest.mark.parametrize("body", [{}, {"value": ""}, {"value": "x" * 8193}, {"value": 5}, {"value": "a", "x": 1}])
def test_invalid_value_is_422_and_not_echoed(client, keyed, tenant_secrets_cleanup, body):
    _, h = _tenant(tenant_secrets_cleanup)
    r = client.put(f"{API}/secrets/tok", json=body, headers=h)
    assert r.status_code == 422
    assert "x" * 50 not in r.text


def test_max_length_value_is_accepted(client, keyed, tenant_secrets_cleanup):
    _, h = _tenant(tenant_secrets_cleanup)
    assert client.put(f"{API}/secrets/tok", json={"value": "v" * 8192}, headers=h).status_code == 204


def test_without_the_platform_key_the_write_is_503(client, monkeypatch, tenant_secrets_cleanup):
    monkeypatch.delenv(KEY_ENV, raising=False)
    _, h = _tenant(tenant_secrets_cleanup)
    r = client.put(f"{API}/secrets/tok", json={"value": FAKE_VALUE}, headers=h)
    assert r.status_code == 503 and FAKE_VALUE not in r.text
    # Listing names needs no key.
    assert client.get(f"{API}/secrets", headers=h).status_code == 200


def test_api_key_principal_is_401(client, keyed):
    r = client.get(f"{API}/secrets", headers={"Authorization": "Bearer pos_" + "0" * 24 + "_" + "a" * 43})
    assert r.status_code == 401
    assert client.get(f"{API}/secrets").status_code == 401


def test_request_logs_carry_neither_value_nor_query(client, keyed, tenant_secrets_cleanup, caplog):
    caplog.set_level(logging.DEBUG)
    _, h = _tenant(tenant_secrets_cleanup)
    client.put(f"{API}/secrets/tok", json={"value": FAKE_VALUE}, headers=h)
    client.put(f"{API}/secrets/tok", json={"value": ""}, headers=h)  # a 422 path too
    assert FAKE_VALUE not in caplog.text


def test_ref_helpers():
    from eap_schemas.secrets import SECRET_REF_PREFIX, SECRET_NAME_PATTERN, is_secret_ref

    assert SECRET_REF_PREFIX == "secret:"
    assert is_secret_ref("secret:slack-token") == "slack-token"
    for bad in ["secret:", "secret:Bad", "Secret:a", "x secret:a", "secret:a b", "secret:a\n", 5, None, "secret:" + "a" * 65]:
        assert is_secret_ref(bad) is None
    assert SECRET_NAME_PATTERN == r"^[a-z][a-z0-9_.-]{0,63}$"
