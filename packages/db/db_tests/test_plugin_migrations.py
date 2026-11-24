"""The plug-in Alembic branch runner (IAM design 5.3): own revision directory, own version table, installed AND
listed plug-ins only, core untouched, downgrade of one branch alone. A throwaway schema on the dev Postgres."""

from __future__ import annotations

import os
import textwrap
import uuid

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from eap_db.plugin_migrations import (
    PluginBranch,
    configured_plugins,
    downgrade_branch,
    upgrade_plugin_branches,
)
from eap_db.testing import SCHEMA_ENV, create_schema, drop_schema, schema_url

pytestmark = pytest.mark.skipif(
    not os.environ.get(SCHEMA_ENV), reason="needs the eap_db pytest plugin's migrated schema (dev Postgres)"
)

REVISION = '''
"""demo"""
from alembic import op

revision = "demo_0001"
down_revision = None
branch_labels = ("iam_demo",)
depends_on = None


def upgrade():
    op.execute("CREATE TABLE iam_demo_things (id text PRIMARY KEY, tenant_id text NOT NULL)")


def downgrade():
    op.execute("DROP TABLE iam_demo_things")
'''


def _base_url() -> str:
    url = make_url(os.environ.get("EAP_TEST_DATABASE_URL", ""))
    return url.set(query={k: v for k, v in url.query.items() if k != "options"}).render_as_string(hide_password=False)


@pytest.fixture
def branch(tmp_path):
    versions = tmp_path / "versions"
    versions.mkdir()
    (versions / "demo_0001.py").write_text(textwrap.dedent(REVISION))
    (tmp_path / "script.py.mako").write_text("")
    return PluginBranch(name="demo", script_location=tmp_path)


@pytest.fixture
def conn():
    base = _base_url()
    schema = f"eap_test_iamplug_{uuid.uuid4().hex[:12]}"
    admin = create_engine(base)
    with admin.begin() as c:
        create_schema(c, schema)
    engine = create_engine(schema_url(base, schema))
    try:
        with engine.begin() as connection:
            yield connection
    finally:
        engine.dispose()
        drop_schema(base, schema)
        admin.dispose()


def test_a_listed_plugin_branch_upgrades_with_its_own_version_table(conn, branch):
    assert upgrade_plugin_branches(conn, {"demo"}, [branch]) == ["demo"]
    assert "iam_demo_things" in inspect(conn).get_table_names()
    assert conn.execute(text("SELECT version_num FROM alembic_version_demo")).scalar_one() == "demo_0001"
    assert "alembic_version" not in inspect(conn).get_table_names()  # core's table is never created or touched
    assert upgrade_plugin_branches(conn, {"demo"}, [branch]) == ["demo"]  # idempotent


def test_an_unlisted_plugin_branch_is_not_touched(conn, branch):
    assert upgrade_plugin_branches(conn, set(), [branch]) == []
    assert "iam_demo_things" not in inspect(conn).get_table_names()


def test_downgrading_a_branch_leaves_everything_else(conn, branch):
    upgrade_plugin_branches(conn, {"demo"}, [branch])
    downgrade_branch(conn, branch)
    assert "iam_demo_things" not in inspect(conn).get_table_names()


def test_configured_plugins_reads_the_three_selection_variables():
    env = {"EAP_IAM_IDENTITY": "apikey, devloopback", "EAP_IAM_POLICY": "openaccess", "EAP_IAM_MEMBERSHIP": ""}
    assert configured_plugins(env) == {"apikey", "devloopback", "openaccess"}


def test_with_no_branch_installed_the_runner_is_a_no_op(conn):
    assert upgrade_plugin_branches(conn, {"apikey"}, []) == []
