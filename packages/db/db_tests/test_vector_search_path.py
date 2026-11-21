"""The test schemas see pgvector's objects in `public` without migrations ever reading `public`."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from eap_db import current_revisions, head_revisions
from eap_db.testing import SCHEMA_ENV, create_migrated_schema, create_schema, drop_schema, schema_url

URL = os.environ.get("EAP_TEST_DATABASE_URL", "")
requires_schema = pytest.mark.skipif(
    not os.environ.get(SCHEMA_ENV), reason="needs the eap_db pytest plugin's migrated schema (dev Postgres)"
)


def _base_url() -> str:
    url = make_url(URL)
    return url.set(query={k: v for k, v in url.query.items() if k != "options"}).render_as_string(hide_password=False)


def test_schema_url_puts_public_last_only_when_asked():
    base = "postgresql+psycopg://u:p@localhost:5432/db"
    assert make_url(schema_url(base, "s1")).query["options"] == "-csearch_path=s1"
    assert make_url(schema_url(base, "s1", public=True)).query["options"] == "-csearch_path=s1,public"
    assert "u:p@" in schema_url(base, "s1", public=True)  # rendered with the password


@requires_schema
def test_session_search_path_is_the_schema_then_public():
    engine = create_engine(URL)
    try:
        with engine.connect() as conn:
            effective = conn.execute(text("SELECT current_schemas(false)")).scalar_one()
            assert effective == [os.environ[SCHEMA_ENV], "public"]
            assert conn.execute(text("SELECT current_schema()")).scalar_one() == os.environ[SCHEMA_ENV]
    finally:
        engine.dispose()


@requires_schema
def test_migrations_build_the_schema_itself_and_only_an_opt_in_url_adds_public():
    base = _base_url()
    schema = f"eap_test_vp_{uuid.uuid4().hex[:8]}"
    url = create_migrated_schema(base, schema, public=True)
    engine = create_engine(url)
    try:
        assert make_url(url).query["options"] == f"-csearch_path={schema},public"
        with engine.connect() as conn:
            tables = set(
                conn.execute(
                    text("SELECT table_name FROM information_schema.tables WHERE table_schema = :s"), {"s": schema}
                ).scalars()
            )
            assert conn.execute(text("SELECT current_schema()")).scalar_one() == schema
        # Built here, not found in public: the version table and the app tables are in the new schema.
        assert {"alembic_version", "runs", "agents", "execution_events"} <= tables
        assert current_revisions(engine) == head_revisions()
    finally:
        engine.dispose()
        drop_schema(base, schema)


@requires_schema
def test_an_empty_schema_does_not_see_the_public_version_table():
    base = _base_url()
    schema = f"eap_test_vp_empty_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base)
    with admin.begin() as conn:
        create_schema(conn, schema)
    admin.dispose()
    engine = create_engine(schema_url(base, schema))
    try:
        assert current_revisions(engine) == set()
    finally:
        engine.dispose()
        drop_schema(base, schema)


def _has_vector_extension(engine) -> bool:
    with engine.connect() as conn:
        return conn.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")).scalar() is not None


@requires_schema
def test_vector_type_and_operators_resolve_in_the_session_schema():
    engine = create_engine(URL)
    try:
        if not _has_vector_extension(engine):
            pytest.skip("the vector extension is not installed in this database (scripts/dev-up.sh creates it)")
        with engine.begin() as conn:
            assert conn.execute(text("SELECT to_regtype('vector')::text")).scalar_one() == "vector"
            conn.execute(text("CREATE TEMP TABLE vp_probe (id int, embedding vector(3)) ON COMMIT DROP"))
            conn.execute(text("INSERT INTO vp_probe VALUES (1, '[1,0,0]'), (2, '[0,1,0]'), (3, '[0.9,0.1,0]')"))
            ranked = conn.execute(
                text("SELECT id FROM vp_probe ORDER BY embedding <=> CAST('[1,0,0]' AS vector(3))")
            ).scalars().all()
            assert ranked == [1, 3, 2]
    finally:
        engine.dispose()


def _create_schemas(base: str, *names: str) -> None:
    admin = create_engine(base)
    try:
        with admin.begin() as conn:
            for name in names:
                create_schema(conn, name)
    finally:
        admin.dispose()


def _decoy_with_version_table(base: str, decoy: str) -> None:
    # A stand-in for the dev database's public schema: an alembic_version at head and a table every
    # downgrade touches. Nothing may read or change it.
    admin = create_engine(base)
    try:
        with admin.begin() as conn:
            conn.execute(text(f'CREATE TABLE "{decoy}".alembic_version (version_num varchar(32) PRIMARY KEY)'))
            conn.execute(text(f'INSERT INTO "{decoy}".alembic_version VALUES (:v)'), {"v": sorted(head_revisions())[0]})
            conn.execute(text(f'CREATE TABLE "{decoy}".runs (id text PRIMARY KEY)'))
            conn.execute(text(f"INSERT INTO \"{decoy}\".runs VALUES ('decoy_run')"))
    finally:
        admin.dispose()


def _decoy_intact(base: str, decoy: str) -> bool:
    admin = create_engine(base)
    try:
        with admin.connect() as conn:
            runs = conn.execute(text(f'SELECT id FROM "{decoy}".runs')).scalars().all()
            version = conn.execute(text(f'SELECT version_num FROM "{decoy}".alembic_version')).scalars().all()
        return runs == ["decoy_run"] and version == sorted(head_revisions())[:1]
    finally:
        admin.dispose()


@requires_schema
def test_the_round_trip_url_is_the_schema_alone():
    """A URL returned for migrations has no second schema a downgrade could fall through to."""
    base = _base_url()
    schema = f"eap_test_vp_rt_{uuid.uuid4().hex[:8]}"
    url = create_migrated_schema(base, schema)
    engine = create_engine(url)
    try:
        assert make_url(url).query["options"] == f"-csearch_path={schema}"
        with engine.connect() as conn:
            assert conn.execute(text("SELECT current_schemas(false)")).scalar_one() == [schema]
    finally:
        engine.dispose()
        drop_schema(base, schema)


@requires_schema
@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_migrations_refuse_a_search_path_with_a_second_schema(direction):
    """Upgrade and downgrade refuse `<schema>,<other>`, so a decoy (like public) is never read or changed."""
    from alembic import command

    from eap_db import alembic_config

    base = _base_url()
    tag = uuid.uuid4().hex[:8]
    target, decoy = f"eap_test_vp_tg_{tag}", f"eap_test_vp_decoy_{tag}"
    _create_schemas(base, decoy)
    _decoy_with_version_table(base, decoy)
    if direction == "downgrade":
        create_migrated_schema(base, target)
    else:
        _create_schemas(base, target)
    engine = create_engine(schema_url(base, f"{target},{decoy}"))
    try:
        with pytest.raises(Exception, match="exactly one schema"):
            with engine.begin() as conn:
                if direction == "upgrade":
                    command.upgrade(alembic_config(conn), "head")
                else:
                    command.downgrade(alembic_config(conn), "base")
        assert _decoy_intact(base, decoy)
        check = create_engine(schema_url(base, target))
        try:
            with check.connect() as conn:
                tables = conn.execute(
                    text("SELECT table_name FROM information_schema.tables WHERE table_schema = :s"), {"s": target}
                ).scalars().all()
        finally:
            check.dispose()
        assert ("runs" in tables) == (direction == "downgrade")  # nothing built, nothing dropped
    finally:
        engine.dispose()
        drop_schema(base, target)
        drop_schema(base, decoy)
