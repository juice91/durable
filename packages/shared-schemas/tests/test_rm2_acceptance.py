"""RM.2 acceptance tests: hash stability, merge table, schema subset, embedding policy, limits,
reserved tool names, pickling and event types. Complements test_rm2_contracts.py; no DB."""

from __future__ import annotations

import copy
import json
import pickle
from typing import get_args

import pytest
from pydantic import ValidationError

import test_spec_hash_stability as stored
from eap_schemas import AgentSpec, AgentVersion, RunEventType, canonical_spec_hash
from eap_schemas.embedding_policy import (
    EmbeddingDimensionMismatch,
    EmbeddingModelNotAllowed,
    EmbeddingModelPolicy,
)
from eap_schemas.knowledge import (
    IngestBudgetExceeded,
    KnowledgeBaseNotFound,
    KnowledgeBaseRef,
    check_knowledge_base_refs,
)
from eap_schemas.memory import (
    MAX_SCHEMA_BYTES,
    MAX_TEMPLATE_BYTES,
    MAX_WORKING_MEMORY_BYTES,
    MemoryConfig,
    SemanticRecallConfig,
    WorkingMemoryConfig,
    WorkingMemoryInvalid,
    merge_working_memory,
    validate_working_memory_content,
    validate_working_memory_schema,
    validate_working_memory_text,
)

KB = [f"00000000-0000-0000-0000-{i:012d}" for i in range(1, 12)]
QWEN = "litellm:Qwen3-VL-Embedding-8B-mlx-8bit"
SECRET = "sk-SECRET-VALUE-12345"


# --- hash stability ------------------------------------------------------------------------------

STORED = [
    (stored.PRE_CHANGE_FULL_JSON, stored.FULL_HASH),
    (stored.PRE_CHANGE_MINIMAL_JSON, stored.MINIMAL_HASH),
    (stored.PRE_P02_MCP_JSON, stored.PRE_P02_MCP_HASH),
]


@pytest.mark.parametrize(("spec_json", "spec_hash"), STORED)
def test_stored_fixture_keeps_json_and_hash(spec_json, spec_hash):
    spec = AgentSpec.model_validate(spec_json)
    assert spec.model_dump(mode="json") == spec_json
    assert json.loads(spec.model_dump_json()) == spec_json
    assert canonical_spec_hash(spec) == spec_hash
    for name in ("knowledge_bases", "memory"):
        assert name not in spec.model_dump()


@pytest.mark.parametrize(("spec_json", "spec_hash"), STORED)
def test_stored_fixture_round_trips_through_agent_version(spec_json, spec_hash):
    version = AgentVersion.model_validate({
        "agent_id": "00000000-0000-0000-0000-000000000001", "tenant_id": "t1", "version": 1,
        "spec": spec_json, "spec_hash": spec_hash, "created_at": "2026-11-01T00:00:00+00:00",
    })
    assert canonical_spec_hash(version.spec) == spec_hash


def test_memory_subconfigs_absent_equal_explicit_nones():
    nones = {"history": None, "working": None, "observational": None, "semantic_recall": None, "redact": None}
    absent = AgentSpec.model_validate({**stored.FULL, "memory": {}})
    explicit = AgentSpec.model_validate({**stored.FULL, "memory": nones})
    assert canonical_spec_hash(absent) == canonical_spec_hash(explicit)
    assert absent.model_dump_json() == explicit.model_dump_json()
    # memory={} turns memory on, so it differs from no memory at all
    assert canonical_spec_hash(absent) != stored.FULL_HASH


def test_nested_optional_none_does_not_change_hash():
    base = {**stored.FULL, "knowledge_bases": [{"id": KB[0]}], "memory": {"history": {"last_turns": 5}}}
    withnone = {
        **stored.FULL,
        "knowledge_bases": [{"id": KB[0], "min_score": None, "filter": None}],
        "memory": {"history": {"last_turns": 5, "max_tokens": None}, "working": None},
    }
    assert canonical_spec_hash(AgentSpec.model_validate(base)) == canonical_spec_hash(AgentSpec.model_validate(withnone))


def test_empty_knowledge_bases_is_omitted():
    spec = AgentSpec.model_validate({**stored.FULL, "knowledge_bases": ()})
    assert "knowledge_bases" not in spec.model_dump(mode="json")
    assert canonical_spec_hash(spec) == stored.FULL_HASH


# --- merge_working_memory ------------------------------------------------------------------------

MERGE_CASES = [
    pytest.param({"a": 1}, {"b": 2}, {"a": 1, "b": 2}, id="adds-key"),
    pytest.param({"a": 1, "b": 2}, {"a": None}, {"b": 2}, id="null-deletes"),
    pytest.param({"a": 1}, {"zz": None}, {"a": 1}, id="null-on-missing-noop"),
    pytest.param({"a": {"x": 1, "y": 2}}, {"a": {"y": 3, "z": 4}}, {"a": {"x": 1, "y": 3, "z": 4}}, id="deep-merge"),
    pytest.param({"a": {"x": 1, "y": 2}}, {"a": {"x": None}}, {"a": {"y": 2}}, id="nested-delete"),
    pytest.param({"a": {"b": {"c": 1, "d": 2}}}, {"a": {"b": {"c": None}}}, {"a": {"b": {"d": 2}}}, id="deep-nested-delete"),
    pytest.param({"a": {"x": 1}}, {"a": None}, {}, id="delete-whole-object"),
    pytest.param({"l": [1, 2, 3]}, {"l": [9]}, {"l": [9]}, id="array-replaces"),
    pytest.param({"l": [1, 2]}, {"l": []}, {"l": []}, id="array-replaced-by-empty"),
    pytest.param({"a": 1}, {"a": "s"}, {"a": "s"}, id="scalar-replaces"),
    pytest.param({"a": {"x": 1}}, {"a": 5}, {"a": 5}, id="scalar-over-object"),
    pytest.param({"a": 5}, {"a": {"x": 1}}, {"a": {"x": 1}}, id="object-over-scalar"),
    pytest.param({"a": 5}, {"a": {"x": 1, "y": None}}, {"a": {"x": 1}}, id="new-object-strips-nulls"),
    pytest.param({"a": 1}, {}, {"a": 1}, id="empty-patch"),
    pytest.param(None, {"a": 1}, {"a": 1}, id="none-content"),
    pytest.param({"l": [1]}, {"l": [None, 2]}, {"l": [None, 2]}, id="null-inside-array-kept"),
    pytest.param({"a": 1}, {"b": False, "c": 0}, {"a": 1, "b": False, "c": 0}, id="falsy-scalars"),
]


@pytest.mark.parametrize(("content", "patch", "expected"), MERGE_CASES)
def test_merge_table(content, patch, expected):
    assert merge_working_memory(content, patch) == expected


@pytest.mark.parametrize(("content", "patch", "expected"), MERGE_CASES)
def test_merge_does_not_mutate_inputs(content, patch, expected):
    c0, p0 = copy.deepcopy(content), copy.deepcopy(patch)
    result = merge_working_memory(content, patch)
    assert content == c0
    assert patch == p0
    assert result is not content


def test_merge_result_shares_no_structure_with_inputs():
    content = {"a": {"x": [1]}}
    patch = {"b": {"y": [2]}, "l": [3]}
    result = merge_working_memory(content, patch)
    result["a"]["x"].append(9)
    result["b"]["y"].append(9)
    result["l"].append(9)
    assert content == {"a": {"x": [1]}}
    assert patch == {"b": {"y": [2]}, "l": [3]}


@pytest.mark.parametrize("patch", [None, [], "x", 3])
def test_merge_rejects_non_object_patch(patch):
    with pytest.raises(WorkingMemoryInvalid):
        merge_working_memory({}, patch)


# --- schema subset validator ---------------------------------------------------------------------


def _obj(**props):
    return {"type": "object", "properties": props}


def _nest(levels):
    node = {"type": "string"}
    for i in range(levels):
        node = _obj(**{f"k{i}": node})
    return node


def test_depth_limit_is_three_object_levels():
    validate_working_memory_schema(_nest(3))  # root + 2 nested = depth 3
    with pytest.raises(WorkingMemoryInvalid, match="at most 3 deep"):
        validate_working_memory_schema(_nest(4))


def test_depth_error_names_the_path():
    with pytest.raises(WorkingMemoryInvalid) as err:
        validate_working_memory_schema(_nest(4))
    assert err.value.path == "k3.k2.k1"
    assert "k3.k2.k1" in str(err.value)


@pytest.mark.parametrize("kw", ["pattern", "minimum", "$ref", "oneOf", "allOf", "default", "format", "minLength"])
def test_unsupported_keywords_rejected_with_path(kw):
    schema = _obj(name={"type": "string", kw: "x"})
    with pytest.raises(WorkingMemoryInvalid, match="unsupported keyword") as err:
        validate_working_memory_schema(schema)
    assert err.value.path == "name"
    assert f"'{kw}'" in str(err.value)


@pytest.mark.parametrize("bad", [
    {"type": "array", "items": {"type": "object", "properties": {}}},
    {"type": "null"},
    {"type": "object", "properties": {"p": {"type": "array"}}},
    {"type": "string", "enum": ["a", "a"]},
])
def test_other_unsupported_shapes(bad):
    with pytest.raises(WorkingMemoryInvalid):
        validate_working_memory_schema(_obj(p=bad))


def test_schema_size_cap():
    ok = _obj(a={"type": "string", "description": "x" * (MAX_SCHEMA_BYTES - 200)})
    validate_working_memory_schema(ok)
    big = _obj(a={"type": "string", "description": "x" * MAX_SCHEMA_BYTES})
    with pytest.raises(WorkingMemoryInvalid, match="at most 8192 bytes"):
        validate_working_memory_schema(big)


def test_schema_size_cap_counts_utf8_bytes():
    big = _obj(a={"type": "string", "description": "é" * (MAX_SCHEMA_BYTES // 2)})
    with pytest.raises(WorkingMemoryInvalid):
        validate_working_memory_schema(big)


def test_template_size_cap_in_bytes():
    WorkingMemoryConfig(template="x" * MAX_TEMPLATE_BYTES)
    with pytest.raises(ValidationError, match="4096 bytes"):
        WorkingMemoryConfig(template="x" * (MAX_TEMPLATE_BYTES + 1))
    with pytest.raises(ValidationError):
        WorkingMemoryConfig(template="é" * (MAX_TEMPLATE_BYTES // 2 + 1))
    with pytest.raises(ValidationError):
        WorkingMemoryConfig(template="   ")


def test_exactly_one_of_template_and_schema():
    with pytest.raises(ValidationError, match="not both"):
        WorkingMemoryConfig(template="# x", json_schema=_obj(a={"type": "string"}))
    assert WorkingMemoryConfig().effective_template()
    assert WorkingMemoryConfig(json_schema=_obj(a={"type": "string"})).effective_template() is None


def test_content_size_cap_16kib():
    schema = _obj(a={"type": "string"})
    validate_working_memory_content(schema, {"a": "x" * (MAX_WORKING_MEMORY_BYTES - 20)})
    with pytest.raises(WorkingMemoryInvalid, match="16384 bytes"):
        validate_working_memory_content(schema, {"a": "x" * MAX_WORKING_MEMORY_BYTES})
    validate_working_memory_text("x" * MAX_WORKING_MEMORY_BYTES)
    with pytest.raises(WorkingMemoryInvalid, match="16384 bytes"):
        validate_working_memory_text("x" * (MAX_WORKING_MEMORY_BYTES + 1))


def test_errors_name_path_never_value():
    schema = _obj(profile=_obj(age={"type": "integer"}, mood={"enum": ["happy", "sad"]}))
    cases = [
        {"profile": {"age": SECRET}},
        {"profile": {"mood": SECRET}},
        {"profile": SECRET},
    ]
    for content in cases:
        with pytest.raises(WorkingMemoryInvalid) as err:
            validate_working_memory_content(schema, content)
        assert SECRET not in str(err.value)
        assert SECRET not in repr(err.value)
        assert "profile" in str(err.value)
    with pytest.raises(WorkingMemoryInvalid) as err:
        validate_working_memory_content(schema, {"profile": {"age": SECRET}})
    assert err.value.path == "profile.age"


def test_secret_in_schema_values_not_echoed_in_unsupported_error():
    schema = _obj(a={"type": "string", "pattern": SECRET})
    with pytest.raises(WorkingMemoryInvalid) as err:
        validate_working_memory_schema(schema)
    assert SECRET not in str(err.value)
    strict = {**_obj(a={"type": "string"}), "additionalProperties": False}
    with pytest.raises(WorkingMemoryInvalid) as err2:
        validate_working_memory_content(strict, {"a": "x", "extra": SECRET})
    assert SECRET not in str(err2.value)


# --- EmbeddingModelPolicy ------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [
    f"{QWEN}=4096",
    f"{QWEN}=512",
    f"{QWEN}=0",
    f"{QWEN}=1024.0",
    f"{QWEN}=abc",
    f"{QWEN}=",
    "litellm:*=512",
    f"{QWEN}",  # exact needs dims
    ":x=768",  # missing provider
    "x=768",  # no provider separator
    "Litellm:x=768",  # provider must be lower-case
    f"{QWEN}=1024,{QWEN}=768",  # duplicate exact
    "litellm:*,litellm:*=768",  # duplicate wildcard
    "litellm:a b=768",  # whitespace in name
    "litellm:a*b=768",  # partial wildcard
])
def test_parse_errors(bad):
    with pytest.raises(ValueError):
        EmbeddingModelPolicy.parse(bad)


def test_parse_tolerates_surrounding_whitespace_and_empty_entries():
    policy = EmbeddingModelPolicy.parse(f"  {QWEN}=1024 , , test:*  ")
    assert policy.dimensions("litellm", "Qwen3-VL-Embedding-8B-mlx-8bit") == 1024
    assert policy.allows("test", "hash")


def test_qwen_entry_parses_and_resolves():
    policy = EmbeddingModelPolicy.parse(f"{QWEN}=1024")
    model = policy.resolve(QWEN)
    assert (model.provider, model.name, model.dimensions, model.id) == ("litellm", "Qwen3-VL-Embedding-8B-mlx-8bit", 1024, QWEN)
    assert model.query_instruction is None
    with pytest.raises(EmbeddingModelNotAllowed) as err:
        policy.resolve("litellm:other-model")
    assert err.value.code == "embedding_model_not_allowed"
    with pytest.raises(EmbeddingModelNotAllowed):
        policy.resolve("litellm:qwen3-vl-embedding-8b-mlx-8bit")  # case-sensitive
    with pytest.raises(EmbeddingModelNotAllowed):
        policy.resolve("no-colon")


@pytest.mark.parametrize("dims", [768, 1024, 1536])
def test_allowed_dimensions_parse(dims):
    assert EmbeddingModelPolicy.parse(f"p:m={dims}").resolve("p:m").dimensions == dims


def test_wildcard_without_known_dims_cannot_resolve():
    policy = EmbeddingModelPolicy.parse("litellm:*")
    assert policy.allows("litellm", "anything")
    with pytest.raises(EmbeddingModelNotAllowed) as err:
        policy.resolve("litellm:anything")
    assert err.value.reason == "unknown_dimensions"
    assert EmbeddingModelPolicy.parse("litellm:*=768").resolve("litellm:anything").dimensions == 768


def test_exact_entry_wins_over_wildcard():
    policy = EmbeddingModelPolicy.parse("litellm:*=768,litellm:big=1536")
    assert policy.resolve("litellm:big").dimensions == 1536
    assert policy.resolve("litellm:small").dimensions == 768


def test_from_env_allowlisted_instruction():
    env = {
        "EAP_ALLOWED_EMBEDDING_MODELS": f"{QWEN}=1024",
        "EAP_EMBEDDING_QUERY_INSTRUCTIONS": json.dumps({QWEN: "Retrieve relevant passages"}),
    }
    model = EmbeddingModelPolicy.from_env(env).resolve(QWEN)
    assert model.query_text("how do I reset?") == "Instruct: Retrieve relevant passages\nQuery: how do I reset?"


def test_from_env_instruction_for_unlisted_key_errors():
    env = {
        "EAP_ALLOWED_EMBEDDING_MODELS": f"{QWEN}=1024",
        "EAP_EMBEDDING_QUERY_INSTRUCTIONS": json.dumps({"litellm:not-allowed": "task"}),
    }
    with pytest.raises(ValueError, match="does not allow"):
        EmbeddingModelPolicy.from_env(env)


@pytest.mark.parametrize("raw", ["not json", "[1]", '{"litellm:x": 3}', '{"litellm:x": "  "}', '{"bad": "t"}'])
def test_from_env_malformed_instructions_error(raw):
    env = {"EAP_ALLOWED_EMBEDDING_MODELS": "litellm:x=768", "EAP_EMBEDDING_QUERY_INSTRUCTIONS": raw}
    with pytest.raises(ValueError):
        EmbeddingModelPolicy.from_env(env)


def test_query_text_plain_without_instruction():
    env = {"EAP_ALLOWED_EMBEDDING_MODELS": f"{QWEN}=1024"}
    model = EmbeddingModelPolicy.from_env(env).resolve(QWEN)
    assert model.query_text("plain question") == "plain question"
    default = EmbeddingModelPolicy.from_env({}).resolve("test:hash")
    assert default.query_text("q") == "q"
    assert default.dimensions == 1536


def test_dimension_mismatch_guard():
    model = EmbeddingModelPolicy.parse(f"{QWEN}=1024").resolve(QWEN)
    model.check_dimensions(1024)
    with pytest.raises(EmbeddingDimensionMismatch):
        model.check_dimensions(4096)


# --- limits --------------------------------------------------------------------------------------


@pytest.mark.parametrize("top_k", [1, 8, 20])
def test_top_k_in_range(top_k):
    assert KnowledgeBaseRef(id=KB[0], top_k=top_k).top_k == top_k


@pytest.mark.parametrize("top_k", [0, -1, 21, 100])
def test_top_k_out_of_range(top_k):
    with pytest.raises(ValidationError):
        KnowledgeBaseRef(id=KB[0], top_k=top_k)


def test_at_most_eight_knowledge_bases():
    ok = [{"id": k} for k in KB[:8]]
    assert len(AgentSpec.model_validate({**stored.MINIMAL, "knowledge_bases": ok}).knowledge_bases) == 8
    with pytest.raises(ValidationError):
        AgentSpec.model_validate({**stored.MINIMAL, "knowledge_bases": [{"id": k} for k in KB[:9]]})
    with pytest.raises(ValueError, match="at most 8"):
        check_knowledge_base_refs(tuple(KnowledgeBaseRef(id=k) for k in KB[:9]))


def test_duplicate_kb_ids_rejected_even_with_different_case():
    with pytest.raises(ValidationError, match="duplicate"):
        AgentSpec.model_validate({**stored.MINIMAL, "knowledge_bases": [{"id": KB[0]}, {"id": KB[0].upper()}]})


@pytest.mark.parametrize("score", [0.0, 0.5, 1.0])
def test_min_score_in_range(score):
    assert KnowledgeBaseRef(id=KB[0], min_score=score).min_score == score
    assert SemanticRecallConfig(embedding_model="test:hash", min_score=score).min_score == score


@pytest.mark.parametrize("score", [-0.01, 1.01])
def test_min_score_out_of_range(score):
    with pytest.raises(ValidationError):
        KnowledgeBaseRef(id=KB[0], min_score=score)
    with pytest.raises(ValidationError):
        SemanticRecallConfig(embedding_model="test:hash", min_score=score)


def test_other_memory_thresholds():
    for bad in ({"top_k": 0}, {"top_k": 11}, {"message_range": 4}, {"message_range": -1}):
        with pytest.raises(ValidationError):
            SemanticRecallConfig(embedding_model="test:hash", **bad)
    with pytest.raises(ValidationError):
        MemoryConfig.model_validate({"history": {"last_turns": 0}})
    with pytest.raises(ValidationError):
        MemoryConfig.model_validate({"history": {"last_turns": 201}})


def test_filter_key_limits():
    ok = {f"k{i}": "v" for i in range(16)}
    assert len(KnowledgeBaseRef(id=KB[0], filter=ok).filter) == 16
    bad_filters = [
        {f"k{i}": "v" for i in range(17)},
        {"bad key": "v"},
        {"k" * 65: "v"},
        {"": "v"},
        {"k": []},
        {"k": ["v"] * 33},
        {"k": {"nested": 1}},
        {"k": "x" * 257},
        {"k": float("nan")},
    ]
    for bad in bad_filters:
        with pytest.raises(ValidationError):
            KnowledgeBaseRef(id=KB[0], filter=bad)
    assert KnowledgeBaseRef(id=KB[0], filter={"k": ["v"] * 32}).filter == {"k": ("v",) * 32}
    assert KnowledgeBaseRef(id=KB[0], filter={}).filter is None


# --- reserved tool names -------------------------------------------------------------------------

KB_ON = {"knowledge_bases": [{"id": KB[0]}]}
MEMORY_ON = {"memory": {}}


@pytest.mark.parametrize("extra", [KB_ON, MEMORY_ON, {**KB_ON, **MEMORY_ON}])
@pytest.mark.parametrize("name", ["retrieve", "update_working_memory"])
def test_builtin_named_like_platform_tool_rejected(extra, name):
    with pytest.raises(ValidationError, match="reserved"):
        AgentSpec.model_validate({**stored.MINIMAL, "tools": [name], **extra})


def test_dotted_builtin_flattening_to_reserved_name_rejected():
    # builtin_tool_name turns dots into "__"; no dotted id can equal "retrieve", but plain ids can
    with pytest.raises(ValidationError, match="reserved"):
        AgentSpec.model_validate({**stored.MINIMAL, "tools": ["clock.now", "retrieve"], **KB_ON})


def test_mcp_tool_named_retrieve_is_namespaced_and_accepted():
    # MCP model-facing names are prefixed (mcp__server__tool), so tool "retrieve" cannot collide
    spec = AgentSpec.model_validate({**stored.MINIMAL, "mcp_tools": [{"server": "docs", "tool": "retrieve"}], **KB_ON, **MEMORY_ON})
    from eap_schemas import reserved_tool_name_conflicts

    assert reserved_tool_name_conflicts(spec) == ()


@pytest.mark.parametrize("name", ["retrieve", "update_working_memory"])
def test_same_tool_accepted_without_knowledge_or_memory(name):
    spec = AgentSpec.model_validate({**stored.MINIMAL, "tools": [name]})
    assert spec.tools == (name,)
    AgentSpec.model_validate({**stored.MINIMAL, "tools": [name], "knowledge_bases": [], "memory": None})


# --- pickling and events -------------------------------------------------------------------------

EXCEPTIONS = [
    EmbeddingModelNotAllowed(QWEN),
    EmbeddingModelNotAllowed("litellm:x", "unknown_dimensions"),
    EmbeddingDimensionMismatch(QWEN, 1024, 4096),
    IngestBudgetExceeded(2_500_000, 2_000_000),
    KnowledgeBaseNotFound(KB[0]),
    WorkingMemoryInvalid("must be of type string", "prefs.theme"),
    WorkingMemoryInvalid("the schema must be a JSON object"),
]


@pytest.mark.parametrize("exc", EXCEPTIONS, ids=lambda e: f"{type(e).__name__}-{e.args[-1]}")
def test_exception_pickle_round_trip(exc):
    for protocol in range(2, pickle.HIGHEST_PROTOCOL + 1):
        restored = pickle.loads(pickle.dumps(exc, protocol=protocol))
        assert type(restored) is type(exc)
        assert str(restored) == str(exc)
        assert restored.code == exc.code
        assert restored.args == exc.args


def test_exceptions_have_distinct_stable_codes():
    codes = {type(e).__name__: e.code for e in EXCEPTIONS}
    assert codes == {
        "EmbeddingModelNotAllowed": "embedding_model_not_allowed",
        "EmbeddingDimensionMismatch": "embedding_dimension_mismatch",
        "IngestBudgetExceeded": "ingest_budget_exceeded",
        "KnowledgeBaseNotFound": "knowledge_base_not_found",
        "WorkingMemoryInvalid": "working_memory_invalid",
    }


@pytest.mark.parametrize("name", ["ingest_progress", "embedding", "memory_observe_failed", "memory_updated"])
def test_new_run_event_types_parse(name):
    from eap_schemas import RunEvent

    assert name in get_args(RunEventType)
    event = RunEvent.model_validate({
        "run_id": "00000000-0000-0000-0000-000000000002", "seq": 1,
        "type": name, "data": {"n": 1}, "ts": "2026-11-13T00:00:00+00:00",
    })
    assert event.type == name


# --- review fixes (RM.2 Minor findings) ----------------------------------------------------------


def _deep(levels: int) -> dict:
    node: dict = {"leaf": 1}
    for _ in range(levels):
        node = {"n": node}
    return node


def test_deep_patch_is_rejected_not_recursion_error():
    with pytest.raises(WorkingMemoryInvalid, match="nests deeper"):
        merge_working_memory({}, _deep(3000))
    with pytest.raises(WorkingMemoryInvalid, match="nests deeper"):
        merge_working_memory(_deep(3000), {"a": 1})


def test_deep_content_and_schema_are_rejected_not_recursion_error():
    with pytest.raises(WorkingMemoryInvalid, match="nests deeper"):
        validate_working_memory_content(_obj(), _deep(3000))
    with pytest.raises(WorkingMemoryInvalid, match="nests deeper"):
        validate_working_memory_schema({"type": "object", "properties": _deep(3000)})


def test_size_check_runs_before_walking_content():
    schema = _obj(a={"type": "integer"})
    # Over the size cap AND type-invalid: the size error wins (checked first).
    with pytest.raises(WorkingMemoryInvalid, match="16384 bytes"):
        validate_working_memory_content(schema, {"a": "x" * (MAX_WORKING_MEMORY_BYTES + 1)})


def test_model_written_keys_are_never_echoed():
    secret_key = "alice@example.com"
    closed = {**_obj(prefs={**_obj(theme={"type": "string"}), "additionalProperties": False})}
    with pytest.raises(WorkingMemoryInvalid) as err:
        validate_working_memory_content(closed, {"prefs": {secret_key: "x"}})
    assert secret_key not in str(err.value) and secret_key not in repr(err.value)
    assert err.value.path == "prefs" and "unknown property" in str(err.value)
    # Additional (non-schema) keys: the path stops at the first non-schema key.
    with pytest.raises(WorkingMemoryInvalid) as err:
        validate_working_memory_content(_obj(), {secret_key: {"nested": float("nan")}})
    assert secret_key not in str(err.value) and secret_key not in repr(err.value)
    assert err.value.path == "(root)"


@pytest.mark.parametrize("raw", [",", ",,", " , , "])
def test_all_empty_policy_list_is_rejected(raw):
    with pytest.raises(ValueError, match="no entries"):
        EmbeddingModelPolicy.parse(raw)
