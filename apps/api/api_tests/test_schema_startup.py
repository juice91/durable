"""The API starts only on a Postgres database that Alembic has migrated to head; it never creates tables."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from eap_api.app import create_app
from eap_db import SchemaNotAtHead

from .run_helpers import PG_URL, requires_pg

UPGRADE = "database schema is not at the latest migration; run: uv run alembic -c packages/db/alembic.ini upgrade head"


def test_database_url_is_required(monkeypatch):
    monkeypatch.delenv("EAP_DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="EAP_DATABASE_URL is not set"):
        create_app()


def test_sqlite_is_refused():
    with pytest.raises(RuntimeError, match="must point at Postgres"):
        create_app(database_url="sqlite:///:memory:")


@requires_pg
def test_unmigrated_database_fails_fast_and_is_left_untouched():
    base = make_url(PG_URL)
    base = base.set(query={k: v for k, v in base.query.items() if k != "options"})
    schema = f"eap_test_unmigrated_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        url = base.update_query_dict({"options": f"-csearch_path={schema}"}).render_as_string(hide_password=False)
        with pytest.raises(SchemaNotAtHead) as err:
            create_app(database_url=url)
        assert str(err.value).startswith(UPGRADE)
        assert inspect(admin).get_table_names(schema=schema) == []
    finally:
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
