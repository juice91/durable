"""AGUI.1 acceptance tests: client tools vs server tools, caps, unicode, results, run options,
pickling, layering and the import-linter contract (beyond the implementer's tests)."""

from __future__ import annotations

import json
import pickle
import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from eap_schemas.client_tools import (
    MAX_CLIENT_TOOL_RESULT_BYTES,
    MAX_CLIENT_TOOL_SCHEMA_DEPTH,
    MAX_CLIENT_TOOL_SCHEMAS_BYTES,
    MAX_CLIENT_TOOLS,
    AgUiRunOptions,
    ClientToolDecl,
    ClientToolResult,
    ClientToolsInvalid,
    check_client_tools,
    client_tool_clashes,
)
from eap_schemas.tools import RESERVED_TOOL_NAMES

OBJ = {"type": "object", "properties": {"q": {"type": "string"}}}
REPO = Path(__file__).resolve().parents[3]


def _type(exc: ValidationError) -> str:
    return exc.errors()[0]["type"]


def _decl(name: str = "t", **kw) -> ClientToolDecl:
    return ClientToolDecl(name=name, parameters=kw.pop("parameters", OBJ), **kw)


# --- client tools versus agent / reserved / MCP / built-in tools ---------------------------------


def test_reserved_set_is_exactly_the_platform_names():
    assert set(RESERVED_TOOL_NAMES) == {"final_result", "retrieve", "update_working_memory"}


@pytest.mark.parametrize("name", sorted(RESERVED_TOOL_NAMES))
def test_every_reserved_name_is_refused_with_code(name):
    with pytest.raises(ValidationError) as info:
        _decl(name)
    assert _type(info.value) == "client_tool_shadows_server_tool"


def test_reserved_check_is_case_sensitive_but_clash_check_is_exact():
    assert _decl("Final_Result").name == "Final_Result"
    assert client_tool_clashes([_decl("Final_Result")], []) == []


def test_clashes_with_agent_builtin_and_mcp_names_in_order():
    tools = [_decl("zeta"), _decl("web__fetch"), _decl("mcp__gh__search"), _decl("mine")]
    server = ["mcp__gh__search", "web__fetch", "other"]
    assert client_tool_clashes(tools, server) == ["web__fetch", "mcp__gh__search"]


def test_mcp_prefixed_client_name_allowed_when_spec_lacks_it():
    tool = _decl("mcp__gh__search")
    assert client_tool_clashes([tool], ["mcp__gh__other"]) == []


def test_clash_between_mcp_and_builtin_name_reported_once_per_client_tool():
    assert client_tool_clashes([_decl("web__fetch")], ["web__fetch", "web__fetch"]) == ["web__fetch"]


def test_clashes_include_reserved_even_if_decl_built_bypassing_validation():
    bypass = ClientToolDecl.model_construct(name="retrieve", description="", parameters=OBJ)
    assert client_tool_clashes([bypass], []) == ["retrieve"]


def test_clashes_accepts_generators_and_empty():
    assert client_tool_clashes(iter([]), iter([])) == []
    assert client_tool_clashes((t for t in [_decl("a")]), (n for n in ["a"])) == ["a"]


# --- schema caps ---------------------------------------------------------------------------------


def test_depth_cap_is_exactly_32():
    def build(levels: int) -> dict:
        schema: dict = {"type": "object"}
        for _ in range(levels - 1):
            schema = {"type": "object", "additionalProperties": schema}
        return schema

    assert MAX_CLIENT_TOOL_SCHEMA_DEPTH == 32
    _decl(parameters=build(32))
    with pytest.raises(ValidationError) as info:
        _decl(parameters=build(33))
    assert _type(info.value) == "client_tool_schema_too_deep"


def test_deeply_nested_pathological_schema_does_not_recurse_to_crash():
    deep: dict = {"type": "object"}
    for _ in range(50_000):
        deep = {"type": "object", "not": deep}
    with pytest.raises(ValidationError) as info:
        _decl(parameters=deep)
    assert _type(info.value) == "client_tool_schema_too_deep"


def test_depth_counts_arrays_too():
    nested: list = [1]
    for _ in range(40):
        nested = [nested]
    schema = {"type": "object", "allOf": nested}
    with pytest.raises(ValidationError) as info:
        _decl(parameters=schema)
    assert _type(info.value) == "client_tool_schema_too_deep"


def test_size_cap_counts_utf8_bytes_not_characters():
    base = len(json.dumps({"description": "", "type": "object"}, separators=(",", ":"), ensure_ascii=False).encode())
    room = 64 * 1024 - base
    _decl(parameters={"type": "object", "description": "é" * (room // 2)})
    with pytest.raises(ValidationError) as info:
        _decl(parameters={"type": "object", "description": "é" * (room // 2 + 1)})
    assert _type(info.value) == "client_tool_schema_too_large"


def test_description_cap_4096_chars_not_bytes():
    assert len(_decl(description="é" * 4096).description) == 4096
    with pytest.raises(ValidationError) as info:
        _decl(description="a" * 4097)
    assert _type(info.value) == "string_too_long"


@pytest.mark.parametrize("bad", [None, [], "object", 3, [OBJ]])
def test_parameters_must_be_a_dict(bad):
    with pytest.raises(ValidationError) as info:
        _decl(parameters=bad)
    assert _type(info.value) == "client_tool_schema_not_object"


@pytest.mark.parametrize("schema", [{}, {"type": "string"}, {"type": ["object", "null"]}, {"properties": {}}])
def test_root_type_must_be_object(schema):
    with pytest.raises(ValidationError) as info:
        _decl(parameters=schema)
    assert _type(info.value) == "client_tool_schema_not_object"


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "properties": {"a": {"$ref": "https://evil.example/x.json"}}},
        {"type": "object", "properties": {"a": {"$ref": "file:///etc/passwd"}}},
        {"type": "object", "properties": {"a": {"$ref": "other.json#/x"}}},
        {"type": "object", "$dynamicRef": "http://x/y"},
        {"type": "object", "items": [{"allOf": [{"$ref": "//host/x"}]}]},
    ],
)
def test_remote_refs_rejected(schema):
    with pytest.raises(ValidationError) as info:
        _decl(parameters=schema)
    assert _type(info.value) == "client_tool_schema_remote_ref"


def test_unresolvable_and_resolvable_local_refs():
    ok = {"type": "object", "$defs": {"s": {"type": "string"}}, "properties": {"a": {"$ref": "#/$defs/s"}}}
    _decl(parameters=ok)
    bad = {"type": "object", "properties": {"a": {"$ref": "#/$defs/missing"}}}
    with pytest.raises(ValidationError) as info:
        _decl(parameters=bad)
    assert _type(info.value) == "client_tool_schema_invalid"


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "$schema": "http://json-schema.org/draft-07/schema#"},
        {"type": "object", "properties": {"a": {"type": "nope"}}},
        {"type": "object", "required": "a"},
        {"type": "object", "minProperties": -1},
    ],
)
def test_invalid_schemas(schema):
    with pytest.raises(ValidationError) as info:
        _decl(parameters=schema)
    assert _type(info.value) == "client_tool_schema_invalid"


def test_2020_12_dialect_declared_ok():
    _decl(parameters={"$schema": "https://json-schema.org/draft/2020-12/schema", **OBJ})


def test_non_json_parameters_rejected_without_crash():
    with pytest.raises(ValidationError):
        _decl(parameters={"type": "object", "default": {1, 2}})
    with pytest.raises(ValidationError):
        _decl(parameters={"type": "object", "default": float("nan")})


def test_error_messages_never_contain_secret_values():
    secret = "SECRET-VALUE-12345"
    for params in (
        {"type": "object", "properties": {"a": {"$ref": f"https://x/{secret}"}}},
        {"type": "object", "properties": {"a": {"type": secret}}},
        {"type": "object", "properties": {"a": {"$ref": f"#/{secret}"}}},
    ):
        with pytest.raises(ValidationError) as info:
            _decl(parameters=params)
        assert secret not in json.dumps([e["msg"] for e in info.value.errors()])


# --- unicode and whitespace in names -------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["café", "tool name", " tool", "tool ", "tool\n", "tool\t", "a​b", "ｔool", "工具", "a.b", "a/b",
     "🙂", "a\x00b", " ", "", "x" * 65, "á"],
)
def test_unicode_and_whitespace_names_rejected(name):
    with pytest.raises(ValidationError) as info:
        _decl(name)
    assert _type(info.value) == "string_pattern_mismatch"


def test_trailing_newline_is_not_accepted_by_dollar_anchor():
    with pytest.raises(ValidationError):
        _decl("ok\n")


def test_name_length_boundaries_and_charset():
    assert _decl("a" * 64).name == "a" * 64
    assert _decl("A-b_9").name == "A-b_9"


# --- ClientToolResult ----------------------------------------------------------------------------


def test_tool_call_id_cap_256():
    assert ClientToolResult(tool_call_id="i" * 256).tool_call_id == "i" * 256
    with pytest.raises(ValidationError) as info:
        ClientToolResult(tool_call_id="i" * 257)
    assert _type(info.value) == "string_too_long"


def test_result_content_boundary_utf8_bytes():
    ClientToolResult(tool_call_id="c", content="a" * MAX_CLIENT_TOOL_RESULT_BYTES)
    with pytest.raises(ValidationError) as info:
        ClientToolResult(tool_call_id="c", content="a" * (MAX_CLIENT_TOOL_RESULT_BYTES + 1))
    assert _type(info.value) == "client_tool_result_too_large"
    ClientToolResult(tool_call_id="c", content="é" * (MAX_CLIENT_TOOL_RESULT_BYTES // 2))
    with pytest.raises(ValidationError):
        ClientToolResult(tool_call_id="c", content="é" * (MAX_CLIENT_TOOL_RESULT_BYTES // 2 + 1))


def test_result_content_and_error_share_one_cap():
    half = MAX_CLIENT_TOOL_RESULT_BYTES // 2
    ClientToolResult(tool_call_id="c", content="a" * half, error="b" * half)
    with pytest.raises(ValidationError) as info:
        ClientToolResult(tool_call_id="c", content="a" * half, error="b" * (half + 1))
    assert _type(info.value) == "client_tool_result_too_large"


def test_result_content_is_opaque_text_not_json():
    for text in ("not json {", "", "null", '{"a": 1}', "\x00\ud800".encode("utf-8", "surrogatepass").decode("latin-1")):
        assert ClientToolResult(tool_call_id="c", content=text).content == text


@pytest.mark.parametrize("content", [None, 5, {"a": 1}, ["x"]])
def test_result_content_must_be_string(content):
    with pytest.raises(ValidationError):
        ClientToolResult(tool_call_id="c", content=content)


def test_result_lone_surrogate_does_not_crash_size_check():
    with pytest.raises((ValidationError, UnicodeEncodeError)):
        ClientToolResult(tool_call_id="c", content="\ud800")


def test_result_extra_and_frozen():
    with pytest.raises(ValidationError):
        ClientToolResult(tool_call_id="c", output="x")
    result = ClientToolResult(tool_call_id="c", error="boom")
    with pytest.raises(ValidationError):
        result.content = "x"


# --- AgUiRunOptions ------------------------------------------------------------------------------


@pytest.mark.parametrize("version", [1, 2, 10**6, "draft", None])
def test_version_accepts(version):
    assert AgUiRunOptions.model_validate({"version": version}).version == version


@pytest.mark.parametrize("version", [0, -1, "Draft", "latest", "", 1.5, [1], {}])
def test_version_rejects(version):
    with pytest.raises(ValidationError):
        AgUiRunOptions.model_validate({"version": version})


def test_model_override_shapes():
    opts = AgUiRunOptions.model_validate({"model": {"provider": "openai", "name": "gpt"}})
    assert opts.model is not None and opts.model.provider == "openai"
    for bad in ("openai:gpt", {"provider": "x"}, {"provider": "x", "name": "y", "extra": 1}, 3):
        with pytest.raises(ValidationError):
            AgUiRunOptions.model_validate({"model": bad})


def test_request_context_must_be_object():
    assert AgUiRunOptions.model_validate({"requestContext": {}}).request_context == {}
    for bad in ([], "x", 3):
        with pytest.raises(ValidationError):
            AgUiRunOptions.model_validate({"requestContext": bad})


@pytest.mark.parametrize("rid", ["a", "user:42", "a@b.c", "x_y-z.1", "r" * 128])
def test_resource_id_accepts(rid):
    assert AgUiRunOptions.model_validate({"resourceId": rid}).resource_id == rid


@pytest.mark.parametrize("rid", ["", "a b", "a/b", "é", "a\n", "r" * 129, 5])
def test_resource_id_rejects(rid):
    with pytest.raises(ValidationError):
        AgUiRunOptions.model_validate({"resourceId": rid})


@pytest.mark.parametrize("value", [None, 0, "", [], {}, {"a": [1, {"b": None}]}, "text", False])
def test_workflow_input_any_json_and_presence(value):
    opts = AgUiRunOptions.model_validate({"input": value})
    assert opts.input == value and opts.has_input


def test_input_absent_vs_null_survive_round_trip():
    absent = AgUiRunOptions.model_validate({})
    null = AgUiRunOptions.model_validate({"input": None})
    assert not absent.has_input and null.has_input
    dump = {"by_alias": True, "exclude_unset": True}
    assert AgUiRunOptions.model_validate_json(null.model_dump_json(**dump)).has_input
    assert not AgUiRunOptions.model_validate_json(absent.model_dump_json(**dump)).has_input


def test_unknown_fields_forbidden_and_snake_case_accepted_via_populate_by_name():
    for data in ({"foo": 1}, {"Version": 1}, {"request_context_x": {}}):
        with pytest.raises(ValidationError):
            AgUiRunOptions.model_validate(data)
    opts = AgUiRunOptions.model_validate({"request_context": {"a": 1}, "resource_id": "r"})
    assert opts.request_context == {"a": 1} and opts.resource_id == "r"


def test_run_options_frozen_and_json_alias_round_trip():
    opts = AgUiRunOptions.model_validate({"version": 2, "requestContext": {"k": "v"}, "resourceId": "r"})
    with pytest.raises(ValidationError):
        opts.version = 3
    wire = json.loads(opts.model_dump_json(by_alias=True))
    assert wire["requestContext"] == {"k": "v"} and wire["resourceId"] == "r"
    assert AgUiRunOptions.model_validate_json(json.dumps(wire)) == opts


# --- pickle and JSON round trips -----------------------------------------------------------------


def test_pickle_round_trips():
    decl = _decl("t", description="d")
    result = ClientToolResult(tool_call_id="c", content="x", error="e")
    opts = AgUiRunOptions.model_validate({"version": "draft", "input": None, "resourceId": "r"})
    for obj in (decl, result, opts):
        clone = pickle.loads(pickle.dumps(obj))
        assert clone == obj and type(clone) is type(obj)
    assert pickle.loads(pickle.dumps(opts)).has_input


def test_clients_invalid_pickles_keeps_code_and_str():
    err = pickle.loads(pickle.dumps(ClientToolsInvalid("client_tool_duplicate", "tools[1] repeats")))
    assert err.code == "client_tool_duplicate" and "tools[1]" in str(err) and isinstance(err, ValueError)


def test_json_round_trips():
    schema = {"$schema": "https://json-schema.org/draft/2020-12/schema", **OBJ}
    decl = ClientToolDecl(name="t", description="é", parameters=schema)
    assert ClientToolDecl.model_validate_json(decl.model_dump_json()) == decl
    result = ClientToolResult(tool_call_id="c", content="é\n")
    assert ClientToolResult.model_validate_json(result.model_dump_json()) == result


def test_list_limits_at_boundary():
    tools = [_decl(f"t{i}") for i in range(MAX_CLIENT_TOOLS)]
    check_client_tools(tools)
    check_client_tools([])
    with pytest.raises(ClientToolsInvalid) as info:
        check_client_tools([*tools, _decl("extra")])
    assert info.value.code == "client_tools_too_many"
    big = {"type": "object", "description": "x" * (MAX_CLIENT_TOOL_SCHEMAS_BYTES // 2)}
    with pytest.raises(ClientToolsInvalid) as info:
        check_client_tools([_decl("a", parameters=big), _decl("b", parameters=big)])
    assert info.value.code == "client_tools_schemas_too_large"


# --- layering ------------------------------------------------------------------------------------


def _modules_after_import(statement: str) -> set[str]:
    code = f"import sys, json\n{statement}\nprint(json.dumps(sorted(sys.modules)))"
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True, timeout=120)
    return set(json.loads(out.stdout.strip().split("\n")[-1]))


@pytest.mark.parametrize("statement", ["import eap_schemas", "import eap_processors", "import eap_processors, eap_schemas"])
def test_processors_and_schemas_package_do_not_pull_jsonschema(statement):
    mods = _modules_after_import(statement)
    assert not {m for m in mods if m == "jsonschema" or m.startswith("jsonschema.")}
    assert "eap_schemas.client_tools" not in mods


def test_importing_client_tools_does_import_jsonschema_lazily_only_on_validate():
    mods = _modules_after_import("import eap_schemas.client_tools")
    assert "jsonschema" not in mods  # the import is deferred to schema validation


def test_worker_import_paths_do_not_load_client_tools_in_schemas_package():
    mods = _modules_after_import("import eap_schemas, eap_runtime, eap_run_store")
    assert "eap_schemas.client_tools" not in mods
    assert "ag_ui" not in mods


# --- import-linter contract ----------------------------------------------------------------------

ROOTS = ["eap_schemas", "eap_runtime", "eap_run_store", "eap_api", "eap_worker", "eap_observability", "eap_db",
         "eap_secrets", "eap_processors", "eap_knowledge", "eap_pricing"]  # eap_pricing: the AC merge


def _agui_contract() -> dict:
    cfg = tomllib.loads((REPO / "pyproject.toml").read_text())["tool"]["importlinter"]
    contract = next(c for c in cfg["contracts"] if "ag_ui" in c["name"])
    assert cfg["root_packages"] == ROOTS and set(contract["source_modules"]) == set(ROOTS)
    return {k: v for k, v in contract.items()}


def _toml(value) -> str:
    return json.dumps(value)  # JSON scalars/arrays are valid TOML


def _lint(tmp_path: Path, files: dict[str, str]) -> subprocess.CompletedProcess:
    contract = _agui_contract()
    for root in ROOTS:
        (tmp_path / root).mkdir(parents=True, exist_ok=True)
        (tmp_path / root / "__init__.py").write_text("")
    (tmp_path / "ag_ui").mkdir()
    (tmp_path / "ag_ui" / "__init__.py").write_text("")
    (tmp_path / "ag_ui" / "core.py").write_text("X = 1\n")
    for rel, body in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        for parent in path.parents:
            if parent == tmp_path:
                break
            if not (parent / "__init__.py").exists():
                (parent / "__init__.py").write_text("")
    lines = ["[tool.importlinter]", f"root_packages = {_toml(ROOTS)}", "include_external_packages = true",
             "[[tool.importlinter.contracts]]"]
    lines += [f"{k} = {_toml(v)}" for k, v in contract.items()]
    (tmp_path / "pyproject.toml").write_text("\n".join(lines) + "\n")
    exe = Path(sys.executable).parent / "lint-imports"
    return subprocess.run([str(exe)], cwd=tmp_path, capture_output=True, text=True, timeout=120)


def test_contract_allows_ag_ui_only_inside_eap_api_ag_ui(tmp_path):
    result = _lint(tmp_path, {
        "eap_api/ag_ui/__init__.py": "import ag_ui\n",
        "eap_api/ag_ui/translate.py": "from ag_ui.core import X\n",
        "eap_api/ag_ui/sub/deep.py": "import ag_ui.core\n",
        "eap_api/other.py": "from eap_api.ag_ui import translate\n",
    })
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 kept, 0 broken" in result.stdout


@pytest.mark.parametrize(
    "module",
    ["eap_schemas/probe.py", "eap_worker/probe.py", "eap_api/probe.py", "eap_api/routes/probe.py",
     "eap_api/ag_ui_extra.py", "eap_runtime/probe.py", "eap_run_store/probe.py", "eap_db/probe.py",
     "eap_observability/probe.py", "eap_secrets/probe.py", "eap_processors/probe.py", "eap_knowledge/probe.py",
     "eap_pricing/probe.py"],
)
@pytest.mark.parametrize("statement", ["import ag_ui\n", "from ag_ui.core import X\n", "import ag_ui.core\n"])
def test_contract_breaks_for_every_module_outside_eap_api_ag_ui(tmp_path, module, statement):
    result = _lint(tmp_path, {module: statement})
    assert result.returncode != 0, result.stdout
    assert "BROKEN" in result.stdout
    dotted = module.removesuffix(".py").replace("/", ".")
    assert dotted in result.stdout
