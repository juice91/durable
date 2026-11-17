"""The Architect's restricted agent and workflow formats and their compile functions."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from eap_schemas.architect import (
    Allowlist,
    AllowlistAgent,
    AllowlistKnowledgeBase,
    AllowlistMcpServer,
    AllowlistModel,
    AllowlistTool,
    ArchitectAgentSpec,
    ArchitectWorkflowSpec,
    compile_agent,
    compile_workflow,
)
from eap_schemas.workflows import AgentStep, ParallelStep, ToolStep

KB = str(uuid4())
WRITER, REVIEWER = str(uuid4()), str(uuid4())


def allowlist(**extra) -> Allowlist:
    base = dict(
        tools=(
            AllowlistTool(id="clock.now", title="Current time", description="UTC time", requires_approval=False),
            AllowlistTool(id="tickets.create", title="Create ticket", description="New ticket", requires_approval=True),
        ),
        mcp_servers=(AllowlistMcpServer(id="docs", name="docs", tools=("search",)),),
        knowledge_bases=(AllowlistKnowledgeBase(id=KB, name="Handbook"),),
        models=(AllowlistModel(id="test:echo", provider="test"),),
        agents=(
            AllowlistAgent(id="writer", name="Writer", row_id=WRITER),
            AllowlistAgent(id="reviewer", name="Reviewer", row_id=REVIEWER),
        ),
        default_model="test:echo",
    )
    base.update(extra)
    return Allowlist(**base)


def agent(**over) -> ArchitectAgentSpec:
    return ArchitectAgentSpec(**{"name": "Helper", "instructions": "Help.", **over})


def test_agent_format_is_strict():
    with pytest.raises(ValidationError):
        agent(approvals={"tools": ["x"]})
    with pytest.raises(ValidationError):
        agent(id="Bad Id")
    with pytest.raises(ValidationError):
        agent(name="x" * 81)
    with pytest.raises(ValidationError):
        agent(instructions="x" * 8001)
    with pytest.raises(ValidationError):
        agent(model="no-colon")
    assert agent(id="helper-1").id == "helper-1"


def test_compile_agent_defaults_and_fields():
    spec = compile_agent(
        agent(memory=True, knowledge_bases=(KB,), model_settings={"temperature": 0.2}), allowlist()
    )
    assert (spec.model.provider, spec.model.name) == ("test", "echo")
    assert spec.memory is not None and spec.approvals is None
    assert spec.knowledge_bases[0].id == KB and spec.model_settings.temperature == 0.2
    assert spec.tools == ()  # tools default to none


def test_compile_agent_adds_approvals_for_gated_tools():
    spec = compile_agent(
        agent(tools=("clock.now", "tickets.create"), mcp_tools=({"server": "docs", "tool": "search"},)), allowlist()
    )
    assert spec.tools == ("clock.now", "tickets.create")
    assert spec.approvals is not None
    assert spec.approvals.tools == ("tickets.create", "docs/search")  # clock.now is not gated


@pytest.mark.parametrize(
    "over, code",
    [
        ({"tools": ("shell.run",)}, "tool_not_allowed: shell.run"),
        ({"tools": ("architect.save_agent",)}, "tool_not_allowed: architect.save_agent"),
        ({"mcp_tools": ({"server": "docs", "tool": "delete"},)}, "tool_not_allowed: docs/delete"),
        ({"mcp_tools": ({"server": "other", "tool": "search"},)}, "tool_not_allowed: other/search"),
        ({"knowledge_bases": (str(uuid4()),)}, "knowledge_base_not_allowed"),
        ({"model": "litellm:gpt"}, "model_not_allowed: litellm:gpt"),
    ],
)
def test_compile_agent_refuses_anything_outside_the_allowlist(over, code):
    with pytest.raises(ValueError, match=code):
        compile_agent(agent(**over), allowlist())


def test_compile_agent_denylist_holds_even_if_the_allowlist_offers_the_tool():
    forged = allowlist(tools=(AllowlistTool(id="architect.publish_agent", title="t", description="d"),))
    with pytest.raises(ValueError, match="tool_not_allowed: architect.publish_agent"):
        compile_agent(agent(tools=("architect.publish_agent",)), forged)


def test_compile_agent_needs_a_model():
    with pytest.raises(ValueError, match="model_required"):
        compile_agent(agent(), allowlist(default_model=None))


def wf(steps, **over) -> ArchitectWorkflowSpec:
    return ArchitectWorkflowSpec(**{"name": "Flow", "steps": steps, **over})


def test_compile_workflow_chains_steps_and_placeholders():
    spec = compile_workflow(
        wf([
            {"kind": "agent", "id": "draft", "agent": "writer", "prompt": "{{input}}"},
            {"kind": "agent", "id": "review", "agent": "reviewer", "prompt": "Check this: {{previous}}"},
            {"kind": "tool", "id": "stamp", "tool": "clock.now", "params": {"note": "{{steps.draft}}", "n": 1}},
        ]),
        allowlist(),
    )
    assert spec.start == "draft"
    draft, review, stamp = spec.steps
    assert isinstance(draft, AgentStep) and draft.agent_id.hex == WRITER.replace("-", "")
    assert draft.input == {"prompt": {"$ref": "input.input"}} and draft.next == "review"
    assert review.input == {"instruction": "Check this: {{previous}}", "previous": {"$ref": "steps.draft.output"}}
    assert isinstance(stamp, ToolStep) and stamp.next is None
    assert stamp.input == {"note": {"$ref": "steps.draft.output"}, "n": 1}
    assert spec.input_schema["required"] == ["input"]
    assert compile_workflow(  # deterministic
        wf([{"kind": "agent", "id": "draft", "agent": "writer", "prompt": "hi"}]), allowlist()
    ) == compile_workflow(wf([{"kind": "agent", "id": "draft", "agent": "writer", "prompt": "hi"}]), allowlist())


def test_compile_workflow_parallel_joins_at_the_next_step():
    spec = compile_workflow(
        wf([
            {"kind": "parallel", "id": "both", "branches": [
                {"id": "a", "agent": "writer", "prompt": "{{input}}"},
                {"id": "b", "agent": "reviewer", "prompt": "{{input}}"},
            ]},
            {"kind": "agent", "id": "merge", "agent": "writer", "prompt": "A: {{steps.a}} B: {{steps.b}}"},
        ]),
        allowlist(),
    )
    parallel = spec.steps[0]
    assert isinstance(parallel, ParallelStep) and parallel.branches == ("a", "b") and parallel.join == "merge"
    with pytest.raises(ValueError, match="parallel_needs_following_step"):
        compile_workflow(wf([{"kind": "parallel", "id": "p", "branches": [
            {"id": "a", "agent": "writer", "prompt": "x"}, {"id": "b", "agent": "writer", "prompt": "x"}]}]),
            allowlist())


def test_compile_workflow_rejects_loops_and_unknown_placeholders():
    for kind in ("loop", "foreach", "branch", "code", "approval"):
        with pytest.raises(ValidationError):
            wf([{"kind": kind, "id": "s1"}])
    with pytest.raises(ValidationError):  # a branch of a parallel step may only be an agent step
        wf([{"kind": "parallel", "id": "p", "branches": [
            {"kind": "tool", "id": "a", "tool": "clock.now"}, {"id": "b", "agent": "writer", "prompt": "x"}]}])
    with pytest.raises(ValidationError):
        wf([{"kind": "agent", "id": "run_end", "agent": "writer", "prompt": "x"}])  # reserved
    with pytest.raises(ValidationError):
        wf([{"kind": "agent", "id": f"s{i}", "agent": "writer", "prompt": "x"} for i in range(9)])  # at most 8
    for prompt, message in [
        ("{{previous}}", "unknown_placeholder"),  # first step has no previous
        ("{{steps.later}}", "unknown_placeholder"),
        ("{{context.x}}", "unknown_placeholder"),
        ("{{ input", "unknown_placeholder"),
    ]:
        with pytest.raises(ValueError, match=message):
            compile_workflow(wf([{"kind": "agent", "id": "s1", "agent": "writer", "prompt": prompt}]), allowlist())
    with pytest.raises(ValueError, match="agent_not_allowed: ghost"):
        compile_workflow(wf([{"kind": "agent", "id": "s1", "agent": "ghost", "prompt": "x"}]), allowlist())
    with pytest.raises(ValueError, match="tool_not_allowed: shell.run"):
        compile_workflow(wf([{"kind": "tool", "id": "s1", "tool": "shell.run"}]), allowlist())


def test_allowlist_dump_never_carries_row_ids():
    dumped = allowlist().model_dump(mode="json")
    assert "row_id" not in str(dumped) and WRITER not in str(dumped)
