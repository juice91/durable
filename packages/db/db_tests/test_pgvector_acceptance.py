"""RM.1 acceptance: pgvector in the dev database, seen through the migrated test schemas."""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from eap_db import current_revisions, head_revisions
from eap_db.testing import SCHEMA_ENV, create_migrated_schema, create_schema, drop_schema, schema_url

URL = os.environ.get("EAP_TEST_DATABASE_URL", "")
# The repo convention: skip (with a reason) only when the plugin has no migrated schema, i.e. Postgres
# is absent. With Postgres present these tests run, and fail if pgvector is missing.
pytestmark = pytest.mark.skipif(
    not os.environ.get(SCHEMA_ENV), reason="needs the eap_db pytest plugin's migrated schema (dev Postgres)"
)


def _base_url() -> str:
    url = make_url(URL)
    return url.set(query={k: v for k, v in url.query.items() if k != "options"}).render_as_string(hide_password=False)


@contextmanager
def _migrated(prefix: str):
    base = _base_url()
    schema = f"eap_test_{prefix}_{uuid.uuid4().hex[:8]}"
    # public=True: these tests run app queries that need pgvector, and never migrate this URL again.
    engine = create_engine(create_migrated_schema(base, schema, public=True))
    try:
        yield engine, schema
    finally:
        engine.dispose()
        drop_schema(base, schema)


def test_the_session_runs_as_the_app_role_in_a_migrated_schema():
    engine = create_engine(URL)
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SELECT current_user")).scalar_one() == "eap_app"
            assert conn.execute(text("SELECT current_schema()")).scalar_one() == os.environ[SCHEMA_ENV]
        assert current_revisions(engine) == head_revisions()
    finally:
        engine.dispose()


def test_extension_is_present_in_eap_app():
    engine = create_engine(URL)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT e.extversion, n.nspname FROM pg_extension e "
                    "JOIN pg_namespace n ON n.oid = e.extnamespace WHERE e.extname = 'vector'"
                )
            ).one()
            assert conn.execute(text("SELECT current_database()")).scalar_one() == "eap_app"
        assert row.extversion and row.nspname == "public"
    finally:
        engine.dispose()


def test_vector_resolves_in_a_migrated_schema_and_ranks_by_cosine():
    with _migrated("vr") as (engine, schema):
        with engine.begin() as conn:
            assert conn.execute(text("SELECT to_regtype('vector')::text")).scalar_one() == "vector"
            conn.execute(text("CREATE TABLE vec_probe (id int PRIMARY KEY, embedding vector(3))"))
            conn.execute(text("INSERT INTO vec_probe VALUES (1,'[1,0,0]'),(2,'[0,1,0]'),(3,'[0.9,0.1,0]'),(4,'[-1,0,0]')"))
            ranked = conn.execute(
                text("SELECT id FROM vec_probe ORDER BY embedding <=> CAST('[1,0,0]' AS vector(3)), id")
            ).scalars().all()
            assert ranked == [1, 3, 2, 4]
            dist = conn.execute(text("SELECT '[1,0]'::vector <=> '[0,1]'::vector")).scalar_one()
            assert abs(dist - 1.0) < 1e-9
            table_schema = conn.execute(text("SELECT to_regclass('vec_probe')::text")).scalar_one()
            assert table_schema == "vec_probe"  # created in the session schema, not public
            assert conn.execute(
                text("SELECT table_schema FROM information_schema.tables WHERE table_name = 'vec_probe'")
            ).scalar_one() == schema


def test_partial_expression_hnsw_index_is_used_for_cosine_search():
    with _migrated("hn") as (engine, _schema):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE chunk_probe (id int PRIMARY KEY, dimensions int NOT NULL, embedding vector NOT NULL)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO chunk_probe SELECT g, 1024, "
                    "(SELECT array_agg(((g * i * 7919 + g * g) % 1009) / 100.0 - 5)::vector FROM generate_series(1, 1024) i) "
                    "FROM generate_series(1, 200) g"
                )
            )
            conn.execute(
                text(
                    "CREATE INDEX chunk_probe_hnsw ON chunk_probe USING hnsw "
                    "((embedding::vector(1024)) vector_cosine_ops) WHERE dimensions = 1024"
                )
            )
            conn.execute(text("ANALYZE chunk_probe"))
            conn.execute(text("SET LOCAL enable_seqscan = off"))
            query = (
                "SELECT id FROM chunk_probe WHERE dimensions = 1024 "
                "ORDER BY (embedding::vector(1024)) <=> (SELECT embedding::vector(1024) FROM chunk_probe WHERE id = 7) "
                "LIMIT 5"
            )
            plan = "\n".join(conn.execute(text("EXPLAIN " + query)).scalars())
            assert "chunk_probe_hnsw" in plan, plan
            ids = conn.execute(text(query)).scalars().all()
            assert ids[0] == 7 and len(ids) == 5


def test_an_unmigrated_schema_does_not_see_another_schemas_alembic_version():
    base = _base_url()
    tag = uuid.uuid4().hex[:8]
    schema, decoy = f"eap_test_um_{tag}", f"eap_test_um_decoy_{tag}"
    admin = create_engine(base)
    with admin.begin() as conn:
        create_schema(conn, schema)
        # A decoy version table at head, standing in for the dev database's public one (whether or
        # not public has one): the premise of running migrations on the schema alone.
        create_schema(conn, decoy)
        conn.execute(text(f'CREATE TABLE "{decoy}".alembic_version (version_num varchar(32) PRIMARY KEY)'))
        conn.execute(text(f'INSERT INTO "{decoy}".alembic_version VALUES (:v)'), {"v": sorted(head_revisions())[0]})
    admin.dispose()
    alone = create_engine(schema_url(base, schema))
    with_decoy = create_engine(schema_url(base, f"{schema},{decoy}"))
    try:
        # The hazard: with a second schema on the path the empty schema looks migrated.
        assert current_revisions(with_decoy) == set(sorted(head_revisions())[:1])
        # The migration URL is the schema alone and sees nothing.
        with alone.connect() as conn:
            assert conn.execute(text("SELECT to_regclass('alembic_version')")).scalar_one() is None
        assert current_revisions(alone) == set()
    finally:
        alone.dispose()
        with_decoy.dispose()
        drop_schema(base, schema)
        drop_schema(base, decoy)


def test_app_role_can_create_use_and_index_the_type():
    with _migrated("ar") as (engine, _schema):
        with engine.begin() as conn:
            assert conn.execute(text("SELECT current_user")).scalar_one() == "eap_app"
            assert not conn.execute(text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")).scalar_one()
            conn.execute(text("CREATE TABLE role_probe (id int, v vector(2))"))
            conn.execute(text("INSERT INTO role_probe VALUES (1, '[1,0]'), (2, '[0,1]')"))
            conn.execute(text("CREATE INDEX role_probe_hnsw ON role_probe USING hnsw (v vector_cosine_ops)"))
            row = conn.execute(
                text("SELECT id, v <=> '[1,0]' AS cos, v <-> '[1,0]' AS l2, v <#> '[1,0]' AS ip FROM role_probe ORDER BY 2, 1 LIMIT 1")
            ).one()
            assert row.id == 1 and row.cos == 0 and row.l2 == 0 and row.ip == -1
