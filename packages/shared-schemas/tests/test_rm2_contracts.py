"""RM.2 contracts: knowledge base refs, memory config, working memory schema and merge, embedding
policy, reserved tool names, event types, error types, and hash stability of the new fields."""

from __future__ import annotations

import json
import pickle
from typing import get_args

import pytest
from pydantic import ValidationError

from eap_schemas import AgentSpec, AgentVersion, RunEventType, canonical_spec_hash
from eap_schemas.embedding_policy import (
    EmbeddingDimensionMismatch,
    EmbeddingModelNotAllowed,
    EmbeddingModelPolicy,
)
from eap_schemas.knowledge import (
    ChunkingConfig,
    IngestBudgetExceeded,
    KnowledgeBaseNotFound,
    KnowledgeBaseRef,
    approx_tokens,
    validate_metadata,
)
from eap_schemas.memory import (
    DEFAULT_WORKING_MEMORY_TEMPLATE,
    MemoryConfig,
    WorkingMemoryConfig,
    WorkingMemoryInvalid,
    merge_working_memory,
    validate_working_memory_content,
    validate_working_memory_schema,
    validate_working_memory_text,
)
from eap_schemas.tools import RESERVED_TOOL_NAMES

from test_spec_hash_stability import (
    FULL,
    FULL_HASH,
    MINIMAL,
    MINIMAL_HASH,
    PRE_CHANGE_FULL_JSON,
    PRE_CHANGE_MINIMAL_JSON,
    PRE_P02_MCP_HASH,
    PRE_P02_MCP_JSON,
)

KB1 = "00000000-0000-0000-0000-0000000000a1"
KB2 = "00000000-0000-0000-0000-0000000000a2"
QWEN = "litellm:Qwen3-VL-Embedding-8B-mlx-8bit"


# --- hash stability ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "base, expected_json, expected_hash",
    [
        (FULL, PRE_CHANGE_FULL_JSON, FULL_HASH),
        (MINIMAL, PRE_CHANGE_MINIMAL_JSON, MINIMAL_HASH),
        (PRE_P02_MCP_JSON, PRE_P02_MCP_JSON, PRE_P02_MCP_HASH),
    ],
)
@pytest.mark.parametrize("extra", [{}, {"knowledge_bases": []}, {"memory": None}, {"knowledge_bases": [], "memory": None}])
def test_empty_new_fields_keep_json_and_hash(base, expected_json, expected_hash, extra):
    spec = AgentSpec.model_validate({**base, **extra})
    assert canonical_spec_hash(spec) == expected_hash
    assert spec.model_dump(mode="json") == expected_json
    assert json.loads(spec.model_dump_json()) == expected_json
    assert "knowledge_bases" not in spec.model_dump() and "memory" not in spec.model_dump()


def test_stored_versions_still_validate():
    for spec_json, spec_hash in ((PRE_CHANGE_FULL_JSON, FULL_HASH), (PRE_P02_MCP_JSON, PRE_P02_MCP_HASH)):
        AgentVersion.model_validate({
            "agent_id": "00000000-0000-0000-0000-000000000001",
            "tenant_id": "t1",
            "version": 1,
            "spec": spec_json,
            "spec_hash": spec_hash,
            "created_at": "2026-11-01T00:00:00+00:00",
        })


def test_memory_empty_and_explicit_nones_hash_the_same():
    empty = AgentSpec.model_validate({**FULL, "memory": {}})
    nones = AgentSpec.model_validate({
        **FULL,
        "memory": {"history": None, "working": None, "observational": None, "semantic_recall": None, "redact": None},
    })
    assert canonical_spec_hash(empty) == canonical_spec_hash(nones) != FULL_HASH
    assert empty.model_dump(mode="json")["memory"] == {}


def test_nested_none_fields_are_dropped():
    spec = AgentSpec.model_validate({
        **FULL,
        "knowledge_bases": [{"id": KB1, "min_score": None, "filter": None}],
        "memory": {
            "history": {"last_turns": 10, "max_tokens": None},
            "working": {"template": None, "json_schema": None},
            "semantic_recall": {"embedding_model": "test:hash", "min_score": None},
        },
    })
    dumped = spec.model_dump(mode="json")
    assert dumped["knowledge_bases"] == [{"id": KB1, "top_k": 8}]
    assert dumped["memory"] == {
        "history": {"last_turns": 10},
        "working": {"scope": "resource", "read_only": False},
        "semantic_recall": {"embedding_model": "test:hash", "top_k": 4, "message_range": 1, "scope": "resource"},
    }
    again = AgentSpec.model_validate(dumped)
    assert canonical_spec_hash(again) == canonical_spec_hash(spec)


def test_knowledge_and_memory_are_part_of_the_hash():
    a = AgentSpec.model_validate({**FULL, "knowledge_bases": [{"id": KB1}]})
    b = AgentSpec.model_validate({**FULL, "knowledge_bases": [{"id": KB1, "top_k": 3}]})
    c = AgentSpec.model_validate({**FULL, "memory": {"history": {"last_turns": 5}}})
    assert len({canonical_spec_hash(s) for s in (a, b, c)} | {FULL_HASH}) == 4


# --- KnowledgeBaseRef ----------------------------------------------------------------------------


def test_kb_ref_normalises_uuid_and_validates_limits():
    assert KnowledgeBaseRef(id=KB1.upper()).id == KB1
    for bad in ({"id": "nope"}, {"id": KB1, "top_k": 0}, {"id": KB1, "top_k": 21}, {"id": KB1, "min_score": 1.5}):
        with pytest.raises(ValidationError):
            KnowledgeBaseRef.model_validate(bad)


def test_kb_ref_filter():
    ref = KnowledgeBaseRef.model_validate({"id": KB1, "filter": {"lang": "en", "tier": [1, 2], "public": True}})
    assert ref.model_dump(mode="json")["filter"] == {"lang": "en", "tier": [1, 2], "public": True}
    assert KnowledgeBaseRef.model_validate({"id": KB1, "filter": {}}).filter is None
    for bad in ({"a": {"nested": 1}}, {"a": []}, {"bad key": 1}, {"a": float("nan")}, {f"k{i}": 1 for i in range(17)}):
        with pytest.raises(ValidationError):
            KnowledgeBaseRef.model_validate({"id": KB1, "filter": bad})


def test_at_most_eight_unique_knowledge_bases():
    ids = [f"00000000-0000-0000-0000-{i:012d}" for i in range(9)]
    AgentSpec.model_validate({**MINIMAL, "knowledge_bases": [{"id": i} for i in ids[:8]]})
    with pytest.raises(ValidationError):
        AgentSpec.model_validate({**MINIMAL, "knowledge_bases": [{"id": i} for i in ids]})
    with pytest.raises(ValidationError, match="duplicate knowledge base"):
        AgentSpec.model_validate({**MINIMAL, "knowledge_bases": [{"id": KB1}, {"id": KB1.upper()}]})


def test_metadata_rules():
    assert validate_metadata({"a": "x", "b": 1, "c": 1.5, "d": False}) == {"a": "x", "b": 1, "c": 1.5, "d": False}
    for bad in ({"a": None}, {"a": [1]}, {"": 1}, {f"k{i}": 1 for i in range(17)}):
        with pytest.raises(ValueError):
            validate_metadata(bad)


def test_chunking_config_and_tokens():
    assert ChunkingConfig().model_dump() == {"max_tokens": 512, "overlap_tokens": 64, "min_tokens": 40, "chunker_version": 1}
    for bad in ({"overlap_tokens": 300}, {"min_tokens": 512}, {"max_tokens": 10}):
        with pytest.raises(ValidationError):
            ChunkingConfig.model_validate(bad)
    assert (approx_tokens(""), approx_tokens("a"), approx_tokens("abcd"), approx_tokens("abcde"), approx_tokens("漢")) == (0, 1, 1, 2, 1)


# --- memory config -------------------------------------------------------------------------------


def test_memory_limits():
    bad_configs = [
        {"history": {"last_turns": 0}},
        {"history": {"last_turns": 201}},
        {"working": {"template": "x", "json_schema": {"type": "object"}}},
        {"working": {"template": "x" * 4097}},
        {"working": {"template": "   "}},
        {"working": {"scope": "global"}},
        {"observational": {"model": {"provider": "test", "name": "observer"}, "observe_after_tokens": 10}},
        {"semantic_recall": {"embedding_model": "test:hash", "top_k": 11}},
        {"semantic_recall": {"embedding_model": "test:hash", "message_range": 4}},
        {"semantic_recall": {"embedding_model": "no-provider"}},
        {"redact": {"action": "block"}},
        {"unknown": {}},
    ]
    for bad in bad_configs:
        with pytest.raises(ValidationError):
            MemoryConfig.model_validate(bad)


def test_memory_helpers():
    m = MemoryConfig.model_validate({
        "observational": {"model": {"provider": "test", "name": "observer"}},
        "semantic_recall": {"embedding_model": "test:hash768"},
    })
    assert m.effective_history().last_turns == 20
    assert [r.name for r in m.chat_models()] == ["observer"]
    assert m.embedding_models() == ("test:hash768",)
    assert WorkingMemoryConfig().effective_template() == DEFAULT_WORKING_MEMORY_TEMPLATE
    assert WorkingMemoryConfig(json_schema={"type": "object"}).format == "json"
    spec = AgentSpec.model_validate({
        **MINIMAL,
        "input_processors": [{"type": "pii_redact", "config": {"kinds": ["email"], "action": "block"}}],
        "memory": {},
    })
    redact = spec.memory.effective_redact(spec.input_processors)
    assert (redact.kinds, redact.action) == (("email",), "redact")
    assert MemoryConfig().effective_redact(()) is None


# --- working memory schema, content and merge ----------------------------------------------------

GOOD_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["name"],
    "properties": {
        "name": {"type": "string", "description": "Name"},
        "age": {"type": "integer"},
        "tier": {"enum": ["free", "pro"]},
        "tags": {"type": "array", "items": {"type": "string"}},
        "prefs": {"type": "object", "additionalProperties": False, "properties": {
            "theme": {"type": "string"},
            "notify": {"type": "object", "properties": {"email": {"type": "boolean"}}},
        }},
    },
}


def test_schema_subset_accepts_good_schema():
    assert validate_working_memory_schema(GOOD_SCHEMA) is GOOD_SCHEMA


@pytest.mark.parametrize("bad", [
    {"type": "string"},
    {"type": "object", "properties": {"a": {"type": "null"}}},
    {"type": "object", "properties": {"a": {"type": "string", "pattern": "x"}}},
    {"type": "object", "properties": {"a": {"$ref": "#/x"}}},
    {"type": "object", "properties": {"a": {"type": "array", "items": {"type": "object"}}}},
    {"type": "object", "properties": {"a": {"type": "array"}}},
    {"type": "object", "properties": {"a": {"enum": []}}},
    {"type": "object", "properties": {"a": {"enum": [1, True]}, "b": {"enum": ["x", "x"]}}},
    {"type": "object", "properties": {"a": {"type": "string", "enum": [1]}}},
    {"type": "object", "additionalProperties": {"type": "string"}},
    {"type": "object", "properties": {"a": {"type": "object", "properties": {"b": {"type": "object", "properties": {
        "c": {"type": "object"}}}}}}},
    {"type": "object", "properties": {f"p{i}": {"type": "string"} for i in range(51)}},
    {"type": "object", "properties": {"a": {"type": "string", "description": "x" * 9000}}},
])
def test_schema_subset_rejects(bad):
    with pytest.raises(WorkingMemoryInvalid):
        validate_working_memory_schema(bad)
    with pytest.raises(ValidationError):
        WorkingMemoryConfig(json_schema=bad)


def test_depth_three_is_allowed():
    validate_working_memory_schema({"type": "object", "properties": {"a": {"type": "object", "properties": {
        "b": {"type": "object", "properties": {"c": {"type": "string"}}}}}}})


@pytest.mark.parametrize("content, ok", [
    ({}, True),  # required is ignored: the note starts empty
    ({"name": "Ada", "age": 36, "tier": "pro", "tags": ["a"], "prefs": {"notify": {"email": True}}}, True),
    ({"age": 36.0}, True),
    ({"extra": {"anything": [1, None]}}, True),  # root allows additional properties
    ({"age": "36"}, False),
    ({"age": True}, False),
    ({"tier": "gold"}, False),
    ({"tags": [1]}, False),
    ({"prefs": {"unknown": 1}}, False),  # additionalProperties false
    ({"name": "x" * 17000}, False),
])
def test_content_validation(content, ok):
    if ok:
        assert validate_working_memory_content(GOOD_SCHEMA, content) is content
    else:
        with pytest.raises(WorkingMemoryInvalid):
            validate_working_memory_content(GOOD_SCHEMA, content)


def test_content_errors_never_echo_values():
    with pytest.raises(WorkingMemoryInvalid) as info:
        validate_working_memory_content(GOOD_SCHEMA, {"tier": "secret-value"})
    assert "secret-value" not in str(info.value) and info.value.path == "tier"


def test_text_content():
    assert validate_working_memory_text("# Note") == "# Note"
    with pytest.raises(WorkingMemoryInvalid):
        validate_working_memory_text("x" * 16385)
    with pytest.raises(WorkingMemoryInvalid):
        validate_working_memory_text({"a": 1})


@pytest.mark.parametrize("content, patch, expected", [
    ({}, {"a": 1}, {"a": 1}),
    (None, {"a": 1}, {"a": 1}),
    ({"a": 1, "b": 2}, {"a": None}, {"b": 2}),
    ({"a": 1}, {"missing": None}, {"a": 1}),
    ({"o": {"x": 1, "y": 2}}, {"o": {"y": 3, "z": 4}}, {"o": {"x": 1, "y": 3, "z": 4}}),
    ({"o": {"x": 1, "y": 2}}, {"o": {"x": None}}, {"o": {"y": 2}}),
    ({"l": [1, 2, 3]}, {"l": [4]}, {"l": [4]}),
    ({"o": "scalar"}, {"o": {"k": 1, "gone": None}}, {"o": {"k": 1}}),
    ({"o": {"k": 1}}, {"o": "scalar"}, {"o": "scalar"}),
    ({"d": {"e": {"f": 1, "g": 2}}}, {"d": {"e": {"g": None, "h": 3}}}, {"d": {"e": {"f": 1, "h": 3}}}),
    ({"a": 1}, {}, {"a": 1}),
])
def test_merge_semantics(content, patch, expected):
    before = json.dumps(content)
    patch_before = json.dumps(patch)
    assert merge_working_memory(content, patch) == expected
    assert json.dumps(content) == before and json.dumps(patch) == patch_before  # inputs untouched


def test_merge_rejects_non_object_patch():
    with pytest.raises(WorkingMemoryInvalid):
        merge_working_memory({}, ["a"])


# --- embedding policy ----------------------------------------------------------------------------


def test_policy_default_and_builtins():
    policy = EmbeddingModelPolicy.parse(None)
    assert (policy.resolve("test:hash").dimensions, policy.resolve("test:hash768").dimensions) == (1536, 768)
    with pytest.raises(EmbeddingModelNotAllowed, match="dimensions are unknown"):
        policy.resolve("test:other")
    with pytest.raises(EmbeddingModelNotAllowed, match="not allowed"):
        policy.resolve("openai:text-embedding-3-small")
    assert [m.id for m in policy.listed()] == ["test:hash", "test:hash768"]


def test_policy_parses_qwen_and_examples():
    policy = EmbeddingModelPolicy.parse(
        f"litellm:text-embedding-3-small=1536, litellm:nomic-embed-text=768 ,{QWEN}=1024,openai:*=1536"
    )
    assert policy.resolve(QWEN).dimensions == 1024
    assert policy.resolve(QWEN).name == "Qwen3-VL-Embedding-8B-mlx-8bit"
    assert policy.resolve("litellm:nomic-embed-text").dimensions == 768
    assert policy.resolve("openai:anything").dimensions == 1536
    assert not policy.allows("litellm", "qwen3-vl-embedding-8b-mlx-8bit")  # case-sensitive
    with pytest.raises(EmbeddingModelNotAllowed):
        policy.resolve("test:hash")


@pytest.mark.parametrize("bad", [
    "litellm:model",  # exact entry needs dims
    "litellm:model=512",
    "litellm:model=1024.0",
    "litellm:model=abc",
    "litellm=1024",
    ":model=1024",
    "*:*",
    "litellm:mod*el=1024",
    "litellm:a b=1024",
    "litellm:a=1024,litellm:a=768",
    "test:*,test:*",
])
def test_policy_parse_errors(bad):
    with pytest.raises(ValueError):
        EmbeddingModelPolicy.parse(bad)


def test_query_instruction():
    instructions = json.dumps({QWEN: "Given a question, retrieve passages that answer it"})
    policy = EmbeddingModelPolicy.from_env({
        "EAP_ALLOWED_EMBEDDING_MODELS": f"{QWEN}=1024,test:*",
        "EAP_EMBEDDING_QUERY_INSTRUCTIONS": instructions,
    })
    model = policy.resolve(QWEN)
    assert model.query_text("what is RAG?") == (
        "Instruct: Given a question, retrieve passages that answer it\nQuery: what is RAG?"
    )
    assert policy.resolve("test:hash").query_text("q") == "q"
    for bad in ("not json", "[]", json.dumps({"litellm:other": "x"}), json.dumps({QWEN: ""}), json.dumps({QWEN: 1})):
        with pytest.raises(ValueError):
            EmbeddingModelPolicy.parse(f"{QWEN}=1024", bad)


def test_dimension_check():
    model = EmbeddingModelPolicy.parse(None).resolve("test:hash")
    model.check_dimensions(1536)
    with pytest.raises(EmbeddingDimensionMismatch, match="returned 768-dimensional"):
        model.check_dimensions(768)


# --- reserved names, events, errors --------------------------------------------------------------


def test_reserved_tool_names_rejected_with_knowledge_or_memory():
    assert RESERVED_TOOL_NAMES == {"retrieve", "update_working_memory", "final_result"}  # final_result: AC.1
    plain = AgentSpec.model_validate({**MINIMAL, "tools": ["retrieve"]})  # stored specs keep validating
    from eap_schemas import reserved_tool_name_conflicts

    assert reserved_tool_name_conflicts(plain) == ("retrieve",)
    with pytest.raises(ValidationError, match="reserved"):
        AgentSpec.model_validate({**MINIMAL, "tools": ["retrieve"], "knowledge_bases": [{"id": KB1}]})
    with pytest.raises(ValidationError, match="reserved"):
        AgentSpec.model_validate({**MINIMAL, "tools": ["update_working_memory"], "memory": {}})


def test_new_event_types():
    for name in ("ingest_progress", "embedding", "memory_observe_failed", "memory_updated"):
        assert name in get_args(RunEventType)


@pytest.mark.parametrize("exc", [
    EmbeddingModelNotAllowed(QWEN),
    EmbeddingModelNotAllowed("test:x", "unknown_dimensions"),
    EmbeddingDimensionMismatch(QWEN, 1024, 4096),
    IngestBudgetExceeded(2_500_000, 2_000_000),
    KnowledgeBaseNotFound(KB1),
    WorkingMemoryInvalid("must be of type string", "prefs.theme"),
])
def test_errors_pickle(exc):
    restored = pickle.loads(pickle.dumps(exc))
    assert type(restored) is type(exc)
    assert str(restored) == str(exc)
    assert restored.args == exc.args
    assert vars(restored) == vars(exc)
    assert restored.code == exc.code
