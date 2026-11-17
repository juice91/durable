"""The Architect's read tools, draft tools and allowlist (Task 3), over a real Postgres, no model."""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from eap_runtime import ToolRegistry
from eap_runtime._default_registry import default_registry
from eap_schemas.architect import (
    Allowlist,
    AllowlistAgent,
    AllowlistMcpServer,
    AllowlistModel,
    AllowlistTool,
    ArchitectAgentSpec,
    ArchitectWorkflowSpec,
)
from eap_schemas.models_policy import ModelPolicy
from eap_worker.architect_allowlist import approved_for_tenant
from eap_worker.architect_tools import (
    TOOL_IDS,
    list_agent_versions,
    list_created_agents,
    list_created_workflows,
    list_workflow_step_kinds,
    list_workflow_versions,
    register_architect_tools,
    save_agent,
    save_workflow,
)
from eap_worker.platform_tools import register_platform_tools
from worker_tests.conftest import APP_DB_URL


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
        for table in ("agent_versions", "agents", "workflow_versions", "workflows", "mcp_servers"):
            conn.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tenant_id})


def allowlist(**extra) -> Allowlist:
    base = dict(
        tools=(
            AllowlistTool(id="clock.now", title="Current time", description="UTC time"),
            AllowlistTool(id="tickets.create", title="Create ticket", description="New ticket", requires_approval=True),
        ),
        mcp_servers=(AllowlistMcpServer(id="docs", name="docs", tools=("search",)),),
        models=(AllowlistModel(id="test:echo", provider="test"),),
        default_model="test:echo",
    )
    base.update(extra)
    return Allowlist(**base)


def agent_spec(**over) -> ArchitectAgentSpec:
    return ArchitectAgentSpec(**{"name": "Helper", "instructions": "Help the person.", **over})


def agents_rows(engine, tenant):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT id, slug, name, draft_spec, builtin FROM agents WHERE tenant_id = :t ORDER BY slug"),
            {"t": tenant},
        ).mappings().all()


def add_agent_version(engine, tenant, agent_row_id, version, created_via):
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO agent_versions (id, agent_id, tenant_id, version, spec, spec_hash, created_at, created_by, created_via) "
            "SELECT :id, a.id, a.tenant_id, :v, a.draft_spec, 'h' || :v, now(), 'user-1', :via FROM agents a WHERE a.id = :a"
        ), {"id": str(uuid.uuid4()), "a": agent_row_id, "v": version, "via": created_via})


def test_registered_tools_are_the_architect_namespace_and_listed_by_id():
    registry = ToolRegistry()
    register_architect_tools(registry)
    assert set(TOOL_IDS) == registry.tool_ids()
    assert all(t.startswith("architect.") for t in TOOL_IDS)
    runtime_registry = default_registry()
    register_platform_tools(runtime_registry)
    assert set(TOOL_IDS) <= runtime_registry.tool_ids()


def test_list_approved_tools_excludes_architect_and_platform_tools(engine, tenant):
    registry = default_registry()
    register_platform_tools(registry)  # knowledge.search and the architect.* tools
    with engine.begin() as conn:
        for name, enabled in (("docs", True), ("old", False)):
            conn.execute(text(
                "INSERT INTO mcp_servers (id, tenant_id, name, url, enabled, created_at, updated_at) "
                "VALUES (:id, :t, :n, 'https://example.test/mcp', :e, now(), now())"
            ), {"id": str(uuid.uuid4()), "t": tenant, "n": name, "e": enabled})
    seen = {}

    async def lister(_engine, tenant_id, names):
        seen["call"] = (tenant_id, list(names))
        return {"docs": ("search", "read")}

    settings = SimpleNamespace(allowed_models=ModelPolicy.parse("test:*,litellm:qwen"), litellm_base_url=None)
    result = run(approved_for_tenant(
        engine, tenant, registry=registry, settings=settings, allowed_tools=frozenset(registry.tool_ids()),
        mcp_lister=lister,
    ))
    ids = [t.id for t in result.tools]
    assert ids == ["clock.now", "tickets.create"]  # no architect.*, no knowledge.search
    assert {t.id: t.requires_approval for t in result.tools} == {"clock.now": False, "tickets.create": True}
    assert seen["call"] == (tenant, ["docs"])  # the disabled server is not offered
    assert [(s.id, s.tools) for s in result.mcp_servers] == [("docs", ("read", "search"))]
    assert "litellm:qwen" in {m.id for m in result.models} and "test:echo" in {m.id for m in result.models}
    assert result.default_model == result.models[0].id

    # A tool outside the worker's allowed set is not offered either.
    narrowed = run(approved_for_tenant(
        engine, tenant, registry=registry, settings=settings, allowed_tools=frozenset({"clock.now"}),
        mcp_lister=lister,
    ))
    assert [t.id for t in narrowed.tools] == ["clock.now"]


def test_save_agent_refuses_tools_outside_the_allowlist(engine, tenant):
    for over in (
        {"tools": ("shell.run",)},
        {"tools": ("architect.save_agent",)},
        {"mcp_tools": ({"server": "other-tenant-server", "tool": "search"},)},
    ):
        result = run(save_agent(engine, tenant, agent_spec(**over), allowlist=allowlist()))
        assert result["error"]["code"] == "tool_not_allowed", result
    assert agents_rows(engine, tenant) == []  # nothing was written
    # A forged allowlist that offers an architect tool is still refused (the code denylist).
    forged = allowlist(tools=(AllowlistTool(id="architect.publish_agent", title="p", description="p"),))
    refused = run(save_agent(engine, tenant, agent_spec(tools=("architect.publish_agent",)), allowlist=forged))
    assert refused["error"]["code"] == "tool_not_allowed" and agents_rows(engine, tenant) == []


def test_save_agent_writes_a_draft_and_updates_it(engine, tenant):
    spec = agent_spec(id="helper", tools=("clock.now", "tickets.create"))
    first = run(save_agent(engine, tenant, spec, allowlist=allowlist(), run_id="r1"))
    assert first["id"] == "helper" and first["version"] is None and first["status"] == "draft"
    assert any("tickets.create" in w for w in first["warnings"])  # approvals were added
    (row,) = agents_rows(engine, tenant)
    assert row["slug"] == "helper" and row["builtin"] is False
    assert row["draft_spec"]["approvals"] == {"tools": ["tickets.create"]}
    assert row["draft_spec"]["model"] == {"provider": "test", "name": "echo"}
    assert str(row["id"]) != "helper"  # the row id is a UUID; the slug is what the model sees

    run(save_agent(engine, tenant, agent_spec(id="helper", instructions="New text."), allowlist=allowlist()))
    (row,) = agents_rows(engine, tenant)
    assert row["draft_spec"]["instructions"] == "New text."

    derived = run(save_agent(engine, tenant, agent_spec(name="Billing Helper!"), allowlist=allowlist()))
    assert derived["id"] == "billing-helper"
    again = run(save_agent(engine, tenant, agent_spec(name="Billing Helper!"), allowlist=allowlist()))
    assert again["id"] == "billing-helper"  # the same draft again is a retry, not a second agent
    other = run(save_agent(engine, tenant, agent_spec(name="Billing Helper!", instructions="Other."),
                           allowlist=allowlist()))
    assert other["id"] == "billing-helper-2"

    listed = run(list_created_agents(engine, tenant))["agents"]
    assert [(a["id"], a["versions"], a["latest_created_via"], a["draft"]) for a in listed] == [
        ("billing-helper", 0, None, True), ("billing-helper-2", 0, None, True), ("helper", 0, None, True),
    ]
    assert run(list_agent_versions(engine, tenant, "helper")) == {"id": "helper", "name": "Helper", "versions": []}
    assert run(list_agent_versions(engine, tenant, "nope"))["error"]["code"] == "not_found"
    assert str(row["id"]) not in json.dumps(listed)


def test_created_definitions_are_tenant_scoped(engine, tenant):
    run(save_agent(engine, tenant, agent_spec(id="mine"), allowlist=allowlist()))
    other = f"t_{uuid.uuid4().hex[:10]}"
    assert run(list_created_agents(engine, other))["agents"] == []
    assert run(list_agent_versions(engine, other, "mine"))["error"]["code"] == "not_found"
    # Saving the same slug in another tenant makes a separate agent, not an edit.
    run(save_agent(engine, other, agent_spec(id="mine", instructions="Theirs."), allowlist=allowlist()))
    try:
        (mine,) = agents_rows(engine, tenant)
        assert mine["draft_spec"]["instructions"] == "Help the person."
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM agents WHERE tenant_id = :t"), {"t": other})


def test_architect_refuses_its_own_spec_and_hand_edited_definitions(engine, tenant):
    # The built-in specs: reserved slugs and builtin rows are never edited.
    for slug in ("architect", "architect-drafter"):
        refused = run(save_agent(engine, tenant, agent_spec(id=slug), allowlist=allowlist()))
        assert refused["error"]["code"] == "reserved_id"
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO agents (id, tenant_id, name, draft_spec, archived, builtin, slug, created_at, updated_at) "
            "VALUES (:id, :t, 'Seeded', '{}'::jsonb, false, true, 'seeded-one', now(), now())"
        ), {"id": str(uuid.uuid4()), "t": tenant})
    refused = run(save_agent(engine, tenant, agent_spec(id="seeded-one"), allowlist=allowlist()))
    assert refused["error"]["code"] == "builtin_agent"
    assert [r["draft_spec"] for r in agents_rows(engine, tenant)] == [{}]

    # A definition the Architect made, later published by hand: needs a yes, then saves.
    run(save_agent(engine, tenant, agent_spec(id="helper"), allowlist=allowlist()))
    row = next(r for r in agents_rows(engine, tenant) if r["slug"] == "helper")
    add_agent_version(engine, tenant, row["id"], 1, None)
    asked = run(save_agent(engine, tenant, agent_spec(id="helper", instructions="Changed."), allowlist=allowlist()))
    assert asked["status"] == "needs_confirmation" and asked["reason"] == "edited_by_hand"
    row = next(r for r in agents_rows(engine, tenant) if r["slug"] == "helper")
    assert row["draft_spec"]["instructions"] == "Help the person."  # untouched
    ok = run(save_agent(engine, tenant, agent_spec(id="helper", instructions="Changed."), confirm=True,
                        allowlist=allowlist()))
    assert ok["status"] == "draft"
    row = next(r for r in agents_rows(engine, tenant) if r["slug"] == "helper")
    assert row["draft_spec"]["instructions"] == "Changed."

    # A version the Architect made needs no confirmation.
    add_agent_version(engine, tenant, row["id"], 2, "architect:run-9")
    fine = run(save_agent(engine, tenant, agent_spec(id="helper", instructions="Again."), allowlist=allowlist()))
    assert fine["status"] == "draft"
    listed = run(list_created_agents(engine, tenant))["agents"]
    helper = next(a for a in listed if a["id"] == "helper")
    assert (helper["versions"], helper["latest_created_via"], helper["draft"]) == (2, "architect:run-9", True)
    assert [v["version"] for v in run(list_agent_versions(engine, tenant, "helper"))["versions"]] == [2, 1]


def test_save_workflow_creates_a_draft_with_marker(engine, tenant):
    writer = run(save_agent(engine, tenant, agent_spec(id="writer"), allowlist=allowlist()))
    assert writer["id"] == "writer"
    (row,) = agents_rows(engine, tenant)
    add_agent_version(engine, tenant, row["id"], 1, "architect:run-1")
    with_agents = allowlist(agents=(AllowlistAgent(id="writer", name="Writer", row_id=str(row["id"])),))

    spec = ArchitectWorkflowSpec(name="Daily note", id="daily-note", steps=[
        {"kind": "agent", "id": "draft", "agent": "writer", "prompt": "{{input}}"},
        {"kind": "tool", "id": "stamp", "tool": "clock.now"},
    ])
    saved = run(save_workflow(engine, tenant, spec, allowlist=with_agents, run_id="r1"))
    assert saved == {"id": "daily-note", "version": None, "status": "draft", "warnings": []}
    with engine.connect() as conn:
        wf = conn.execute(
            text("SELECT slug, name, draft_spec, archived FROM workflows WHERE tenant_id = :t"), {"t": tenant}
        ).mappings().one()
    assert wf["slug"] == "daily-note" and wf["archived"] is False  # the slug is the Architect's marker
    assert wf["draft_spec"]["start"] == "draft" and wf["draft_spec"]["steps"][0]["agent_id"] == str(row["id"])
    listed = run(list_created_workflows(engine, tenant))["workflows"]
    assert listed == [{"id": "daily-note", "name": "Daily note", "versions": 0, "latest_created_via": None, "draft": True}]
    assert run(list_workflow_versions(engine, tenant, "daily-note"))["versions"] == []

    # An agent the allowlist does not offer, an unknown placeholder and a denied tool are errors, not rows.
    bad = ArchitectWorkflowSpec(name="Bad", id="bad", steps=[{"kind": "agent", "id": "s", "agent": "ghost", "prompt": "x"}])
    assert run(save_workflow(engine, tenant, bad, allowlist=with_agents))["error"]["code"] == "agent_not_allowed"
    bad = ArchitectWorkflowSpec(name="Bad", id="bad", steps=[{"kind": "agent", "id": "s", "agent": "writer", "prompt": "{{nope}}"}])
    assert run(save_workflow(engine, tenant, bad, allowlist=with_agents))["error"]["code"] == "unknown_placeholder"
    bad = ArchitectWorkflowSpec(name="Bad", id="bad", steps=[{"kind": "tool", "id": "s", "tool": "architect.save_agent"}])
    assert run(save_workflow(engine, tenant, bad, allowlist=with_agents))["error"]["code"] == "tool_not_allowed"
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM workflows WHERE tenant_id = :t"), {"t": tenant}).scalar_one() == 1

    # A hand-published version needs a yes first.
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO workflow_versions (id, workflow_id, tenant_id, version, spec, spec_hash, created_at, created_by, created_via) "
            "SELECT :id, w.id, w.tenant_id, 1, w.draft_spec, 'h', now(), 'user-1', NULL FROM workflows w WHERE w.tenant_id = :t"
        ), {"id": str(uuid.uuid4()), "t": tenant})
    edited = ArchitectWorkflowSpec(name="Daily note v2", id="daily-note", steps=[{"kind": "tool", "id": "stamp", "tool": "clock.now"}])
    asked = run(save_workflow(engine, tenant, edited, allowlist=with_agents))
    assert asked["status"] == "needs_confirmation" and asked["reason"] == "edited_by_hand"
    assert run(save_workflow(engine, tenant, edited, confirm=True, allowlist=with_agents))["status"] == "draft"


def test_workflow_step_kinds_lists_the_supported_kinds_only():
    result = run(list_workflow_step_kinds())
    assert [k["kind"] for k in result["step_kinds"]] == ["agent", "parallel", "tool"]
    assert "{{previous}}" in result["placeholders"]
