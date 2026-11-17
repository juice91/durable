"""The seeded Architect specs, the drafter's schema and the draft filter (Task 5; no I/O)."""

from __future__ import annotations

import json

from eap_schemas.architect import (
    ARCHITECT_GATED_TOOL_IDS,
    ARCHITECT_TOOL_IDS,
    Allowlist,
    AllowlistKnowledgeBase,
    AllowlistMcpServer,
    AllowlistModel,
    AllowlistTool,
    ArchitectAgentSpec,
    ArchitectMcpTool,
    offer_tool,
    slugify,
)
from eap_schemas.architect_seed import architect_agent_spec, drafter_agent_spec, drafter_output_schema, filter_draft
from eap_schemas.output import OutputSpec


def test_drafter_schema_is_inside_the_output_subset_and_has_no_pattern():
    schema = drafter_output_schema()
    OutputSpec(json_schema=schema, mode="tool")  # the subset validator accepts it
    assert "pattern" not in json.dumps(schema) and "$ref" not in json.dumps(schema)
    fields = set(ArchitectAgentSpec.model_fields)
    assert set(schema["properties"]) == fields | {"assumptions"}
    assert schema["required"] == ["name", "instructions", "assumptions"]
    assert schema["properties"]["id"]["type"] == ["string", "null"]
    assert schema["properties"]["mcp_tools"]["items"]["properties"]["server"]["type"] == "string"
    assert drafter_output_schema() == schema  # deterministic


def test_the_seeded_specs():
    spec = architect_agent_spec("test:echo")
    assert spec.tools == ARCHITECT_TOOL_IDS and len(ARCHITECT_TOOL_IDS) == 15
    assert spec.approvals is not None and spec.approvals.tools == ARCHITECT_GATED_TOOL_IDS
    assert spec.memory.history.last_turns == 30 and spec.model_settings.max_steps == 12
    drafter = drafter_agent_spec("litellm:qwen")
    assert drafter.tools == () and drafter.output.effective_mode() == "tool"


def test_filter_draft_drops_what_the_allowlist_did_not_offer():
    allow = Allowlist(
        tools=(AllowlistTool(id="clock.now", title="Clock", description="Time"),),
        mcp_servers=(AllowlistMcpServer(id="crm", name="crm", tools=("find",)),),
        knowledge_bases=(AllowlistKnowledgeBase(id="kb1", name="Docs"),),
        models=(AllowlistModel(id="test:echo", provider="test"),),
    )
    spec = ArchitectAgentSpec(
        name="x", instructions="y", model="openai:gpt-4",
        tools=("clock.now", "clock.now", "shell.run", "architect.publish_agent"),
        mcp_tools=(ArchitectMcpTool(server="crm", tool="find"), ArchitectMcpTool(server="crm", tool="delete")),
        knowledge_bases=("kb1", "kb2"),
    )
    cleaned, dropped = filter_draft(spec, allow)
    assert cleaned.tools == ("clock.now",) and cleaned.model is None
    assert [(m.server, m.tool) for m in cleaned.mcp_tools] == [("crm", "find")] and cleaned.knowledge_bases == ("kb1",)
    assert {(d["kind"], d["id"]) for d in dropped} == {
        ("tool", "shell.run"), ("tool", "architect.publish_agent"), ("mcp_tool", "crm/delete"),
        ("knowledge_base", "kb2"), ("model", "openai:gpt-4"),
    }


def test_offer_tool_and_slugify():
    assert offer_tool("shell.exec", "Shell", "x") is None and offer_tool("clock.now", "C", "d", host_only=True) is None
    tool = offer_tool("tickets.create", "Ticket", "", side_effect=False)
    assert tool is not None and tool.requires_approval and tool.description == "Ticket"
    assert slugify("Weekly Report Writer!") == "weekly-report-writer" and slugify("123") == "a-123"


def test_secrets_request_is_offerable_and_gated_and_the_instructions_say_how_to_ask():
    from eap_schemas.architect_seed import ARCHITECT_INSTRUCTIONS
    from eap_schemas.tools import TOOL_DECLARATIONS

    decl = TOOL_DECLARATIONS["secrets.request"]
    tool = offer_tool("secrets.request", decl.title, decl.description, side_effect=decl.side_effect)
    assert tool is not None and tool.requires_approval
    assert "secrets.request" in ARCHITECT_INSTRUCTIONS
    assert "never ask the person to paste" in ARCHITECT_INSTRUCTIONS
