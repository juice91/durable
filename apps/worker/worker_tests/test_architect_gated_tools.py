"""The Architect's gated tools (Task 4): publish, restore, archive and schedules.

The tools hold no logic of their own: they call eap_run_store.definitions (the API's functions) and
eap_run_store.ScheduleStore, so these tests check the wiring, the refusals and the approval wording.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import create_engine, text

from eap_runtime import ToolRegistry
from eap_schemas.architect import (
    ARCHITECT_GATED_TOOL_IDS,
    AllowlistAgent,
    ArchitectWorkflowSpec,
    ungated_architect_tools,
)
from eap_worker.architect_tools import (
    GATED_TOOL_IDS,
    TOOL_IDS,
    approval_summary_text,
    archive_agent,
    archive_workflow,
    describe_cron,
    list_created_agents,
    publish_agent,
    publish_workflow,
    register_architect_tools,
    restore_agent_version,
    restore_workflow_version,
    save_agent,
    save_workflow,
    set_workflow_schedule,
)
from worker_tests.conftest import APP_DB_URL
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


def versions(engine, tenant, table="agent_versions"):
    with engine.connect() as conn:
        return conn.execute(
            text(f"SELECT * FROM {table} WHERE tenant_id = :t ORDER BY version"), {"t": tenant}
        ).mappings().all()


def save_and_publish_agent(engine, tenant, slug="helper", run_id="r-1", **over):
    run(save_agent(engine, tenant, agent_spec(id=slug, **over), allowlist=allowlist()))
    return run(publish_agent(engine, tenant, slug, run_id=run_id))


def draft_workflow(engine, tenant, slug="daily", name="Daily note"):
    spec = ArchitectWorkflowSpec(name=name, id=slug, steps=[{"kind": "tool", "id": "stamp", "tool": "clock.now"}])
    return run(save_workflow(engine, tenant, spec, allowlist=allowlist()))


# ------------------------------------------------------------------------------------ gating


def test_publish_without_approval_is_impossible_from_the_tool_path():
    """Every gated tool is registered with a summary, and a spec that lists one without its approvals
    entry is refused (the API on save, the worker before the model runs): there is no path to a
    publish, restore, archive or schedule change that skips a recorded approval."""
    registry = ToolRegistry()
    register_architect_tools(registry)
    assert set(TOOL_IDS) == registry.tool_ids()
    assert GATED_TOOL_IDS == ARCHITECT_GATED_TOOL_IDS and len(GATED_TOOL_IDS) == 7
    for tool_id in GATED_TOOL_IDS:
        assert registry.summary_for(tool_id, {"id": "x", "version": 1, "workflow_id": "x", "action": "pause"})
        assert registry.get_metadata(tool_id)["side_effect"] is True
    for tool_id in set(TOOL_IDS) - set(GATED_TOOL_IDS):
        assert registry.summary_for(tool_id, {}) is None  # the read and draft tools are never gated
    assert ungated_architect_tools(["architect.publish_agent", "architect.save_agent"], None) == [
        "architect.publish_agent"
    ]
    assert ungated_architect_tools(GATED_TOOL_IDS, GATED_TOOL_IDS) == []


def test_denied_publish_leaves_the_draft(engine, tenant):
    """A denied approval means the tool function never runs; the draft stays a draft. A refused call
    (here: the platform's model policy) leaves no version and the draft intact too."""
    run(save_agent(engine, tenant, agent_spec(id="helper"), allowlist=allowlist()))
    (before,) = agents_rows(engine, tenant)
    assert versions(engine, tenant) == []
    assert run(list_created_agents(engine, tenant))["agents"][0]["draft"] is True

    import os

    os.environ["EAP_ALLOWED_MODELS"] = "openai:gpt-4"
    try:
        refused = run(publish_agent(engine, tenant, "helper", run_id="r-1"))
    finally:
        del os.environ["EAP_ALLOWED_MODELS"]
    assert refused["error"]["code"] == "publish_refused" and "test:echo" in refused["error"]["message"]
    assert versions(engine, tenant) == []
    (after,) = agents_rows(engine, tenant)
    assert after["draft_spec"] == before["draft_spec"]


def test_approved_publish_creates_a_version_with_marker(engine, tenant):
    result = save_and_publish_agent(engine, tenant, run_id="run-42")
    assert result == {"id": "helper", "version": 1, "status": "published", "warnings": []}
    (v,) = versions(engine, tenant)
    assert v["created_via"] == "architect:run-42" and v["created_by"] == "architect"
    assert v["spec"]["name"] == "Helper" and v["restored_from"] is None
    assert run(list_created_agents(engine, tenant))["agents"] == [
        {"id": "helper", "uuid": run(list_created_agents(engine, tenant))["agents"][0]["uuid"], "name": "Helper",
         "versions": 1, "latest_created_via": "architect:run-42", "draft": False, "published_version": 1,
         "draft_pending": False, "archived": False}
    ]
    again = run(publish_agent(engine, tenant, "helper", run_id="run-43"))
    assert again["error"]["code"] == "conflict" and "no changes since version 1" in again["error"]["message"]
    assert len(versions(engine, tenant)) == 1


def test_gated_tools_refuse_builtin_agents_unknown_ids_and_other_tenants(engine, tenant):
    save_and_publish_agent(engine, tenant)
    save_and_publish_agent(engine, tenant, slug="second")
    with engine.begin() as conn:  # a built-in row that has a slug (the seed gives the Architect one)
        conn.execute(text("UPDATE agents SET builtin = true WHERE tenant_id = :t AND slug = 'second'"), {"t": tenant})
        conn.execute(text(
            "INSERT INTO agents (id, tenant_id, name, draft_spec, archived, builtin, created_at, updated_at) "
            "SELECT CAST(:id AS varchar), CAST(:t AS varchar), 'Hand made', draft_spec, false, false, now(), now() FROM agents "
            "WHERE tenant_id = :t AND slug = 'helper'"), {"id": str(uuid.uuid4()), "t": tenant})
    assert run(publish_agent(engine, tenant, "second"))["error"]["code"] == "builtin_agent"
    assert run(archive_agent(engine, tenant, "second"))["error"]["code"] == "builtin_agent"
    assert run(restore_agent_version(engine, tenant, "second", 1))["error"]["code"] == "builtin_agent"
    # A definition without a slug is not reachable by id at all.
    assert run(publish_agent(engine, tenant, "hand-made"))["error"]["code"] == "not_found"
    # Another tenant sees nothing.
    other = f"t_{uuid.uuid4().hex[:10]}"
    for call in (
        publish_agent(engine, other, "helper"),
        archive_agent(engine, other, "helper"),
        restore_agent_version(engine, other, "helper", 1),
        publish_workflow(engine, other, "daily"),
        archive_workflow(engine, other, "daily"),
        set_workflow_schedule(engine, other, "daily", "pause", name="x"),
    ):
        assert run(call)["error"]["code"] == "not_found"
    assert len(versions(engine, tenant)) == 2
    with engine.connect() as conn:
        assert conn.execute(
            text("SELECT archived FROM agents WHERE tenant_id = :t AND slug = 'second'"), {"t": tenant}
        ).scalar_one() is False


# ---------------------------------------------------------------------------- restore and archive


def test_restore_agent_version_publishes_the_old_spec_again(engine, tenant):
    save_and_publish_agent(engine, tenant, run_id="r-1")
    run(save_agent(engine, tenant, agent_spec(id="helper", instructions="Be brief."), allowlist=allowlist()))
    assert run(publish_agent(engine, tenant, "helper", run_id="r-2"))["version"] == 2

    run(save_agent(engine, tenant, agent_spec(id="helper", instructions="Unpublished edit."), allowlist=allowlist()))
    dirty = run(restore_agent_version(engine, tenant, "helper", 1, run_id="r-3"))
    assert dirty["error"]["code"] == "conflict" and "unpublished changes" in dirty["error"]["message"]

    done = run(restore_agent_version(engine, tenant, "helper", 1, discard_draft=True, run_id="r-3"))
    assert done == {"id": "helper", "version": 3, "status": "restored", "warnings": [], "restored_from": 1}
    v1, _v2, v3 = versions(engine, tenant)
    assert v3["restored_from"] == 1 and v3["spec_hash"] == v1["spec_hash"] and v3["created_via"] == "architect:r-3"
    (row,) = agents_rows(engine, tenant)
    assert row["draft_spec"] == v1["spec"]
    assert run(restore_agent_version(engine, tenant, "helper", 3))["error"]["code"] == "conflict"
    assert run(restore_agent_version(engine, tenant, "helper", 9))["error"]["code"] == "not_found"


def test_archive_agent_is_refused_while_a_published_workflow_runs_it(engine, tenant):
    save_and_publish_agent(engine, tenant)
    with engine.connect() as conn:
        row_id = conn.execute(text("SELECT id FROM agents WHERE tenant_id = :t"), {"t": tenant}).scalar_one()
    with_agent = allowlist(agents=(AllowlistAgent(id="helper", name="Helper", row_id=str(row_id)),))
    spec = ArchitectWorkflowSpec(name="Uses helper", id="uses-helper", steps=[
        {"kind": "agent", "id": "ask", "agent": "helper", "prompt": "{{input}}"}])
    run(save_workflow(engine, tenant, spec, allowlist=with_agent))
    assert run(publish_workflow(engine, tenant, "uses-helper", run_id="r-1"))["version"] == 1

    summary = approval_summary_text(engine, tenant, "architect.archive_agent", {"id": "helper"})
    assert "Uses helper" in summary and "refused" in summary
    refused = run(archive_agent(engine, tenant, "helper"))
    assert refused["error"]["code"] == "conflict" and refused["error"]["workflows"][0]["workflow_name"] == "Uses helper"

    assert run(archive_workflow(engine, tenant, "uses-helper"))["status"] == "archived"
    assert run(archive_agent(engine, tenant, "helper")) == {"id": "helper", "status": "archived", "archived": True}
    assert run(list_created_agents(engine, tenant))["agents"] == []  # archived ones leave the list


def test_workflow_publish_restore_archive_share_the_apis_behaviour(engine, tenant):
    assert draft_workflow(engine, tenant)["status"] == "draft"
    assert run(publish_workflow(engine, tenant, "daily", run_id="r-1")) == {
        "id": "daily", "version": 1, "status": "published", "warnings": []}
    assert versions(engine, tenant, "workflow_versions")[0]["created_via"] == "architect:r-1"
    assert run(publish_workflow(engine, tenant, "daily"))["error"]["code"] == "conflict"

    draft_workflow(engine, tenant, name="Daily note v2")
    assert run(publish_workflow(engine, tenant, "daily", run_id="r-2"))["version"] == 2
    restored = run(restore_workflow_version(engine, tenant, "daily", 1, run_id="r-3"))
    assert restored["version"] == 3 and restored["restored_from"] == 1 and restored["status"] == "restored"
    v1, _v2, v3 = versions(engine, tenant, "workflow_versions")
    assert v3["spec_hash"] == v1["spec_hash"] and v3["created_via"] == "architect:r-3"

    assert run(archive_workflow(engine, tenant, "daily"))["archived"] is True
    assert run(publish_workflow(engine, tenant, "daily"))["error"]["code"] == "conflict"
    assert "archived" in run(restore_workflow_version(engine, tenant, "daily", 1))["error"]["message"]


def test_archive_workflow_not_found_points_at_the_workflow_page(engine, tenant):
    missing = run(archive_workflow(engine, tenant, "nope"))["error"]
    assert missing["code"] == "not_found"
    assert "archived from the workflow page in Studio" in missing["message"]
    # Only the archive tool says so: publishing someone else's workflow is a plain not_found.
    assert "Studio" not in run(publish_workflow(engine, tenant, "nope"))["error"]["message"]


# --------------------------------------------------------------------------- schedules and summaries


def test_schedule_create_is_gated_and_names_the_cron_in_words(engine, tenant):
    registry = ToolRegistry()
    register_architect_tools(registry)
    args = {"workflow_id": "daily", "action": "create", "name": "Morning", "cron": "30 9 * * *", "timezone": "Asia/Taipei"}
    pure = registry.summary_for("architect.set_workflow_schedule", args)
    assert "Every day at 09:30" in pure and "Asia/Taipei" in pure and "Morning" in pure
    assert approval_summary_text(engine, tenant, "architect.set_workflow_schedule", args) == pure
    assert "0 9 * * 1-5 (Asia/Taipei)" in registry.summary_for(  # not a common pattern: the cron as written
        "architect.set_workflow_schedule", {**args, "cron": "0 9 * * 1-5"})
    assert [describe_cron(c) for c in ("* * * * *", "*/5 * * * *", "5 * * * *", "0 * * * *", "30 9 * * *")] == [
        "Every minute", "Every 5 minutes", "Every hour at :05", "Every hour at :00", "Every day at 09:30"]
    assert describe_cron("nonsense") == "nonsense"

    draft_workflow(engine, tenant)
    early = run(set_workflow_schedule(engine, tenant, "daily", "create", name="Morning", cron="30 9 * * *"))
    assert early["error"]["code"] == "not_published"
    run(publish_workflow(engine, tenant, "daily"))
    bad = run(set_workflow_schedule(engine, tenant, "daily", "create", name="Morning", cron="every day"))
    assert bad["error"]["code"] == "invalid_cron"
    bad = run(set_workflow_schedule(engine, tenant, "daily", "create", name="Morning", cron="0 9 * * *", timezone="Mars/Base"))
    assert bad["error"]["code"] == "invalid_timezone"

    wrong = run(set_workflow_schedule(engine, tenant, "daily", "create", name="Morning", cron="0 9 * * *"))
    assert wrong["error"]["code"] == "input_schema_errors"  # workflows made by the Architect take {"input": text}
    made = run(set_workflow_schedule(
        engine, tenant, "daily", "create", name="Morning", cron="30 9 * * *", timezone="Asia/Taipei",
        input={"input": "go"}))
    assert made["status"] == "created" and made["cron_in_words"] == "Every day at 09:30" and made["active"] is True
    taken = run(set_workflow_schedule(engine, tenant, "daily", "create", name="Morning", cron="0 9 * * *", input={"input": "x"}))
    assert taken["error"]["code"] == "schedule_name_taken"

    paused = run(set_workflow_schedule(engine, tenant, "daily", "pause", schedule_id=made["schedule_id"]))
    assert paused["status"] == "paused" and paused["active"] is False
    resumed = run(set_workflow_schedule(engine, tenant, "daily", "resume", name="Morning"))
    assert resumed["status"] == "resumed" and resumed["active"] is True
    gone = run(set_workflow_schedule(engine, tenant, "daily", "delete", name="Morning"))
    assert gone["status"] == "deleted"
    assert run(set_workflow_schedule(engine, tenant, "daily", "pause", name="Morning"))["error"]["code"] == "not_found"
    assert run(set_workflow_schedule(engine, tenant, "daily", "explode"))["error"]["code"] == "invalid_action"


def test_approval_summaries_carry_the_facts_the_real_call_would_meet(engine, tenant):
    save_and_publish_agent(engine, tenant)
    run(save_agent(engine, tenant, agent_spec(id="helper", instructions="Be brief."), allowlist=allowlist()))
    publish = approval_summary_text(engine, tenant, "architect.publish_agent", {"id": "helper"})
    assert "Publishes agent 'Helper' as version 2" in publish and "No publish warnings." in publish
    assert len(versions(engine, tenant)) == 1  # a dry run: nothing written

    run(publish_agent(engine, tenant, "helper"))
    restore = approval_summary_text(engine, tenant, "architect.restore_agent_version", {"id": "helper", "version": 1})
    assert "from version 2 (live now) to version 1, published as version 3" in restore
    assert len(versions(engine, tenant)) == 2

    ghost = approval_summary_text(engine, tenant, "architect.publish_agent", {"id": "ghost"})
    assert "It would be refused" in ghost
    assert approval_summary_text(engine, tenant, "architect.list_created_agents", {}) is None

    draft_workflow(engine, tenant)
    run(publish_workflow(engine, tenant, "daily"))
    made = run(set_workflow_schedule(
        engine, tenant, "daily", "create", name="Morning", cron="30 9 * * *", input={"input": "go"}))
    assert made["status"] == "created", made
    archive = approval_summary_text(engine, tenant, "architect.archive_workflow", {"id": "daily"})
    assert "pauses 'Morning' (Every day at 09:30, UTC)" in archive
    with engine.connect() as conn:  # still active: the summary was a dry run
        assert conn.execute(text("SELECT active FROM workflow_schedules WHERE tenant_id = :t"), {"t": tenant}).scalar_one()
    done = run(archive_workflow(engine, tenant, "daily"))
    assert done["paused_schedules"] == 1
    with engine.connect() as conn:
        assert conn.execute(text("SELECT active FROM workflow_schedules WHERE tenant_id = :t"), {"t": tenant}).scalar_one() is False


def test_the_approval_begin_step_words_architect_calls_with_database_facts(engine, tenant, monkeypatch):
    """capabilities._enriched_summary (inside the begin step) swaps in the facts-based summary for an
    architect.* call and leaves every other tool's payload alone."""
    from types import SimpleNamespace

    from eap_worker import capabilities, runtime

    registry = ToolRegistry()
    register_architect_tools(registry)
    monkeypatch.setattr(runtime, "get_runtime", lambda: SimpleNamespace(registry=registry, engine=engine))
    save_and_publish_agent(engine, tenant)
    run(save_agent(engine, tenant, agent_spec(id="helper", instructions="Be brief."), allowlist=allowlist()))
    data = {"tool_name": "architect__publish_agent", "args": {"id": "helper"}}
    assert "as version 2" in capabilities._enriched_summary(tenant, data)
    assert capabilities._enriched_summary(tenant, {"tool_name": "tickets__create", "args": {}}) is None
