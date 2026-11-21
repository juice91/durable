"""WorkflowService: the drafts, versions and archive logic behind /workflows, without HTTP."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from eap_api.services.errors import Conflict
from eap_api.services.workflows import WorkflowService
from eap_run_store.definition_errors import DefinitionError
from eap_schemas.workflows import WorkflowSpec

from ..run_helpers import PG_URL


@pytest.fixture
def session():
    if not (PG_URL or "").startswith("postgresql"):
        pytest.skip("needs Postgres: start the dev database (bash scripts/dev-db.sh up)")
    engine = create_engine(PG_URL)
    with Session(engine) as db:
        yield db
    engine.dispose()


@pytest.fixture
def tenant():
    return f"t{uuid.uuid4().hex[:10]}"


def _spec(name: str = "wf") -> WorkflowSpec:
    return WorkflowSpec.model_validate({
        "name": name,
        "input_schema": {"type": "object"},
        "start": "now",
        "steps": [{"id": "now", "type": "tool", "tool_id": "clock.now", "input": {}}],
    })


def test_create_get_and_list_are_tenant_scoped(session, tenant):
    service = WorkflowService(session)
    body, revision = service.create(tenant, _spec("one"))
    assert body["name"] == "one" and body["latest_version"] is None and body["revision"] == revision
    assert service.get(tenant, body["id"])[0] == body
    assert [w["id"] for w in service.list(tenant)] == [body["id"]]
    assert service.list(f"{tenant}x") == []
    with pytest.raises(DefinitionError) as refused:
        service.get(f"{tenant}x", body["id"])
    assert refused.value.status_code == 404 and refused.value.body == {"detail": "Workflow not found"}


def test_replace_draft_bumps_the_revision_and_refuses_a_stale_one(session, tenant):
    service = WorkflowService(session)
    created, revision = service.create(tenant, _spec("one"))
    updated, new_revision = service.replace_draft(tenant, created["id"], _spec("two"), revision)
    assert updated["name"] == "two" and new_revision == revision + 1
    with pytest.raises(Conflict) as stale:
        service.replace_draft(tenant, created["id"], _spec("three"), revision)
    assert stale.value.code == "revision_mismatch"
    assert stale.value.current_revision == new_revision
    assert stale.value.current["name"] == "two"
    # No If-Match: no comparison.
    assert service.replace_draft(tenant, created["id"], _spec("three"), None)[0]["name"] == "three"


def test_archive_hides_the_workflow_from_the_default_list(session, tenant):
    service = WorkflowService(session)
    created, _ = service.create(tenant, _spec())
    archived, _ = service.archive(tenant, created["id"], True)
    assert archived["archived"] is True and archived["paused_schedules"] == []
    assert service.list(tenant) == []
    assert [w["id"] for w in service.list(tenant, "true")] == [created["id"]]
    assert len(service.list(tenant, "all")) == 1
    assert service.archive(tenant, created["id"], False)[0]["archived"] is False


def test_unknown_version_and_unpublished_workflow(session, tenant):
    service = WorkflowService(session)
    created, _ = service.create(tenant, _spec())
    assert service.list_versions(tenant, created["id"]) == []
    with pytest.raises(DefinitionError) as refused:
        service.get_version(tenant, created["id"], 1)
    assert refused.value.body == {"detail": "Version not found"}
