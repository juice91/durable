"""A run for a person who may only read (a viewer chatting with the Architect): the tool list holds read tools only, a
write tool called anyway is refused, and the instructions say so. The real worker, DBOS and Postgres; the policy engine is
the real `OpenFgaPolicy` over the API suite's model-evaluating OpenFGA."""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "api"))

from api_tests.iam_model_engine import ModelServer  # noqa: E402
from eap_run_store import RunActor  # noqa: E402
from eap_schemas.architect import (  # noqa: E402
    ARCHITECT_PLATFORM_TOOL_IDS,
    ARCHITECT_READ_TOOL_IDS,
    ARCHITECT_TOOL_IDS,
    CONTROL_LIST_TOOL_ID,
)
from eap_schemas.architect_seed import architect_agent_spec  # noqa: E402
from eap_worker import architect_tools, grants, read_only  # noqa: E402
from eap_worker import control_bridge as bridge  # noqa: E402
from eap_worker.workflow import _toolset_for_run  # noqa: E402
from worker_tests.conftest import APP_VERSION, enqueue_run, event_rows, wait_terminal  # noqa: E402
from worker_tests.test_architect_system_context import builtin_agent  # noqa: E402

WRITE_TOOLS = tuple(t for t in ARCHITECT_TOOL_IDS if t not in ARCHITECT_READ_TOOL_IDS)


@pytest.fixture(autouse=True)
def policy():
    server = ModelServer()
    engine = server.policy()
    grants.set_policy_engine(engine)
    yield engine
    grants.set_policy_engine(None)


@pytest.fixture(autouse=True)
def _no_control_plane(monkeypatch):
    monkeypatch.delenv(bridge.API_URL_ENV, raising=False)
    bridge.reset_cache()
    yield
    bridge.reset_cache()


def person(role: str) -> RunActor:
    return RunActor("user", f"u-{role}", on_behalf_of=f"u-{role}", roles=(role,))


def run_for(worker, client, tenant: str, who: RunActor | None, spec: dict[str, Any], input_text: str = "hello",
            agent_id: uuid.UUID | None = None):
    agent_id = agent_id or builtin_agent(worker, tenant)
    run_id = uuid.uuid4()
    worker.run_store.create_run(tenant, agent_id, 1, input_text, run_id, actor=who)
    enqueue_run(client, worker.run_store, tenant_id=tenant, spec=spec, input_text=input_text, app_version=APP_VERSION,
                run_id=run_id, agent_id=agent_id, create_row=False)
    return run_id, wait_terminal(worker.run_store, tenant, run_id)


def scripted_spec(model: str, **over: Any) -> dict[str, Any]:
    base = architect_agent_spec(f"test:{model}").model_dump(mode="json")
    return {**base, "memory": None, **over}


# ------------------------------------------------------------------------------------------ the decision


def test_the_read_tools_are_exactly_the_ones_that_change_nothing():
    assert set(ARCHITECT_READ_TOOL_IDS) <= {*ARCHITECT_TOOL_IDS, *ARCHITECT_PLATFORM_TOOL_IDS}
    assert set(ARCHITECT_PLATFORM_TOOL_IDS) <= set(ARCHITECT_READ_TOOL_IDS)
    assert {t.split(".", 1)[1].split("_")[0] for t in ARCHITECT_READ_TOOL_IDS if t.startswith("architect.")} == {"list", "get"}
    assert {"architect.save_agent", "architect.save_workflow", "architect.publish_agent"} <= set(WRITE_TOOLS)


@pytest.mark.parametrize("who,expected", [
    (person("viewer"), True),
    (person("editor"), False),
    (person("admin"), False),
    (RunActor("user", "stranger", on_behalf_of="stranger", roles=()), True),
    (RunActor("api_key", "key-1", on_behalf_of="u-viewer"), False),  # keys and services: unchanged
    (RunActor("service", "schedule-1"), False),
    (None, False),
])
def test_only_a_person_without_write_is_read_only(worker, tenant, who, expected):
    run_id = uuid.uuid4()
    worker.run_store.create_run(tenant, uuid.uuid4(), 1, "hi", run_id, actor=who)
    assert asyncio.run(read_only.run_is_read_only(worker, tenant, str(run_id))) is expected


def test_a_policy_engine_that_cannot_answer_makes_a_persons_run_read_only(worker, tenant, policy):
    import httpx2

    def down(request):
        raise httpx2.ConnectError("down")

    policy.engine.client._http = httpx2.AsyncClient(transport=httpx2.MockTransport(down))
    run_id = uuid.uuid4()
    worker.run_store.create_run(tenant, uuid.uuid4(), 1, "hi", run_id, actor=person("editor"))
    assert asyncio.run(read_only.run_is_read_only(worker, tenant, str(run_id))) is True


# ------------------------------------------------------------------------------------------ the tool list


def _listed(worker, tenant: str, *, readonly: bool, spec_over: dict[str, Any], monkeypatch) -> set[str]:
    spec = architect_agent_spec("test:echo").model_copy(update=spec_over)
    deps = replace(worker.deps_for(str(uuid.uuid4()), tenant, spec, builtin=True), read_only=readonly)
    ctx = RunContext(deps=deps, model=TestModel(), usage=RunUsage())

    async def catalog(*_a: Any, **_k: Any):
        return [bridge.ControlTool("scorer.list", "List.", bridge.READ, {"type": "object"}),
                bridge.ControlTool("scorer.create", "Create.", bridge.MANAGE, {"type": "object"})]

    monkeypatch.setattr(bridge, "fetch_catalog", catalog)

    async def go():
        toolset = await _toolset_for_run(ctx)
        return set(await toolset.get_tools(ctx))

    return asyncio.run(go())


def test_a_read_only_architect_is_offered_read_tools_only(worker, tenant, monkeypatch):
    from eap_schemas.agent_tools import AgentToolRef

    extra = {
        "tools": (*ARCHITECT_TOOL_IDS, *ARCHITECT_PLATFORM_TOOL_IDS, "secrets.request", "clock.now"),
        "agent_tools": (AgentToolRef(agent_id=str(uuid.uuid4()), name="ask_helper", description="Ask."),),
    }
    names = _listed(worker, tenant, readonly=True, spec_over=extra, monkeypatch=monkeypatch)
    expected = {t.replace(".", "__") for t in ARCHITECT_READ_TOOL_IDS} | {"control__scorer_list"}
    assert names == expected


def test_a_writer_keeps_every_tool(worker, tenant, monkeypatch):
    names = _listed(worker, tenant, readonly=False, spec_over={}, monkeypatch=monkeypatch)
    assert {t.replace(".", "__") for t in ARCHITECT_TOOL_IDS} <= names
    assert {"control__scorer_list", "control__scorer_create"} <= names


def test_the_capability_listing_of_a_read_only_run_names_read_capabilities_only(monkeypatch):
    async def catalog(*_a: Any, **_k: Any):
        return [bridge.ControlTool("scorer.list", "List.", bridge.READ, {}),
                bridge.ControlTool("scorer.create", "Create.", bridge.MANAGE, {})]

    monkeypatch.setattr(bridge, "fetch_catalog", catalog)
    ctx = SimpleNamespace(deps=SimpleNamespace(tenant_id="t", run_id=uuid.uuid4(), read_only=True))
    found = asyncio.run(bridge.list_capabilities(ctx))
    assert [c["name"] for c in found["capabilities"]] == ["scorer.list"]


# ------------------------------------------------------------------------------- refused if called anyway


def _readonly_ctx(tenant: str = "t") -> Any:
    return SimpleNamespace(deps=SimpleNamespace(tenant_id=tenant, run_id=uuid.uuid4(), read_only=True), tool_call_id="c1")


def test_the_write_tool_bodies_refuse_a_read_only_run():
    ctx = _readonly_ctx()
    refused = [
        asyncio.run(architect_tools._save_agent(ctx, {"name": "x"})),
        asyncio.run(architect_tools._save_workflow(ctx, {"name": "x"})),
        asyncio.run(architect_tools._publish_agent(ctx, "x")),
        asyncio.run(architect_tools._publish_workflow(ctx, "x")),
        asyncio.run(architect_tools._set_workflow_schedule(ctx, "x", "create")),
        asyncio.run(architect_tools._start_eval_run(ctx, "x")),
    ]
    assert [r["error"]["code"] for r in refused] == ["read_only"] * len(refused)


def test_a_manage_control_call_is_refused_in_the_worker(monkeypatch):
    sent: list[str] = []

    async def call(tenant_id, run_id, name, args):
        sent.append(name)
        return {}

    monkeypatch.setattr(bridge, "call_control", call)
    toolset = bridge.ControlToolset()
    ctx = _readonly_ctx()
    tool = toolset._tool(bridge.ToolDefinition(name="control__scorer_create", metadata={"control": "scorer.create", "control_kind": bridge.MANAGE}))
    result = asyncio.run(toolset.call_tool(tool.tool_def.name, {}, ctx, tool))
    assert result["error"]["code"] == "read_only" and sent == []


@pytest.mark.parametrize("tool", ["architect__save_agent", "architect__publish_agent"])
def test_a_forced_write_call_in_a_viewers_run_changes_nothing(worker, client, tenant, tool):
    agent = builtin_agent(worker, tenant)
    prompt = json.dumps({"tool": tool, "args": {"spec": {"name": "Sneaky"}, "agent_id": "sneaky"}})
    run_id, row = run_for(worker, client, tenant, person("viewer"), scripted_spec("approval"), prompt, agent)
    assert row["status"] == "completed", row["error"]
    assert f"Unknown tool name: '{tool}'" in row["output"]
    with worker.engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM agents WHERE tenant_id = :t"), {"t": tenant}).scalar_one() == 1
    assert not [e for e in event_rows(worker.engine, run_id) if e[2] == "tool_approval_waiting"]


# ------------------------------------------------------------------------------------------ the instructions


def test_only_the_architect_is_told_so_not_the_other_built_in_helpers(worker, client, tenant):
    """A viewer may have a built-in helper run for them (a workflow explanation): its instructions stay as seeded."""
    agent = builtin_agent(worker, tenant)
    with worker.engine.begin() as conn:
        conn.execute(text("UPDATE agents SET slug = 'workflow-explainer' WHERE id = :a"), {"a": str(agent)})
    _, row = run_for(worker, client, tenant, person("viewer"), scripted_spec("context", tools=[], approvals=None), agent_id=agent)
    assert row["status"] == "completed", row["error"]
    assert read_only.READ_ONLY_NOTE not in row["output"]


def test_the_instructions_tell_a_viewers_architect_it_is_read_only_and_an_editors_do_not(worker, client, tenant):
    agent = builtin_agent(worker, tenant)
    _, viewer = run_for(worker, client, tenant, person("viewer"), scripted_spec("context"), agent_id=agent)
    _, editor = run_for(worker, client, tenant, person("editor"), scripted_spec("context"), agent_id=agent)
    assert viewer["status"] == editor["status"] == "completed", (viewer["error"], editor["error"])
    assert read_only.READ_ONLY_NOTE in viewer["output"] and "ask an editor" in viewer["output"]
    assert read_only.READ_ONLY_NOTE not in editor["output"]
    assert CONTROL_LIST_TOOL_ID in ARCHITECT_READ_TOOL_IDS
