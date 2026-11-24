"""Personal API keys in ApiKeyStore: the owner columns, own-key listing and revoking, the per-person cap."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text

from eap_run_store import ApiKeyLimitExceeded, ApiKeyStore
from eap_run_store import api_keys as mod


@pytest.fixture
def store(engine):
    return ApiKeyStore(engine)


@pytest.fixture
def tenant(engine):
    t = f"test_tenant_{uuid4().hex[:8]}"
    yield t
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM api_keys WHERE tenant_id = :t"), {"t": t})


def test_owner_is_stored_and_a_tenant_key_has_none(store, tenant):
    mine = store.create(tenant, name="a", created_by="zitadel:u1", owner_subject="zitadel:u1", owner_email="u1@example.test")
    tenant_key = store.create(tenant, name="b", created_by="admin", owner_email="ignored@example.test")
    assert (mine.record.owner_subject, mine.record.owner_email) == ("zitadel:u1", "u1@example.test")
    assert (tenant_key.record.owner_subject, tenant_key.record.owner_email) == (None, None)
    assert store.authenticate(mine.key).owner_subject == "zitadel:u1"


def test_list_and_revoke_owned_never_touch_another_owner_or_tenant(store, tenant):
    a = store.create(tenant, name="a", created_by="u1", owner_subject="u1")
    b = store.create(tenant, name="b", created_by="u2", owner_subject="u2")
    t = store.create(tenant, name="t", created_by="admin")
    other = f"test_tenant_{uuid4().hex[:8]}"
    assert [k.id for k in store.list_owned(tenant, "u1")] == [a.record.id]
    assert store.list_owned(other, "u1") == []
    assert store.revoke_owned(tenant, "u1", b.record.id) is None
    assert store.revoke_owned(tenant, "u1", t.record.id) is None
    assert store.revoke_owned(other, "u1", a.record.id) is None
    assert store.authenticate(b.key) is not None and store.authenticate(t.key) is not None
    assert store.revoke_owned(tenant, "u1", a.record.id).status() == "revoked"
    assert store.revoke_owned(tenant, "u1", a.record.id) is not None  # idempotent
    assert store.authenticate(a.key) is None


def test_per_owner_cap_counts_only_active_personal_keys(store, tenant, monkeypatch):
    monkeypatch.setattr(mod, "MAX_ACTIVE_API_KEYS_PER_OWNER", 2)
    first = store.create(tenant, name="1", created_by="u1", owner_subject="u1")
    store.create(tenant, name="2", created_by="u1", owner_subject="u1")
    store.create(tenant, name="tenant", created_by="admin")  # not counted
    with pytest.raises(ApiKeyLimitExceeded) as exc:
        store.create(tenant, name="3", created_by="u1", owner_subject="u1")
    assert exc.value.per_owner and "per person" in str(exc.value)
    store.revoke_owned(tenant, "u1", first.record.id)
    store.create(tenant, name="3", created_by="u1", owner_subject="u1")
    store.create(tenant, name="x", created_by="u2", owner_subject="u2")


def test_a_blank_owner_is_refused(store, tenant):
    with pytest.raises(ValueError):
        store.create(tenant, name="a", created_by="u", owner_subject="")
