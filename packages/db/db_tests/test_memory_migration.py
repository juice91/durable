"""0012_memory: upgrade, downgrade and upgrade on a populated 0011 schema. Existing conversations get
a NULL resource_id; the new checks, the tenant-composite FKs and the cascades hold."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from eap_db.metadata import schema_diff
from eap_db.testing import SCHEMA_ENV, drop_schema, schema_url

pytestmark = pytest.mark.skipif(
    not os.environ.get(SCHEMA_ENV), reason="needs the eap_db pytest plugin's migrated schema (dev Postgres)"
)


def _base_url() -> str:
    url = make_url(os.environ.get("EAP_TEST_DATABASE_URL", ""))
    return url.set(query={k: v for k, v in url.query.items() if k != "options"}).render_as_string(hide_password=False)


def _tables(conn) -> set[str]:
    return set(conn.execute(text(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
    )).scalars())


def _columns(conn, table: str) -> set[str]:
    return set(conn.execute(text(
        "SELECT column_name FROM information_schema.columns WHERE table_name = :t AND table_schema = current_schema()"
    ), {"t": table}).scalars())


def _fails(engine, sql: str, constraint: str, **params) -> None:
    with pytest.raises(IntegrityError, match=constraint):
        with engine.begin() as conn:
            conn.execute(text(sql), params)


_WM = (
    "INSERT INTO working_memories (tenant_id, agent_id, scope, scope_key, conversation_id, format, content, version)"
    " VALUES (:t, 'ag', :scope, :key, :conv, :fmt, CAST(:content AS jsonb), :version)"
)
_OBS = (
    "INSERT INTO memory_observations (id, tenant_id, agent_id, conversation_id, kind, covers_from_turn,"
    " covers_to_turn, content, approx_tokens, run_id, superseded_by)"
    " VALUES (:id, :t, 'ag', :conv, :kind, :f, :to, 'x', 1, :run, :sup)"
)


def test_memory_revision_round_trip_on_a_populated_0011_schema():
    from alembic import command

    from eap_db import alembic_config

    base = _base_url()
    schema = f"eap_test_0012_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    admin.dispose()
    engine = create_engine(schema_url(base, schema))
    try:
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0011_knowledge_bases")
            conn.execute(text(
                "INSERT INTO conversations (id, tenant_id, agent_id, title, created_at, updated_at)"
                " VALUES ('c-old', 't1', 'ag', 'old', now(), now())"
            ))
            conn.execute(text(
                "INSERT INTO runs (id, tenant_id, kind, agent_id, agent_version, status, input, conversation_id, turn,"
                " created_at, updated_at) VALUES ('r1', 't1', 'agent', 'ag', 1, 'completed', 'hi', 'c-old', 1,"
                " now(), now())"
            ))
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0012_memory")

        with engine.connect() as conn:
            assert conn.execute(text("SELECT resource_id FROM conversations WHERE id = 'c-old'")).scalar() is None
            assert {"working_memories", "memory_observations"} <= _tables(conn)

        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO conversations (id, tenant_id, agent_id, resource_id, created_at, updated_at)"
                " VALUES ('c1', 't1', 'ag', 'user:ana@example.com', now(), now()),"
                " ('c2', 't2', 'ag', 'u2', now(), now())"
            ))
            conn.execute(text(_WM), {"t": "t1", "scope": "conversation", "key": "c1", "conv": "c1", "fmt": "markdown",
                                     "content": '"# note"', "version": 1})
            conn.execute(text(_WM), {"t": "t1", "scope": "resource", "key": "u1", "conv": None, "fmt": "json",
                                     "content": '{"name": "Ana"}', "version": 1})
            o1, o2, r = (str(uuid.uuid4()) for _ in range(3))
            conn.execute(text(_OBS), {"id": r, "t": "t1", "conv": "c1", "kind": "reflection", "f": 1, "to": 4,
                                      "run": None, "sup": None})
            conn.execute(text(_OBS), {"id": o1, "t": "t1", "conv": "c1", "kind": "observation", "f": 1, "to": 2,
                                      "run": None, "sup": r})
            conn.execute(text(_OBS), {"id": o2, "t": "t1", "conv": "c1", "kind": "observation", "f": 3, "to": 4,
                                      "run": None, "sup": r})

        _fails(engine, "UPDATE conversations SET resource_id = 'bad id' WHERE id = 'c1'", "ck_conversations_resource_id")
        _fails(engine, "UPDATE conversations SET resource_id = :r WHERE id = 'c1'", "ck_conversations_resource_id",
               r="x" * 129)
        # conversation scope names its conversation (and the same tenant's: composite FK)
        _fails(engine, _WM, "ck_working_memories_scope", t="t1", scope="conversation", key="c1", conv=None,
               fmt="markdown", content='"x"', version=1)
        _fails(engine, _WM, "fk_working_memories_tenant_conversation", t="t1", scope="conversation", key="c2",
               conv="c2", fmt="markdown", content='"x"', version=1)
        _fails(engine, _WM, "ck_working_memories_format", t="t1", scope="resource", key="u9", conv=None,
               fmt="json", content='"not an object"', version=1)
        _fails(engine, _WM, "ck_working_memories_version", t="t1", scope="resource", key="u9", conv=None,
               fmt="json", content="{}", version=0)
        # observation slot uniqueness, turn range, kind, tenant-composite conversation and run FKs
        _fails(engine, _OBS, "uq_memory_observations_slot", id=str(uuid.uuid4()), t="t1", conv="c1",
               kind="observation", f=1, to=2, run=None, sup=None)
        _fails(engine, _OBS, "ck_memory_observations_turns", id=str(uuid.uuid4()), t="t1", conv="c1",
               kind="observation", f=5, to=4, run=None, sup=None)
        _fails(engine, _OBS, "ck_memory_observations_kind", id=str(uuid.uuid4()), t="t1", conv="c1",
               kind="summary", f=5, to=6, run=None, sup=None)
        _fails(engine, _OBS, "fk_memory_observations_tenant_conversation", id=str(uuid.uuid4()), t="t1", conv="c2",
               kind="observation", f=1, to=1, run=None, sup=None)
        _fails(engine, _OBS, "fk_memory_observations_tenant_run", id=str(uuid.uuid4()), t="t2", conv="c2",
               kind="observation", f=1, to=1, run="r1", sup=None)

        # Deleting a conversation cascades to its note and observations, never the resource note.
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM conversations WHERE id = 'c1'"))
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM memory_observations")).scalar() == 0
            assert conn.execute(text("SELECT scope FROM working_memories")).scalars().all() == ["resource"]

        with engine.begin() as conn:
            command.downgrade(alembic_config(conn), "0011_knowledge_bases")
        with engine.connect() as conn:
            assert {"working_memories", "memory_observations"}.isdisjoint(_tables(conn))
            assert "resource_id" not in _columns(conn, "conversations")
            assert set(conn.execute(text("SELECT id FROM conversations")).scalars()) == {"c-old", "c2"}
            assert conn.execute(text("SELECT count(*) FROM runs")).scalar() == 1

        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "head")
        check = create_engine(schema_url(base, schema, public=True))
        try:
            with check.connect() as conn:
                assert schema_diff(conn) == []
        finally:
            check.dispose()
    finally:
        engine.dispose()
        drop_schema(base, schema)
