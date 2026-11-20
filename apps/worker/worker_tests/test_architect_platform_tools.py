"""The Architect's `platform.*` read tools: knowledge bases and sources, syncs, every workflow and agent,
schedules and runs. Real Postgres, no model. They call eap_run_store.platform_reads (the one implementation),
so these tests check what comes back, the redaction, the limits, tenant isolation and the registration."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from eap_run_store import KnowledgeStore, RunStore, ScheduleStore, platform_reads
from eap_run_store.kb_sources import ConnectionStore, SourceStore, SyncStore
from eap_runtime import ToolRegistry
from eap_schemas.architect import (
    ARCHITECT_DENIED_TOOL_PREFIXES,
    ARCHITECT_PLATFORM_TOOL_IDS,
    ARCHITECT_TOOL_IDS,
    BUILTIN_ONLY_TOOL_PREFIXES,
    ungated_architect_tools,
)
from eap_schemas.architect_seed import architect_agent_spec
from eap_worker import platform_read_tools as tools
from eap_worker.architect_allowlist import registry_tools
from eap_worker.architect_tools import list_created_agents, publish_agent, save_agent
from eap_worker.platform_tools import register_platform_tools
from worker_tests.conftest import cleanup_tenant
from worker_tests.kb_ingest_helpers import cleanup_knowledge, create_kb, upload
from worker_tests.test_architect_gated_tools import draft_workflow, run, save_and_publish_agent


@pytest.fixture
def engine(worker):
    return worker.engine


def _cleanup(engine, tenant_id: str) -> None:
    with engine.begin() as conn:
        for table in ("workflow_schedules", "agent_versions", "agents", "workflow_versions", "workflows"):
            conn.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tenant_id})
    cleanup_tenant(engine, tenant_id)
    cleanup_knowledge(engine, tenant_id)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM settings_connections WHERE tenant_id = :t"), {"t": tenant_id})


@pytest.fixture
def tenant(engine):
    tenant_id = f"t_{uuid.uuid4().hex[:10]}"
    yield tenant_id
    _cleanup(engine, tenant_id)


@pytest.fixture
def other(engine):
    tenant_id = f"t_{uuid.uuid4().hex[:10]}"
    yield tenant_id
    _cleanup(engine, tenant_id)


def make_source(engine, tenant, *, config=None, kb_id=None):
    kb_id = kb_id or create_kb(KnowledgeStore(engine), tenant)
    conn = ConnectionStore(engine).create(
        tenant, name=f"c-{uuid.uuid4().hex[:6]}", kind="oauth2_client_credentials", base_url=None, config={},
        credential_refs={"client_secret": "secret:ti-secret"},
    )
    source = SourceStore(engine).create(
        tenant, knowledge_base_id=kb_id, connection_id=conn.id, name="TI parts", kind="product_catalog",
        provider="ti_catalog", config=config or {"part_prefix": "LM", "api_key": "sk-abcdefghijklmnop1234", "limit": 5},
    )
    return kb_id, source


def make_sync(engine, tenant, source, *, report=None, status="completed", counters=None, errors=()):
    run_uuid = uuid.uuid4()
    run_id = str(run_uuid)
    RunStore(engine).create_kb_source_sync_run(tenant, run_uuid, source.id, {"mode": "incremental"})
    SyncStore(engine).create(tenant, str(uuid.uuid4()), source.id, run_id, "incremental", uuid.uuid4().hex)
    SyncStore(engine).finish(
        tenant, run_id, status, counters=counters or {"discovered": 10, "inserted": 4, "updated": 3, "failed": 1},
        bounded_errors=list(errors), completed_at=datetime.now(timezone.utc),
    )
    if report is not None:  # the sync run's stored report, as the worker writes it
        with engine.begin() as c:
            c.execute(text("UPDATE runs SET status = 'completed', output = :o WHERE id = :i"), {"i": run_id, "o": json.dumps(report)})
    return run_id


# ------------------------------------------------------------------------------------ knowledge bases


def test_list_knowledge_bases_has_counts_model_and_source_count(engine, tenant):
    kb_id, _ = make_source(engine, tenant)
    upload(KnowledgeStore(engine), tenant, kb_id, "Hello handbook. " * 20)
    out = run(tools.list_knowledge_bases(engine, tenant))
    (kb,) = out["items"]
    assert kb["id"] == kb_id and kb["documents"] == 1 and kb["sources"] == 1
    assert kb["embedding_model"] == "test:hash" and "chunks" in kb and "name" in kb


def test_get_knowledge_base_lists_sources_without_secrets(engine, tenant):
    kb_id, source = make_source(engine, tenant)
    make_sync(engine, tenant, source, status="incomplete")
    out = run(tools.get_knowledge_base(engine, tenant, kb_id))
    (src,) = out["sources_list"]
    assert src["name"] == "TI parts" and src["provider"] == "ti_catalog" and src["kind"] == "product_catalog"
    assert src["last_sync_status"] == "incomplete" and src["last_sync_mode"] == "incremental"
    assert src["config"] == {"part_prefix": "LM", "limit": 5}  # the credential-looking key is gone
    assert src["records"] == 0 and src["documents"] == 0
    blob = json.dumps(out)
    assert "sk-abcdefghijklmnop1234" not in blob and "ti-secret" not in blob and "credential" not in blob


def test_get_knowledge_base_unknown_is_an_error(engine, tenant):
    assert run(tools.get_knowledge_base(engine, tenant, str(uuid.uuid4())))["error"]["code"] == "not_found"
    assert run(tools.get_knowledge_base(engine, tenant, "not-a-uuid"))["error"]["code"] == "not_found"


# ----------------------------------------------------------------------------------------------- syncs


def test_list_source_syncs_reads_counters_and_the_stored_report(engine, tenant):
    _, source = make_source(engine, tenant)
    report = {"budget": {"calls": 12, "limit": 200}, "calls_used": 12, "stop_reason": "complete",
              "diagnostics": {"listing": {"pages": 3}}}
    make_sync(engine, tenant, source, report=report, errors=[{"code": "x"}, {"code": "y"}])
    make_sync(engine, tenant, source, status="failed", counters={"discovered": 0, "inserted": 0, "updated": 0, "failed": 0})
    out = run(tools.list_source_syncs(engine, tenant, source.id, limit=10))
    newest, oldest = out["items"]
    assert newest["status"] == "failed" and newest["has_diagnostics"] is False and newest["budget_used"] is None
    assert oldest["mode"] == "incremental" and (oldest["discovered"], oldest["inserted"], oldest["updated"], oldest["failed"]) == (10, 4, 3, 1)
    assert oldest["errors_count"] == 2 and oldest["has_diagnostics"] is True
    assert oldest["budget_used"] == {"calls": 12, "limit": 200} and oldest["calls_used"] == 12


def test_list_source_syncs_limit_is_bounded_and_unknown_source_errors(engine, tenant):
    _, source = make_source(engine, tenant)
    for _ in range(3):
        make_sync(engine, tenant, source)
    assert len(run(tools.list_source_syncs(engine, tenant, source.id, limit=2))["items"]) == 2
    assert len(run(tools.list_source_syncs(engine, tenant, source.id, limit=10_000))["items"]) == 3
    assert run(tools.list_source_syncs(engine, tenant, str(uuid.uuid4())))["error"]["code"] == "not_found"


# --------------------------------------------------------------------------- workflows and agents


def plain_workflow(engine, tenant, name, *, archived=False, with_version=True):
    """A workflow nobody made through the Architect (no slug)."""
    wid = str(uuid.uuid4())
    spec = {"spec_version": 1, "name": name, "description": "hand made", "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
            "steps": [{"kind": "tool", "id": "stamp", "tool": "clock.now"}, {"kind": "agent", "id": "ask", "agent": wid}], "start": "stamp"}
    with engine.begin() as c:
        c.execute(text("INSERT INTO workflows (id, tenant_id, name, draft_spec, archived, created_at, updated_at) "
                       "VALUES (:i, :t, :n, CAST(:s AS jsonb), :a, now(), now())"), {"i": wid, "t": tenant, "n": name, "s": json.dumps(spec), "a": archived})
        if with_version:
            c.execute(text("INSERT INTO workflow_versions (id, workflow_id, tenant_id, version, spec, spec_hash, created_at, created_by) "
                           "VALUES (gen_random_uuid(), :i, :t, 1, CAST(:s AS jsonb), 'h', now(), 'someone')"), {"i": wid, "t": tenant, "s": json.dumps(spec)})
    return wid


def test_list_workflows_shows_every_workflow_with_publication_state(engine, tenant):
    draft_workflow(engine, tenant, slug="draft-only", name="Draft only")
    draft_workflow(engine, tenant, slug="live", name="Live")
    from eap_worker.architect_tools import publish_workflow

    run(publish_workflow(engine, tenant, "live", run_id="r1"))
    hand = plain_workflow(engine, tenant, "Hand made")
    gone = plain_workflow(engine, tenant, "Old", archived=True)
    items = {i["name"]: i for i in run(tools.list_workflows(engine, tenant))["items"]}
    assert set(items) == {"Draft only", "Live", "Hand made", "Old"}
    assert items["Draft only"]["published_version"] is None and items["Draft only"]["created_by_architect"] is True
    assert items["Live"]["published_version"] == 1 and items["Live"]["draft_pending"] is False
    assert items["Hand made"]["id"] == hand and items["Hand made"]["created_by_architect"] is False
    assert items["Old"]["archived"] is True and items["Old"]["id"] == gone
    active = run(tools.list_workflows(engine, tenant, include_archived=False))["items"]
    assert "Old" not in {i["name"] for i in active}


def test_get_workflow_summarises_the_published_spec_for_any_maker(engine, tenant):
    wid = plain_workflow(engine, tenant, "Hand made")
    out = run(tools.get_workflow(engine, tenant, wid))
    assert out["spec_of"] == "published" and out["published_version"] == 1 and out["start"] == "stamp"
    assert [(s["id"], s["kind"], s.get("tool"), s.get("agent")) for s in out["steps"]] == [
        ("stamp", "tool", "clock.now", None), ("ask", "agent", None, wid)]
    assert out["input_fields"] == ["q"]
    draft_workflow(engine, tenant, slug="mine", name="Mine")
    assert run(tools.get_workflow(engine, tenant, "mine"))["spec_of"] == "draft"  # by slug, never published
    assert run(tools.get_workflow(engine, tenant, str(uuid.uuid4())))["error"]["code"] == "not_found"


def test_list_agents_has_published_version_and_model(engine, tenant):
    save_and_publish_agent(engine, tenant, slug="helper")
    from eap_worker.architect_tools import save_agent as _save
    from worker_tests.test_architect_tools import agent_spec, allowlist

    run(_save(engine, tenant, agent_spec(id="sketch", name="Sketch"), allowlist=allowlist()))
    with engine.begin() as c:  # an Architect drafter helper stays out
        c.execute(text("INSERT INTO agents (id, tenant_id, name, draft_spec, archived, builtin, slug, created_at, updated_at) "
                       "VALUES (:i, :t, 'D', CAST('{}' AS jsonb), false, true, 'x-drafter', now(), now())"), {"i": str(uuid.uuid4()), "t": tenant})
    items = {i["name"]: i for i in run(tools.list_agents(engine, tenant))["items"]}
    assert set(items) == {"Helper", "Sketch"}
    assert items["Helper"]["published_version"] == 1 and items["Helper"]["model"] == "test:echo"
    assert items["Sketch"]["published_version"] is None


# --------------------------------------------------------------------------------- schedules and runs


def test_list_schedules_and_filter_by_workflow(engine, tenant):
    wid = plain_workflow(engine, tenant, "Posting")
    other_wid = plain_workflow(engine, tenant, "Other")
    store = ScheduleStore(engine)
    when = datetime.now(timezone.utc) + timedelta(hours=1)
    s = store.create(tenant, wid, name="morning", cron="0 9 * * *", timezone="Asia/Taipei", input={"q": "x"}, next_fire_at=when)
    store.create(tenant, other_wid, name="noon", cron="0 12 * * *", input={}, next_fire_at=when)
    store.stop(s["id"], "workflow_archived")
    all_items = run(tools.list_schedules(engine, tenant))["items"]
    assert {i["name"] for i in all_items} == {"morning", "noon"}
    (one,) = run(tools.list_schedules(engine, tenant, wid))["items"]
    assert one["workflow_name"] == "Posting" and one["cron"] == "0 9 * * *" and one["timezone"] == "Asia/Taipei"
    assert one["active"] is False and one["last_error"] == "workflow_archived" and one["next_fire_at"].endswith("Z")
    assert "input" not in one  # a schedule's input is not returned


def make_runs(engine, tenant):
    wf = plain_workflow(engine, tenant, "Posting")
    ag = save_and_publish_agent(engine, tenant, slug="helper")
    with engine.connect() as c:
        agent_uuid = c.execute(text("SELECT id FROM agents WHERE tenant_id = :t AND slug = 'helper'"), {"t": tenant}).scalar()
    rs = RunStore(engine)
    wr, _ = rs.create_workflow_run(tenant, wf, 1, "SECRET-INPUT-TEXT", uuid.uuid4())
    ar = uuid.uuid4()
    rs.create_run(tenant, agent_uuid, 1, "ANOTHER-INPUT", ar)
    with engine.begin() as c:
        c.execute(text("UPDATE runs SET status='failed', error='provider_error: boom\nsecond line sk-abcdefghijklmnop1234', "
                       "finished_at = now() WHERE id = :i"), {"i": str(wr)})
    return wf, str(agent_uuid), str(wr), str(ar)


def test_list_recent_runs_filters_and_never_returns_input_or_output(engine, tenant):
    wf, ag, wr, ar = make_runs(engine, tenant)
    out = run(tools.list_recent_runs(engine, tenant))
    assert {i["run_id"] for i in out["items"]} == {wr, ar}
    failed = next(i for i in out["items"] if i["run_id"] == wr)
    assert failed["kind"] == "workflow" and failed["status"] == "failed" and failed["name"] == "Posting"
    assert failed["error"] == "provider_error: boom" and failed["finished_at"] is not None
    blob = json.dumps(out)
    assert "SECRET-INPUT-TEXT" not in blob and "ANOTHER-INPUT" not in blob and "sk-abcdefghijklmnop1234" not in blob
    assert [i["run_id"] for i in run(tools.list_recent_runs(engine, tenant, workflow_id=wf))["items"]] == [wr]
    assert [i["run_id"] for i in run(tools.list_recent_runs(engine, tenant, agent_id=ag))["items"]] == [ar]
    assert [i["run_id"] for i in run(tools.list_recent_runs(engine, tenant, kind="workflow"))["items"]] == [wr]
    assert [i["run_id"] for i in run(tools.list_recent_runs(engine, tenant, status="failed"))["items"]] == [wr]
    assert len(run(tools.list_recent_runs(engine, tenant, limit=1))["items"]) == 1
    assert run(tools.list_recent_runs(engine, tenant, kind="nonsense"))["error"]["code"] == "invalid_argument"
    assert run(tools.list_recent_runs(engine, tenant, workflow_id="nope"))["error"]["code"] == "invalid_argument"


# ----------------------------------------------------------------------------- redaction and bounds


def test_strings_are_redacted_and_cut(engine, tenant):
    token = "sk-" + "a" * 30
    name = f"Posting {token} " + "x" * 600
    plain_workflow(engine, tenant, name)
    (item,) = run(tools.list_workflows(engine, tenant))["items"]
    assert token not in item["name"] and len(item["name"]) <= 320


def test_limits_are_bounded():
    assert platform_reads.clamp_limit(10_000) == platform_reads.MAX_LIMIT == 50
    assert platform_reads.clamp_limit(0) == 1 and platform_reads.clamp_limit(-5) == 1
    assert platform_reads.clamp_limit("many") == platform_reads.DEFAULT_LIMIT and platform_reads.clamp_limit(True) == platform_reads.DEFAULT_LIMIT


def test_list_workflows_never_returns_more_than_fifty(engine, tenant):
    for i in range(55):
        plain_workflow(engine, tenant, f"wf {i:02}", with_version=False)
    out = run(tools.list_workflows(engine, tenant, limit=1000))
    assert len(out["items"]) == 50 and out["total"] == 55


# -------------------------------------------------------------------------------- tenant isolation


def test_nothing_of_another_tenant_is_visible(engine, tenant, other):
    kb_id, source = make_source(engine, other)
    make_sync(engine, other, source)
    wid = plain_workflow(engine, other, "Theirs")
    save_and_publish_agent(engine, other, slug="theirs")
    ScheduleStore(engine).create(other, wid, name="s", cron="0 9 * * *", input={}, next_fire_at=datetime.now(timezone.utc))
    make_runs(engine, other)
    assert run(tools.list_knowledge_bases(engine, tenant))["items"] == []
    assert run(tools.get_knowledge_base(engine, tenant, kb_id))["error"]["code"] == "not_found"
    assert run(tools.list_source_syncs(engine, tenant, source.id))["error"]["code"] == "not_found"
    assert run(tools.list_workflows(engine, tenant))["items"] == []
    assert run(tools.get_workflow(engine, tenant, wid))["error"]["code"] == "not_found"
    assert run(tools.list_agents(engine, tenant))["items"] == []
    assert run(tools.list_schedules(engine, tenant))["items"] == []
    assert run(tools.list_schedules(engine, tenant, wid))["items"] == []
    assert run(tools.list_recent_runs(engine, tenant))["items"] == []
    assert run(tools.list_recent_runs(engine, tenant, workflow_id=wid))["items"] == []


# -------------------------------------------------------------------------------- registration


def test_registered_for_the_architect_only():
    registry = ToolRegistry()
    register_platform_tools(registry)
    assert set(ARCHITECT_PLATFORM_TOOL_IDS) <= registry.tool_ids() and "platform.list_capabilities" in ARCHITECT_PLATFORM_TOOL_IDS
    for tool_id in ARCHITECT_PLATFORM_TOOL_IDS:
        meta = registry.get_metadata(tool_id)
        assert not meta.get("side_effect")  # read-only: no approval
        assert tool_id.startswith(BUILTIN_ONLY_TOOL_PREFIXES)
    # not offered to a created agent or workflow (list_approved_tools), whatever EAP_ALLOWED_TOOLS says
    offered = {t.id for t in registry_tools(registry, frozenset(registry.tool_ids()))}
    assert not offered & set(ARCHITECT_PLATFORM_TOOL_IDS) and not any(i.startswith("platform.") for i in offered)
    assert "platform." in ARCHITECT_DENIED_TOOL_PREFIXES
    # the seeded Architect lists them, none gated
    spec = architect_agent_spec("test:echo")
    assert set(ARCHITECT_PLATFORM_TOOL_IDS) <= set(spec.tools) and set(ARCHITECT_TOOL_IDS) <= set(spec.tools)
    assert ungated_architect_tools(spec.tools, spec.approvals.tools) == []


def test_only_a_builtin_run_may_use_them(worker):
    rt = worker
    assert set(ARCHITECT_PLATFORM_TOOL_IDS) <= rt.allowed_tools_for(builtin=True)
    assert not set(ARCHITECT_PLATFORM_TOOL_IDS) & rt.allowed_tools_for(builtin=False)
