"""P0.2 acceptance (spec contracts), written independently of the implementer's tests.

Covers: hash stability against the code on main, processor config rules and caps, ModerationVerdict
failing closed, request-context validation (every error type, sentinel values never echoed), pickling,
and the single-pass, no-evaluation renderer.
"""

from __future__ import annotations

import hashlib
import importlib.util
import itertools
import json
import pickle
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from eap_schemas import AgentSpec, AgentVersion, canonical_spec_hash
from eap_schemas.processors import (
    MAX_PROCESSORS_PER_PHASE,
    MODERATION_CATEGORIES,
    PII_KINDS,
    PROCESSOR_DECLARATIONS,
    PROCESSOR_SPEC_ADAPTER,
    ModerationConfig,
    ModerationVerdict,
)
from eap_schemas.request_context import (
    INT64_MAX,
    INT64_MIN,
    MAX_CONTEXT_BYTES,
    ContextField,
    RequestContextError,
    context_json_schema,
    placeholder_errors,
    placeholders,
    render_instructions,
    validate_request_context,
)

REPO = Path(__file__).resolve().parents[3]
BASE = {"name": "a", "model": {"provider": "test", "name": "echo"}}
SENTINEL = "ZQ-SENTINEL-7731"
MODEL = {"provider": "test", "name": "moderation"}


def _load_main_schemas():
    """The package __init__ on main, loaded as its own module (its submodule imports resolve to the
    unchanged eap_schemas.mcp and eap_schemas.tools)."""
    shown = subprocess.run(
        ["git", "-C", str(REPO), "show", "main:packages/shared-schemas/src/eap_schemas/__init__.py"],
        capture_output=True, text=True, check=True,
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "main_eap_schemas_snapshot.py"
        path.write_text(shown.stdout)
        spec = importlib.util.spec_from_file_location("main_eap_schemas_snapshot", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module


@pytest.fixture(scope="module")
def main_schemas():
    module = _load_main_schemas()
    yield module
    sys.modules.pop("main_eap_schemas_snapshot", None)


SHAPES = [
    {**BASE},
    {**BASE, "instructions": "Open a ticket.", "tools": ["tickets.create", "clock.now"],
     "model_settings": {"temperature": 0.2}},
    {**BASE, "instructions": "Unicode é中 {{context.x}} {0} %s", "model_settings": {"max_tokens": 50}},
    {**BASE, "mcp_tools": [{"server": "docs", "tool": "search"}]},
]


# --- hash stability ------------------------------------------------------------------------------

@pytest.mark.parametrize("shape", SHAPES)
def test_hash_equals_the_hash_computed_by_the_code_on_main(main_schemas, shape):
    old = main_schemas.AgentSpec.model_validate(shape)
    new = AgentSpec.model_validate(shape)
    assert canonical_spec_hash(new) == main_schemas.canonical_spec_hash(old)
    assert new.model_dump(mode="json") == old.model_dump(mode="json")
    assert new.model_dump_json() == old.model_dump_json()


@pytest.mark.parametrize("shape", SHAPES)
def test_explicit_empty_new_fields_keep_the_main_hash(main_schemas, shape):
    expected = main_schemas.canonical_spec_hash(main_schemas.AgentSpec.model_validate(shape))
    empty = {"input_processors": [], "output_processors": [], "context_fields": []}
    for r in range(1, 4):
        for keys in itertools.combinations(empty, r):
            spec = AgentSpec.model_validate({**shape, **{k: empty[k] for k in keys}})
            assert canonical_spec_hash(spec) == expected
            assert not set(empty) & set(spec.model_dump(mode="json"))


def test_a_version_hashed_by_main_validates_with_the_new_code(main_schemas):
    shape = SHAPES[1]
    old_spec = main_schemas.AgentSpec.model_validate(shape)
    stored = {
        "agent_id": "00000000-0000-0000-0000-000000000001", "tenant_id": "t1", "version": 1,
        "spec": old_spec.model_dump(mode="json"), "spec_hash": main_schemas.canonical_spec_hash(old_spec),
        "created_at": "2026-11-01T00:00:00+00:00",
    }
    assert AgentVersion.model_validate(stored).spec_hash == stored["spec_hash"]


def test_pinned_literal_for_the_documented_spec():
    spec = AgentSpec.model_validate(SHAPES[1])
    pinned = {"spec_version": 1, "name": "a", "instructions": "Open a ticket.", "model": BASE["model"],
              "tools": ["tickets.create", "clock.now"], "model_settings": {"temperature": 0.2, "max_tokens": None}}
    digest = hashlib.sha256(json.dumps(pinned, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert canonical_spec_hash(spec) == digest


def test_non_empty_new_fields_change_the_hash():
    plain = canonical_spec_hash(AgentSpec.model_validate(BASE))
    with_ctx = AgentSpec.model_validate({**BASE, "context_fields": [{"name": "plan", "type": "string"}]})
    with_in = AgentSpec.model_validate({**BASE, "input_processors": [{"type": "pii_redact"}]})
    assert len({plain, canonical_spec_hash(with_ctx), canonical_spec_hash(with_in)}) == 3


# --- processor config rules and caps -------------------------------------------------------------

def _proc(kind, **config):
    return PROCESSOR_SPEC_ADAPTER.validate_python({"type": kind, "config": config} if config else {"type": kind})


def test_defaults_of_every_processor_type():
    pii = _proc("pii_redact")
    assert pii.config.kinds == PII_KINDS and pii.config.action == "redact"
    assert _proc("regex_blocklist", patterns=["a"]).config.action == "block"
    assert _proc("max_length", max_chars=5).config.action == "block"
    mod = _proc("moderation", model=MODEL)
    assert mod.config.categories == MODERATION_CATEGORIES and mod.config.action == "block"


@pytest.mark.parametrize(
    ("kind", "good", "bad"),
    [
        ("pii_redact", [{"kinds": ["email"]}, {"kinds": list(PII_KINDS), "action": "block"}],
         [{"kinds": []}, {"kinds": ["email", "email"]}, {"action": "truncate"}, {"kinds": "email"}]),
        ("regex_blocklist", [{"patterns": ["a"]}, {"patterns": ["x" * 200], "action": "redact"},
                             {"patterns": [f"p{i}" for i in range(20)]}],
         [{"patterns": []}, {"patterns": [""]}, {"patterns": ["x" * 201]}, {"patterns": ["a", "a"]},
          {"patterns": [f"p{i}" for i in range(21)]}, {"patterns": ["a"], "action": "truncate"}, {}]),
        ("max_length", [{"max_chars": 1}, {"max_chars": 1_000_000, "action": "truncate"}],
         [{"max_chars": 0}, {"max_chars": -1}, {"max_chars": 1_000_001}, {"max_chars": 1.5},
          {"max_chars": 5, "action": "redact"}, {}]),
        ("moderation", [{"model": MODEL}, {"model": MODEL, "categories": ["hate"]}],
         [{}, {"model": MODEL, "categories": []}, {"model": MODEL, "categories": ["hate", "hate"]},
          {"model": MODEL, "categories": ["spam"]}, {"model": MODEL, "action": "truncate"},
          {"model": {"provider": "test"}}]),
    ],
)
def test_config_rules(kind, good, bad):
    for config in good:
        assert PROCESSOR_SPEC_ADAPTER.validate_python({"type": kind, "config": config}).type == kind
    for config in bad:
        with pytest.raises(ValidationError):
            PROCESSOR_SPEC_ADAPTER.validate_python({"type": kind, "config": config})


def test_unknown_type_extra_keys_and_frozen_models_are_rejected():
    for bad in ({"type": "nope"}, {"type": "pii_redact", "extra": 1}, {"config": {}}, {"type": "max_length"}):
        with pytest.raises(ValidationError):
            PROCESSOR_SPEC_ADAPTER.validate_python(bad)
    with pytest.raises(ValidationError):
        _proc("max_length", max_chars=5).config.max_chars = 6


def test_eight_processors_per_phase_and_each_phase_counts_separately():
    ok = [{"type": "max_length", "config": {"max_chars": 5}}] * MAX_PROCESSORS_PER_PHASE
    spec = AgentSpec.model_validate({**BASE, "input_processors": ok, "output_processors": ok})
    assert len(spec.input_processors) == len(spec.output_processors) == 8
    for field in ("input_processors", "output_processors"):
        with pytest.raises(ValidationError):
            AgentSpec.model_validate({**BASE, field: [*ok, ok[0]]})


def test_every_type_is_allowed_in_both_phases_and_declarations_are_consistent():
    assert MAX_PROCESSORS_PER_PHASE == 8
    mod = {"type": "moderation", "config": {"model": MODEL}}
    spec = AgentSpec.model_validate({**BASE, "input_processors": [mod], "output_processors": [mod]})
    assert spec.input_processors == spec.output_processors
    assert {k for k, d in PROCESSOR_DECLARATIONS.items() if not d.deterministic} == {"moderation"}
    for key, decl in PROCESSOR_DECLARATIONS.items():
        assert decl.id == key and decl.phases == (("input", "output") if key == "moderation" else ("input", "step", "stream", "output")) and decl.title and decl.description
    assert PROCESSOR_DECLARATIONS["moderation"].actions == ("block",)
    assert set(PROCESSOR_DECLARATIONS["max_length"].actions) == {"block", "truncate"}


def test_config_json_schemas_have_no_refs_titles_or_const():
    for decl in PROCESSOR_DECLARATIONS.values():
        text = json.dumps(decl.config_json_schema())
        assert "$ref" not in text and "$defs" not in text and '"title"' not in text and '"const"' not in text
    action = PROCESSOR_DECLARATIONS["moderation"].config_json_schema()["properties"]["action"]
    assert action["enum"] == ["block"]


# --- ModerationVerdict fails closed --------------------------------------------------------------

@pytest.mark.parametrize("categories", [("hate",), ("violence", "illicit"), MODERATION_CATEGORIES])
def test_flagged_verdict_without_a_category_blocks_for_any_config(categories):
    config = ModerationConfig(model=MODEL, categories=categories)
    assert ModerationVerdict(flagged=True).blocks(config)
    assert ModerationVerdict(flagged=True, categories=()).blocks(config)


def test_verdict_blocks_only_on_configured_categories_and_rejects_inconsistency():
    config = ModerationConfig(model=MODEL, categories=("violence",))
    assert not ModerationVerdict(flagged=False).blocks(config)
    assert not ModerationVerdict(flagged=True, categories=("hate",)).blocks(config)
    assert ModerationVerdict(flagged=True, categories=("hate", "violence")).blocks(config)
    for bad in ({"flagged": False, "categories": ["hate"]}, {"flagged": True, "categories": ["nope"]},
                {"flagged": True, "categories": ["hate", "hate"]}, {"flagged": "maybe"}, {}):
        with pytest.raises(ValidationError):
            ModerationVerdict.model_validate(bad)


# --- request context: every error type -----------------------------------------------------------

FIELDS = (
    ContextField(name="customer", type="string", required=True, max_length=8),
    ContextField(name="seats", type="integer"),
    ContextField(name="ratio", type="number"),
    ContextField(name="beta", type="boolean"),
    ContextField(name="plan", type="enum", values=("free", "pro")),
)
GOOD = {"customer": "acme"}

# (name, value, expected loc, expected type). Every value carries the sentinel where a value is echoed-able.
CASES = [
    ("dict_list", [SENTINEL], [], "dict_type"),
    ("dict_str", SENTINEL, [], "dict_type"),
    ("json_nan", {**GOOD, "ratio": float("nan")}, [], "json_invalid"),
    ("json_inf", {**GOOD, "ratio": float("inf")}, [], "json_invalid"),
    ("json_neg_inf", {**GOOD, "ratio": float("-inf")}, [], "json_invalid"),
    ("json_bytes", {**GOOD, "note": SENTINEL.encode()}, [], "json_invalid"),
    ("too_large", {**GOOD, "plan": SENTINEL * 2000}, [], "too_large"),
    ("extra", {**GOOD, "other": SENTINEL}, ["other"], "extra_forbidden"),
    ("reserved_tenant", {**GOOD, "tenant_id": SENTINEL}, ["tenant_id"], "reserved_name"),
    ("reserved_user", {**GOOD, "user_id": SENTINEL}, ["user_id"], "reserved_name"),
    ("reserved_run", {**GOOD, "run_id": SENTINEL}, ["run_id"], "reserved_name"),
    ("missing", {}, ["customer"], "missing"),
    ("missing_null", {"customer": None}, ["customer"], "missing"),
    ("string_type", {"customer": {"k": SENTINEL}}, ["customer"], "string_type"),
    ("string_too_long", {"customer": SENTINEL}, ["customer"], "string_too_long"),
    ("int_type_str", {**GOOD, "seats": SENTINEL}, ["seats"], "int_type"),
    ("int_type_float", {**GOOD, "seats": 1.5}, ["seats"], "int_type"),
    ("int_type_bool", {**GOOD, "seats": True}, ["seats"], "int_type"),
    ("int_range_hi", {**GOOD, "seats": INT64_MAX + 1}, ["seats"], "int_out_of_range"),
    ("int_range_lo", {**GOOD, "seats": INT64_MIN - 1}, ["seats"], "int_out_of_range"),
    ("float_type_str", {**GOOD, "ratio": SENTINEL}, ["ratio"], "float_type"),
    ("float_type_bool", {**GOOD, "ratio": True}, ["ratio"], "float_type"),
    ("number_too_large", {**GOOD, "ratio": 10**500}, ["ratio"], "number_too_large"),
    ("bool_type_int", {**GOOD, "beta": 1}, ["beta"], "bool_type"),
    ("bool_type_str", {**GOOD, "beta": SENTINEL}, ["beta"], "bool_type"),
    ("enum_value", {**GOOD, "plan": SENTINEL}, ["plan"], "enum"),
    ("enum_type", {**GOOD, "plan": 3}, ["plan"], "enum"),
]


def _raise(value, fields=FIELDS):
    with pytest.raises(RequestContextError) as info:
        validate_request_context(fields, value)
    return info.value


@pytest.mark.parametrize(("name", "value", "loc", "type_"), CASES, ids=[c[0] for c in CASES])
def test_error_type_loc_and_no_value_echo(name, value, loc, type_):
    exc = _raise(value)
    assert [(e["loc"], e["type"]) for e in exc.errors] == [(loc, type_)]
    assert all(set(e) == {"loc", "msg", "type"} for e in exc.errors)
    rendered = str(exc) + json.dumps(exc.errors) + repr(exc)
    assert SENTINEL not in rendered
    assert "10000000000" not in rendered and "9223372036854775808" not in rendered
    assert pickle.loads(pickle.dumps(exc)).errors == exc.errors  # every error type survives pickling


def test_over_8_kib_is_checked_first_and_alone():
    big = {"customer": 5, "seats": "x", "bogus": 1, "tenant_id": "t", "pad": "a" * (MAX_CONTEXT_BYTES + 1)}
    exc = _raise(big)
    assert [e["type"] for e in exc.errors] == ["too_large"]
    # Exactly 8 KiB of UTF-8 is accepted, one byte more is not (multi-byte characters count as bytes).
    note = (ContextField(name="note", type="string"),)
    overhead = len(b'{"note":""}')
    assert validate_request_context(note, {"note": "a" * (MAX_CONTEXT_BYTES - overhead)})
    assert _raise({"note": "a" * (MAX_CONTEXT_BYTES - overhead + 1)}, note).errors[0]["type"] == "too_large"
    assert _raise({"note": "é" * (MAX_CONTEXT_BYTES // 2)}, note).errors[0]["type"] == "too_large"


def test_booleans_are_not_numbers_and_ints_are_not_floats_in_integer_fields():
    for bad in (True, False):
        assert _raise({**GOOD, "seats": bad}).errors[0]["type"] == "int_type"
        assert _raise({**GOOD, "ratio": bad}).errors[0]["type"] == "float_type"
    assert validate_request_context(FIELDS, {**GOOD, "ratio": 3})["ratio"] == 3
    assert validate_request_context(FIELDS, {**GOOD, "seats": INT64_MAX, "beta": False})["beta"] is False


def test_valid_context_keeps_declared_keys_in_declaration_order_and_nulls_are_absent():
    value = {"plan": "pro", "beta": True, "ratio": 0.5, "seats": None, "customer": "acme"}
    assert list(validate_request_context(FIELDS, value)) == ["customer", "ratio", "beta", "plan"]
    assert validate_request_context((), None) == {}
    assert validate_request_context(FIELDS, {"customer": "acme", "plan": None}) == GOOD


def test_several_errors_are_all_reported_without_values():
    exc = _raise({"seats": SENTINEL, "beta": SENTINEL, "other": 1, "run_id": SENTINEL})
    assert {e["type"] for e in exc.errors} == {"missing", "int_type", "bool_type", "extra_forbidden", "reserved_name"}
    assert SENTINEL not in str(exc)


def test_pickle_round_trip_of_error_spec_and_field():
    exc = _raise({"customer": SENTINEL})
    again = pickle.loads(pickle.dumps(exc))
    assert type(again) is RequestContextError and str(again) == str(exc) and again.errors == exc.errors
    assert pickle.loads(pickle.dumps(FIELDS[4])) == FIELDS[4]
    spec = AgentSpec.model_validate({**BASE, "context_fields": [f.model_dump() for f in FIELDS],
                                     "input_processors": [{"type": "pii_redact"}]})
    assert pickle.loads(pickle.dumps(spec)) == spec


def test_context_field_declaration_rules():
    for name in ("tenant_id", "user_id", "run_id", "Bad", "1a", "a-b", "", "a" * 33):
        with pytest.raises(ValidationError):
            ContextField(name=name, type="string")
    for bad in ({"type": "string", "max_length": 0}, {"type": "string", "max_length": 8193},
                {"type": "integer", "max_length": 5}, {"type": "enum"}, {"type": "string", "values": ("a",)},
                {"type": "enum", "values": ("a", "a")}, {"type": "enum", "values": ("x" * 129,)},
                {"type": "enum", "values": tuple(f"v{i}" for i in range(65))},
                {"type": "string", "description": "d" * 501}, {"type": "date"}):
        with pytest.raises(ValidationError):
            ContextField(name="ok", **bad)
    ContextField(name="a" * 32, type="enum", values=tuple(f"v{i}" for i in range(64)), description="d" * 500)
    ContextField(name="s", type="string", max_length=8192)
    many = [{"name": f"f{i}", "type": "string"} for i in range(33)]
    AgentSpec.model_validate({**BASE, "context_fields": many[:32]})
    with pytest.raises(ValidationError):
        AgentSpec.model_validate({**BASE, "context_fields": many})
    with pytest.raises(ValidationError):
        AgentSpec.model_validate({**BASE, "context_fields": [many[0], many[0]]})


def test_context_json_schema_is_closed_and_enums_are_strings():
    schema = context_json_schema(FIELDS)
    assert schema["additionalProperties"] is False and schema["required"] == ["customer"]
    assert schema["properties"]["plan"] == {"type": "string", "enum": ["free", "pro"]}
    assert schema["properties"]["customer"] == {"type": "string", "maxLength": 8}
    assert context_json_schema(())["additionalProperties"] is False


# --- render_instructions: single pass, nothing evaluated -----------------------------------------

RFIELDS = tuple(ContextField(name=n, type="string") for n in ("a", "b", "name", "plan")) + (
    ContextField(name="n", type="integer"), ContextField(name="flag", type="boolean"),
)


@pytest.mark.parametrize("hostile", ["{0}", "{}", "{name}", "{__class__}", "{0.__class__}", "%s", "%(name)s", "%d",
                                     "${x}", "$name", "${name}", "\\1", "\\g<0>", "{{context.plan}}",
                                     "{{ context.plan }}", "`id`", "$(id)", "{{7*7}}", "<%= 1 %>"])
def test_values_are_inserted_verbatim_and_never_evaluated_or_re_expanded(hostile):
    ctx = {"name": hostile, "plan": "PRO"}
    assert render_instructions("[{{context.name}}]", ctx, RFIELDS) == f"[{hostile}]"
    assert render_instructions("{{context.name}}|{{context.plan}}", ctx, RFIELDS) == f"{hostile}|PRO"


def test_template_text_with_format_syntax_is_left_alone():
    text = "{0} {} {name} %s %(x)d ${x} $y {{7*7}} {{context.plan}}"
    assert render_instructions(text, {"plan": "pro"}, RFIELDS) == text.replace("{{context.plan}}", "pro")


def test_a_value_cannot_assemble_a_placeholder_across_substitutions():
    ctx = {"a": "{{context.", "b": "plan}}", "plan": "SECRET"}
    assert render_instructions("{{context.a}}{{context.b}}", ctx, RFIELDS) == "{{context.plan}}"
    assert render_instructions("{{context.a}}", {"a": "{{context.a}}"}, RFIELDS) == "{{context.a}}"  # no loop


def test_scalar_formats_missing_and_unknown_placeholders():
    ctx = {"n": 0, "flag": False, "a": ""}
    out = render_instructions("{{context.n}}|{{context.flag}}|{{context.a}}|{{context.b}}|{{context.zz}}", ctx, RFIELDS)
    assert out == "0|false||" + "|{{context.zz}}"
    assert render_instructions("{{context.flag}}", {"flag": True}, RFIELDS) == "true"
    assert render_instructions("{{ context.plan }}{{context.plan }}", {"plan": "p"}, RFIELDS) == "pp"
    for literal in ("{{context.__class__}}", "{{context.plan.upper}}", "{{context}}", "{{context.}}",
                    "{{Context.plan}}", "{context.plan}", "{{context.Plan}}"):
        assert render_instructions(literal, {"plan": "p"}, RFIELDS) == literal


def test_render_does_not_mutate_the_context_with_many_placeholders():
    ctx = {"plan": "p"}
    render_instructions("{{context.plan}}" * 20000, ctx, RFIELDS)
    assert ctx == {"plan": "p"}


def test_placeholders_and_errors():
    text = "{{context.b}} {{ context.a }} {{context.b}} {{context.zz}} {{context.}} {{context.A}} {{context}}"
    assert placeholders(text) == ("b", "a", "zz")
    errors = placeholder_errors(text, RFIELDS)
    assert any("names no context field" in e and "zz" in e for e in errors)
    assert sum("malformed" in e for e in errors) >= 2
    assert placeholder_errors("{{context.a}} {{ context.b }} plain {0} %s", RFIELDS) == []
    assert placeholder_errors("{{context.a}}", ()) == ["placeholder {{context.a}} names no context field"]
    assert placeholder_errors("{{other.x}} text", ()) == []
