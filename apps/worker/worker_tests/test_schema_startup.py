"""The worker refuses to start on a database that Alembic has not migrated to head; it never creates tables."""

from __future__ import annotations

import dataclasses
import uuid

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from eap_db import SchemaNotAtHead
from eap_worker.config import WorkerSettings
from eap_worker.runtime import WorkerRuntime

from .conftest import APP_DB_URL
from eap_db.testing import create_schema

UPGRADE = "database schema is not at the latest migration; run: uv run alembic -c packages/db/alembic.ini upgrade head"


def test_unmigrated_database_fails_fast_and_is_left_untouched():
    base = make_url(APP_DB_URL)
    base = base.set(query={k: v for k, v in base.query.items() if k != "options"})
    schema = f"eap_test_unmigrated_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base)
    with admin.begin() as conn:
        create_schema(conn, schema)
    try:
        url = base.update_query_dict({"options": f"-csearch_path={schema}"}).render_as_string(hide_password=False)
        settings = dataclasses.replace(WorkerSettings.from_env(), app_database_url=url)
        runtime = WorkerRuntime(settings)
        try:
            with pytest.raises(SchemaNotAtHead) as err:
                runtime.check_schema()
        finally:
            runtime.engine.dispose()
        assert str(err.value).startswith(UPGRADE)
        assert inspect(admin).get_table_names(schema=schema) == []
    finally:
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
