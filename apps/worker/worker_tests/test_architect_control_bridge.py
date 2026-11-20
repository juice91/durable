"""The Architect's bridge to the MCP control plane: discovery (`platform__list_capabilities`), `control__*` calls,
approval gating of the manage tools, refusals, the audit actor and the name mapping. The real API app (uvicorn
thread, real Postgres) is the other end of the seam: the worker calls its MCP endpoint with a run token."""

from __future__ import annotations

import logging
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic_ai import Agent, DeferredToolRequests, DeferredToolResults
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.tools import ToolDefinition

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "api"))

from api_tests.conftest import dev_auth_mode  # noqa: E402,F401 - autouse fixture
from api_tests.mcp_control_helpers import secrets_key, world  # noqa: E402,F401 - fixtures
from api_tests.test_architect_control_seam import cleanup_agents, seed_architect  # noqa: E402,F401
from eap_runtime import InMemorySideEffectStore, ToolRegistry
from eap_schemas.architect import CONTROL_LIST_TOOL_ID, CONTROL_TOOL_PREFIX, control_tool_name
from eap_schemas.architect_seed import architect_agent_spec
from eap_worker import control_bridge as bridge
from eap_worker.platform_read_tools import register_platform_read_tools


@pytest.fixture
def seam(world, secrets_key, cleanup_agents, monkeypatch):  # noqa: F811
    """A live Architect run of a fresh tenant, and the worker side pointed at the real API."""
    monkeypatch.setenv(bridge.API_URL_ENV, world.base)
    bridge.reset_cache()
    tenant, _ = world.tenant_of()
    run_id = seed_architect(world, tenant)
    yield SimpleNamespace(world=world, tenant=tenant, run_id=run_id)
    bridge.reset_cache()


def ctx_for(seam, call_id: str = "call_1", store: Any = None) -> Any:
    deps = SimpleNamespace(tenant_id=seam.tenant, run_id=uuid.UUID(seam.run_id), side_effects=store or InMemorySideEffectStore())
    return SimpleNamespace(deps=deps, tool_call_id=call_id)


async def tools_of(seam) -> dict[str, Any]:
    return await bridge.ControlToolset().get_tools(ctx_for(seam))


# ------------------------------------------------------------------------------------------ discovery


@pytest.mark.asyncio
async def test_discovery_lists_the_control_plane_with_kinds_and_gating(seam):
    result = await bridge.list_capabilities(ctx_for(seam))
    assert result["total"] >= 40 and result["matched"] == result["total"]
    by_name = {c["name"]: c for c in result["capabilities"]}
    assert by_name["eval.suite.create"]["kind"] == "manage" and by_name["eval.suite.create"]["requires_approval"] is True
    assert by_name["scorer.list"]["kind"] == "read" and by_name["scorer.list"]["requires_approval"] is False
    assert all(c["kind"] in {"read", "manage"} and c["description"] for c in by_name.values())
    # the tools that would let the Architect approve itself or drive itself are not offered
    assert "approvals.decide" not in by_name and "agent.chat" not in by_name


@pytest.mark.asyncio
async def test_discovery_filters_by_name_and_description(seam):
    found = await bridge.list_capabilities(ctx_for(seam), "eval")
    names = {c["name"] for c in found["capabilities"]}
    assert {"eval.suite.create", "eval.case.add", "eval.run.start", "eval.suite.list", "eval.run.get"} <= names
    assert found["matched"] == len(names) < found["total"]
    assert (await bridge.list_capabilities(ctx_for(seam), "ZZZ-nothing"))["capabilities"] == []
    by_text = await bridge.list_capabilities(ctx_for(seam), "schedule")  # in the description or the name
    assert any(c["name"] == "workflow.schedule.set" for c in by_text["capabilities"])


@pytest.mark.asyncio
async def test_names_round_trip_between_the_control_plane_and_the_architect(seam):
    catalog = await bridge.fetch_catalog(seam.tenant, seam.run_id)
    assert control_tool_name("eval.suite.create") == "control__eval_suite_create"
    tools = await tools_of(seam)
    assert set(tools) == {t.tool_name for t in catalog} and len(tools) == len(catalog)  # no two collide
    for t in catalog:
        definition = tools[t.tool_name].tool_def
        assert definition.metadata["control"] == t.name and t.tool_name.startswith(CONTROL_TOOL_PREFIX)
        assert t.tool_name == control_tool_name(t.name)


@pytest.mark.asyncio
async def test_the_tool_declarations_mirror_the_control_tools_signatures_and_kinds(seam):
    tools = await tools_of(seam)
    create = tools["control__eval_suite_create"].tool_def
    assert create.kind == "unapproved"  # a manage tool defers to the approval card
    assert create.parameters_json_schema["required"] == ["name"]
    assert set(create.parameters_json_schema["properties"]) >= {"name", "agent_id", "workflow_id", "evaluators", "suite_id"}
    assert "A person approves each call" in create.description
    listing = tools["control__scorer_list"].tool_def
    assert listing.kind == "function" and "A person approves" not in listing.description


def test_list_capabilities_is_registered_for_the_built_in_architect_only():
    registry = ToolRegistry()
    register_platform_read_tools(registry)
    assert CONTROL_LIST_TOOL_ID in registry.tool_ids() and registry.tool_id_for_name("platform__list_capabilities")
    assert not registry.get_metadata(CONTROL_LIST_TOOL_ID)["side_effect"]
    spec = architect_agent_spec("test:echo")
    assert isinstance(bridge.control_toolset_for_run(spec), bridge.ControlToolset)
    plain = spec.model_copy(update={"tools": ("clock.now",)})
    assert bridge.control_toolset_for_run(plain) is None


# --------------------------------------------------------------------------------------------- calls


@pytest.mark.asyncio
async def test_a_read_bridge_call_returns_the_control_tools_result(seam):
    toolset = bridge.ControlToolset()
    tools = await toolset.get_tools(ctx_for(seam))
    made = await bridge.call_control(seam.tenant, seam.run_id, "scorer.create",
                                     {"name": "Polite", "evaluator": {"kind": "regex", "pattern": "please"}})
    assert "error" not in made, made
    tool = tools["control__scorer_list"]
    result = await toolset.call_tool(tool.tool_def.name, {}, ctx_for(seam), tool)
    assert "error" not in result and any(s["name"] == "Polite" for s in result["items"]), result


@pytest.mark.asyncio
async def test_a_refused_control_call_surfaces_the_error_code_and_message(seam):
    toolset = bridge.ControlToolset()
    tools = await toolset.get_tools(ctx_for(seam))
    bad = tools["control__scorer_create"]
    result = await toolset.call_tool(bad.tool_def.name, {"name": "x", "evaluator": {"kind": "nope"}}, ctx_for(seam), bad)
    assert set(result) == {"error"} and result["error"]["code"] and result["error"]["message"]
    missing = await bridge.call_control(seam.tenant, seam.run_id, "eval.run.get", {"run_id": str(uuid.uuid4())})
    assert missing["error"]["code"] == "not_found"
    # a tool reserved for people is refused by the API even if the model names it
    refused = await bridge.call_control(seam.tenant, seam.run_id, "approvals.decide",
                                        {"run_id": seam.run_id, "kind": "tool", "key": "k", "decision": "approve"})
    assert refused["error"]["code"] == "human_only"


@pytest.mark.asyncio
async def test_an_unconfigured_or_unauthorised_bridge_says_so_instead_of_raising(seam, monkeypatch):
    from eap_run_store.run_tokens import mint_run_token, signing_key

    # a worker whose key differs from the API's: the API cannot verify its token (401)
    monkeypatch.setattr(bridge, "_token", lambda tenant, run: mint_run_token(signing_key("another-key-entirely"), tenant, run))
    assert (await bridge.call_control(seam.tenant, seam.run_id, "scorer.list", {}))["error"]["code"] == "control_unavailable"
    assert (await bridge.list_capabilities(ctx_for(seam)))["error"]["code"] == "control_unavailable"
    assert await bridge.ControlToolset().get_tools(ctx_for(seam)) == {}  # the Architect runs without the bridge
    monkeypatch.delenv(bridge.API_URL_ENV)
    assert (await bridge.call_control(seam.tenant, seam.run_id, "scorer.list", {}))["error"]["code"] == "control_unavailable"


@pytest.mark.asyncio
async def test_the_audit_actor_is_the_architect_run(seam, caplog):
    with caplog.at_level(logging.INFO, logger="eap_api.mcp_control"):
        await bridge.list_capabilities(ctx_for(seam))
        await bridge.call_control(seam.tenant, seam.run_id, "scorer.list", {})
        await bridge.call_control(seam.tenant, seam.run_id, "scorer.create", {"name": "A", "evaluator": {"kind": "regex", "pattern": "a"}})
    audit = [r.__dict__ for r in caplog.records if r.__dict__.get("eap.audit") == "mcp_call"]
    assert {a["eap.tool"] for a in audit} >= {"scorer.list", "scorer.create"}
    assert {a["eap.principal"] for a in audit} == {f"architect-run:{seam.run_id}"}


# --------------------------------------------------------------------------------- the approval gate


def scripted(tool: str, args: dict[str, Any]) -> FunctionModel:
    def model(messages, info):
        if any(getattr(m, "kind", "") == "request" and any(p.part_kind == "tool-return" for p in m.parts) for m in messages):
            return ModelResponse(parts=[__import__("pydantic_ai").messages.TextPart("done")])
        return ModelResponse(parts=[ToolCallPart(tool, args, tool_call_id="call_a")])

    return FunctionModel(model)


@pytest.mark.asyncio
async def test_a_manage_bridge_call_waits_for_approval_and_runs_only_after_it(seam):
    toolset = bridge.ControlToolset()
    deps = ctx_for(seam).deps
    args = {"name": "Approved suite", "agent_id": "does-not-matter-the-run-is-gated"}
    agent = Agent(scripted("control__scorer_create", {"name": "Gated", "evaluator": {"kind": "regex", "pattern": "x"}}),
                  toolsets=[toolset], deps_type=type(deps), output_type=[str, DeferredToolRequests])
    first = await agent.run("make a scorer", deps=deps)
    assert isinstance(first.output, DeferredToolRequests)
    (call,) = first.output.approvals
    assert call.tool_name == "control__scorer_create"
    before = await bridge.call_control(seam.tenant, seam.run_id, "scorer.list", {})
    assert not any(s["name"] == "Gated" for s in before["items"])  # nothing ran while the card is open

    second = await agent.run(
        message_history=first.all_messages(), deps=deps,
        deferred_tool_results=DeferredToolResults(approvals={call.tool_call_id: True}),
    )
    assert second.output == "done"
    after = await bridge.call_control(seam.tenant, seam.run_id, "scorer.list", {})
    assert [s["name"] for s in after["items"]].count("Gated") == 1
    # a replayed call of the same tool call id returns the recorded result: no second scorer
    tools = await toolset.get_tools(ctx_for(seam))
    tool = tools["control__scorer_create"]
    ctx = ctx_for(seam, "call_a", deps.side_effects)
    again = await toolset.call_tool(tool.tool_def.name, {"name": "Gated", "evaluator": {"kind": "regex", "pattern": "x"}}, ctx, tool)
    assert "error" not in again
    final = await bridge.call_control(seam.tenant, seam.run_id, "scorer.list", {})
    assert [s["name"] for s in final["items"]].count("Gated") == 1


@pytest.mark.asyncio
async def test_the_run_capability_sends_every_deferred_control_call_to_the_card(seam, monkeypatch):
    from pydantic_ai.tools import ToolDenied  # noqa: F401
    from eap_worker import capabilities

    seen: list[str] = []

    async def fake_wait(run_id, tenant_id, calls, *a, **k):
        seen.extend(c.tool_name for c in calls)
        return DeferredToolResults(approvals={c.tool_call_id: True for c in calls})

    monkeypatch.setattr(capabilities, "wait_for_tool_approvals", fake_wait)
    monkeypatch.setattr(capabilities, "check_approved_tool_calls", lambda ctx, results: None)
    spec = architect_agent_spec("test:echo")
    ctx = SimpleNamespace(deps=SimpleNamespace(spec=spec, run_id=seam.run_id, tenant_id=seam.tenant), run_step=2)
    requests = DeferredToolRequests(approvals=[ToolCallPart("control__eval_suite_create", {"name": "n"}, tool_call_id="c1")])
    out = await capabilities.SpecCapability().handle_deferred_tool_calls(ctx, requests=requests)
    assert seen == ["control__eval_suite_create"] and out is not None


# ---------------------------------------------------------------------------------------- summaries


def test_the_card_summary_says_what_the_call_does_in_words():
    description = (
        "Create a test suite for an agent or a workflow (give one) with up to 10 suite evaluators (the kinds\n"
        "of scorer.suggest's specs, plus equals_expected, max_duration and llm_judge). `suite_id` makes a retry\n"
        "idempotent. Add cases with eval.case.add."
    )
    text = bridge.control_approval_summary(
        "eval.suite.create", description,
        {"name": "小天娛樂新聞貼文", "agent_id": "xiaotian-post", "evaluators": [{"kind": "regex"}, {"kind": "max_duration"}], "suite_id": None},
    )
    assert text == ("Create a test suite for an agent or a workflow with up to 10 suite evaluators. "
                    "With name '小天娛樂新聞貼文', agent_id 'xiaotian-post', 2 evaluators.")
    assert bridge.control_approval_summary("workflow.publish", None, {}) == "Workflow publish."
    assert bridge.control_approval_summary("x.y", "Do a thing.", {"n": 3, "q": "a" * 90}).startswith("Do a thing. With n 3, q '" + "a" * 60 + "…'")


@pytest.mark.asyncio
async def test_the_card_summary_of_a_listed_control_tool_comes_from_its_description(seam):
    await bridge.fetch_catalog(seam.tenant, seam.run_id)
    summary = bridge.control_summary_for_call("control__eval_suite_create", {"name": "Xiaotian news", "agent_id": "xiaotian"})
    print("SUMMARY:", summary)
    assert summary.startswith("Create a test suite for an agent or a workflow with up to 10 suite evaluators. With")
    assert summary.endswith("With name 'Xiaotian news', agent_id 'xiaotian'.")
    bridge.reset_cache()  # a cold process words it from the tool's name
    assert bridge.control_summary_for_call("control__eval_suite_create", {"name": "n"}) == "Eval suite create. With name 'n'."
