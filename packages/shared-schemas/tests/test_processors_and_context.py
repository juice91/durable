"""P0.2 contracts: processor declarations and configs, request context fields, validation,
JSON schema and the placeholder renderer."""

from __future__ import annotations

import pickle

import pytest
from pydantic import ValidationError

from eap_schemas import AgentSpec, ModelRef, canonical_spec_hash
from eap_schemas.processors import (
    MODERATION_CATEGORIES,
    PROCESSOR_DECLARATIONS,
    PROCESSOR_SPEC_ADAPTER,
    ModerationConfig,
    ModerationProcessor,
    ModerationVerdict,
    PiiRedactProcessor,
    processor_models,
)
from eap_schemas.request_context import (
    ContextField,
    RequestContextError,
    context_json_schema,
    placeholder_errors,
    placeholders,
    render_instructions,
    validate_request_context,
)

BASE = {"name": "a", "model": {"provider": "test", "name": "echo"}}
MOD = {"type": "moderation", "config": {"model": {"provider": "test", "name": "moderation"}}}


def _spec(**extra):
    return AgentSpec.model_validate({**BASE, **extra})


# --- processors ----------------------------------------------------------------------------------

def test_every_processor_type_round_trips():
    procs = [
        {"type": "pii_redact"},
        {"type": "regex_blocklist", "config": {"patterns": ["secret\\d+"], "action": "redact"}},
        {"type": "max_length", "config": {"max_chars": 10, "action": "truncate"}},
        MOD,
    ]
    spec = _spec(input_processors=procs, output_processors=procs)
    again = AgentSpec.model_validate_json(spec.model_dump_json())
    assert again == spec
    assert canonical_spec_hash(again) == canonical_spec_hash(spec)
    assert isinstance(spec.input_processors[0], PiiRedactProcessor)
    assert spec.input_processors[0].config.action == "redact"
    assert processor_models(spec.output_processors) == (ModelRef(provider="test", name="moderation"),)


def test_processor_limits():
    ok = [{"type": "max_length", "config": {"max_chars": 5}}] * 8
    _spec(input_processors=ok, output_processors=ok)
    with pytest.raises(ValidationError):
        _spec(input_processors=ok * 2)
    with pytest.raises(ValidationError):
        _spec(output_processors=[*ok, ok[0]])
    patterns = [f"p{i}" for i in range(20)]
    PROCESSOR_SPEC_ADAPTER.validate_python({"type": "regex_blocklist", "config": {"patterns": patterns}})
    bad_configs = [
        {"type": "regex_blocklist", "config": {"patterns": [*patterns, "p20"]}},
        {"type": "regex_blocklist", "config": {"patterns": ["x" * 201]}},
        {"type": "regex_blocklist", "config": {"patterns": []}},
        {"type": "regex_blocklist", "config": {"patterns": ["a", "a"]}},
        {"type": "regex_blocklist", "config": {"patterns": ["a"], "action": "truncate"}},
        {"type": "max_length", "config": {"max_chars": 0}},
        {"type": "max_length", "config": {"max_chars": 1_000_001}},
        {"type": "pii_redact", "config": {"kinds": []}},
        {"type": "pii_redact", "config": {"kinds": ["email", "email"]}},
        {"type": "pii_redact", "config": {"kinds": ["passport"]}},
        {"type": "moderation", "config": {"model": {"provider": "test", "name": "m"}, "action": "redact"}},
        {"type": "moderation", "config": {"model": {"provider": "test", "name": "m"}, "categories": ["spam"]}},
        {"type": "moderation", "config": {}},
        {"type": "unknown"},
        {"type": "pii_redact", "config": {}, "extra": 1},
        {"type": "max_length", "config": {"max_chars": 5, "code": "x"}},
    ]
    for bad in bad_configs:
        with pytest.raises(ValidationError):
            PROCESSOR_SPEC_ADAPTER.validate_python(bad)
    assert PROCESSOR_SPEC_ADAPTER.validate_python({"type": "regex_blocklist", "config": {"patterns": ["x" * 200]}})


def test_declarations_and_config_schemas_fit_the_form_subset():
    assert set(PROCESSOR_DECLARATIONS) == {"pii_redact", "regex_blocklist", "max_length", "moderation"}
    for decl in PROCESSOR_DECLARATIONS.values():
        assert decl.phases == (("input", "output") if decl.id == "moderation" else ("input", "step", "stream", "output"))
        schema = decl.config_json_schema()
        text = repr(schema)
        assert "$ref" not in text and "$defs" not in text and "'const'" not in text and "'title'" not in text
        assert schema["type"] == "object"
        for prop in schema["properties"].values():
            assert prop["type"] in {"string", "integer", "array", "object"}
    assert PROCESSOR_DECLARATIONS["moderation"].deterministic is False
    mod_schema = PROCESSOR_DECLARATIONS["moderation"].config_json_schema()
    assert mod_schema["properties"]["model"]["required"] == ["provider", "name"]
    assert mod_schema["properties"]["action"]["enum"] == ["block"]


def test_moderation_verdict():
    config = ModerationConfig(model=ModelRef(provider="test", name="m"), categories=("violence",))
    assert not ModerationVerdict(flagged=False).blocks(config)
    assert ModerationVerdict(flagged=True, categories=("violence",)).blocks(config)
    assert not ModerationVerdict(flagged=True, categories=("hate",)).blocks(config)
    assert ModerationVerdict(flagged=True).blocks(config)  # fails closed
    with pytest.raises(ValidationError):
        ModerationVerdict(flagged=False, categories=("hate",))
    with pytest.raises(ValidationError):
        ModerationVerdict(flagged=True, categories=("spam",))
    assert len(MODERATION_CATEGORIES) == len(set(MODERATION_CATEGORIES))
    assert isinstance(_spec(output_processors=[MOD]).output_processors[0], ModerationProcessor)


# --- context fields ------------------------------------------------------------------------------

FIELDS = (
    ContextField(name="customer", type="string", required=True, max_length=5),
    ContextField(name="seats", type="integer"),
    ContextField(name="ratio", type="number"),
    ContextField(name="beta", type="boolean"),
    ContextField(name="plan", type="enum", values=("free", "pro")),
)


def test_context_field_rules():
    for bad in (
        {"name": "Customer", "type": "string"},
        {"name": "1x", "type": "string"},
        {"name": "a" * 33, "type": "string"},
        {"name": "tenant_id", "type": "string"},
        {"name": "user_id", "type": "string"},
        {"name": "run_id", "type": "string"},
        {"name": "x", "type": "date"},
        {"name": "x", "type": "integer", "max_length": 3},
        {"name": "x", "type": "enum"},
        {"name": "x", "type": "string", "values": ["a"]},
        {"name": "x", "type": "enum", "values": ["a", "a"]},
        {"name": "x", "type": "enum", "values": [""]},
        {"name": "x", "type": "string", "unknown": 1},
    ):
        with pytest.raises(ValidationError):
            ContextField.model_validate(bad)
    ContextField(name="a" * 32, type="string")
    fields = [{"name": f"f{i}", "type": "string"} for i in range(32)]
    _spec(context_fields=fields)
    with pytest.raises(ValidationError):
        _spec(context_fields=[*fields, {"name": "f32", "type": "string"}])
    with pytest.raises(ValidationError, match="duplicate context field"):
        _spec(context_fields=[{"name": "a", "type": "string"}, {"name": "a", "type": "integer"}])


def _errors(value):
    with pytest.raises(RequestContextError) as info:
        validate_request_context(FIELDS, value)
    return info.value.errors


def test_validate_request_context_accepts_and_normalises():
    value = {"plan": "pro", "customer": "acme", "seats": 3, "ratio": 0.5, "beta": False}
    assert list(validate_request_context(FIELDS, value)) == ["customer", "seats", "ratio", "beta", "plan"]
    assert validate_request_context(FIELDS, {"customer": "a", "seats": None}) == {"customer": "a"}
    assert validate_request_context(FIELDS, {"customer": "a", "ratio": 2}) == {"customer": "a", "ratio": 2}
    assert validate_request_context((), None) == {}


@pytest.mark.parametrize(
    ("value", "loc", "type_"),
    [
        ({}, ["customer"], "missing"),
        ({"customer": None}, ["customer"], "missing"),
        ({"customer": "a", "extra": 1}, ["extra"], "extra_forbidden"),
        ({"customer": "a", "tenant_id": "t2"}, ["tenant_id"], "reserved_name"),
        ({"customer": "a", "user_id": "u"}, ["user_id"], "reserved_name"),
        ({"customer": 5}, ["customer"], "string_type"),
        ({"customer": "toolong"}, ["customer"], "string_too_long"),
        ({"customer": "a", "seats": True}, ["seats"], "int_type"),
        ({"customer": "a", "seats": 1.5}, ["seats"], "int_type"),
        ({"customer": "a", "seats": "3"}, ["seats"], "int_type"),
        ({"customer": "a", "ratio": False}, ["ratio"], "float_type"),
        ({"customer": "a", "ratio": "1"}, ["ratio"], "float_type"),
        ({"customer": "a", "beta": 1}, ["beta"], "bool_type"),
        ({"customer": "a", "plan": "gold"}, ["plan"], "enum"),
        ({"customer": "a", "plan": 1}, ["plan"], "enum"),
        ([], [], "dict_type"),
        ("x", [], "dict_type"),
        ({"customer": "a", "ratio": float("nan")}, [], "json_invalid"),
        ({"customer": "a" * 9000}, [], "too_large"),
    ],
)
def test_validate_request_context_rejects(value, loc, type_):
    errors = _errors(value)
    assert {"loc": loc, "type": type_} in [{"loc": e["loc"], "type": e["type"]} for e in errors]
    # Errors never echo a supplied value.
    for err in errors:
        assert set(err) == {"loc", "msg", "type"}
        assert "toolong" not in err["msg"] and "gold" not in err["msg"]


def test_context_size_limit_is_8_kib_of_utf8():
    field = (ContextField(name="note", type="string"),)
    overhead = len('{"note":""}')
    validate_request_context(field, {"note": "a" * (8192 - overhead)})
    with pytest.raises(RequestContextError):
        validate_request_context(field, {"note": "a" * (8193 - overhead)})
    with pytest.raises(RequestContextError):
        validate_request_context(field, {"note": "é" * 4096})  # 2 bytes each


def test_request_context_error_pickles():
    err = RequestContextError([{"loc": ["a"], "msg": "is required", "type": "missing"}])
    again = pickle.loads(pickle.dumps(err))
    assert again.errors == err.errors
    assert str(again) == "invalid request context: a: is required"


def test_context_json_schema():
    schema = context_json_schema(FIELDS)
    assert schema == {
        "type": "object",
        "additionalProperties": False,
        "required": ["customer"],
        "properties": {
            "customer": {"type": "string", "maxLength": 5},
            "seats": {"type": "integer"},
            "ratio": {"type": "number"},
            "beta": {"type": "boolean"},
            "plan": {"type": "string", "enum": ["free", "pro"]},
        },
    }
    assert context_json_schema(()) == {"type": "object", "properties": {}, "additionalProperties": False}
    described = context_json_schema((ContextField(name="x", type="string", description="Who"),))
    assert described["properties"]["x"]["description"] == "Who"


# --- placeholders and rendering ------------------------------------------------------------------

def test_placeholders_and_errors():
    text = "Hi {{context.customer}}, {{ context.plan }} {{context.customer}} {{context.__class__}} {{context}}"
    assert placeholders(text) == ("customer", "plan")
    errors = placeholder_errors(text, FIELDS)
    assert len(errors) == 2 and all("malformed" in e for e in errors)
    assert placeholder_errors("{{context.nope}}", FIELDS) == ["placeholder {{context.nope}} names no context field"]
    assert placeholder_errors("no placeholders {{other}}", ()) == []


def test_render_is_plain_single_pass_substitution():
    ctx = {"customer": "{{context.plan}}", "plan": "pro", "seats": 3, "beta": True, "ratio": 0.25}
    text = "{{context.customer}}|{{context.plan}}|{{context.seats}}|{{context.beta}}|{{context.ratio}}"
    assert render_instructions(text, ctx, FIELDS) == "{{context.plan}}|pro|3|true|0.25"
    # Nothing is evaluated: attribute access and unknown names stay as written.
    assert render_instructions("{{context.__class__}} {{context.nope}}", ctx, FIELDS) == (
        "{{context.__class__}} {{context.nope}}"
    )
    # Declared field without a value renders empty; nested braces keep the outer ones.
    assert render_instructions("[{{context.seats}}]", {}, FIELDS) == "[]"
    assert render_instructions("{{{context.plan}}}", ctx, FIELDS) == "{pro}"
    # Without fields, only supplied names are replaced.
    assert render_instructions("{{context.plan}} {{context.seats}}", {"plan": "pro"}) == "pro {{context.seats}}"
    # Backslashes and regex group syntax in values are inserted literally.
    assert render_instructions("{{context.plan}}", {"plan": r"\1 \g<0>"}) == r"\1 \g<0>"


# --- review fix: huge numbers never raise, integers are int64 ------------------------------------

@pytest.mark.parametrize(
    ("value", "loc", "type_"),
    [
        ({"customer": "a", "ratio": 10**400}, ["ratio"], "number_too_large"),
        ({"customer": "a", "ratio": -(10**400)}, ["ratio"], "number_too_large"),
        ({"customer": "a", "seats": 2**63}, ["seats"], "int_out_of_range"),
        ({"customer": "a", "seats": -(2**63) - 1}, ["seats"], "int_out_of_range"),
        ({"customer": "a", "seats": 10**400}, ["seats"], "int_out_of_range"),
    ],
)
def test_huge_numbers_are_rejected_not_raised(value, loc, type_):
    errors = _errors(value)  # RequestContextError, never OverflowError
    assert [{"loc": e["loc"], "type": e["type"]} for e in errors] == [{"loc": loc, "type": type_}]
    for err in errors:
        assert "1000" not in err["msg"] and str(value.get(loc[0]))[:20] not in err["msg"]


def test_int64_bounds_and_large_finite_numbers_accepted():
    ok = validate_request_context(FIELDS, {"customer": "a", "seats": 2**63 - 1, "ratio": 10**300})
    assert ok["seats"] == 2**63 - 1 and ok["ratio"] == 10**300
    assert validate_request_context(FIELDS, {"customer": "a", "seats": -(2**63)})["seats"] == -(2**63)
    assert validate_request_context(FIELDS, {"customer": "a", "ratio": 1.7e308})["ratio"] == 1.7e308
