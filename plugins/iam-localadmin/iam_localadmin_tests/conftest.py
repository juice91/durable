"""A throw-away Postgres schema holding only the plug-in's tables, built by the plug-in's own Alembic branch."""

from __future__ import annotations

import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

KEY = secrets.token_hex(32)  # made at run time: no key value lives in the repository
INITIAL = "initial-password-0001"
CHOSEN = "chosen-password-0002"
ENV = {
    "EAP_LOCAL_ADMIN_USERNAME": "admin",
    "EAP_LOCAL_ADMIN_INITIAL_PASSWORD": INITIAL,
    "EAP_LOCAL_AUTH_SIGNING_KEY": KEY,
}


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def database():
    """(engine, url) of a fresh schema with no plug-in table yet; skipped without the dev Postgres."""
    from eap_db.testing import SCHEMA_ENV, create_schema, drop_schema, schema_url

    if not os.environ.get(SCHEMA_ENV):
        pytest.skip("needs the eap_db pytest plugin's migrated schema (dev Postgres)")
    url = make_url(os.environ["EAP_TEST_DATABASE_URL"])
    base = url.set(query={k: v for k, v in url.query.items() if k != "options"}).render_as_string(hide_password=False)
    schema = f"eap_test_localadmin_{uuid.uuid4().hex[:12]}"
    admin = create_engine(base)
    with admin.begin() as c:
        create_schema(c, schema)
    scoped = schema_url(base, schema)
    engine = create_engine(scoped)
    try:
        yield engine, scoped
    finally:
        engine.dispose()
        drop_schema(base, schema)
        admin.dispose()


def migrate(engine) -> None:
    from eap_db.plugin_migrations import upgrade_plugin_branches

    with engine.begin() as connection:
        assert upgrade_plugin_branches(connection, {"localadmin"}) == ["localadmin"]


@pytest.fixture
def engine(database):
    migrate(database[0])
    return database[0]


@pytest.fixture
def start(engine, clock):
    """Start the provider as a process would, with `ENV` plus overrides."""
    from eap_iam import PluginContext

    from eap_iam_localadmin import create_identity
    from eap_iam_localadmin.passwords import Passwords

    cheap = Passwords(time_cost=1, memory_cost=1024, parallelism=1)  # argon2id, cheap enough for a test run

    def _start(**overrides: str):
        env = {**ENV, **overrides}
        return create_identity(PluginContext(engine=engine, env=env), passwords=cheap, clock=clock)

    return _start


@pytest.fixture
def signed_in(start):
    """(provider, tokens) after the forced first change."""
    provider = start()
    result = provider.change_password("admin", INITIAL, CHOSEN, "192.0.2.1")
    return provider, result.tokens
