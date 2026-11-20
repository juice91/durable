"""0034_workflow_explanations: the table, its unique key, the cascade from workflows, and the downgrade."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from eap_db.testing import SCHEMA_ENV, drop_schema, schema_url

pytestmark = pytest.mark.skipif(
    not os.environ.get(SCHEMA_ENV), reason="needs the eap_db pytest plugin's migrated schema (dev Postgres)"
)

INSERT = text(
    "INSERT INTO workflow_explanations (id, tenant_id, workflow_id, version, locale, body, created_at)"
    " VALUES (:i, :t, :w, :v, :l, '{}'::jsonb, now())"
)


def _base_url() -> str:
    url = make_url(os.environ.get("EAP_TEST_DATABASE_URL", ""))
    return url.set(query={k: v for k, v in url.query.items() if k != "options"}).render_as_string(hide_password=False)


def test_table_unique_key_cascade_and_downgrade():
    from alembic import command

    from eap_db import alembic_config

    base = _base_url()
    schema = f"eap_test_0034_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(schema_url(base, schema))
    try:
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0033_revisions")
            conn.execute(text("INSERT INTO workflows (id, tenant_id, name, created_at, updated_at)"
                              " VALUES ('w1', 't1', 'w', now(), now())"))
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0034_workflow_explanations")
        with engine.begin() as conn:
            conn.execute(INSERT, {"i": "e1", "t": "t1", "w": "w1", "v": 1, "l": "zh-TW"})
            conn.execute(INSERT, {"i": "e2", "t": "t1", "w": "w1", "v": 1, "l": "en"})  # another locale is fine
        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                conn.execute(INSERT, {"i": "e3", "t": "t1", "w": "w1", "v": 1, "l": "en"})
        with pytest.raises(IntegrityError):  # a workflow of another tenant
            with engine.begin() as conn:
                conn.execute(INSERT, {"i": "e4", "t": "t2", "w": "w1", "v": 1, "l": "en"})
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM workflows WHERE id = 'w1'"))
            assert conn.execute(text("SELECT count(*) FROM workflow_explanations")).scalar_one() == 0
        with engine.begin() as conn:
            command.downgrade(alembic_config(conn), "0033_revisions")
        with engine.begin() as conn:
            assert conn.execute(text("SELECT to_regclass('workflow_explanations')")).scalar_one() is None
    finally:
        engine.dispose()
        drop_schema(base, schema)
        admin.dispose()
