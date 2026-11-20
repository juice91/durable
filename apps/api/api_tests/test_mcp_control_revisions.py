"""The MCP control write tools under optimistic concurrency (migration 0033): they pass the revision they
read; a write that lands in between makes them read again and re-apply their change once; a second conflict
is the tool error `{error: {code: "revision_mismatch", message, current_revision}}`."""

from __future__ import annotations

import pytest

from eap_api.mcp_control import routing, tools_manage

from .mcp_control_helpers import secrets_key, world  # noqa: F401 - fixtures
from .run_helpers import requires_pg
from .test_mcp_control_manage import SPEC, manage_key

pytestmark = requires_pg


def bump(world, table: str, row_id: str) -> None:
    world.sql(f"UPDATE {table} SET revision = revision + 1 WHERE id = :i", i=row_id)


def race(monkeypatch, world, table: str, row_id: str, times: int):
    """The tool's revision read is followed by somebody else's save, `times` times."""
    state = {"left": times, "reads": 0}
    real = routing.revision_reader

    def reader(ctx, tbl, rid):
        read = real(ctx, tbl, rid)

        def racing() -> int | None:
            value = read()
            state["reads"] += 1
            if state["left"] > 0:
                state["left"] -= 1
                bump(world, table, row_id)
            return value

        return racing

    monkeypatch.setattr(tools_manage, "revision_reader", reader)
    return state


def test_agent_save_draft_retries_once_on_a_conflict_and_applies_its_change(world, monkeypatch):
    _, h = world.tenant_of()
    key = manage_key(world, h)
    agent_id = world.call(key, "agent.save_draft", {"spec": SPEC}).data["id"]
    state = race(monkeypatch, world, "agents", agent_id, times=1)

    saved = world.call(key, "agent.save_draft", {"agent_id": agent_id, "spec": {**SPEC, "name": "Ours"}})
    assert not saved.is_error, saved.data
    assert saved.data["name"] == "Ours" and saved.data["revision"] == 3  # created 1, somebody 2, ours 3
    assert state["reads"] == 2  # it read again after the conflict


def test_agent_save_draft_second_conflict_is_a_tool_error_with_the_current_revision(world, monkeypatch):
    _, h = world.tenant_of()
    key = manage_key(world, h)
    agent_id = world.call(key, "agent.save_draft", {"spec": SPEC}).data["id"]
    race(monkeypatch, world, "agents", agent_id, times=2)

    refused = world.call(key, "agent.save_draft", {"agent_id": agent_id, "spec": {**SPEC, "name": "Ours"}})
    assert refused.is_error
    error = refused.error
    assert error["code"] == "revision_mismatch" and error["current_revision"] == 3
    assert "someone else" in error["message"]
    with world.rest() as c:
        assert c.get(f"/agents/{agent_id}", headers=h).json()["name"] == "Drafted"  # nothing of ours was written


def test_workflow_save_draft_and_schedule_set_use_the_same_retry(world, monkeypatch):
    _, h = world.tenant_of()
    key = manage_key(world, h)
    agent_id = world.publish_agent(h)
    wid = world.publish_workflow(h, agent_id)
    with world.rest() as c:
        draft = c.get(f"/workflows/{wid}", headers=h).json()["draft_spec"]
    state = race(monkeypatch, world, "workflows", wid, times=1)
    saved = world.call(key, "workflow.save_draft", {"workflow_id": wid, "spec": {**draft, "name": "Renamed flow"}})
    assert not saved.is_error and saved.data["name"] == "Renamed flow" and state["reads"] == 2
    monkeypatch.undo()

    created = world.call(key, "workflow.schedule.set", {
        "workflow_id": wid, "name": "hourly", "cron": "5 * * * *", "input": {"q": "x"}})
    assert not created.is_error, created.data
    sid = created.data["id"]
    # a person edits the schedule between the tool's read and its write: one retry, then the error
    from eap_run_store import ScheduleStore

    real_get = ScheduleStore.get
    seen = {"n": 0}

    def get_then_edit(self, tenant_id, schedule_id):
        row = real_get(self, tenant_id, schedule_id)
        seen["n"] += 1
        # get #1 is the tool's workflow check; #2 and #5 are the tool's two revision reads (the route's own
        # get and its 412 re-read are #3, #4 and #6, #7)
        if row is not None and seen["n"] in (2, 5):
            world.sql("UPDATE workflow_schedules SET revision = revision + 1 WHERE id = :i", i=schedule_id)
        return row

    monkeypatch.setattr(ScheduleStore, "get", get_then_edit)
    paused = world.call(key, "workflow.schedule.set", {"workflow_id": wid, "schedule_id": sid, "active": False})
    assert paused.is_error and paused.error["code"] == "revision_mismatch"
    assert isinstance(paused.error["current_revision"], int)
