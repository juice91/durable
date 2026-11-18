"""list_created_agents / list_created_workflows say what is published: published_version, draft_pending, archived."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, text

from eap_worker.architect_tools import (
    list_created_agents,
    list_created_workflows,
    publish_workflow,
    save_agent,
    save_workflow,
)
from eap_schemas.architect import ArchitectWorkflowSpec
from worker_tests.conftest import APP_DB_URL
from worker_tests.test_architect_gated_tools import draft_workflow, run, save_and_publish_agent
from worker_tests.test_architect_tools import agent_spec, allowlist


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(APP_DB_URL)
    yield eng
    eng.dispose()


@pytest.fixture
def tenant(engine):
    tenant_id = f"t_{uuid.uuid4().hex[:10]}"
    yield tenant_id
    with engine.begin() as conn:
        for table in ("workflow_schedules", "agent_versions", "agents", "workflow_versions", "workflows"):
            conn.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tenant_id})


def by_id(rows):
    return {r["id"]: r for r in rows}


def test_agents_show_published_version_and_pending_draft(engine, tenant):
    run(save_agent(engine, tenant, agent_spec(id="sketch", name="Sketch"), allowlist=allowlist()))
    save_and_publish_agent(engine, tenant, slug="live")
    save_and_publish_agent(engine, tenant, slug="edited")
    run(save_agent(engine, tenant, agent_spec(id="edited", instructions="Changed after publishing."), allowlist=allowlist()))
    rows = by_id(run(list_created_agents(engine, tenant))["agents"])
    assert (rows["sketch"]["published_version"], rows["sketch"]["draft_pending"], rows["sketch"]["archived"]) == (None, False, False)
    assert (rows["live"]["published_version"], rows["live"]["draft_pending"]) == (1, False)
    assert (rows["edited"]["published_version"], rows["edited"]["draft_pending"]) == (1, True)
    assert rows["sketch"]["draft"] is True and rows["live"]["draft"] is False  # the older field is kept


def test_workflows_show_published_version_and_pending_draft(engine, tenant):
    draft_workflow(engine, tenant, slug="never", name="Never published")
    draft_workflow(engine, tenant, slug="live", name="Live")
    run(publish_workflow(engine, tenant, "live", run_id="r1"))
    spec = ArchitectWorkflowSpec(name="Live", id="live", steps=[{"kind": "tool", "id": "stamp", "tool": "clock.now"},
                                                               {"kind": "tool", "id": "again", "tool": "clock.now"}])
    run(save_workflow(engine, tenant, spec, allowlist=allowlist()))
    rows = by_id(run(list_created_workflows(engine, tenant))["workflows"])
    assert (rows["never"]["published_version"], rows["never"]["draft_pending"], rows["never"]["archived"]) == (None, False, False)
    assert (rows["live"]["published_version"], rows["live"]["draft_pending"]) == (1, True)
