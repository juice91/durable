"""AC.1 contracts: structured output, approvals, limits, fallback models, evals (implementer tests)."""

from __future__ import annotations

import hashlib
import json
import pickle
import uuid
from decimal import Decimal

import pytest
from pydantic import TypeAdapter, ValidationError

from eap_schemas import (
    RESERVED_TOOL_NAMES,
    AgentSpec,
    AgentVersion,
    OutputInvalid,
    RunBudgetExceeded,
    SchemaSubsetInvalid,
    approval_required_tool_names,
    canonical_spec_hash,
    mcp_tool_name,
    reserved_tool_name_conflicts,
)
from eap_schemas.approvals import denial_message
from eap_schemas.evals import (
    EvalCase,
    EvalSuiteInvalid,
    EvalSuiteSnapshot,
    EvaluatorSpec,
    JudgeVerdict,
    field_path_value,
)
from eap_schemas.json_schema_subset import validate, validate_output_schema
from eap_schemas.limits import UsageLimitsSpec

FULL = {
    "name": "Support triage",
    "instructions": "Open a ticket.",
    "model": {"provider": "test", "name": "ticket"},
    "tools": ["tickets.create", "clock.now"],
    "model_settings": {"temperature": 0.2},
}
MINIMAL = {"name": "Minimal", "model": {"provider": "test", "name": "echo"}}
FULL_HASH = "6a08ca8b955f04c20ebb3250e02563ace304ab65f997d56d094c4b82a1b8ee71"
MINIMAL_HASH = "a353a875819982f6ad02d6ae7bc416cd573402ed78c8a84e32211db1dacc3a1c"
SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}


def _sha(obj: object) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# --- spec stability --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        {"output": None},
        {"approvals": None},
        {"limits": None},
        {"limits": {}},
        {"limits": {"max_requests": None, "max_cost_usd": None}},
        {"fallback_models": []},
        {"output": None, "approvals": None, "limits": {}, "fallback_models": []},
    ],
)
def test_empty_new_fields_keep_json_and_hash(extra):
    for base, expected in ((FULL, FULL_HASH), (MINIMAL, MINIMAL_HASH)):
        spec = AgentSpec.model_validate({**base, **extra})
        assert canonical_spec_hash(spec) == expected
        dumped = spec.model_dump(mode="json")
        for name in ("output", "approvals", "limits", "fallback_models"):
            assert name not in dumped
            assert name not in json.loads(spec.model_dump_json())


def test_explicit_nones_inside_sub_configs_hash_like_absent():
    pairs = [
        ({"output": {"json_schema": SCHEMA}},
         {"output": {"json_schema": SCHEMA, "mode": None, "name": None, "description": None, "max_retries": None}}),
        ({"limits": {"max_requests": 5}},
         {"limits": {"max_requests": 5, "max_tool_calls": None, "max_cost_usd": None, "max_total_tokens": None}}),
        ({"approvals": {"tools": ["tickets.create"]}},
         {"approvals": {"tools": ["tickets.create"], "timeout_hours": None}}),
    ]
    for short, explicit in pairs:
        a = AgentSpec.model_validate({**FULL, **short})
        b = AgentSpec.model_validate({**FULL, **explicit})
        assert canonical_spec_hash(a) == canonical_spec_hash(b) != FULL_HASH
        assert a.model_dump(mode="json") == b.model_dump(mode="json") == json.loads(a.model_dump_json())
        key = next(iter(short))
        assert a.model_dump(mode="json")[key] == json.loads(json.dumps(short[key]))


def test_stored_version_with_new_fields_round_trips():
    spec_json = {
        **AgentSpec.model_validate(FULL).model_dump(mode="json"),
        "output": {"json_schema": SCHEMA, "mode": "native"},
        "approvals": {"tools": ["tickets.create"], "timeout_hours": 2},
        "limits": {"max_requests": 3, "max_cost_usd": "0.5"},
        "fallback_models": [{"provider": "test", "name": "echo"}],
    }
    version = AgentVersion.model_validate({
        "agent_id": str(uuid.uuid4()), "tenant_id": "t1", "version": 1, "spec": spec_json,
        "spec_hash": _sha(spec_json), "created_at": "2026-11-15T00:00:00+00:00",
    })
    assert version.spec.model_dump(mode="json") == spec_json


def test_cost_cap_is_normalised_so_equal_amounts_hash_the_same():
    hashes = {
        canonical_spec_hash(AgentSpec.model_validate({**MINIMAL, "limits": {"max_cost_usd": v}}))
        for v in ("0.5", "0.50", "0.5000", 0.5, Decimal("5E-1"))
    }
    assert len(hashes) == 1
    assert AgentSpec.model_validate({**MINIMAL, "limits": {"max_cost_usd": "1E+3"}}).model_dump(mode="json")[
        "limits"] == {"max_cost_usd": "1000"}
    for bad in ("0", "0.00009", "1000.0001", "0.12345", "NaN", "Infinity"):
        with pytest.raises(ValidationError):
            UsageLimitsSpec(max_cost_usd=bad)


def test_usage_limits_kwargs():
    limits = UsageLimitsSpec(max_requests=3, max_total_tokens=100, max_cost_usd="1")
    assert limits.usage_limits_kwargs() == {"request_limit": 3, "total_tokens_limit": 100}
    for field, bad in (("max_requests", 201), ("max_tool_calls", 501), ("max_input_tokens", 10_000_001),
                       ("max_requests", 0)):
        with pytest.raises(ValidationError):
            UsageLimitsSpec(**{field: bad})


# --- JSON Schema subset ----------------------------------------------------------------------------


def _nest(levels: int) -> dict:
    node: dict = {"type": "string"}
    for _ in range(levels):
        node = {"type": "object", "properties": {"n": node}}
    return node


def _props(count: int) -> dict:
    return {"type": "object", "properties": {f"p{i}": {"type": "string"} for i in range(count)}}


ACCEPTED = [
    SCHEMA,
    _nest(5),
    _props(100),
    {"type": "object", "properties": {
        "a": {"type": ["string", "null"], "minLength": 1, "maxLength": 9, "format": "email"},
        "b": {"type": "integer", "minimum": 0, "maximum": 10},
        "c": {"type": "array", "items": {"type": "object", "properties": {"x": {"type": "number"}}}, "minItems": 1},
        "d": {"enum": ["x", "y", None]},
        "e": {"const": "fixed"},
        "f": {"type": "null"},
        "g": {"type": "array", "items": {"type": "array", "items": {"type": "boolean"}}},
        "h": {"type": ["object", "null"], "properties": {}, "additionalProperties": False},
    }, "required": ["a", "b"], "additionalProperties": False, "$schema": "https://json-schema.org/draft/2020-12/schema"},
]

REJECTED = [
    ({"type": "object", "properties": {"a": {"$ref": "#/$defs/x"}}}, "'$ref'"),
    ({"type": "object", "$defs": {}}, "'$defs'"),
    ({"type": "object", "properties": {"a": {"type": "string", "pattern": "^a+$"}}}, "'pattern'"),
    ({"type": "object", "patternProperties": {}}, "'patternProperties'"),
    ({"type": "object", "properties": {"a": {"anyOf": [{"type": "string"}]}}}, "'anyOf'"),
    ({"type": "object", "properties": {"a": {"oneOf": []}}}, "'oneOf'"),
    ({"type": "object", "properties": {"a": {"allOf": []}}}, "'allOf'"),
    ({"type": "object", "properties": {"a": {"not": {}}}}, "'not'"),
    (_nest(6), "at most 5 deep"),
    (_props(101), "at most 100 properties"),
    ({"type": "array", "items": {"type": "string"}}, 'root must have "type": "object"'),
    ({"type": ["object", "null"]}, 'root must have "type": "object"'),
    ({"type": "object", "description": "x" * (17 * 1024)}, "at most 16384 bytes"),
    ({"type": "object", "properties": {"a": {"type": ["string", "integer"]}}}, '"type" must be one type'),
    ({"type": "object", "properties": {"a": {"type": "array"}}}, 'needs "items"'),
    ({"type": "object", "properties": {"a": {"type": "string", "minimum": 1}}}, "'minimum' on a string"),
    ({"type": "object", "properties": {"a": {"type": "integer", "minimum": 5, "maximum": 1}}}, "not be greater"),
    ({"type": "object", "properties": {}, "required": ["ghost"]}, "declared"),
    ({"type": "object", "additionalProperties": {"type": "string"}}, "true or false"),
    ({"type": "object", "properties": {"a": {"enum": [1, True, {"x": 1}]}}}, '"enum" values must be'),
    ({"type": "object", "properties": {"a": {"enum": ["x", "x"]}}}, "unique"),
    ({"type": "object", "properties": {"a": {"const": {"x": 1}}}}, '"const" must be'),
    ({"type": "object", "properties": {"a": {}}}, 'needs a "type"'),
    ({"type": "object", "properties": {"a": {"type": "string", "format": 1}}}, '"format" must be a string'),
    ("not a dict", "JSON object"),
]


@pytest.mark.parametrize("schema", ACCEPTED)
def test_subset_accepts(schema):
    assert validate_output_schema(schema) is schema


@pytest.mark.parametrize(("schema", "message"), REJECTED)
def test_subset_rejects(schema, message):
    with pytest.raises(SchemaSubsetInvalid) as info:
        validate_output_schema(schema)
    assert message in str(info.value)


def test_subset_parameters_and_messages_never_echo_values():
    validate({"type": "object", "properties": {"a": _nest(1)}}, max_depth=2)
    with pytest.raises(SchemaSubsetInvalid):
        validate({"type": "object", "properties": {"a": _nest(2)}}, max_depth=2)
    validate({"type": "string"}, root_object=False)
    with pytest.raises(SchemaSubsetInvalid) as info:
        validate({"type": "object", "properties": {"secret": {"type": "string", "minLength": "s3cr3t-value"}}})
    assert "s3cr3t-value" not in str(info.value) and info.value.path == "secret"


def test_output_spec_rejects_invalid_schema_and_bad_fields():
    for bad in (
        {"json_schema": {"type": "string"}},
        {"json_schema": SCHEMA, "mode": "json"},
        {"json_schema": SCHEMA, "name": "has space"},
        {"json_schema": SCHEMA, "name": "x" * 65},
        {"json_schema": SCHEMA, "max_retries": 6},
        {"json_schema": SCHEMA, "description": "d" * 1001},
    ):
        with pytest.raises(ValidationError):
            AgentSpec.model_validate({**MINIMAL, "output": bad})
    output = AgentSpec.model_validate({**MINIMAL, "output": {"json_schema": SCHEMA}}).output
    assert (output.effective_mode(), output.effective_name(), output.effective_max_retries()) == ("tool", "output", 2)


# --- reserved names, approvals, fallbacks ----------------------------------------------------------


def test_final_result_is_reserved_when_output_is_set():
    assert "final_result" in RESERVED_TOOL_NAMES
    plain = AgentSpec.model_validate({**MINIMAL, "tools": ["final_result"]})  # stored specs keep validating
    assert reserved_tool_name_conflicts(plain) == ("final_result",)
    with pytest.raises(ValidationError, match="reserved"):
        AgentSpec.model_validate({**MINIMAL, "tools": ["final_result"], "output": {"json_schema": SCHEMA}})


def test_approval_refs_must_name_tools_of_the_spec():
    spec = AgentSpec.model_validate({
        **FULL, "mcp_tools": [{"server": "crm", "tool": "delete.user"}],
        "approvals": {"tools": ["tickets.create", "crm/delete.user"], "timeout_hours": 168},
    })
    assert approval_required_tool_names(spec) == {"tickets__create", mcp_tool_name("crm", "delete.user")}
    assert approval_required_tool_names(AgentSpec.model_validate(FULL)) == frozenset()
    for tools, message in (
        (["web.search"], "not a tool of this agent"),
        (["crm/other"], "not a tool of this agent"),
        (["retrieve"], "platform tool"),
        (["update_working_memory"], "platform tool"),
        (["final_result"], "platform tool"),
        (["tickets.create", "tickets.create"], "duplicate"),
        ([], "at least 1"),
    ):
        with pytest.raises(ValidationError, match=message):
            AgentSpec.model_validate({**FULL, "approvals": {"tools": tools}})
    for hours in (0, 169):
        with pytest.raises(ValidationError):
            AgentSpec.model_validate({**FULL, "approvals": {"tools": ["clock.now"], "timeout_hours": hours}})


def test_fallback_models_distinct_and_not_the_primary():
    ok = AgentSpec.model_validate({**FULL, "fallback_models": [{"provider": "test", "name": "a"}, {"provider": "x", "name": "ticket"}]})
    assert len(ok.fallback_models) == 2
    for fallbacks, message in (
        ([{"provider": "test", "name": "ticket"}], "primary"),
        ([{"provider": "test", "name": "a"}, {"provider": "test", "name": "a"}], "duplicate"),
        ([{"provider": "test", "name": n} for n in "abcd"], "at most 3"),
    ):
        with pytest.raises(ValidationError, match=message):
            AgentSpec.model_validate({**FULL, "fallback_models": fallbacks})


def test_denial_message():
    assert denial_message() == "The user denied this tool call."
    assert denial_message("not today") == "The user denied this tool call. Comment: not today"


# --- evals -----------------------------------------------------------------------------------------

EVALUATOR = TypeAdapter(EvaluatorSpec)


def test_evaluator_union_by_kind():
    judge = {"kind": "llm_judge", "rubric": "MUST_CONTAIN:x", "model": {"provider": "test", "name": "judge"}}
    for raw in (
        {"kind": "equals_expected"},
        {"kind": "contains", "value": "abc", "case_sensitive": False},
        {"kind": "regex", "pattern": "^a+$"},
        {"kind": "schema_valid", "json_schema": SCHEMA},
        {"kind": "schema_valid"},
        {"kind": "field_equals", "path": "items.0.name", "value": None},
        {"kind": "max_duration", "seconds": 2.5},
        {"kind": "max_cost", "usd": "0.01"},
        {**judge, "pass_threshold": 0.5, "name": "quality"},
    ):
        parsed = EVALUATOR.validate_python(raw)
        assert parsed.kind == raw["kind"]
        assert EVALUATOR.validate_python(EVALUATOR.dump_python(parsed, mode="json")) == parsed
    for bad in (
        {"kind": "nope"},
        {"kind": "regex", "pattern": "a" * 201},
        {"kind": "llm_judge", "rubric": "r" * (4 * 1024 + 1), "model": judge["model"]},
        {**judge, "rubric": "   "},
        {**judge, "pass_threshold": 1.5},
        {"kind": "schema_valid", "json_schema": {"type": "object", "properties": {"a": {"$ref": "#"}}}},
        {"kind": "field_equals", "path": "a..b", "value": 1},
        {"kind": "field_equals", "path": "a"},
        {"kind": "max_duration", "seconds": 0},
        {"kind": "equals_expected", "extra": 1},
    ):
        with pytest.raises(ValidationError):
            EVALUATOR.validate_python(bad)


def _case(i: int, **extra) -> dict:
    return {"id": str(uuid.UUID(int=i + 1)), "position": i, "name": f"case {i}", "input": f"q{i}", **extra}


def test_snapshot_hash_and_bounds():
    snap = EvalSuiteSnapshot.model_validate({
        "suite_id": str(uuid.uuid4()), "name": "S", "evaluators": [{"kind": "equals_expected"}],
        "cases": [_case(1, expected_output={"a": 1}), _case(0)],
    })
    assert [c.position for c in snap.ordered_cases()] == [0, 1]
    assert json.loads(snap.canonical_json())["max_concurrency"] == 4
    renamed = snap.model_copy(update={"name": "Other", "max_concurrency": 8})
    assert renamed.suite_hash() == snap.suite_hash() and renamed.canonical_json() != snap.canonical_json()
    assert EvalSuiteSnapshot.model_validate_json(snap.canonical_json()) == snap
    changed = EvalSuiteSnapshot.model_validate({**json.loads(snap.canonical_json()), "evaluators": []})
    assert changed.suite_hash() != snap.suite_hash()
    base = json.loads(snap.canonical_json())
    for patch in (
        {"max_concurrency": 0}, {"max_concurrency": 9}, {"cases": []},
        {"cases": [_case(0), {**_case(1), "name": "case 0"}]},
        {"cases": [_case(0), {**_case(1), "id": _case(0)["id"]}]},
        {"evaluators": [{"kind": "equals_expected"}] * 11},
        {"evaluators": [{"kind": "regex", "pattern": "a", "name": "n"}],
         "cases": [_case(0, evaluators=[{"kind": "equals_expected", "name": "n"}])]},
    ):
        with pytest.raises(ValidationError):
            EvalSuiteSnapshot.model_validate({**base, **patch})


def test_case_bounds():
    EvalCase(name="c", input="x" * (32 * 1024), metadata={f"k{i}": i for i in range(16)})
    for bad in (
        {"input": "x" * (32 * 1024 + 1)},
        {"metadata": {f"k{i}": i for i in range(17)}},
        {"metadata": {"nested": {"a": 1}}},
        {"metadata": {"f": float("nan")}},
        {"evaluators": [{"kind": "equals_expected"}] * 6},
        {"name": "  "},
    ):
        with pytest.raises(ValidationError):
            EvalCase.model_validate({"name": "c", "input": "x", **bad})


def test_field_path_value():
    data = {"items": [{"name": "a"}], "n": 0}
    assert field_path_value(data, "items.0.name") == (True, "a")
    assert field_path_value(data, "n") == (True, 0)
    assert field_path_value(data, "items.1") == (False, None)
    assert field_path_value(data, "n.x") == (False, None)


def test_judge_verdict_uses_pass_alias():
    verdict = JudgeVerdict.model_validate({"reason": "ok", "pass": True, "score": 1.0})
    assert verdict.passed is True
    assert verdict.model_dump() == {"reason": "ok", "pass": True, "score": 1.0}
    assert "pass" in JudgeVerdict.model_json_schema()["properties"]
    for bad in ({"reason": "r" * 2001, "pass": True, "score": 1}, {"reason": "", "pass": True, "score": 1.1}):
        with pytest.raises(ValidationError):
            JudgeVerdict.model_validate(bad)


# --- errors ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        OutputInvalid(2, ("answer", "items.0")),
        RunBudgetExceeded(Decimal("1.2"), "1"),
        EvalSuiteInvalid("too many cases", "cases"),
        SchemaSubsetInvalid("unsupported keyword '$ref'", "a"),
    ],
)
def test_errors_pickle_with_codes(exc):
    restored = pickle.loads(pickle.dumps(exc))
    assert type(restored) is type(exc)
    assert str(restored) == str(exc)
    assert restored.code == exc.code
    assert vars(restored) == vars(exc)


def test_error_messages_and_codes():
    assert OutputInvalid.code == "output_invalid"
    assert str(OutputInvalid(2)) == "the agent's output did not match its output schema after 2 retries"
    budget = RunBudgetExceeded("1.25", "1")
    assert budget.code == "run_budget_exceeded"
    assert str(budget) == "the run reached its cost limit of $1 (spent $1.25)"
    assert budget.data() == {"limit": "max_cost_usd", "value": "1", "spent_usd": "1.25"}
    assert EvalSuiteInvalid.code == "eval_suite_invalid"
    assert str(EvalSuiteInvalid("bad", "cases.0")) == "the test suite is invalid at cases.0: bad"


# --- AC.1 review fixes (2026-11-15) ----------------------------------------------------------------


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_eval_json_values_reject_non_finite_numbers_at_any_depth(bad):
    import yaml

    for value in (bad, {"a": [1, {"b": bad}]}, [bad]):
        for raw in (
            {"kind": "contains", "value": value},
            {"kind": "field_equals", "path": "a", "value": value},
        ):
            with pytest.raises(ValidationError, match="NaN or Infinity"):
                EVALUATOR.validate_python(raw)
        with pytest.raises(ValidationError, match="NaN or Infinity"):
            EvalCase.model_validate({"name": "c", "input": "x", "expected_output": value})
    loaded = yaml.safe_load("name: c\ninput: x\nexpected_output: {score: .nan}\n")
    with pytest.raises(ValidationError, match="NaN or Infinity"):
        EvalCase.model_validate(loaded)


def test_output_property_names_share_the_segment_syntax():
    for name in ("a", "A-b_9", "x" * 64):
        validate_output_schema({"type": "object", "properties": {name: {"type": "string"}}})
    for name in ("", "has space", "dot.ted", "x" * 65, "é", "a/b"):
        with pytest.raises(SchemaSubsetInvalid, match=r"must match \^\[A-Za-z0-9_-\]\{1,64\}\$"):
            validate_output_schema({"type": "object", "properties": {name: {"type": "string"}}})
    # working memory keeps its rule (1 to 64 characters, anything)
    from eap_schemas.memory import validate_working_memory_schema

    validate_working_memory_schema({"type": "object", "properties": {"has space.é": {"type": "string"}}})


def test_schema_uri_only_2020_12_in_the_output_dialect():
    validate_output_schema({"type": "object", "$schema": "https://json-schema.org/draft/2020-12/schema"})
    for uri in ("http://json-schema.org/draft-07/schema#", "https://evil.example/schema", ""):
        with pytest.raises(SchemaSubsetInvalid, match="may only be"):
            validate_output_schema({"type": "object", "$schema": uri})
    from eap_schemas.memory import validate_working_memory_schema

    validate_working_memory_schema({"type": "object", "$schema": "http://json-schema.org/draft-07/schema#"})


def test_platform_names_reserved_per_feature():
    kb = {"knowledge_bases": [{"id": str(uuid.UUID(int=1))}]}
    memory = {"memory": {}}
    output = {"output": {"json_schema": SCHEMA}}
    for tool, feature, others in (
        ("retrieve", kb, (memory, output)),
        ("update_working_memory", memory, (kb, output)),
        ("final_result", output, (kb, memory)),
    ):
        with pytest.raises(ValidationError, match="reserved"):
            AgentSpec.model_validate({**MINIMAL, "tools": [tool], **feature})
        for other in others:  # another feature on: the name stays readable in AgentSpec
            spec = AgentSpec.model_validate({**MINIMAL, "tools": [tool], **other})
            assert reserved_tool_name_conflicts(spec) == (tool,)  # publish still refuses it


def _snapshot(evaluators, cases):
    return EvalSuiteSnapshot.model_validate(
        {"suite_id": str(uuid.UUID(int=99)), "name": "S", "evaluators": evaluators, "cases": cases}
    )


def test_suite_hash_canonical_form():
    cases = [_case(0)]
    hashes = {_snapshot([{"kind": "max_cost", "usd": v}], cases).suite_hash() for v in ("0.5", "0.50", 0.5, "5E-1")}
    assert len(hashes) == 1
    assert _snapshot([{"kind": "max_cost", "usd": "1E+3"}], cases).evaluators[0].model_dump(mode="json")["usd"] == "1000"
    with pytest.raises(ValidationError, match="decimal places"):
        _snapshot([{"kind": "max_cost", "usd": "0.00001"}], cases)
    # None model fields are left out: explicit Nones hash like absent ones
    a = _snapshot([{"kind": "contains"}], [_case(0)])
    b = _snapshot([{"kind": "contains", "value": None, "name": None}], [_case(0, expected_output=None)])
    assert a.suite_hash() == b.suite_hash()
    # but None inside a JSON value is content
    c = _snapshot([], [_case(0, expected_output={"a": None})])
    d = _snapshot([], [_case(0, expected_output={})])
    assert c.suite_hash() != d.suite_hash()
