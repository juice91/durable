"""AGUI.1: client tool contracts and AG-UI run options (implementer's focused tests)."""

from __future__ import annotations

import json
import pickle

import pytest
from pydantic import ValidationError

from eap_schemas.client_tools import (
    MAX_CLIENT_TOOL_RESULT_BYTES,
    MAX_CLIENT_TOOLS,
    AgUiRunOptions,
    ClientToolDecl,
    ClientToolResult,
    ClientToolsInvalid,
    check_client_tools,
    client_tool_clashes,
)

OBJ = {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}


def _error_type(exc: ValidationError) -> str:
    return exc.errors()[0]["type"]


def test_valid_decl_round_trip():
    decl = ClientToolDecl(name="get_weather", description="Weather", parameters=OBJ)
    again = ClientToolDecl.model_validate_json(decl.model_dump_json())
    assert again == decl
    assert decl.model_dump() == {"name": "get_weather", "description": "Weather", "parameters": OBJ}


@pytest.mark.parametrize("name", ["a" * 64, "A-b_9"])
def test_name_ok(name):
    ClientToolDecl(name=name, parameters=OBJ)


@pytest.mark.parametrize("name", ["", "a" * 65, "has space", "dot.name", "ü"])
def test_name_rejected(name):
    with pytest.raises(ValidationError):
        ClientToolDecl(name=name, parameters=OBJ)


@pytest.mark.parametrize("name", ["retrieve", "update_working_memory", "final_result"])
def test_reserved_name(name):
    with pytest.raises(ValidationError) as info:
        ClientToolDecl(name=name, parameters=OBJ)
    assert _error_type(info.value) == "client_tool_shadows_server_tool"


@pytest.mark.parametrize(
    ("schema", "code"),
    [
        ([], "client_tool_schema_not_object"),
        ({"type": "string"}, "client_tool_schema_not_object"),
        ({"properties": {}}, "client_tool_schema_not_object"),
        ({"type": "object", "properties": {"a": {"$ref": "https://evil.example/s.json"}}},
         "client_tool_schema_remote_ref"),
        ({"type": "object", "$defs": {"x": {"$ref": "other.json#/x"}}}, "client_tool_schema_remote_ref"),
        ({"type": "object", "properties": {"a": {"type": 5}}}, "client_tool_schema_invalid"),
        ({"type": "object", "required": "a"}, "client_tool_schema_invalid"),
        ({"type": "object", "properties": {"a": {"$ref": "#/$defs/missing"}}}, "client_tool_schema_invalid"),
        ({"$schema": "http://json-schema.org/draft-07/schema#", "type": "object"}, "client_tool_schema_invalid"),
    ],
)
def test_bad_schemas(schema, code):
    with pytest.raises(ValidationError) as info:
        ClientToolDecl(name="t", parameters=schema)
    assert _error_type(info.value) == code
    assert "evil" not in info.value.errors()[0]["msg"]


def test_local_ref_ok():
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "$defs": {"city": {"type": "string"}},
        "properties": {"city": {"$ref": "#/$defs/city"}},
    }
    ClientToolDecl(name="t", parameters=schema)


def test_schema_size_boundary():
    def schema_of(n):
        return {"type": "object", "description": "x" * n}

    base = len('{"description":"","type":"object"}')
    ClientToolDecl(name="t", parameters=schema_of(64 * 1024 - base))
    with pytest.raises(ValidationError) as info:
        ClientToolDecl(name="t", parameters=schema_of(64 * 1024 - base + 1))
    assert _error_type(info.value) == "client_tool_schema_too_large"


def test_schema_depth():
    deep: dict = {"type": "object"}
    node = deep
    for _ in range(40):
        node["properties"] = {"a": {"type": "object"}}
        node = node["properties"]["a"]
    with pytest.raises(ValidationError) as info:
        ClientToolDecl(name="t", parameters=deep)
    assert _error_type(info.value) == "client_tool_schema_too_deep"


def test_description_boundary():
    ClientToolDecl(name="t", description="d" * 4096, parameters=OBJ)
    with pytest.raises(ValidationError):
        ClientToolDecl(name="t", description="d" * 4097, parameters=OBJ)


def test_extra_field_forbidden():
    with pytest.raises(ValidationError):
        ClientToolDecl(name="t", parameters=OBJ, strict=True)


def test_list_rules():
    tools = [ClientToolDecl(name=f"t{i}", parameters=OBJ) for i in range(MAX_CLIENT_TOOLS + 1)]
    check_client_tools(tools[:MAX_CLIENT_TOOLS])
    with pytest.raises(ClientToolsInvalid) as info:
        check_client_tools(tools)
    assert info.value.code == "client_tools_too_many"
    with pytest.raises(ClientToolsInvalid) as info:
        check_client_tools([tools[0], tools[0]])
    assert info.value.code == "client_tool_duplicate"


def test_total_schema_size():
    base = len('{"description":"","type":"object"}')
    half = {"type": "object", "description": "x" * (32 * 1024 - base)}
    a = ClientToolDecl(name="a", parameters=half)
    b = ClientToolDecl(name="b", parameters=half)
    check_client_tools([a, b])  # exactly 64 KiB
    c = ClientToolDecl(name="c", parameters={"type": "object"})
    with pytest.raises(ClientToolsInvalid) as info:
        check_client_tools([a, b, c])
    assert info.value.code == "client_tools_schemas_too_large"


def test_clients_invalid_pickles():
    err = pickle.loads(pickle.dumps(ClientToolsInvalid("client_tool_duplicate", "x")))
    assert err.code == "client_tool_duplicate" and str(err) == "client tools are invalid: x"


def test_clashes():
    tools = [ClientToolDecl(name=n, parameters=OBJ) for n in ("mcp__srv__x", "mine", "clock_now")]
    assert client_tool_clashes(tools, ["mcp__srv__x", "clock_now"]) == ["mcp__srv__x", "clock_now"]


def test_result_boundary_and_round_trip():
    ok = ClientToolResult(tool_call_id="call_1", content="é" * (MAX_CLIENT_TOOL_RESULT_BYTES // 2))
    assert ClientToolResult.model_validate_json(ok.model_dump_json()) == ok
    with pytest.raises(ValidationError) as info:
        ClientToolResult(tool_call_id="c", content="a" * MAX_CLIENT_TOOL_RESULT_BYTES, error="b")
    assert _error_type(info.value) == "client_tool_result_too_large"
    ClientToolResult(tool_call_id="c" * 256, error="boom")
    with pytest.raises(ValidationError):
        ClientToolResult(tool_call_id="c" * 257)
    with pytest.raises(ValidationError):
        ClientToolResult(tool_call_id="")
    with pytest.raises(ValidationError):
        ClientToolResult(tool_call_id="c", error="")


def test_run_options_camel_case():
    opts = AgUiRunOptions.model_validate(
        {
            "version": 3,
            "model": {"provider": "test", "name": "echo"},
            "requestContext": {"locale": "de"},
            "resourceId": "user:42",
        }
    )
    assert opts.version == 3 and opts.model.name == "echo" and opts.request_context == {"locale": "de"}
    assert opts.resource_id == "user:42" and not opts.has_input
    assert AgUiRunOptions.model_validate(opts.model_dump(by_alias=True)) == opts
    assert AgUiRunOptions.model_validate({"version": "draft"}).version == "draft"
    assert AgUiRunOptions.model_validate({}).version is None


def test_run_options_input_presence():
    assert AgUiRunOptions.model_validate({"input": None}).has_input
    assert AgUiRunOptions.model_validate({"input": {"a": 1}}).input == {"a": 1}


@pytest.mark.parametrize(
    "data",
    [{"version": 0}, {"version": "latest"}, {"unknown": 1}, {"resourceId": "bad id"}, {"resourceId": "r" * 129},
     {"model": {"provider": "", "name": "x"}}],
)
def test_run_options_rejected(data):
    with pytest.raises(ValidationError):
        AgUiRunOptions.model_validate(data)


# --- review fixes ---------------------------------------------------------------------------------

DEFS = {"a": {"type": "string"}}


@pytest.mark.parametrize(
    "schema",
    [
        # MAJOR 1: a nested $id would re-base "#/$defs/a" (jsonschema then raises PointerToNowhere).
        {"type": "object", "$defs": DEFS,
         "properties": {"p": {"$id": "https://x.example/y", "type": "object",
                              "properties": {"q": {"$ref": "#/$defs/a"}}}}},
        {"type": "object", "$id": "https://evil/x"},
        {"type": "object", "$defs": {"a": {"$anchor": "here", "type": "string"}}},
        {"type": "object", "$dynamicAnchor": "meta"},
        {"type": "object", "properties": {"p": {"$dynamicRef": "#meta"}}},
    ],
)
def test_id_and_anchor_rejected(schema):
    with pytest.raises(ValidationError) as info:
        ClientToolDecl(name="t", parameters=schema)
    assert _error_type(info.value) == "client_tool_schema_id_unsupported"


@pytest.mark.parametrize("ref", ["#", "#/properties/a", "#/$defs/a/type", "#here", "#/$defs/", "#/definitions/a"])
def test_only_direct_defs_refs(ref):
    schema = {"type": "object", "$defs": DEFS, "definitions": DEFS, "properties": {"a": {"$ref": ref}}}
    with pytest.raises(ValidationError) as info:
        ClientToolDecl(name="t", parameters=schema)
    assert _error_type(info.value) == "client_tool_schema_invalid"


def test_accepted_refs_validate_with_jsonschema():
    """What the contract accepts, a Draft 2020-12 validator (the worker's) applies without error."""
    import jsonschema

    schema = {"type": "object", "$defs": {"a": {"type": "string"}, "b": {"type": "array", "items": {"$ref": "#/$defs/a"}}},
              "properties": {"p": {"type": "object", "properties": {"q": {"$ref": "#/$defs/b"}}},
                             "r": {"anyOf": [{"$ref": "#/$defs/a"}, {"type": "null"}]}}}
    decl = ClientToolDecl(name="t", parameters=schema)
    validator = jsonschema.Draft202012Validator(decl.parameters)
    assert validator.is_valid({"p": {"q": ["x"]}, "r": None})
    assert not validator.is_valid({"p": {"q": [1]}})


def test_nested_schema_keyword_rejected():
    with pytest.raises(ValidationError) as info:
        ClientToolDecl(name="t", parameters={"type": "object", "properties": {
            "a": {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "string"}}})
    assert _error_type(info.value) == "client_tool_schema_invalid"


@pytest.mark.parametrize(
    "schema",
    [
        # MAJOR 2: ReDoS, as AC.1's subset.
        {"type": "object", "properties": {"a": {"type": "string", "pattern": "^(a+)+$"}}},
        {"type": "object", "patternProperties": {"^x": {"type": "string"}}},
        {"type": "object", "propertyNames": {"pattern": "^(a|aa)+$"}},
        {"type": "object", "$defs": {"d": {"pattern": "x"}}},
        {"type": "object", "properties": {"a": {"items": {"anyOf": [{"pattern": "x"}]}}}},
        {"type": "object", "properties": {"a": {"type": "string", "format": "regex"}}},
    ],
)
def test_pattern_rejected(schema):
    with pytest.raises(ValidationError) as info:
        ClientToolDecl(name="t", parameters=schema)
    assert _error_type(info.value) == "client_tool_schema_pattern_unsupported"


def test_property_named_pattern_is_fine():
    ClientToolDecl(name="grep", parameters={"type": "object", "properties": {
        "pattern": {"type": "string"}, "$id": {"type": "string"}}, "required": ["pattern"]})
    ClientToolDecl(name="t", parameters={"type": "object", "properties": {
        "a": {"type": "object", "default": {"pattern": "x", "$id": "y"}}}})


@pytest.mark.parametrize("version", [True, False, "3", 3.0, 2**31])
def test_version_strict_and_bounded(version):
    with pytest.raises(ValidationError):
        AgUiRunOptions.model_validate({"version": version})
    with pytest.raises(ValidationError):
        AgUiRunOptions.model_validate_json(f'{{"version": {json.dumps(version)}}}')


def test_version_upper_bound_ok():
    assert AgUiRunOptions.model_validate_json('{"version": 2147483647}').version == 2**31 - 1


@pytest.mark.parametrize("call_id", ["a b", "a\n", "é", "\t", "a\x7f"])
def test_tool_call_id_charset(call_id):
    with pytest.raises(ValidationError):
        ClientToolResult(tool_call_id=call_id)


def test_tool_call_id_printable_ascii():
    ClientToolResult(tool_call_id="toolu_01:AZ-az~!/" + "x" * 200)


@pytest.mark.parametrize("params", [{}, None])
def test_empty_parameters_pinned_for_agui5(params):
    """Pinned: AGUI.5 normalises an AG-UI zero-argument tool's empty/null parameters before this."""
    with pytest.raises(ValidationError) as info:
        ClientToolDecl(name="t", parameters=params)
    assert _error_type(info.value) == "client_tool_schema_not_object"


def test_list_content_pinned_for_agui5():
    """Pinned: AGUI.5 maps a 1.0 ToolMessage list content (text parts) to a string before this."""
    with pytest.raises(ValidationError):
        ClientToolResult(tool_call_id="c", content=[{"type": "text", "text": "x"}])


def test_errors_never_echo_input():
    secret = "SECRET-123"
    attempts = [
        lambda: ClientToolDecl(name="t", parameters={"type": "object", "properties": {"a": {"$ref": f"https://{secret}"}}}),
        lambda: ClientToolDecl(name=f"bad {secret}", parameters=OBJ),
        lambda: ClientToolResult(tool_call_id=f"{secret} x"),
        lambda: AgUiRunOptions.model_validate({"resourceId": f"{secret} x"}),
    ]
    for attempt in attempts:
        with pytest.raises(ValidationError) as info:
            attempt()
        assert secret not in str(info.value)
        assert secret not in repr(info.value)
