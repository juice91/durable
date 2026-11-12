"""Pytest plugin (registered through the `pytest11` entry point of eap-db): one migrated schema per session.

At configure time, before any test module is imported, the session creates a uniquely named schema
(`eap_test_<hex>`) in the eap_app database, runs `alembic upgrade head` in it, and points
EAP_TEST_DATABASE_URL and EAP_DATABASE_URL at it through `options=-csearch_path=<schema>`. Every
suite, and every worker/API subprocess a test starts with the inherited environment, therefore uses
a schema built by the migrations, never the shared public schema. The schema is dropped when the
session ends.

The base database is EAP_TEST_DATABASE_URL, else the dev default below. When Postgres is unreachable
the plugin warns and leaves the environment alone, so the Postgres-backed suites skip or fail as they
would without a database. Disable with `-p no:eap_db`.
"""

from __future__ import annotations

import os
import uuid
import warnings

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://eap_app:eap_app_password@localhost:54329/eap_app"
SCHEMA_ENV = "EAP_TEST_SCHEMA"

_state: dict[str, str] = {}


def schema_url(base_url: str, schema: str) -> str:
    """`base_url` with every connection's search_path set to `schema` (rendered with the password)."""
    url = make_url(base_url).update_query_dict({"options": f"-csearch_path={schema}"})
    return url.render_as_string(hide_password=False)


def create_migrated_schema(base_url: str, schema: str) -> str:
    """Create `schema`, migrate it to head, and return a URL whose connections use it."""
    from alembic import command

    from eap_db import alembic_config

    admin = create_engine(base_url, connect_args={"connect_timeout": 5})
    try:
        with admin.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    finally:
        admin.dispose()
    url = schema_url(base_url, schema)
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "head")
    except BaseException:
        drop_schema(base_url, schema)
        raise
    finally:
        engine.dispose()
    return url


def drop_schema(base_url: str, schema: str) -> None:
    engine = create_engine(base_url, connect_args={"connect_timeout": 5})
    try:
        with engine.begin() as conn:
            conn.execute(text("SET LOCAL lock_timeout = '30s'"))
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    finally:
        engine.dispose()


def pytest_configure(config: pytest.Config) -> None:
    if hasattr(config, "workerinput"):  # pragma: no cover - xdist workers inherit the controller's env
        return
    base_url = os.environ.get("EAP_TEST_DATABASE_URL") or DEFAULT_TEST_DATABASE_URL
    if not base_url.startswith("postgresql"):
        return
    schema = f"eap_test_{uuid.uuid4().hex[:12]}"
    try:
        url = create_migrated_schema(base_url, schema)
    except OperationalError as exc:
        warnings.warn(f"eap_db: no migrated test schema, Postgres unreachable at {make_url(base_url)!r}: {exc}")
        return
    _state.update(base_url=base_url, schema=schema)
    os.environ["EAP_TEST_DATABASE_URL"] = url
    os.environ["EAP_DATABASE_URL"] = url
    os.environ[SCHEMA_ENV] = schema


def pytest_report_header(config: pytest.Config) -> str | None:
    if "schema" in _state:
        return f"eap_db: migrated test schema {_state['schema']} (alembic head)"
    return None


def pytest_unconfigure(config: pytest.Config) -> None:
    if "schema" not in _state:
        return
    try:
        drop_schema(_state["base_url"], _state["schema"])
    except Exception as exc:  # never mask the test result; the schema name says what to drop by hand
        warnings.warn(f"eap_db: could not drop test schema {_state['schema']}: {exc}")
    _state.clear()
