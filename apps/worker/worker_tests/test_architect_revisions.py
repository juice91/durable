"""The Architect's draft and schedule tools under optimistic concurrency (migration 0033): a save is guarded
by the revision the call read; a concurrent write makes it re-read once and re-apply on the fresh row, and a
second conflict is the tool error `revision_mismatch` with the current revision."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import create_engine, text

from eap_run_store import ScheduleStore
from eap_worker import architect_tools
from eap_worker.architect_tools import AGENTS, save_agent, set_workflow_schedule
from worker_tests.conftest import APP_DB_URL
from worker_tests.test_architect_gated_tools import draft_workflow
from worker_tests.test_architect_gated_tools import publish_workflow as publish_wf
from worker_tests.test_architect_tools import agent_spec, agents_rows, allowlist


def run(coro):
    return asyncio.run(coro)


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


def revision_of(engine, tenant, table="agents") -> int:
    with engine.connect() as conn:
        return conn.execute(text(f"SELECT revision FROM {table} WHERE tenant_id = :t"), {"t": tenant}).scalar_one()


def someone_else_saves(engine, tenant, instructions: str = "Theirs."):
    """A person saves the draft in the editor: the row's revision moves on."""
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE agents SET draft_spec = jsonb_set(draft_spec, '{instructions}', to_jsonb(CAST(:i AS text))),"
                 " revision = revision + 1 WHERE tenant_id = :t"),
            {"i": instructions, "t": tenant},
        )


def test_save_agent_reports_the_revision_and_bumps_it(engine, tenant):
    first = run(save_agent(engine, tenant, agent_spec(id="helper"), allowlist=allowlist()))
    assert first["status"] == "draft" and first["revision"] == 1
    second = run(save_agent(engine, tenant, agent_spec(id="helper", instructions="Be brief."), allowlist=allowlist()))
    assert second["revision"] == 2 and revision_of(engine, tenant) == 2


def test_a_concurrent_save_makes_the_tool_re_read_and_apply_its_change_on_the_fresh_row(engine, tenant):
    run(save_agent(engine, tenant, agent_spec(id="helper"), allowlist=allowlist()))
    attempts: list[int] = []

    def race(attempt: int) -> None:  # between the tool's read and its write, on the first attempt only
        attempts.append(attempt)
        if attempt == 1:
            someone_else_saves(engine, tenant)

    result = architect_tools._save(
        engine, tenant, AGENTS, slug="helper", derived_from="Helper", name="Helper",
        spec_json={**agents_rows(engine, tenant)[0]["draft_spec"], "instructions": "Ours."}, confirm=False,
        before_write=race,
    )
    assert attempts == [1, 2]
    assert result == {"id": "helper", "version": None, "status": "draft", "revision": 3}  # 1 -> theirs 2 -> ours 3
    assert agents_rows(engine, tenant)[0]["draft_spec"]["instructions"] == "Ours."


def test_a_second_conflict_is_the_tool_error_with_the_current_revision(engine, tenant):
    run(save_agent(engine, tenant, agent_spec(id="helper"), allowlist=allowlist()))
    before = agents_rows(engine, tenant)[0]["draft_spec"]

    result = architect_tools._save(
        engine, tenant, AGENTS, slug="helper", derived_from="Helper", name="Helper",
        spec_json={**before, "instructions": "Ours."}, confirm=False,
        before_write=lambda attempt: someone_else_saves(engine, tenant, f"Theirs {attempt}."),
    )
    error = result["error"]
    assert error["code"] == "revision_mismatch" and error["current_revision"] == 3
    assert "someone else" in error["message"]
    # nothing of ours was written: the last writer's draft stands
    assert agents_rows(engine, tenant)[0]["draft_spec"]["instructions"] == "Theirs 2."


def test_pause_and_resume_retry_once_on_a_schedule_somebody_edited(engine, tenant, monkeypatch):
    draft_workflow(engine, tenant)
    run(publish_wf(engine, tenant, "daily"))
    made = run(set_workflow_schedule(
        engine, tenant, "daily", "create", name="Morning", cron="30 9 * * *", input={"input": "go"}))
    assert made["revision"] == 1

    real_update = ScheduleStore.update
    interleave = {"left": 1}

    def racing_update(self, tenant_id, schedule_id, *, expected_revision=None, **fields):
        if interleave["left"] > 0 and expected_revision is not None:  # the person's PATCH lands first
            interleave["left"] -= 1
            real_update(self, tenant_id, schedule_id, name="Morning (edited)")
        return real_update(self, tenant_id, schedule_id, expected_revision=expected_revision, **fields)

    monkeypatch.setattr(ScheduleStore, "update", racing_update)
    paused = run(set_workflow_schedule(engine, tenant, "daily", "pause", schedule_id=made["schedule_id"]))
    assert paused["status"] == "paused" and paused["revision"] == 3  # theirs (2), then ours on the fresh row (3)
    assert paused["name"] == "Morning (edited)" and paused["active"] is False  # their edit survived

    interleave["left"] = 2  # a writer wins both attempts
    refused = run(set_workflow_schedule(engine, tenant, "daily", "resume", schedule_id=made["schedule_id"]))
    assert refused["error"]["code"] == "revision_mismatch" and refused["error"]["current_revision"] == 5
    with engine.connect() as conn:
        assert conn.execute(text("SELECT active FROM workflow_schedules WHERE tenant_id = :t"), {"t": tenant}).scalar_one() is False
