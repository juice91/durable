"""The Architect's read tools, draft tools and allowlist (Task 3), over a real Postgres, no model."""

from __future__ import annotations

import asyncio
import json
import re
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
    assert ids == ["clock.now", "clock.sleep", "feed.parse", "html.extract", "http.request", "json.query", "secrets.request", "text.check", "tickets.create", "workflow.return"]  # no architect.*, no knowledge.search
    assert {t.id: t.requires_approval for t in result.tools} == {
        "clock.now": False, "clock.sleep": False, "feed.parse": False, "html.extract": False, "http.request": True, "json.query": False, "secrets.request": True, "text.check": False, "tickets.create": True,
        "workflow.return": False}
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
    assert all(a["uuid"] for a in listed)
    assert [(a["id"], a["versions"], a["latest_created_via"], a["draft"]) for a in listed] == [
        ("billing-helper", 0, None, True), ("billing-helper-2", 0, None, True), ("helper", 0, None, True),
    ]
    versions = run(list_agent_versions(engine, tenant, "helper"))
    assert versions == {"id": "helper", "uuid": versions["uuid"], "name": "Helper", "versions": []}
    assert run(list_agent_versions(engine, tenant, "nope"))["error"]["code"] == "not_found"
    assert {a["uuid"] for a in listed} == {str(r["id"]) for r in agents_rows(engine, tenant)}


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
    assert saved == {"id": "daily-note", "version": None, "status": "draft", "revision": 1, "warnings": []}
    with engine.connect() as conn:
        wf = conn.execute(
            text("SELECT slug, name, draft_spec, archived FROM workflows WHERE tenant_id = :t"), {"t": tenant}
        ).mappings().one()
    assert wf["slug"] == "daily-note" and wf["archived"] is False  # the slug is the Architect's marker
    assert wf["draft_spec"]["start"] == "draft" and wf["draft_spec"]["steps"][0]["agent_id"] == str(row["id"])
    listed = run(list_created_workflows(engine, tenant))["workflows"]
    assert listed == [{"id": "daily-note", "uuid": listed[0]["uuid"], "name": "Daily note", "versions": 0,
                       "latest_created_via": None, "draft": True, "published_version": None,
                       "draft_pending": False, "archived": False}]
    assert uuid.UUID(listed[0]["uuid"])
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
    assert [k["kind"] for k in result["step_kinds"]] == ["agent", "parallel", "tool", "branch"]
    assert "{{previous}}" in result["placeholders"]


# ----------------------------------------------------------- a spec that arrives as a JSON string

# The shape a thinking model sent in a real run: the whole spec JSON-encoded in the `spec` argument.
STRING_WORKFLOW_SPEC = json.dumps({
    "name": "Daily brief",
    "id": "daily-brief",
    "steps": [{"kind": "agent", "id": "write", "agent": "writer", "prompt": "Brief {{input}}"}],
}, ensure_ascii=False)


def test_save_agent_accepts_the_spec_as_a_json_string(engine, tenant):
    raw = json.dumps({"name": "Helper", "id": "helper", "instructions": "Help the person."})
    result = run(save_agent(engine, tenant, raw, allowlist=allowlist()))
    assert result["id"] == "helper" and result["status"] == "draft", result
    assert [r["slug"] for r in agents_rows(engine, tenant)] == ["helper"]


def test_save_workflow_accepts_the_spec_as_a_json_string(engine, tenant):
    agents = allowlist(agents=(AllowlistAgent(id="writer", name="Writer", row_id=str(uuid.uuid4())),))
    result = run(save_workflow(engine, tenant, STRING_WORKFLOW_SPEC, allowlist=agents))
    assert result["id"] == "daily-brief" and result["status"] == "draft", result


@pytest.mark.parametrize("tool", [save_agent, save_workflow])
@pytest.mark.parametrize("bad", ["not json at all", "[1, 2]", '"just a string"', '{"name": "x"}', "{broken"])
def test_a_string_spec_that_is_not_a_valid_object_is_an_error_object(engine, tenant, tool, bad):
    result = run(tool(engine, tenant, bad, allowlist=allowlist()))
    assert result["error"]["code"] == "invalid_spec" and result["error"]["message"], result
    assert "ValidationError" not in result["error"]["message"]


# ------------------------------------------------- ranked default model, ids from the name


def test_the_allowlist_lists_models_best_first_and_the_default_is_the_ranked_pick():
    from eap_worker.architect_allowlist import models_from_settings

    settings = SimpleNamespace(
        allowed_models=ModelPolicy.parse("litellm:*"), litellm_base_url="http://gw", litellm_api_key=None)
    listed = ["CausalLM-34b-beta-4bit", "bge-reranker", "qwen3.8-thinking", "qwen3.8"]
    models = models_from_settings(settings, gateway=lambda _url, _key: listed)
    assert [m.id for m in models][0] == "litellm:qwen3.8"
    assert [m.id for m in models][-1] == "litellm:bge-reranker"


def test_anthropic_wildcard_lists_what_the_key_can_use_and_falls_back_when_listing_fails():
    from eap_worker.architect_allowlist import models_from_settings

    settings = SimpleNamespace(allowed_models=ModelPolicy.parse("litellm:*,anthropic:*"), litellm_base_url="http://gw",
                               litellm_api_key=None)
    env = {"ANTHROPIC_API_KEY": "k"}
    got = models_from_settings(settings, gateway=lambda _u, _k: ["qwen3.8"], environ=env,
                               anthropic=lambda _key: ["claude-sonnet-5-5", "claude-x"])
    ids = [m.id for m in got]
    assert {"anthropic:claude-sonnet-5-5", "anthropic:claude-x", "litellm:qwen3.8"} == set(ids)
    assert ids[0] == "anthropic:claude-sonnet-5-5"  # the ranked pick is a general chat model
    assert not [m for m in models_from_settings(settings, gateway=lambda _u, _k: [], environ={}) if m.provider == "anthropic"]
    exact = SimpleNamespace(allowed_models=ModelPolicy.parse("anthropic:claude-sonnet-5-5"), litellm_base_url=None)
    assert [m.id for m in models_from_settings(exact, environ=env)] == ["anthropic:claude-sonnet-5-5"]


def test_anthropic_listing_failure_gives_the_small_fallback_set(monkeypatch):
    import urllib.request

    from eap_worker.architect_allowlist import _anthropic_models

    def boom(*_a, **_k):
        raise TimeoutError("slow")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert _anthropic_models("k") == ["claude-haiku-5-5", "claude-opus-5-5", "claude-sonnet-5-5"]


def test_save_agent_without_a_model_uses_the_ranked_model_not_the_first_listed(engine, tenant):
    models = tuple(AllowlistModel(id=f"litellm:{n}", provider="litellm")
                   for n in ("CausalLM-34b-beta-4bit", "qwen3.8-thinking", "qwen3.8"))
    result = run(save_agent(engine, tenant, agent_spec(id="bat"), allowlist=allowlist(models=models, default_model=models[0].id)))
    assert result["status"] == "draft", result
    assert agents_rows(engine, tenant)[0]["draft_spec"]["model"] == {"provider": "litellm", "name": "qwen3.8"}


@pytest.mark.parametrize("placeholder", ["definition", "agent", "workflow", "spec", "draft", "new", "untitled", "test"])
def test_a_placeholder_id_is_replaced_by_one_from_the_name(engine, tenant, placeholder):
    result = run(save_agent(engine, tenant, agent_spec(name="Weekly Reporter", id=placeholder), allowlist=allowlist()))
    assert result["id"] == "weekly-reporter", result
    missing = run(save_agent(engine, tenant, agent_spec(name="Weekly Reporter"), allowlist=allowlist()))
    assert missing["id"] == "weekly-reporter"  # the same draft again, not a second agent
    assert [r["slug"] for r in agents_rows(engine, tenant)] == ["weekly-reporter"]


def test_a_cjk_name_gets_a_short_hashed_id(engine, tenant):
    spec = agent_spec(name="球棒（棒球角色帳號）", id="definition")
    first = run(save_agent(engine, tenant, spec, allowlist=allowlist()))
    again = run(save_agent(engine, tenant, spec, allowlist=allowlist()))
    assert re.fullmatch(r"agent-[0-9a-f]{4}", first["id"]), first
    assert again["id"] == first["id"] and len(agents_rows(engine, tenant)) == 1


def test_a_workflow_with_a_placeholder_id_gets_its_id_from_the_name(engine, tenant):
    agents = allowlist(agents=(AllowlistAgent(id="writer", name="Writer", row_id=str(uuid.uuid4())),))
    spec = {"name": "Daily Brief", "id": "workflow", "steps": [{"kind": "agent", "id": "w", "agent": "writer", "prompt": "{{input}}"}]}
    assert run(save_workflow(engine, tenant, spec, allowlist=agents))["id"] == "daily-brief"


# ----------------------------------------------------- composing a collect, write, check and post workflow

FIVE = ("http.request", "workflow.return", "feed.parse", "json.query", "text.check")


def test_step_kind_guidance_covers_inputs_conditions_structured_output_and_returns():
    text_ = json.dumps(run(list_workflow_step_kinds()), ensure_ascii=False)
    for needle in ("$template", "{{ steps.<id>.output.<field> }}", "$ref", "exactly one placeholder", "keeps the value's type", "only as a whole value",
                   "branch", '"when"', "steps.fetch.output.ok", "output_schema", "steps.<agent step id>.output.picks",
                   "workflow.return", "{error: {code", "fails the run", "input_schema", "dry"):
        assert needle in text_, needle
    for tool in FIVE:
        assert tool in text_ or tool == "workflow.return", tool
    for tool in ("http.request", "feed.parse", "json.query", "text.check"):
        assert tool in run(list_workflow_step_kinds())["pattern"]


def test_list_approved_tools_gives_the_five_general_tools_a_schema_and_an_example(engine, tenant):
    registry = default_registry()
    register_platform_tools(registry)
    settings = SimpleNamespace(allowed_models=ModelPolicy.parse("test:*"), litellm_base_url=None)

    async def lister(_engine, tenant_id, names):
        return {}

    result = run(approved_for_tenant(
        engine, tenant, registry=registry, settings=settings, allowed_tools=frozenset(registry.tool_ids()), mcp_lister=lister))
    by_id = {t.id: t for t in result.tools}
    assert set(FIVE) <= set(by_id)
    for tool_id in FIVE:
        tool = by_id[tool_id]
        assert tool.input_schema and tool.input_schema["type"] == "object" and tool.example, tool_id
        assert len(tool.description) > 40
    assert "feeds" in by_id["feed.parse"].input_schema["properties"] and by_id["feed.parse"].example["feeds"][0]["label"]
    assert "[?id==" in by_id["json.query"].example["expression"]
    assert {"min_graphemes", "forbid_urls", "rumour_terms", "max_verbatim_run"} <= set(by_id["text.check"].example["rules"])
    assert "secret:" in json.dumps(by_id["http.request"].example) and "drop_response_paths" in by_id["http.request"].input_schema["properties"]
    assert "ended" in json.dumps(by_id["workflow.return"].example)
    assert "{error" in by_id["http.request"].description and "text" in by_id["http.request"].description


def test_save_workflow_accepts_a_collect_shape_check_gate_post_draft_using_template_inputs(engine, tenant):
    picker_id, writer_id = str(uuid.uuid4()), str(uuid.uuid4())
    tools = tuple(AllowlistTool(id=t, title=t, description=t * 5) for t in (*FIVE, "clock.now"))
    wide = allowlist(tools=tools, agents=(
        AllowlistAgent(id="picker", name="Picker", row_id=picker_id), AllowlistAgent(id="writer", name="Writer", row_id=writer_id)))
    ret = lambda i, value: {"kind": "tool", "id": i, "tool": "workflow.return", "params": {"value": value}}  # noqa: E731
    gate = lambda i, ref, nxt, default: {"kind": "branch", "id": i, "default": default, "cases": [  # noqa: E731
        {"when": {"op": "eq", "left": {"$ref": ref}, "right": True}, "next": nxt}]}
    steps = [
        {"kind": "tool", "id": "now", "tool": "clock.now"},
        {"kind": "tool", "id": "feed", "tool": "http.request", "params": {"method": "GET", "url": "https://feeds.example.com/a.xml"}},
        gate("feed_gate", "steps.feed.output.ok", "candidates", "feed_failed"),
        ret("feed_failed", {"$template": {"error": {"code": "feed_fetch_failed", "message": "status {{steps.feed.output.status}}"}}}),
        {"kind": "tool", "id": "candidates", "tool": "feed.parse", "params": {
            "feeds": {"$template": [{"text": "{{steps.feed.output.text}}", "label": "Source A", "include_terms": ["keyword"]}]},
            "max_age_hours": 24, "now": {"$ref": "steps.now.output"}}},
        {"kind": "branch", "id": "none_gate", "default": "pick", "cases": [
            {"when": {"op": "eq", "left": {"$ref": "steps.candidates.output.count"}, "right": 0}, "next": "nothing"}]},
        ret("nothing", {"ended": "nothing new"}),
        {"kind": "agent", "id": "pick", "agent": "picker", "input": {"prompt": {"$template": "Task: pick\n{{steps.candidates.output.listing}}"}}},
        {"kind": "tool", "id": "selected", "tool": "json.query", "params": {
            "data": {"$template": {"items": "{{steps.candidates.output.items}}"}},
            "expression": {"$template": "(items[?id=='{{steps.pick.output.picks.0}}'] | [0]) || items[0]"}}},
        {"kind": "agent", "id": "write", "agent": "writer", "input": {"prompt": {"$template": "Task: write\n{{steps.selected.output.result.title}}"}}},
        {"kind": "tool", "id": "check", "tool": "text.check", "params": {
            "text": {"$ref": "steps.write.output.post"},
            "rules": {"min_graphemes": 20, "max_graphemes": 300, "forbid_urls": True, "min_han_ratio": 0.5}}},
        gate("check_gate", "steps.check.output.ok", "dry_gate", "check_failed"),
        ret("check_failed", {"$template": {"ended": "check failed: {{steps.check.output.reason}}"}}),
        {"kind": "branch", "id": "dry_gate", "default": "post", "cases": [
            {"when": {"op": "eq", "left": {"$ref": "input.dry"}, "right": True}, "next": "dry_done"}]},
        ret("dry_done", {"$template": {"ended": "dry", "text": "{{steps.check.output.text}}"}}),
        {"kind": "tool", "id": "post", "tool": "http.request", "params": {
            "method": "POST", "url": "https://api.example.com/posts",
            "headers": {"$template": {"Authorization": "Bearer secret:api-token"}},
            "json": {"$template": {"text": "{{steps.check.output.text}}", "createdAt": "{{steps.now.output}}", "langs": ["zh-Hant"]}}}},
        ret("done", {"$template": {"ended": "posted", "status": "{{steps.post.output.status}}"}}),
    ]
    spec = {"name": "Collect and post", "id": "collect-and-post", "steps": steps,
            "input_schema": {"type": "object", "properties": {"dry": {"type": "boolean"}}, "required": ["dry"],
                             "additionalProperties": False}}
    saved = run(save_workflow(engine, tenant, spec, allowlist=wide))
    assert saved == {"id": "collect-and-post", "version": None, "status": "draft", "revision": 1, "warnings": []}, saved
    with engine.connect() as conn:
        draft = conn.execute(text("SELECT draft_spec FROM workflows WHERE tenant_id = :t"), {"t": tenant}).scalar_one()
    by_id = {s["id"]: s for s in draft["steps"]}
    assert draft["start"] == "now" and by_id["pick"]["agent_id"] == picker_id
    assert by_id["feed_failed"].get("next") is None and by_id["done"].get("next") is None  # workflow.return ends a path
    assert by_id["post"]["next"] == "done" and by_id["pick"]["next"] == "selected"
    assert by_id["post"]["input"]["json"]["$template"]["langs"] == ["zh-Hant"]
    assert by_id["feed_gate"]["cases"][0]["next"] == "candidates" and by_id["feed_gate"]["default"] == "feed_failed"


def test_save_workflow_refuses_what_the_format_cannot_express(engine, tenant):
    wide = allowlist(tools=tuple(AllowlistTool(id=t, title=t, description=t * 5) for t in FIVE))
    bad_next = [{"kind": "tool", "id": "a", "tool": "json.query", "params": {"data": 1, "expression": "@"}, "next": "ghost"}]
    assert run(save_workflow(engine, tenant, {"name": "X", "steps": bad_next}, allowlist=wide))["error"]["code"] == "unknown_step"
    both = [{"kind": "agent", "id": "a", "agent": "writer", "prompt": "p", "input": {"prompt": "q"}}]
    assert run(save_workflow(engine, tenant, {"name": "X", "steps": both}, allowlist=wide))["error"]["code"] == "invalid_spec"
    mixed = [{"kind": "tool", "id": "a", "tool": "workflow.return", "params": {"value": "x {{input}}"}}]
    assert run(save_workflow(engine, tenant, {"name": "X", "steps": mixed}, allowlist=wide))["error"]["code"] == "placeholder_not_whole_value"


def test_save_agent_takes_an_output_schema(engine, tenant):
    schema = {"type": "object", "properties": {"picks": {"type": "array", "items": {"type": "string"}, "maxItems": 3}},
              "required": ["picks"], "additionalProperties": False}
    saved = run(save_agent(engine, tenant, agent_spec(id="picker", output_schema=schema), allowlist=allowlist()))
    assert saved["status"] == "draft", saved
    row = next(r for r in agents_rows(engine, tenant) if r["slug"] == "picker")
    assert row["draft_spec"]["output"]["json_schema"] == schema
    bad = run(save_agent(engine, tenant, agent_spec(id="picker2", output_schema={"type": "string"}), allowlist=allowlist()))
    assert bad["error"]["code"] == "agent_invalid"


def _approved(engine, tenant):
    registry = default_registry()
    register_platform_tools(registry)
    settings = SimpleNamespace(allowed_models=ModelPolicy.parse("test:*"), litellm_base_url=None)

    async def lister(_engine, tenant_id, names):
        return {}

    result = run(approved_for_tenant(
        engine, tenant, registry=registry, settings=settings, allowed_tools=frozenset(registry.tool_ids()), mcp_lister=lister))
    return {t.id: t for t in result.tools}


def test_list_approved_tools_gives_every_tool_its_result_shape(engine, tenant):
    by_id = _approved(engine, tenant)
    assert by_id["clock.now"].result_example == "2026-11-18T09:30:00+00:00"  # a bare RFC 3339 string, no field
    assert by_id["clock.now"].result_schema == {"type": "string", "format": "date-time"}
    assert {"status", "ok", "headers", "json", "elapsed_ms"} <= set(by_id["http.request"].result_example)
    assert {"items", "listing", "count", "dropped"} <= set(by_id["feed.parse"].result_example)
    assert by_id["json.query"].result_example == {"result": {"id": "c2"}}
    assert {"title", "text", "published_at", "og_image"} <= set(by_id["html.extract"].result_example)
    assert by_id["html.extract"].result_schema and by_id["html.extract"].example["mode"] == "links"
    assert {"ok", "text", "reason", "graphemes"} <= set(by_id["text.check"].result_example)
    assert by_id["secrets.request"].result_example == {"ref": "secret:api-token", "stored": True}
    assert {"ticket_id", "created"} <= set(by_id["tickets.create"].result_example)
    assert by_id["workflow.return"].result_example == {"ended": "nothing new"}
    from eap_schemas.tools import TOOL_DECLARATIONS
    missing = [t for t, d in TOOL_DECLARATIONS.items() if d.result_example is None]
    assert missing == []


def test_approval_says_where_it_applies_and_requires_approval_stays(engine, tenant):
    by_id = _approved(engine, tenant)
    dumped = by_id["http.request"].model_dump(mode="json")
    assert dumped["approval"] == {"agent_runs": True, "workflow_steps": False}
    assert dumped["requires_approval"] is True  # the Studio still reads it
    assert by_id["clock.now"].approval == {"agent_runs": False, "workflow_steps": False}
    assert "agent-initiated calls only" in by_id["http.request"].approval_note


def test_condition_and_check_semantics_are_spelled_out():
    kinds = run(list_workflow_step_kinds())
    text = json.dumps(kinds)
    assert "exists is true when the path exists and is not null" in text
    assert "ne compares values" in text and "path that does not exist makes the comparison unknown" in text
    assert "steps.<id>.output" in json.dumps(kinds["step_output"])
    from eap_schemas.tools import TOOL_DECLARATIONS
    assert "fails when the longest shared run is >= max_verbatim_run" in TOOL_DECLARATIONS["text.check"].description


def test_a_save_by_uuid_updates_the_existing_agent_and_workflow_instead_of_creating_one(engine, tenant):
    first = run(save_agent(engine, tenant, agent_spec(id="helper"), allowlist=allowlist()))
    (row,) = agents_rows(engine, tenant)
    (listed,) = run(list_created_agents(engine, tenant))["agents"]
    assert (listed["id"], listed["uuid"]) == ("helper", str(row["id"]))
    for spelled in (str(row["id"]), str(row["id"]).upper()):
        done = run(save_agent(engine, tenant, agent_spec(name="Helper 2", id=spelled, instructions=f"Changed {spelled[:2]}."),
                              allowlist=allowlist()))
        assert done["id"] == "helper" and done["status"] == "draft", done  # the slug comes back
    (row,) = agents_rows(engine, tenant)  # still one agent
    assert row["name"] == "Helper 2" and row["draft_spec"]["instructions"].startswith("Changed")
    missing = run(save_agent(engine, tenant, agent_spec(id=str(uuid.uuid4())), allowlist=allowlist()))
    assert missing["error"]["code"] == "not_found"
    other = f"t_{uuid.uuid4().hex[:10]}"
    assert run(save_agent(engine, other, agent_spec(id=str(row["id"])), allowlist=allowlist()))["error"]["code"] == "not_found"
    assert len(agents_rows(engine, tenant)) == 1 and first["id"] == "helper"

    wf = ArchitectWorkflowSpec(name="Note", id="note", steps=[{"kind": "tool", "id": "t", "tool": "clock.now"}])
    run(save_workflow(engine, tenant, wf, allowlist=allowlist()))
    (w,) = run(list_created_workflows(engine, tenant))["workflows"]
    again = ArchitectWorkflowSpec(name="Note 2", id=w["uuid"], steps=[{"kind": "tool", "id": "t2", "tool": "clock.now"}])
    done = run(save_workflow(engine, tenant, again, allowlist=allowlist()))
    assert done["id"] == "note" and done["status"] == "draft"
    (w2,) = run(list_created_workflows(engine, tenant))["workflows"]
    assert w2["uuid"] == w["uuid"] and w2["name"] == "Note 2"


def test_publish_and_other_gated_tools_also_take_the_uuid(engine, tenant):
    run(save_agent(engine, tenant, agent_spec(id="helper"), allowlist=allowlist()))
    (row,) = agents_rows(engine, tenant)
    assert run(list_agent_versions(engine, tenant, str(row["id"])))["id"] == "helper"


@pytest.mark.parametrize("placeholder", ["definition", "id", "agent", "workflow", "<id>"])
def test_the_placeholder_ids_of_the_page_are_replaced_by_one_from_the_name(placeholder):
    spec = ArchitectAgentSpec(name="Weekly Reporter", id=placeholder, instructions="x")
    from eap_schemas.architect import is_generic_id
    assert spec.id is None and is_generic_id(placeholder)
    wf = ArchitectWorkflowSpec(name="W", id=placeholder, steps=[{"kind": "tool", "id": "t", "tool": "clock.now"}])
    assert wf.id is None


def test_a_workflow_draft_names_a_draft_agent_and_publishing_needs_the_agent_published(engine, tenant):
    from eap_worker.architect_tools import publish_workflow_sync

    registry = default_registry()
    register_platform_tools(registry)
    settings = SimpleNamespace(allowed_models=ModelPolicy.parse("test:*"), litellm_base_url=None)

    async def lister(_engine, tenant_id, names):
        return {}

    def approved():
        return run(approved_for_tenant(
            engine, tenant, registry=registry, settings=settings, allowed_tools=frozenset(registry.tool_ids()),
            mcp_lister=lister))

    run(save_agent(engine, tenant, agent_spec(id="writer", model="test:echo"), allowlist=approved()))
    (row,) = agents_rows(engine, tenant)
    assert [a.id for a in approved().agents] == ["writer"]  # a draft agent is offered
    spec = ArchitectWorkflowSpec(name="Note", id="note", steps=[{"kind": "agent", "id": "w", "agent": "writer", "prompt": "{{input}}"}])
    assert run(save_workflow(engine, tenant, spec, allowlist=approved()))["status"] == "draft"

    refused = publish_workflow_sync(engine, tenant, "note")
    assert refused["error"]["code"] == "agent_not_published", refused
    assert "writer" in refused["error"]["message"] and refused["error"]["agents"][0]["id"] == "writer"
    add_agent_version(engine, tenant, row["id"], 1, "architect:run-1")
    done = publish_workflow_sync(engine, tenant, "note")
    assert done["status"] == "published" and done["version"] == 1, done
    # an archived agent leaves the list again
    with engine.begin() as conn:
        conn.execute(text("UPDATE agents SET archived = true WHERE tenant_id = :t"), {"t": tenant})
    assert approved().agents == ()
