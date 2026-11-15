"""AGUI.3 acceptance: ApiKeyStore. Plaintext handling, one failure answer on every bad-key path
(same code path, checked by call counts, not clocks), tenant isolation, immediate revoke, id
uniqueness and concurrency."""

from __future__ import annotations

import dataclasses
import logging
import pickle
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text

from eap_run_store import ApiKeyLimitExceeded, ApiKeyStore, parse_api_key
from eap_run_store import api_keys as mod
from eap_run_store.api_keys import hash_secret


@pytest.fixture
def store(engine):
    return ApiKeyStore(engine)


def _mk_tenant(engine, request):
    t = f"test_tenant_{uuid4().hex[:8]}"

    def _clean():
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM api_keys WHERE tenant_id = :t"), {"t": t})

    request.addfinalizer(_clean)
    return t


@pytest.fixture
def tenant(engine, request):
    return _mk_tenant(engine, request)


@pytest.fixture
def other(engine, request):
    return _mk_tenant(engine, request)


def _row(engine, key_id):
    with engine.connect() as conn:
        return conn.execute(text("SELECT * FROM api_keys WHERE id = :i"), {"i": key_id}).mappings().one()


def test_plaintext_is_in_no_column_and_only_the_hash_is_stored(store, engine, tenant):
    c = store.create(tenant, name="n", created_by="alice", agent_ids=["a"], allowed_origins=["https://x.example"])
    key_id, secret = parse_api_key(c.key)
    row = _row(engine, key_id)
    assert row["secret_hash"] == hash_secret(key_id, secret) and len(row["secret_hash"]) == 64
    blob = repr(dict(row))
    assert secret not in blob and c.key not in blob
    # The hash is not the bare sha256 of the secret either (id is bound in).
    import hashlib
    assert row["secret_hash"] != hashlib.sha256(secret.encode()).hexdigest()


def test_record_never_carries_secret_or_hash_and_repr_hides_the_key(store, tenant):
    c = store.create(tenant, name="n", created_by="alice")
    _, secret = parse_api_key(c.key)
    fields = {f.name for f in dataclasses.fields(c.record)}
    assert not {"secret", "secret_hash", "key", "hash"} & fields
    for text_ in (repr(c), str(c), repr(c.record), str(c.record), repr(store.list(tenant)), repr(store.get(tenant, c.record.id))):
        assert secret not in text_ and c.key not in text_
        assert hash_secret(c.record.id, secret) not in text_


def test_no_log_record_contains_key_secret_or_hash_across_the_lifecycle(store, tenant, caplog):
    caplog.set_level(logging.DEBUG)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.DEBUG)
    c = store.create(tenant, name="n", created_by="alice")
    key_id, secret = parse_api_key(c.key)
    store.authenticate(c.key)
    store.authenticate(c.key[:-1] + ("A" if c.key[-1] != "A" else "B"))
    store.authenticate("pos_garbage")
    store.get(tenant, key_id)
    store.list(tenant)
    store.revoke(tenant, key_id)
    store.authenticate(c.key)
    # Parameters of the lookups (hash is only compared in Python; never sent to the DB for auth).
    for needle in (c.key, secret):
        assert needle not in caplog.text
    logging.getLogger("sqlalchemy.engine").setLevel(logging.NOTSET)


def test_errors_never_contain_a_key_or_hash(store, tenant, monkeypatch):
    for bad in ("", "x" * 5, "pos_" + "g" * 24 + "_" + "s" * 43, None, 5, b"pos_x"):
        assert store.authenticate(bad) is None
    monkeypatch.setattr(mod, "MAX_ACTIVE_API_KEYS_PER_TENANT", 1)
    c = store.create(tenant, name="n", created_by="a")
    _, secret = parse_api_key(c.key)
    with pytest.raises(ApiKeyLimitExceeded) as info:
        store.create(tenant, name="n2", created_by="a")
    assert secret not in str(info.value) and c.key not in repr(info.value)
    clone = pickle.loads(pickle.dumps(info.value))
    assert (clone.tenant_id, clone.limit) == (tenant, 1)
    with pytest.raises(ValueError) as v:
        store.create(tenant, name="", created_by="a")
    assert secret not in str(v.value)


def _bad_tokens(store, engine, tenant):
    """Every kind of key that must fail, keyed by what is wrong with it."""
    good = store.create(tenant, name="g", created_by="a")
    key_id, secret = parse_api_key(good.key)
    revoked = store.create(tenant, name="r", created_by="a")
    store.revoke(tenant, revoked.record.id)
    expired = store.create(tenant, name="e", created_by="a", expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
    with engine.begin() as conn:
        conn.execute(text("UPDATE api_keys SET expires_at = now() - interval '1 second' WHERE id = :i"),
                     {"i": expired.record.id})
    flipped = secret[:-1] + ("A" if secret[-1] != "A" else "B")
    return good, {
        "wrong_secret": f"pos_{key_id}_{flipped}",
        "wrong_first_char": f"pos_{key_id}_{'A' if secret[0] != 'A' else 'B'}{secret[1:]}",
        "unknown_id": f"pos_{'f' * 24}_{secret}",
        "malformed_short": "pos_abc",
        "malformed_chars": f"pos_{key_id}_{'!' * 43}",
        "malformed_trailing_newline": good.key + "\n",
        "malformed_upper_id": good.key.replace(key_id, key_id.upper(), 1) if any(ch.isalpha() for ch in key_id) else "pos_X",
        "no_prefix": f"{key_id}_{secret}",
        "empty": "",
        "revoked": revoked.key,
        "expired": expired.key,
    }


def test_every_bad_key_is_none_and_takes_the_same_compare_path(store, engine, tenant, monkeypatch):
    good, bad = _bad_tokens(store, engine, tenant)
    calls = []
    real = mod.hmac.compare_digest
    monkeypatch.setattr(mod.hmac, "compare_digest", lambda a, b: calls.append(1) or real(a, b))
    assert store.authenticate(good.key) is not None
    assert len(calls) == 1
    for name, token in bad.items():
        calls.clear()
        assert store.authenticate(token) is None, name
        # One constant-time comparison on every path (wrong, unknown, malformed, revoked, expired).
        assert len(calls) == 1, f"{name}: compare_digest called {len(calls)} times"


def test_wrong_secret_does_not_touch_last_used_and_revoked_key_with_right_secret_fails(store, engine, tenant):
    good, bad = _bad_tokens(store, engine, tenant)
    store.authenticate(bad["wrong_secret"])
    assert _row(engine, good.record.id)["last_used_at"] is None
    store.authenticate(bad["revoked"])
    assert store.get(tenant, parse_api_key(bad["revoked"])[0]).last_used_at is None


def test_a_hash_moved_to_another_row_does_not_authenticate_either_key(store, engine, tenant):
    a = store.create(tenant, name="a", created_by="x")
    b = store.create(tenant, name="b", created_by="x")
    with engine.begin() as conn:
        conn.execute(text("UPDATE api_keys SET secret_hash = (SELECT secret_hash FROM api_keys WHERE id = :a)"
                          " WHERE id = :b"), {"a": a.record.id, "b": b.record.id})
    assert store.authenticate(a.key) is not None
    assert store.authenticate(b.key) is None
    _, a_secret = parse_api_key(a.key)
    assert store.authenticate(f"pos_{b.record.id}_{a_secret}") is None  # a's secret under b's id


def test_revoke_is_effective_immediately_and_keeps_the_first_time(store, tenant):
    c = store.create(tenant, name="n", created_by="a")
    assert store.authenticate(c.key) is not None
    first = store.revoke(tenant, c.record.id)
    assert store.authenticate(c.key) is None
    again = store.revoke(tenant, c.record.id)
    assert first.revoked_at is not None and again.revoked_at == first.revoked_at
    assert store.get(tenant, c.record.id).status() == "revoked"


def test_tenant_isolation_on_every_method(store, tenant, other):
    a = store.create(tenant, name="a", created_by="x")
    b = store.create(other, name="b", created_by="y")
    assert [r.id for r in store.list(tenant)] == [a.record.id]
    assert [r.id for r in store.list(other)] == [b.record.id]
    assert store.get(other, a.record.id) is None and store.get(tenant, b.record.id) is None
    assert store.revoke(other, a.record.id) is None
    assert store.authenticate(a.key).revoked_at is None  # foreign revoke changed nothing
    assert store.authenticate(a.key).tenant_id == tenant and store.authenticate(b.key).tenant_id == other
    assert store.list("no_such_tenant") == [] and store.get("", a.record.id) is None
    assert store.revoke(tenant, "nope") is None and store.revoke(tenant, "0" * 24) is None


def test_the_cap_counts_only_this_tenants_active_keys(store, tenant, other, monkeypatch):
    monkeypatch.setattr(mod, "MAX_ACTIVE_API_KEYS_PER_TENANT", 2)
    a = store.create(tenant, name="a", created_by="x")
    store.create(tenant, name="b", created_by="x")
    store.create(other, name="o", created_by="x")  # another tenant is unaffected
    with pytest.raises(ApiKeyLimitExceeded):
        store.create(tenant, name="c", created_by="x")
    store.revoke(tenant, a.record.id)  # a revoked key frees a slot
    store.create(tenant, name="c", created_by="x")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"scopes": ["agui:run", "admin"]},
        {"scopes": []},
        {"scopes": "agui:run"},
        {"agent_ids": [""]},
        {"agent_ids": "abc"},
        {"workflow_ids": ["x" * 201]},
        {"agent_ids": [str(i) for i in range(101)]},
        {"allowed_origins": ["ftp://a.example"]},
        {"allowed_origins": ["https://u:p@a.example"]},
        {"allowed_origins": ["https://a.example/path"]},
        {"allowed_origins": [f"https://h{i}.example" for i in range(21)]},
        {"expires_at": datetime.now() + timedelta(days=1)},  # naive
        {"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)},
        {"name": " "},
        {"name": "x" * 101},
        {"created_by": ""},
    ],
)
def test_invalid_input_is_a_value_error_and_stores_nothing(store, tenant, kwargs):
    base = {"name": "n", "created_by": "a"}
    with pytest.raises(ValueError):
        store.create(tenant, **{**base, **kwargs})
    assert store.list(tenant) == []


def test_last_used_is_set_once_then_throttled_then_refreshed(store, engine, tenant):
    c = store.create(tenant, name="n", created_by="a")
    first = store.authenticate(c.key)
    assert first.last_used_at is not None
    stored = _row(engine, c.record.id)["last_used_at"]
    assert store.authenticate(c.key).last_used_at is not None
    assert _row(engine, c.record.id)["last_used_at"] == stored  # inside the throttle window
    with engine.begin() as conn:
        conn.execute(text("UPDATE api_keys SET last_used_at = now() - interval '2 minutes' WHERE id = :i"),
                     {"i": c.record.id})
    store.authenticate(c.key)
    assert _row(engine, c.record.id)["last_used_at"] > datetime.now(timezone.utc) - timedelta(seconds=30)


def test_expiry_is_checked_on_every_call(store, engine, tenant):
    c = store.create(tenant, name="n", created_by="a", expires_at=datetime.now(timezone.utc) + timedelta(hours=1))
    assert store.authenticate(c.key) is not None and c.record.status() == "active"
    with engine.begin() as conn:
        conn.execute(text("UPDATE api_keys SET expires_at = now() - interval '1 second' WHERE id = :i"),
                     {"i": c.record.id})
    assert store.authenticate(c.key) is None
    assert store.get(tenant, c.record.id).status() == "expired"
    assert [r.id for r in store.list(tenant)] == [c.record.id]  # still listed


def test_key_ids_are_unique_well_formed_and_secrets_differ(store, tenant, engine):
    made = [store.create(tenant, name=f"k{i}", created_by="a") for i in range(60)]
    ids = {m.record.id for m in made}
    assert len(ids) == 60
    assert all(len(i) == 24 and set(i) <= set("0123456789abcdef") for i in ids)
    assert len({parse_api_key(m.key)[1] for m in made}) == 60
    assert all(store.authenticate(m.key).id == m.record.id for m in made[:5])


def test_concurrent_creates_get_distinct_ids_and_working_keys(store, tenant):
    with ThreadPoolExecutor(max_workers=12) as pool:
        made = list(pool.map(lambda i: store.create(tenant, name=f"c{i}", created_by="a"), range(24)))
    assert len({m.record.id for m in made}) == 24
    assert len(store.list(tenant)) == 24
    assert all(store.authenticate(m.key).id == m.record.id for m in made)


def test_concurrent_creates_never_exceed_the_cap(store, tenant, monkeypatch):
    monkeypatch.setattr(mod, "MAX_ACTIVE_API_KEYS_PER_TENANT", 5)

    def attempt(i):
        try:
            store.create(tenant, name=f"c{i}", created_by="a")
            return True
        except ApiKeyLimitExceeded:
            return False

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(attempt, range(15)))
    assert sum(results) == 5 and len(store.list(tenant)) == 5


def test_concurrent_revokes_agree_on_one_revoked_at(store, tenant):
    c = store.create(tenant, name="n", created_by="a")
    with ThreadPoolExecutor(max_workers=10) as pool:
        records = list(pool.map(lambda _: store.revoke(tenant, c.record.id), range(10)))
    assert len({r.revoked_at for r in records}) == 1 and None not in {r.revoked_at for r in records}
    assert store.authenticate(c.key) is None


def test_revoke_racing_with_authenticate_ends_revoked(store, tenant):
    keys = [store.create(tenant, name=f"k{i}", created_by="a") for i in range(8)]

    def work(k):
        store.authenticate(k.key)
        store.revoke(tenant, k.record.id)
        return store.authenticate(k.key)

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(work, keys)) == [None] * 8
