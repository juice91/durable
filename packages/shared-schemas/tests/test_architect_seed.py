"""The seeded Architect specs, the drafter's schema and the draft filter (Task 5; no I/O)."""

from __future__ import annotations

import json
import re

from eap_schemas.architect import (
    ARCHITECT_GATED_TOOL_IDS,
    ARCHITECT_PLATFORM_TOOL_IDS,
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
    ARCHITECT_INSTRUCTIONS,
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
    assert spec.tools == (*ARCHITECT_TOOL_IDS, *ARCHITECT_PLATFORM_TOOL_IDS)
    assert len(ARCHITECT_TOOL_IDS) == 15 and len(ARCHITECT_PLATFORM_TOOL_IDS) == 8 and len(spec.tools) == 23
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


def test_generic_ids_are_one_list_and_slugify_falls_back_for_names_without_latin_letters():
    from eap_schemas.architect import GENERIC_IDS, is_generic_id

    assert GENERIC_IDS == ("definition", "id", "agent", "workflow", "<id>", "spec", "draft", "new", "untitled", "test")
    assert all(is_generic_id(g) for g in GENERIC_IDS) and is_generic_id(None) and is_generic_id(" Draft ")
    assert not is_generic_id("weekly-report") and not is_generic_id("agent-7f3a")
    cjk = slugify("球棒（棒球角色帳號）")
    assert re.fullmatch(r"agent-[0-9a-f]{4}", cjk) and cjk == slugify("球棒（棒球角色帳號）")
    assert cjk != slugify("另一個名字") and not is_generic_id(cjk)
    assert slugify("Baseball 球棒") == "baseball"
    assert slugify("球棒 v2 writer") == "v2-writer"
    wf = slugify("球棒棒球貼文", "workflow")
    assert re.fullmatch(r"workflow-[0-9a-f]{4}", wf) and wf == slugify("球棒棒球貼文", "workflow")
    assert slugify("球棒棒球貼文", "agent").startswith("agent-") and slugify("球棒棒球貼文") .startswith("agent-")
    assert slugify("Weekly Report", "workflow") == "weekly-report"


def test_rank_model_ids_matches_the_seed_pick_and_compile_agent_takes_the_ranked_model():
    from eap_schemas.architect import ArchitectAgentSpec, compile_agent
    from eap_schemas.architect_seed import rank_model_ids

    ids = ["litellm:CausalLM-34b-beta-4bit", "litellm:qwen3.8-thinking", "litellm:qwen3.8", "openai:gpt-4o"]
    ranked = rank_model_ids(ids)
    assert ranked[0] == "openai:gpt-4o" and sorted(ranked) == sorted(ids)
    assert ranked[0] == pick_seed_model([("litellm", [i.split(":")[1] for i in ids[:3]]), ("openai", ["gpt-4o"])], [])
    assert rank_model_ids(ids[:3])[0] == "litellm:qwen3.8"
    assert rank_model_ids([]) == []

    allow = Allowlist(
        models=tuple(AllowlistModel(id=i, provider=i.split(":")[0]) for i in ids[:3]),
        default_model=ids[0],  # the first listed, as the worker used to take
    )
    compiled = compile_agent(ArchitectAgentSpec(name="Bat", instructions="x"), allow)
    assert (compiled.model.provider, compiled.model.name) == ("litellm", "qwen3.8")
    named = compile_agent(ArchitectAgentSpec(name="Bat", instructions="x", model=ids[0]), allow)
    assert named.model.name == "CausalLM-34b-beta-4bit"  # an explicit choice is kept


def test_the_architect_is_told_to_use_descriptive_ids():
    from eap_schemas.architect_seed import ARCHITECT_INSTRUCTIONS

    assert "slug of its name" in ARCHITECT_INSTRUCTIONS and '"definition"' in ARCHITECT_INSTRUCTIONS
    assert "without Latin letters" in ARCHITECT_INSTRUCTIONS and "qiubang-baseball-post" in ARCHITECT_INSTRUCTIONS


def test_the_architect_knows_http_request_and_when_to_pick_it():
    text = ARCHITECT_INSTRUCTIONS
    assert "http.request" in text and "fetch" in text and "secret:<name>" in text
    assert "waits for the person's approval" in text and "workflow tool step" in text
    assert offer_tool("http.request", "HTTP request", "x", side_effect=True) is not None  # not a denied prefix


def test_the_architect_names_the_general_tools_and_the_collect_shape_check_post_pattern():
    text = ARCHITECT_INSTRUCTIONS
    for tool in ("feed.parse", "json.query", "text.check", "html.extract", "body_from_url", "http.request", "list_workflow_step_kinds", "$template", "branch"):
        assert tool in text, tool
    assert "collect with http.request" in text and "dry-run" in text
    for provider_specific in ("Bluesky", "LTN", "Yahoo", "MLB"):
        assert provider_specific not in text


def test_the_picker_prefers_a_general_chat_model_over_tunes_code_embedding_and_medical_names():
    from eap_schemas.architect_seed import rank_model_ids

    names = [
        "Qwen3-VL-Embedding-8B", "medgemma-27b", "Qwen3-Coder-30B", "CausalLM-34b-beta-4bit",
        "text-embedding-3-small", "Qwen3.6-35B-A3B",
    ]
    assert pick_seed_model([("litellm", names)], []) == "litellm:Qwen3.6-35B-A3B"
    ranked = rank_model_ids([f"litellm:{n}" for n in names])
    assert ranked[0] == "litellm:Qwen3.6-35B-A3B"
    for unsuitable in ("Qwen3-VL-Embedding-8B", "medgemma-27b", "Qwen3-Coder-30B", "text-embedding-3-small"):
        assert ranked.index(f"litellm:{unsuitable}") > ranked.index("litellm:CausalLM-34b-beta-4bit")
    # a direct Anthropic model still leads, and the newest catalogue names count as a known family
    both = [("litellm", names), ("anthropic", ["claude-sonnet-5-5", "claude-fable-5-2"])]
    assert pick_seed_model(both, []).startswith("anthropic:claude-")


def test_the_architect_seed_states_what_save_workflow_accepts_and_the_8_step_rule_stays_in_the_drafter():
    from eap_schemas.architect_seed import ARCHITECT_INSTRUCTIONS, WORKFLOW_DRAFTER_INSTRUCTIONS

    flat = " ".join(ARCHITECT_INSTRUCTIONS.split())
    assert "1 to 40 steps" in flat and "branch steps" in flat and "parallel steps" in flat
    assert "1 to 8" not in flat and "8 steps" not in flat
    assert "must use an http.request reducer" in flat and "response_query" in flat
    assert "1 to 8 steps" in " ".join(WORKFLOW_DRAFTER_INSTRUCTIONS.split())


LIVE_ANTHROPIC = [
    "claude-fable-5", "claude-fable-5-1", "claude-fable-5-2", "claude-haiku-4-5", "claude-haiku-5-5",
    "claude-opus-4-5", "claude-opus-4-6", "claude-opus-4-7", "claude-opus-5-1", "claude-opus-5-5",
    "claude-sonnet-4-5", "claude-sonnet-4-6", "claude-sonnet-5-1", "claude-sonnet-5-5", "claude-sonnet-5-3",
]
LIVE_LITELLM = [
    "CausalLM-34b-beta-4bit", "bge-reranker", "qwen3-coder", "qwen3.8", "qwen3.8-thinking", "qwen3-vl-embedding-8b",
    "medgemma-27b", "llama-3.3-70b", "deepseek-v3.2", "mistral-large", "gpt-oss", "other-a", "other-b",
]


def test_the_default_prefers_claude_sonnet_5_5_over_fable_and_the_gateway():
    from eap_schemas.architect_seed import rank_model_ids

    assert len(LIVE_ANTHROPIC) == 15 and len(LIVE_LITELLM) == 13
    both = [("litellm", LIVE_LITELLM), ("anthropic", LIVE_ANTHROPIC)]
    assert pick_seed_model(both, []) == "anthropic:claude-sonnet-5-5"
    ids = rank_model_ids([f"{p}:{n}" for p, names in both for n in names])
    claude = [i.split(":")[1] for i in ids if i.startswith("anthropic:")]
    assert claude[:3] == ["claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5"]
    assert claude[3:-3] == [  # the rest by version, newest first
        "claude-sonnet-5-3", "claude-sonnet-5-1", "claude-opus-5-1", "claude-opus-4-7", "claude-sonnet-4-6",
        "claude-opus-4-6", "claude-sonnet-4-5", "claude-opus-4-5", "claude-haiku-4-5",
    ]
    assert claude[-3:] == ["claude-fable-5", "claude-fable-5-1", "claude-fable-5-2"]
    assert ids.index("anthropic:claude-fable-5-2") < ids.index("litellm:qwen3.8")  # provider still outranks the gateway
    assert ids[-1].startswith("litellm:") and any(m in ids[-1] for m in ("embedding", "reranker", "coder", "medgemma"))
    assert rank_model_ids(["anthropic:claude-sonnet-5-5-20261001", "anthropic:claude-opus-5-5"])[0] == "anthropic:claude-sonnet-5-5-20261001"
