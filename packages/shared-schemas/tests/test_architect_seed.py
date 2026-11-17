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
from eap_schemas.architect_seed import (
    architect_agent_spec,
    drafter_agent_spec,
    drafter_output_schema,
    filter_draft,
    pick_seed_model,
)
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


# ------------------------------------------------------------------------------ seed model ranking

PRODUCTION_LITELLM = [
    "CausalLM-34b-beta-4bit",
    "Mistral-7B-Instruct-v0.3-8bit",
    "Qwen3-VL-Embedding-8B-mlx-8bit",
    "Qwen3.6-27B-JANG_4M-CRACK",
    "cf-qwen2.5-coder-32b",
    "deepseek-r1:70b",
    "embeddinggemma-300m-4bit",
    "gemma4-mlx-bf16",
    "medgemma-27b-it-bf16",
    "novella-local",
    "qwen-coder-next",
    "qwen3-coder-next",
    "qwen3.8-thinking",
]


def test_seed_model_production_list_skips_the_quantised_first_name():
    chosen = pick_seed_model([("litellm", PRODUCTION_LITELLM)], [])
    assert chosen != "litellm:CausalLM-34b-beta-4bit"
    assert chosen == "litellm:Qwen3.6-27B-JANG_4M-CRACK"  # the one non-thinking qwen3*


def test_seed_model_thinking_model_loses_only_to_a_non_thinking_known_family():
    without = [n for n in PRODUCTION_LITELLM if not n.startswith("Qwen3.6")]
    # no non-thinking qwen3*/deepseek-v3: the known-family thinking model still beats the unknown names
    assert pick_seed_model([("litellm", without)], []) == "litellm:qwen3.8-thinking"
    assert pick_seed_model([("litellm", [*without, "deepseek-v3.2"])], []) == "litellm:deepseek-v3.2"


def test_seed_model_prefers_anthropic_then_openai_then_the_rest():
    both = [("litellm", ["qwen3.8"]), ("openai", ["gpt-4o", "text-embedding-3-small"]), ("anthropic", ["claude-sonnet-4-5"])]
    assert pick_seed_model(both, []) == "anthropic:claude-sonnet-4-5"
    assert pick_seed_model(both[:2], []) == "openai:gpt-4o"
    # a provider with only unusable models does not beat a usable one
    assert pick_seed_model([("openai", ["text-embedding-3-small"]), ("litellm", ["qwen3.8"])], []) == "litellm:qwen3.8"


def test_seed_model_demotions_and_unquantised_siblings():
    assert pick_seed_model([("litellm", ["llama-3.3-70b-4bit", "llama-3.3-70b"])], []) == "litellm:llama-3.3-70b"
    assert pick_seed_model([("litellm", ["bge-rerank-v2", "whisper-large", "my-tts", "plain-model"])], []) == "litellm:plain-model"
    assert pick_seed_model([("litellm", ["qwen3-coder", "other"])], []) == "litellm:other"
    assert pick_seed_model([("litellm", ["only-thinking", "m-reasoner"])], []) == "litellm:only-thinking"
    assert pick_seed_model([("litellm", ["gguf-thing"])], []) == "litellm:gguf-thing"  # demoted, still the only one
    assert pick_seed_model([("litellm", ["mistral-medium-latest", "x-mlx"])], []) == "litellm:mistral-medium-latest"
