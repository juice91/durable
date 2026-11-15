"""AGUI.3: ApiKeyStore. Implementer's focused tests; the acceptance suite adds more."""

from __future__ import annotations

import logging
import pickle
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text

from eap_run_store import ApiKeyLimitExceeded, ApiKeyStore, normalize_origin, parse_api_key
from eap_run_store import api_keys as api_keys_mod
from eap_run_store.api_keys import hash_secret


@pytest.fixture
def store(engine):
    return ApiKeyStore(engine)


@pytest.fixture
def tenant(engine):
    t = f"test_tenant_{uuid4().hex[:8]}"
    yield t
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM api_keys WHERE tenant_id = :t"), {"t": t})


@pytest.fixture
def other_tenant(engine):
    t = f"test_tenant_{uuid4().hex[:8]}"
    yield t
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM api_keys WHERE tenant_id = :t"), {"t": t})


def test_create_returns_plaintext_once_and_stores_only_a_hash(store, engine, tenant, caplog):
    caplog.set_level(logging.DEBUG)
    created = store.create(tenant, name=" ci ", created_by="alice", agent_ids=["a", "a", "b"])
    key_id, secret = parse_api_key(created.key)
    assert key_id == created.record.id and created.record.prefix == f"pos_{key_id}_"
    assert created.record.name == "ci" and created.record.agent_ids == ("a", "b")
    assert created.record.scopes == ("agui:run",) and created.record.status() == "active"
    assert created.key not in repr(created)
    with engine.connect() as conn:
        row = conn.execute(text("SELECT * FROM api_keys WHERE id = :i"), {"i": key_id}).mappings().one()
    assert row["secret_hash"] == hash_secret(key_id, secret)
    assert secret not in str(dict(row))
    # No later read carries the secret.
    assert all(secret not in repr(r) for r in store.list(tenant))
    assert secret not in caplog.text


def test_authenticate_accepts_only_the_exact_active_key(store, tenant):
    created = store.create(tenant, name="k", created_by="alice")
    record = store.authenticate(created.key)
    assert record is not None and record.tenant_id == tenant and record.last_used_at is not None
    key_id, secret = parse_api_key(created.key)
    wrong = secret[:-1] + ("A" if secret[-1] != "A" else "B")
    unknown = f"pos_{'f' * 24}_{secret}"
    for bad in [f"pos_{key_id}_{wrong}", unknown, "pos_", "pos_xyz", created.key + "x", "", "Bearer x"]:
        assert store.authenticate(bad) is None
    store.revoke(tenant, key_id)
    assert store.authenticate(created.key) is None


def test_authenticate_uses_constant_time_compare_on_every_path(store, tenant, monkeypatch):
    calls = []
    real = api_keys_mod.hmac.compare_digest
    monkeypatch.setattr(api_keys_mod.hmac, "compare_digest", lambda a, b: calls.append(1) or real(a, b))
    created = store.create(tenant, name="k", created_by="alice")
    _, secret = parse_api_key(created.key)
    store.authenticate(created.key)
    store.authenticate(f"pos_{'e' * 24}_{secret}")
    store.authenticate("garbage")
    assert len(calls) == 3


def test_expired_key_is_refused(store, engine, tenant):
    created = store.create(
        tenant, name="k", created_by="alice", expires_at=datetime.now(timezone.utc) + timedelta(hours=1)
    )
    with engine.begin() as conn:
        conn.execute(text("UPDATE api_keys SET expires_at = now() - interval '1 second' WHERE id = :i"),
                     {"i": created.record.id})
    assert store.authenticate(created.key) is None
    assert store.get(tenant, created.record.id).status() == "expired"


def test_last_used_is_throttled(store, engine, tenant):
    created = store.create(tenant, name="k", created_by="alice")
    first = store.authenticate(created.key).last_used_at
    second = store.authenticate(created.key).last_used_at
    assert first == second


def test_tenant_scoping(store, tenant, other_tenant):
    created = store.create(tenant, name="k", created_by="alice")
    assert store.get(other_tenant, created.record.id) is None
    assert store.revoke(other_tenant, created.record.id) is None
    assert store.list(other_tenant) == []
    assert store.get(tenant, created.record.id) is not None


def test_revoke_is_idempotent(store, tenant):
    created = store.create(tenant, name="k", created_by="alice")
    first = store.revoke(tenant, created.record.id)
    second = store.revoke(tenant, created.record.id)
    assert first.revoked_at is not None and first.revoked_at == second.revoked_at
    assert second.status() == "revoked"
    assert store.revoke(tenant, "not-an-id") is None


def test_list_newest_first(store, tenant):
    a = store.create(tenant, name="a", created_by="alice")
    b = store.create(tenant, name="b", created_by="alice")
    assert [r.id for r in store.list(tenant)] == [b.record.id, a.record.id]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": ""},
        {"name": "x" * 101},
        {"scopes": ["admin"]},
        {"scopes": []},
        {"agent_ids": "abc"},
        {"agent_ids": [""]},
        {"allowed_origins": ["https://a.example/path"]},
        {"allowed_origins": ["ftp://a.example"]},
        {"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)},
        {"expires_at": datetime.now() + timedelta(days=1)},
    ],
)
def test_create_validation(store, tenant, kwargs):
    args = {"name": "k", "created_by": "alice", **kwargs}
    with pytest.raises(ValueError):
        store.create(tenant, **args)


def test_active_key_cap(store, tenant, monkeypatch):
    monkeypatch.setattr(api_keys_mod, "MAX_ACTIVE_API_KEYS_PER_TENANT", 2)
    a = store.create(tenant, name="a", created_by="alice")
    store.create(tenant, name="b", created_by="alice")
    with pytest.raises(ApiKeyLimitExceeded) as exc:
        store.create(tenant, name="c", created_by="alice")
    assert pickle.loads(pickle.dumps(exc.value)).limit == 2
    store.revoke(tenant, a.record.id)
    store.create(tenant, name="c", created_by="alice")


def test_normalize_origin():
    assert normalize_origin("HTTPS://App.Example:443") == "https://app.example"
    assert normalize_origin("http://localhost:3000/") == "http://localhost:3000"
    for bad in ["", "app.example", "https://u:p@a.example", "https://a.example?x=1", "https://a.example:99999"]:
        with pytest.raises(ValueError):
            normalize_origin(bad)


def test_one_allowlist_stores_the_other_as_empty(store, engine, tenant):
    agent_only = store.create(tenant, name="a", created_by="alice", agent_ids=["ag"])
    workflow_only = store.create(tenant, name="w", created_by="alice", workflow_ids=["wf"])
    neither = store.create(tenant, name="n", created_by="alice")
    both = store.create(tenant, name="b", created_by="alice", agent_ids=["ag"], workflow_ids=["wf"])
    assert (agent_only.record.agent_ids, agent_only.record.workflow_ids) == (("ag",), ())
    assert (workflow_only.record.agent_ids, workflow_only.record.workflow_ids) == ((), ("wf",))
    assert (neither.record.agent_ids, neither.record.workflow_ids) == (None, None)
    assert (both.record.agent_ids, both.record.workflow_ids) == (("ag",), ("wf",))
    with engine.connect() as conn:
        rows = dict(conn.execute(
            text("SELECT id, workflow_ids FROM api_keys WHERE id = :i"), {"i": agent_only.record.id}
        ).all())
    assert rows[agent_only.record.id] == []
