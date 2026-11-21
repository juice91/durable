"""Pytest plugin (registered through the `pytest11` entry point of eap-db): one migrated schema per session.

At configure time, before any test module is imported, the session creates a uniquely named schema
(`eap_test_<hex>`) in the eap_app database, runs `alembic upgrade head` in it, and points
EAP_TEST_DATABASE_URL and EAP_DATABASE_URL at it through `options=-csearch_path=<schema>,public`.
Every suite, and every worker/API subprocess a test starts with the inherited environment, therefore
uses a schema built by the migrations, never the shared public schema. The schema is dropped when
the session ends.

`public` stays on the session's search path only so that extension objects resolve: pgvector's
`vector` type, its operators (`<=>`) and operator classes live in `public` (the extension is per
database). The session schema is at head, so each of its tables shadows the same-named table in
`public`. Migrations never run with `public` on the path (the migration environment refuses a
search path of more than one schema): the unqualified `alembic_version` lookup would find the dev
database's own version table in an empty schema and skip every migration, and a downgrade's
`DROP ... IF EXISTS` or `DELETE` would fall through to the dev database's own objects. Migrations
qualify extension types as `public.vector` instead. Only the plugin's session URL carries `public`.

The base database is EAP_TEST_DATABASE_URL, else the dev default below. When Postgres is unreachable
the plugin warns and leaves the environment alone, so the Postgres-backed suites skip or fail as they
would without a database. Disable with `-p no:eap_db`.
"""

from __future__ import annotations

import atexit
import os
import re
import shutil
import signal
import tempfile
import uuid
import warnings
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://eap_app:eap_app_password@localhost:54329/eap_app"
SCHEMA_ENV = "EAP_TEST_SCHEMA"

_state: dict[str, str] = {}

# Leaked-schema hygiene: a killed or crashed run never reaches teardown, and ~100k leaked relations
# make Postgres crash recovery take minutes. Every schema this module creates carries a creation
# comment; the next session start sweeps stale ones.
SWEEP_NAME_RE = re.compile(r"^eap_(test|compat)_[0-9a-f]+$")
COMMENT_RE = re.compile(r"^eap-test created=(\S+) pid=\d+$")
TTL_ENV = "EAP_TEST_SCHEMA_TTL_HOURS"
DEFAULT_TTL_HOURS = 6.0
_swept = False


def schema_comment(now: datetime | None = None) -> str:
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"eap-test created={stamp} pid={os.getpid()}"


def create_schema(conn, schema: str) -> None:
    """CREATE SCHEMA plus its creation comment, in the caller's transaction."""
    conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    conn.execute(text(f'COMMENT ON SCHEMA "{schema}" IS \'{schema_comment()}\''))


def _ttl_hours() -> float:
    try:
        return float(os.environ.get(TTL_ENV, DEFAULT_TTL_HOURS))
    except ValueError:
        return DEFAULT_TTL_HOURS


def is_stale(name: str, comment: str | None, locked: bool, now: datetime, ttl_hours: float) -> bool:
    """Whether the sweep may drop `name`. Never true for a name outside `eap_(test|compat)_<hex>`."""
    if not SWEEP_NAME_RE.match(name):
        return False
    match = COMMENT_RE.match(comment or "")
    if match is None:
        return not locked  # uncommented (older build): only when nothing holds one of its relations
    try:
        created = datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return not locked
    return now - created > timedelta(hours=ttl_hours)


_LIST_SQL = """
SELECT n.nspname, obj_description(n.oid, 'pg_namespace'),
       EXISTS (SELECT 1 FROM pg_locks l JOIN pg_class c ON c.oid = l.relation
               WHERE l.locktype = 'relation' AND c.relnamespace = n.oid)
FROM pg_namespace n WHERE n.nspname ~ '^eap_(test|compat)_[0-9a-f]+$'
"""


def sweep_stale_schemas(
    base_url: str, *, keep: tuple[str, ...] = (), only: tuple[str, ...] | None = None, now: datetime | None = None
) -> int:
    """Drop stale `eap_(test|compat)_<hex>` schemas (see `is_stale`); return how many were dropped.

    One `DROP SCHEMA ... CASCADE` per transaction with a 2s lock timeout; a schema that is busy is
    skipped. `keep` names schemas never to touch; `only` restricts the candidates (tests).
    """
    now = now or datetime.now(timezone.utc)
    ttl = _ttl_hours()
    engine = create_engine(base_url, connect_args={"connect_timeout": 5})
    dropped = 0
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(_LIST_SQL)).all()
            conn.rollback()
        for name, comment, locked in rows:
            if name in keep or (only is not None and name not in only):
                continue
            if not is_stale(name, comment, bool(locked), now, ttl):
                continue
            try:
                with engine.begin() as conn:
                    conn.execute(text("SET LOCAL lock_timeout = '2s'"))
                    conn.execute(text(f'DROP SCHEMA IF EXISTS "{name}" CASCADE'))
                dropped += 1
            except Exception:  # lock timeout or a concurrent drop: leave it for the next session
                continue
    finally:
        engine.dispose()
    return dropped


def sweep_once(base_url: str, *, keep: tuple[str, ...] = ()) -> int:
    """`sweep_stale_schemas` at most once per process, never on an xdist worker, never fatal."""
    global _swept
    if _swept or os.environ.get("PYTEST_XDIST_WORKER"):
        return 0
    _swept = True
    try:
        count = sweep_stale_schemas(base_url, keep=keep)
    except Exception as exc:
        warnings.warn(f"eap_db: stale test schema sweep failed: {exc}")
        return 0
    if count:
        print(f"eap_db: swept {count} stale test schema(s)")
    return count


def _use_temp_blob_dir() -> None:
    """Attachment bytes (eap_blob_store's `fs` backend) go to a temp dir for the session unless the
    environment names one, so a test never writes ./.blobs. Inherited by worker subprocesses."""
    if os.environ.get("EAP_BLOB_DIR") or os.environ.get("EAP_BLOB_BACKEND"):
        return
    path = tempfile.mkdtemp(prefix="eap_test_blobs_")
    os.environ["EAP_BLOB_DIR"] = path
    atexit.register(shutil.rmtree, path, ignore_errors=True)


def schema_url(base_url: str, schema: str, *, public: bool = False) -> str:
    """`base_url` with every connection's search_path set to `schema` (rendered with the password).

    With `public=True` the path is `<schema>,public`, so pgvector's `vector` type and operators
    resolve. Use that only for a schema that is at head: in an empty or partly migrated schema,
    unqualified names (`alembic_version`, `to_regclass('runs')`) fall through to `public`.
    """
    search_path = f"{schema},public" if public else schema
    url = make_url(base_url).update_query_dict({"options": f"-csearch_path={search_path}"})
    return url.render_as_string(hide_password=False)


def create_migrated_schema(base_url: str, schema: str, *, public: bool = False) -> str:
    """Create `schema`, migrate it to head, and return a URL whose connections use it.

    The migrations run with the search path set to `schema` alone, and so does the returned URL
    unless `public=True`. Pass `public=True` only for a URL that runs app queries needing pgvector,
    never for one that will run migrations (upgrade or downgrade) again.
    """
    from alembic import command

    from eap_db import alembic_config

    admin = create_engine(base_url, connect_args={"connect_timeout": 5})
    try:
        with admin.begin() as conn:
            create_schema(conn, schema)
    finally:
        admin.dispose()
    engine = create_engine(schema_url(base_url, schema))
    try:
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "head")
    except BaseException:
        drop_schema(base_url, schema)
        raise
    finally:
        engine.dispose()
    return schema_url(base_url, schema, public=public)


def drop_schema(base_url: str, schema: str) -> None:
    engine = create_engine(base_url, connect_args={"connect_timeout": 5})
    try:
        with engine.begin() as conn:
            conn.execute(text("SET LOCAL lock_timeout = '30s'"))
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    finally:
        engine.dispose()


@pytest.hookimpl(tryfirst=True)
def pytest_load_initial_conftests(early_config: pytest.Config, parser: pytest.Parser, args: list[str]) -> None:
    # Runs before the conftest.py files next to the given paths are imported (e.g.
    # `pytest tests/recovery` or `pytest apps/worker/worker_tests`), and those read the database URL
    # at import time. Setting up here means they already see the migrated schema.
    _setup(early_config)


def pytest_configure(config: pytest.Config) -> None:
    _setup(config)  # no-op when pytest_load_initial_conftests already ran


def _setup(config: pytest.Config) -> None:
    if "schema" in _state:
        return
    _use_temp_blob_dir()
    if hasattr(config, "workerinput"):  # pragma: no cover - xdist workers inherit the controller's env
        return
    base_url = os.environ.get("EAP_TEST_DATABASE_URL") or DEFAULT_TEST_DATABASE_URL
    if not base_url.startswith("postgresql"):
        return
    schema = f"eap_test_{uuid.uuid4().hex[:12]}"
    try:
        sweep_once(base_url, keep=(schema,))
    except Exception:  # pragma: no cover - sweep_once never raises
        pass
    try:
        url = create_migrated_schema(base_url, schema, public=True)
    except OperationalError as exc:
        warnings.warn(f"eap_db: no migrated test schema, Postgres unreachable at {make_url(base_url)!r}: {exc}")
        return
    _state.update(base_url=base_url, schema=schema)
    # Killed by SIGTERM (CI timeout, xdist shutdown) the interpreter would skip pytest_unconfigure;
    # turn it into a normal exit so unconfigure/atexit run. SIGKILL is left to the next sweep.
    atexit.register(_drop_session_schema)
    if signal.getsignal(signal.SIGTERM) is signal.SIG_DFL:
        try:
            signal.signal(signal.SIGTERM, _exit_on_sigterm)
        except ValueError:  # not the main thread
            pass
    os.environ["EAP_TEST_DATABASE_URL"] = url
    os.environ["EAP_DATABASE_URL"] = url
    os.environ[SCHEMA_ENV] = schema


def pytest_report_header(config: pytest.Config) -> str | None:
    if "schema" in _state:
        return f"eap_db: migrated test schema {_state['schema']} (alembic head)"
    return None


def _exit_on_sigterm(signum, frame) -> None:
    raise SystemExit(128 + signum)


def _drop_session_schema() -> None:
    schema = _state.pop("schema", None)
    if schema is None:
        return
    try:
        drop_schema(_state["base_url"], schema)
    except Exception as exc:  # never mask the test result; the schema name says what to drop by hand
        warnings.warn(f"eap_db: could not drop test schema {schema}: {exc}")


def pytest_unconfigure(config: pytest.Config) -> None:
    # Runs on a normal end, on KeyboardInterrupt and on xdist worker shutdown; atexit covers the rest.
    _drop_session_schema()
    _state.clear()
