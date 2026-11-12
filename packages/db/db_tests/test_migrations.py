"""The migrations own the eap_app schema: head equals the models, and apps refuse an unmigrated database."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from eap_db import NOT_AT_HEAD_MESSAGE, SchemaNotAtHead, current_revisions, head_revisions, require_schema_at_head
from eap_db.metadata import schema_diff
from eap_db.testing import SCHEMA_ENV, create_migrated_schema, drop_schema, schema_url

URL = os.environ.get("EAP_TEST_DATABASE_URL", "")
requires_schema = pytest.mark.skipif(
    not os.environ.get(SCHEMA_ENV), reason="needs the eap_db pytest plugin's migrated schema (dev Postgres)"
)


def _base_url() -> str:
    # The session URL without its search_path option: the database the session schema lives in.
    from sqlalchemy.engine import make_url

    url = make_url(URL)
    return url.set(query={k: v for k, v in url.query.items() if k != "options"}).render_as_string(hide_password=False)


@requires_schema
def test_session_schema_is_at_head_and_matches_the_models():
    engine = create_engine(URL)
    try:
        assert current_revisions(engine) == head_revisions()
        with engine.connect() as conn:
            assert conn.execute(text("SELECT current_schema()")).scalar_one() == os.environ[SCHEMA_ENV]
            assert schema_diff(conn) == []
        require_schema_at_head(engine)
    finally:
        engine.dispose()


@requires_schema
def test_empty_schema_is_refused_with_the_upgrade_command():
    base = _base_url()
    schema = f"eap_test_empty_{uuid.uuid4().hex[:8]}"
    admin = create_engine(base)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    admin.dispose()
    engine = create_engine(schema_url(base, schema))
    try:
        with pytest.raises(SchemaNotAtHead) as err:
            require_schema_at_head(engine)
        assert str(err.value).startswith(NOT_AT_HEAD_MESSAGE)
        assert "uv run alembic -c packages/db/alembic.ini upgrade head" in str(err.value)
    finally:
        engine.dispose()
        drop_schema(base, schema)


@requires_schema
def test_upgrade_then_downgrade_round_trips():
    from alembic import command

    from eap_db import alembic_config

    base = _base_url()
    schema = f"eap_test_rt_{uuid.uuid4().hex[:8]}"
    url = create_migrated_schema(base, schema)
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            command.downgrade(alembic_config(conn), "base")
        with engine.connect() as conn:
            tables = conn.execute(
                text("SELECT table_name FROM information_schema.tables WHERE table_schema = :s"), {"s": schema}
            ).scalars().all()
        assert tables == ["alembic_version"]
        with pytest.raises(SchemaNotAtHead):
            require_schema_at_head(engine)
    finally:
        engine.dispose()
        drop_schema(base, schema)
