"""The stale test-schema sweep: fake names and comments, plus a real Postgres round trip."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text

from eap_db.testing import (
    SWEEP_NAME_RE,
    DEFAULT_TEST_DATABASE_URL,
    SCHEMA_ENV,
    create_schema,
    is_stale,
    schema_comment,
    sweep_stale_schemas,
)

NOW = datetime(2026, 11, 21, 12, 0, 0, tzinfo=timezone.utc)


def _comment(age_hours: float) -> str:
    return schema_comment(NOW - timedelta(hours=age_hours)).replace(f"pid={os.getpid()}", "pid=1")


def test_old_commented_schema_is_stale():
    assert is_stale("eap_test_abcdef12", _comment(7), False, NOW, 6)
    assert is_stale("eap_compat_abcdef12", _comment(7), True, NOW, 6)  # a stamp beyond the TTL wins over locks


def test_recent_commented_schema_survives():
    assert not is_stale("eap_test_abcdef12", _comment(1), False, NOW, 6)
    assert is_stale("eap_test_abcdef12", _comment(1), False, NOW, 0.5)  # the TTL is a parameter


def test_uncommented_schema_goes_only_when_unlocked():
    assert is_stale("eap_test_abcdef12", None, False, NOW, 6)
    assert not is_stale("eap_test_abcdef12", None, True, NOW, 6)
    assert not is_stale("eap_test_abcdef12", "someone else's note", True, NOW, 6)


@pytest.mark.parametrize(
    "name",
    [
        "eap_test_0123456789ab",  # plugin session schema
        "eap_test_0027_deadbeef",  # migration test, numeric label
        "eap_test_worker_0123456789",
        "eap_test_kbplan_deadbeef",
        "eap_test_vp_tg_deadbeef",  # two labels
        "eap_test_mcp_crash_0123456789",
        "eap_compat_0123456789",
        "eap_compat_dbos_0123456789",
        "eap_recovery_0123456789",
        "eap_accept_0123456789",
    ],
)
def test_every_test_schema_family_matches(name):
    assert SWEEP_NAME_RE.match(name)
    assert is_stale(name, None, False, NOW, 6)


@pytest.mark.parametrize(
    "name",
    [
        "public", "eap_app", "eap_dbos", "dbos", "eap", "information_schema", "pg_catalog", "pg_temp_1",
        "eap_test_", "eap_test_abc", "eap_test_ABCDEF12", "eap_test_worker", "eap_test_worker_xyz",
        "x_eap_test_abcdef12", "eap_test_abcdef12_x", "eap_tests_abcdef12", "eap_prod_abcdef12", "eap_recovery_",
    ],
)
def test_non_matching_names_are_never_stale(name):
    assert not SWEEP_NAME_RE.match(name)
    assert not is_stale(name, None, False, NOW, 6)
    assert not is_stale(name, _comment(99), False, NOW, 6)


requires_db = pytest.mark.skipif(not os.environ.get(SCHEMA_ENV), reason="needs the dev Postgres")


@requires_db
def test_sweep_drops_old_keeps_recent_and_foreign():
    base = os.environ.get("EAP_TEST_DATABASE_URL") or DEFAULT_TEST_DATABASE_URL
    # Hex-only names (so they match the pattern); the sweep is restricted to these.
    old, fresh = (f"eap_test_{uuid.uuid4().hex[:12]}" for _ in range(2))
    foreign = f"eap_other_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base)
    try:
        with admin.begin() as conn:
            for name, age in ((old, 9), (fresh, 0)):
                conn.execute(text(f'CREATE SCHEMA "{name}"'))
                conn.execute(text(f"COMMENT ON SCHEMA \"{name}\" IS '{_comment_real(age)}'"))
            conn.execute(text(f'CREATE SCHEMA "{foreign}"'))
            conn.execute(text(f"COMMENT ON SCHEMA \"{foreign}\" IS '{_comment_real(99)}'"))
        dropped = sweep_stale_schemas(base, only=(old, fresh, foreign))
        with admin.connect() as conn:
            left = {
                r[0]
                for r in conn.execute(
                    text("SELECT nspname FROM pg_namespace WHERE nspname = ANY(:n)"), {"n": [old, fresh, foreign]}
                )
            }
        assert dropped == 1
        assert left == {fresh, foreign}
    finally:
        with admin.begin() as conn:
            for name in (old, fresh, foreign):
                conn.execute(text(f'DROP SCHEMA IF EXISTS "{name}" CASCADE'))
        admin.dispose()


def _comment_real(age_hours: float) -> str:
    return schema_comment(datetime.now(timezone.utc) - timedelta(hours=age_hours))


@requires_db
def test_create_schema_records_a_comment():
    base = os.environ.get("EAP_TEST_DATABASE_URL") or DEFAULT_TEST_DATABASE_URL
    name = f"eap_test_{uuid.uuid4().hex[:12]}"
    admin = create_engine(base)
    try:
        with admin.begin() as conn:
            create_schema(conn, name)
        with admin.connect() as conn:
            comment = conn.execute(text("SELECT obj_description(oid, 'pg_namespace') FROM pg_namespace WHERE nspname = :n"), {"n": name}).scalar_one()
        assert comment.startswith("eap-test created=") and f"pid={os.getpid()}" in comment
    finally:
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{name}" CASCADE'))
        admin.dispose()
