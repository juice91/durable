"""ScheduleService: schedule CRUD, validation and revision checks, without HTTP."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from eap_api.services.errors import Conflict
from eap_api.services.schedules import ScheduleService
from eap_api.services.workflows import WorkflowService
from eap_run_store import ScheduleStore
from eap_run_store.definition_errors import DefinitionError
from eap_schemas.workflows import WorkflowSpec

from ..run_helpers import PG_URL


@pytest.fixture
def env():
    if not (PG_URL or "").startswith("postgresql"):
        pytest.skip("needs Postgres: start the dev database (bash scripts/dev-db.sh up)")
    engine = create_engine(PG_URL)
    with Session(engine) as db:
        yield db, ScheduleStore(engine)
    engine.dispose()


def _published(db, tenant):
    spec = WorkflowSpec.model_validate({
        "name": "wf", "input_schema": {"type": "object"}, "start": "now",
        "steps": [{"id": "now", "type": "tool", "tool_id": "clock.now", "input": {}}],
    })
    workflows = WorkflowService(db)
    created, _ = workflows.create(tenant, spec)
    workflows.publish(tenant, "alice", created["id"])
    return created["id"]


def test_create_update_and_stale_revision(env):
    db, store = env
    tenant = f"t{uuid.uuid4().hex[:10]}"
    wid = _published(db, tenant)
    service = ScheduleService(db, store)
    body, revision = service.create(tenant, wid, name="daily", cron="0 9 * * *", timezone_name="UTC", input={})
    assert body["workflow_name"] == "wf" and body["active"] is True and body["last_error"] is None
    paused, new_revision = service.update(tenant, body["id"], {"active": False}, revision)
    assert paused["active"] is False and new_revision == revision + 1
    with pytest.raises(Conflict) as stale:
        service.update(tenant, body["id"], {"active": True}, revision)
    assert stale.value.code == "revision_mismatch" and stale.value.current_revision == new_revision
    assert service.page(tenant, wid, 10, None)["items"][0]["id"] == body["id"]
    service.delete(tenant, body["id"])
    with pytest.raises(DefinitionError) as gone:
        service.get(tenant, body["id"])
    assert gone.value.body == {"detail": "Schedule not found"}


def test_validation_refusals_keep_their_nested_detail(env):
    db, store = env
    tenant = f"t{uuid.uuid4().hex[:10]}"
    wid = _published(db, tenant)
    service = ScheduleService(db, store)
    with pytest.raises(DefinitionError) as bad_cron:
        service.create(tenant, wid, name="x", cron="nope", timezone_name="UTC", input={})
    assert bad_cron.value.status_code == 422
    assert bad_cron.value.body["detail"]["code"] == "invalid_cron"
    with pytest.raises(DefinitionError) as bad_zone:
        service.create(tenant, wid, name="x", cron="0 9 * * *", timezone_name="Mars/Base", input={})
    assert bad_zone.value.body["detail"]["code"] == "invalid_timezone"
    service.create(tenant, wid, name="same", cron="0 9 * * *", timezone_name="UTC", input={})
    with pytest.raises(DefinitionError) as taken:
        service.create(tenant, wid, name="same", cron="0 9 * * *", timezone_name="UTC", input={})
    assert taken.value.status_code == 409 and taken.value.body["detail"] == {"code": "schedule_name_taken"}
