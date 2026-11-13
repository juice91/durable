"""Dot-only resource ids (`.`, `..`) are refused (checkpoint review fix): a resource id appears in URL
paths, where path normalisation would turn them into another route."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from .run_helpers import requires_pg
from .test_conversations import create_conversation
from .test_memory_api import make_agent, mem_tenant  # noqa: F401 - fixture
from .test_runs import (  # noqa: F401 - fixtures
    _no_hang,
    deltas,
    fake_dbos,
    pg,
    stream_settings,
    tenant_of,
)

pytestmark = requires_pg


@pytest.mark.parametrize("bad", [".", "..", "...."])
def test_dot_only_resource_id_is_422_and_nothing_is_created(pg, mem_tenant, bad):
    client, app = pg
    tenant, h = mem_tenant()
    aid = make_agent(client, h)
    r = create_conversation(client, h, aid, resource_id=bad)
    assert r.status_code == 422, r.text
    with app.state.database_engine.connect() as conn:
        n = conn.execute(text("SELECT count(*) FROM conversations WHERE tenant_id = :t"), {"t": tenant}).scalar_one()
    assert n == 0


def test_ids_with_dots_still_work(pg, mem_tenant):
    client, _ = pg
    _, h = mem_tenant()
    aid = make_agent(client, h)
    r = create_conversation(client, h, aid, resource_id="user.name..x")
    assert r.status_code == 201 and r.json()["resource_id"] == "user.name..x"
