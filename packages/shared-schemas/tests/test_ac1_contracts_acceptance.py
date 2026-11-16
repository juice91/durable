"""AC.1 acceptance: contracts compared with main, boundaries, messages and round-trips.

Pre-AC main's eap_schemas (PRE_AC_MAIN) is loaded with `git show <commit>:<path>` into a temp package and run in a
subprocess (`python -I`), so hashes, canonical JSON and the working-memory validator are compared
byte for byte with the pre-AC.1 implementation.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
import pickle
import subprocess
import sys
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from pydantic import TypeAdapter, ValidationError

from eap_schemas import (
    AgentSpec,
    OutputInvalid,
    RunBudgetExceeded,
    SchemaSubsetInvalid,
    UsageLimitsSpec,
    canonical_spec_hash,
)
from eap_schemas import json_schema_subset as subset
from eap_schemas.approvals import (
    DENIED_MESSAGE,
    EVAL_DENIED_MESSAGE,
    TIMED_OUT_MESSAGE,
    ApprovalPolicy,
    denial_message,
)
from eap_schemas.evals import (
    EvalCase,
    EvalSuiteInvalid,
    EvalSuiteSnapshot,
    EvaluatorSpec,
    JudgeVerdict,
    field_path_value,
)
from eap_schemas.memory import WorkingMemoryInvalid, validate_working_memory_schema
from eap_schemas.output import OutputSpec

HERE = Path(__file__).resolve()
REPO = next(p for p in HERE.parents if (p / ".git").exists())
MODEL = {"provider": "test", "name": "echo"}
BASE = {"spec_version": 1, "name": "n", "instructions": "i", "model": MODEL}

# --- main's implementation, run in a subprocess --------------------------------------------------

RUNNER = r'''
import json, sys
src, inp, out = sys.argv[1:4]
sys.path.insert(0, src)
import eap_schemas
assert eap_schemas.__file__.startswith(src), eap_schemas.__file__
from eap_schemas import AgentSpec, canonical_spec_hash
from eap_schemas.memory import validate_working_memory_schema

def build(v):
    if isinstance(v, dict):
        if set(v) == {"$deep"}:
            node = {"type": "string"}
            for _ in range(v["$deep"]):
                node = {"a": node}
            return node
        if set(v) == {"$py"}:
            return object()
        return {k: build(x) for k, x in v.items()}
    if isinstance(v, list):
        return [build(x) for x in v]
    return v

data = json.load(open(inp))
res = {"specs": [], "schemas": []}
for raw in data["specs"]:
    try:
        spec = AgentSpec.model_validate(raw)
        text = json.dumps(spec.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        res["specs"].append(["ok", text, canonical_spec_hash(spec)])
    except Exception as exc:
        res["specs"].append(["err", type(exc).__name__, str(exc)])
for raw in data["schemas"]:
    try:
        validate_working_memory_schema(build(raw))
        res["schemas"].append(["ok"])
    except Exception as exc:
        res["schemas"].append(["err", type(exc).__name__, str(exc), getattr(exc, "path", None)])
json.dump(res, open(out, "w"))
'''


# The last main commit before AC landed (main now contains AC, so `main` itself can no longer serve
# as the pre-AC baseline).
PRE_AC_MAIN = "8acdda2b7d6a4c911a17a6455f7cd8a823aac93f"


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO, check=True, capture_output=True, text=True).stdout


@pytest.fixture(scope="module")
def main_src(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("main_schemas")
    prefix = "packages/shared-schemas/src/eap_schemas/"
    for name in _git("ls-tree", "-r", "--name-only", PRE_AC_MAIN, prefix).split():
        target = root / "eap_schemas" / name.removeprefix(prefix)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_git("show", f"{PRE_AC_MAIN}:{name}"))
    assert not (root / "eap_schemas" / "evals.py").exists()  # pre-AC main has none of the AC.1 modules
    return root


def _run(src: Path, specs: list, schemas: list, tmp: Path) -> dict:
    (tmp / "in.json").write_text(json.dumps({"specs": specs, "schemas": schemas}))
    (tmp / "runner.py").write_text(RUNNER)
    subprocess.run(
        [sys.executable, "-I", str(tmp / "runner.py"), str(src), str(tmp / "in.json"), str(tmp / "out.json")],
        check=True, capture_output=True, text=True,
    )
    return json.loads((tmp / "out.json").read_text())


def _both(main_src: Path, tmp_path: Path, specs: list, schemas: list) -> tuple[dict, dict]:
    (tmp_path / "m").mkdir(exist_ok=True)
    (tmp_path / "n").mkdir(exist_ok=True)
    return (
        _run(main_src, specs, schemas, tmp_path / "m"),
        _run(REPO / "packages/shared-schemas/src", specs, schemas, tmp_path / "n"),
    )


# --- harvest every spec shape the repo's Python tests build ---------------------------------------

_SKIP_DIRS = {"node_modules", ".venv", ".git", ".next", "__pycache__"}


class _Unresolved(Exception):
    pass


def _ev(node: ast.AST, env: dict[str, Any]) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in env:
            return copy.deepcopy(env[node.id])
        raise _Unresolved(node.id)
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_ev(e, env) for e in node.elts]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_ev(node.operand, env)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
        return _ev(node.left, env) * _ev(node.right, env)
    if isinstance(node, ast.Dict):
        out: dict[str, Any] = {}
        for key, value in zip(node.keys, node.values):
            if key is None:
                out.update(_ev(value, env))
            else:
                out[_ev(key, env)] = _ev(value, env)
        return out
    if isinstance(node, ast.Call) and not node.args and all(k.arg for k in node.keywords):
        return {k.arg: _ev(k.value, env) for k in node.keywords}  # ModelRef(...), AgentSpec(...)
    raise _Unresolved(type(node).__name__)


def _is_spec(value: Any) -> bool:
    model = value.get("model") if isinstance(value, dict) else None
    return isinstance(model, dict) and "provider" in model and ("name" in value or "instructions" in value)


def _harvest_py(path: Path) -> list[dict]:
    try:
        tree = ast.parse(path.read_text())
    except (SyntaxError, UnicodeDecodeError):
        return []
    env: dict[str, Any] = {}
    nodes = sorted(
        (n for n in ast.walk(tree) if hasattr(n, "lineno")), key=lambda n: (n.lineno, n.col_offset)
    )
    found = []
    for node in nodes:  # in source order: plain-data assignments (any scope) feed later dicts
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            try:
                env[node.targets[0].id] = _ev(node.value, env)
            except (_Unresolved, TypeError, AttributeError, ValueError):
                pass
        if isinstance(node, (ast.Dict, ast.Call)):
            try:
                value = _ev(node, env)
            except (_Unresolved, TypeError, AttributeError, ValueError):
                continue
            if _is_spec(value):
                found.append(value)
    return found


def harvest_specs() -> list[dict]:
    seen: dict[str, dict] = {}
    for path in sorted(REPO.rglob("*.py")):
        if _SKIP_DIRS & set(path.parts) or path == HERE or path.name in OWN:
            continue
        if not ("test" in path.name or "tests" in " ".join(path.parts)):
            continue
        for spec in _harvest_py(path):
            seen.setdefault(json.dumps(spec, sort_keys=True, default=str), spec)
    for path in sorted(REPO.rglob("*.json")):
        if _SKIP_DIRS & set(path.parts):
            continue
        try:
            value = json.loads(path.read_text())
        except (ValueError, UnicodeDecodeError):
            continue
        if _is_spec(value):
            seen.setdefault(json.dumps(value, sort_keys=True), value)
    return list(seen.values())


# Records every dict AgentSpec is validated from while the existing suites run (dynamic harvest).
RECORDER = r'''
import atexit, json, os
from eap_schemas import AgentSpec
rows = []
def _rec(v):
    try:
        rows.append(json.loads(json.dumps(v, default=str)))
    except Exception:
        pass
_mv = AgentSpec.model_validate.__func__
def model_validate(cls, obj, *a, **k):
    if cls is AgentSpec and isinstance(obj, dict):
        _rec(obj)
    return _mv(cls, obj, *a, **k)
AgentSpec.model_validate = classmethod(model_validate)
_init = AgentSpec.__init__
def init(self, **kw):
    _rec(kw)
    _init(self, **kw)
AgentSpec.__init__ = init
atexit.register(lambda: open(os.environ["AC1_RECORD"], "w").write(json.dumps(rows)))
'''
SUITES = ("packages/shared-schemas/tests", "packages/agent-runtime/runtime_tests")
# test_model_settings.py builds specs with model settings that main (the comparison base) rejects.
OWN = ("test_ac1_contracts.py", "test_ac1_contracts_acceptance.py", "test_model_settings.py", "test_processors.py")


@pytest.fixture(scope="module")
def all_specs(tmp_path_factory) -> list[dict]:
    """Static harvest of every Python test and JSON fixture, plus the dicts the DB-free suites
    (shared-schemas, agent-runtime) actually validate when they run."""
    tmp = tmp_path_factory.mktemp("recorder")
    (tmp / "ac1_record.py").write_text(RECORDER)
    out = tmp / "out.json"
    ignores = [f"--ignore=packages/shared-schemas/tests/{name}" for name in OWN]
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *SUITES, "-q", "-p", "no:eap_db", "-p", "ac1_record", *ignores],
        cwd=REPO, capture_output=True, text=True, env={**os.environ, "AC1_RECORD": str(out), "PYTHONPATH": str(tmp)},
    )
    assert proc.returncode == 0, proc.stdout[-2000:]
    seen: dict[str, dict] = {}
    for spec in [*json.loads(out.read_text()), *harvest_specs()]:
        seen.setdefault(json.dumps(spec, sort_keys=True), spec)
    return list(seen.values())


# --- 1. hash stability against main ---------------------------------------------------------------

KB1 = "00000000-0000-0000-0000-0000000000a1"
KB2 = "00000000-0000-0000-0000-0000000000a2"
EXTRA_SPECS = [
    BASE,
    {"name": "Minimal", "model": MODEL},
    {**BASE, "tools": ["tickets.create", "clock.now"], "model_settings": {"temperature": 0.2}},
    {**BASE, "tools": [], "mcp_tools": [], "input_processors": [], "output_processors": [], "context_fields": []},
    {**BASE, "mcp_tools": [{"server": "docs", "tool": "search"}, {"server": "gh", "tool": "issues.list"}]},
    {**BASE, "input_processors": [{"kind": "pii_redact", "config": {}}]},
    {**BASE, "context_fields": [{"key": "tier", "description": "Plan tier"}]},
    {**BASE, "knowledge_bases": []},
    {**BASE, "knowledge_bases": [{"id": KB1}, {"id": KB2, "top_k": 3, "min_score": 0.25}]},
    {**BASE, "knowledge_bases": [{"id": KB1, "filter": {"lang": "en", "tag": ["a", "b"]}}]},
    {**BASE, "memory": {}},
    {**BASE, "memory": {"history": {"last_turns": 5}}},
    {**BASE, "memory": {"working": {"scope": "resource", "format": "json", "json_schema": {
        "type": "object", "properties": {"n": {"type": "integer"}, "t": {"type": "array", "items": {"type": "string"}}}}}}},
    {**BASE, "memory": {"working": {"template": "# User\n- Name:"}, "history": {"last_turns": 2}}},
    {**BASE, "memory": {"observational": {}, "semantic_recall": {}, "redact": {}}},
    {**BASE, "tools": ["clock.now"], "mcp_tools": [{"server": "docs", "tool": "search"}],
     "knowledge_bases": [{"id": KB1}], "memory": {"working": {}}, "model_settings": {"max_tokens": 500}},
    {**BASE, "tools": ["retrieve"]},  # stored specs with a reserved name keep validating
    {**BASE, "tools": ["final_result"]},  # final_result is reserved only when `output` is set
]


def _same(main: list, new: list) -> None:
    assert len(main) == len(new)
    for a, b in zip(main, new):
        assert a[0] == b[0] == a[0]
        if a[0] == "ok":
            assert a == b  # canonical JSON text and sha256, byte for byte
        else:
            assert a[:2] == b[:2]


def test_every_existing_spec_shape_keeps_canonical_json_and_hash(main_src, tmp_path, all_specs):
    assert len(all_specs) > 100  # distinct shapes found in the repo
    keys = {k for spec in all_specs for k in spec}
    assert {"memory", "knowledge_bases", "input_processors", "output_processors", "mcp_tools",
            "context_fields", "model_settings", "tools"} <= keys
    main, new = _both(main_src, tmp_path, [*all_specs, *EXTRA_SPECS], [])
    ok = [a for a in main["specs"] if a[0] == "ok"]
    assert len(ok) > 60
    _same(main["specs"], new["specs"])


# --- 2. working-memory validator: identical before and after the delegation -----------------------


def _obj(**props: Any) -> dict:
    return {"type": "object", "properties": props}


def _nest(levels: int) -> dict:
    node: dict = {"type": "string"}
    for _ in range(levels - 1):
        node = _obj(a=node)
    return node if levels > 1 else _obj(a=node)


def _wide(count: int) -> dict:
    return _obj(**{f"p{i}": {"type": "string"} for i in range(count)})


def _wm_schemas() -> list[Any]:
    leaf = [
        {"type": t} for t in ("string", "number", "integer", "boolean", "object", "array", "null", "date", "", None, 1, ["string"])
    ]
    keywords = [
        {"$ref": "#/x"}, {"$defs": {}}, {"pattern": "^a$"}, {"patternProperties": {}}, {"anyOf": []}, {"oneOf": []},
        {"allOf": []}, {"not": {}}, {"const": "a"}, {"minimum": 1}, {"maximum": 2}, {"minLength": 1}, {"maxLength": 1},
        {"minItems": 1}, {"maxItems": 1}, {"format": "email"}, {"default": 1}, {"examples": []}, {"$id": "x"},
        {"description": "d"}, {"description": 1}, {"title": "t"}, {"title": None}, {"required": ["a"]},
        {"additionalProperties": False}, {"additionalProperties": {"type": "string"}}, {"unknown": 1},
    ]
    out: list[Any] = [{"type": "object"}, {"type": "object", "properties": {}}, {}, [], "x", 1, None, True, {"$py": 1}]
    out += [{"type": "string"}, {"type": "array", "items": {"type": "string"}}, {"type": ["object", "null"]}]
    out += [_obj(a=t) for t in leaf] + [_obj(a={**t, "description": "d"}) for t in leaf[:4]]
    out += [_obj(a={"type": "string", **k}) for k in keywords]
    out += [_obj(a={"type": "array", "items": {"type": "string"}, **k}) for k in keywords]
    out += [_obj(a={"type": "object", "properties": {}, **k}) for k in keywords]
    out += [{"type": "object", **k} for k in keywords]
    out += [_obj(a={"enum": ["x"], **k}) for k in keywords]
    return out


def _wm_schemas_2() -> list[Any]:
    arr = lambda items: _obj(a={"type": "array", "items": items})  # noqa: E731
    enums = [
        [], ["a"], ["a", "a"], [1, 1.0], [1, True], [0, False], [True, True], ["a", 1, True], [None], ["a", None],
        [{"x": 1}], [["a"]], [float("nan")], [float("inf")], list(range(63)), list(range(64)), list(range(65)),
        [str(i) for i in range(64)], [str(i) for i in range(65)], "abc", None, 5, {"a": 1},
    ]
    out: list[Any] = [_obj(a={"enum": e}) for e in enums]
    out += [_obj(a={"type": t, "enum": e}) for t in ("string", "integer", "number", "boolean", "object", "array")
            for e in (["x"], [1], [1.5], [True], [1, 2.0], [None])]
    out += [arr({"type": t}) for t in ("string", "number", "integer", "boolean", "object", "array", "null")]
    out += [arr({"enum": ["a", "b"]}), arr({"enum": [None]}), arr({"type": "array", "items": {"type": "string"}}),
            arr(_obj(x={"type": "string"})), arr({}), arr(None), arr([]), arr("string"), _obj(a={"type": "array"})]
    out += [_obj(a={"required": ["x"], "type": "object", "properties": {"x": {"type": "string"}}})]
    out += [{"type": "object", "required": r, "properties": {"a": {"type": "string"}}}
            for r in (["a"], ["b"], ["a", "a"], "a", [1], [], None)]
    out += [{"type": "object", "properties": p} for p in ([], None, "x", {"": {"type": "string"}}, {"a" * 64: {"type": "string"}},
                                                          {"a" * 65: {"type": "string"}}, {"a": None}, {"a": []}, {"a": "string"}, {1: 2})]
    out += [{"type": "object", "additionalProperties": a} for a in (True, False, None, 1, "x", {})]
    out += [{**{"type": "object", "properties": {}}, "$schema": s} for s in ("http://x", 1, None, [], {})]
    out += [_nest(n) for n in range(1, 7)] + [_wide(n) for n in (1, 49, 50, 51, 52, 100)]
    out += [_obj(a=_wide(25), b=_wide(25)), _obj(a=_wide(25), b=_wide(26)), _obj(a=_obj(b=_wide(49)))]
    out += [{"type": "object", "description": "x" * n} for n in (100, 16000, 16300, 16340, 16384, 16385, 20000)]
    out += [{"type": "object", "description": "é" * n} for n in (8000, 8150, 8160, 8170, 9000)]
    out += [{"type": "object", "properties": {"a": {"type": "string", "description": "é" * n}}} for n in (8100, 8150, 8200)]
    out += [{"$deep": n} for n in (1, 3, 15, 16, 17, 40, 3000)]
    out += [_obj(a={"type": "string", "description": {"$py": 1}}), {"type": "object", "description": {"$py": 1}}]
    out += [{"type": "object", "properties": {"a": {"type": "string", "enum_x": 1}}, "x-extra": 1}]
    return out


@pytest.mark.parametrize("make", [_wm_schemas, _wm_schemas_2])
def test_working_memory_validator_matches_main(main_src, tmp_path, make):
    schemas = make()
    assert len(schemas) >= 100
    main, new = _both(main_src, tmp_path, [], schemas)
    assert len(main["schemas"]) == len(new["schemas"]) == len(schemas)
    for raw, a, b in zip(schemas, main["schemas"], new["schemas"]):
        assert a == b, json.dumps(raw)[:200]
    kinds = {a[0] for a in main["schemas"]}
    assert kinds == {"ok", "err"}
    # every error is the same WorkingMemoryInvalid class, never a bare TypeError/RecursionError
    assert {a[1] for a in main["schemas"] if a[0] == "err"} == {"WorkingMemoryInvalid"}


def test_working_memory_limits_and_exports_unchanged():
    from eap_schemas import memory

    assert (memory.MAX_SCHEMA_DEPTH, memory.MAX_SCHEMA_PROPERTIES, memory.MAX_SCHEMA_ENUM_VALUES) == (3, 50, 64)
    assert (memory.MAX_SCHEMA_BYTES, memory.MAX_JSON_DEPTH) == (8 * 1024, 16)
    with pytest.raises(WorkingMemoryInvalid) as info:
        validate_working_memory_schema({"type": "object", "properties": {"a": {"const": "x"}}})
    assert (info.value.path, str(info.value)) == (
        "a", "working memory is invalid at a: unsupported keyword 'const'")
    assert info.value.code == "working_memory_invalid"
    assert not isinstance(info.value, SchemaSubsetInvalid)


# --- 3. output subset -----------------------------------------------------------------------------

SECRET = "SECRET-VALUE-4711"


def _padded(size: int) -> dict:
    """An object schema whose canonical JSON is exactly `size` bytes."""
    base = {"type": "object", "description": ""}
    pad = size - subset.json_size(base)
    return {**base, "description": "x" * pad}


# (schema, path, reason): each rejection with its exact path and message.
EXACT_REJECTIONS = [
    ({"type": "object", "properties": {"a": {"$ref": "#/$defs/x"}}}, "a", "unsupported keyword '$ref'"),
    ({"type": "object", "$defs": {}}, "(root)", "unsupported keyword '$defs'"),
    ({"type": "object", "properties": {"a": {"type": "string", "pattern": "^a$"}}}, "a", "unsupported keyword 'pattern'"),
    ({"type": "object", "patternProperties": {}}, "(root)", "unsupported keyword 'patternProperties'"),
    ({"type": "object", "properties": {"a": {"anyOf": [{"type": "string"}]}}}, "a", "unsupported keyword 'anyOf'"),
    ({"type": "object", "properties": {"a": {"oneOf": []}}}, "a", "unsupported keyword 'oneOf'"),
    ({"type": "object", "properties": {"a": {"allOf": []}}}, "a", "unsupported keyword 'allOf'"),
    ({"type": "object", "properties": {"a": {"not": {}}}}, "a", "unsupported keyword 'not'"),
    ({"type": "object", "properties": {"a": {"type": "object", "properties": {"b": {"type": "array", "items": {
        "type": "object", "properties": {"c": {"$ref": "x"}}}}}}}}, "a.b[].c", "unsupported keyword '$ref'"),
    ({"type": "object", "properties": {"a": {"type": "string", "minimum": 1}}}, "a", "unsupported keyword 'minimum' on a string"),
    ({"type": "object", "properties": {"a": {"type": "integer", "minLength": 1}}}, "a", "unsupported keyword 'minLength' on a integer"),
    ({"type": "object", "properties": {"a": {"type": "array", "items": {"type": "string"}, "pattern": "x"}}}, "a", "unsupported keyword 'pattern'"),
    ({"type": "object", "properties": {"a": {"type": "object", "properties": {}, "items": {}}}}, "a", 'unsupported keyword "items" on an object'),
    ({"type": "object", "properties": {"a": {"enum": ["x"], "minLength": 1}}}, "a", "unsupported keyword 'minLength' on an enum"),
    ({"type": "object", "properties": {"a": {"const": 1, "enum": [1]}}}, "a", "unsupported keyword 'const' on an enum"),
    ({"type": "object", "properties": {"a": {"const": 1, "minimum": 0}}}, "a", "unsupported keyword 'minimum' on a const"),
    ({"type": "array", "items": {"type": "string"}}, "", 'the root must have "type": "object"'),
    ({"type": ["object", "null"]}, "", 'the root must have "type": "object"'),
    ({"properties": {}}, "", 'the root must have "type": "object"'),
    ({"type": "object", "properties": {"a": {"type": ["string", "integer"]}}}, "a", '"type" must be one type or a list of one type and "null"'),
    ({"type": "object", "properties": {"a": {"type": ["null", "null"]}}}, "a", '"type" must be one type or a list of one type and "null"'),
    ({"type": "object", "properties": {"a": {"type": ["string"]}}}, "a", '"type" must be one type or a list of one type and "null"'),
    ({"type": "object", "properties": {"a": {"type": 7}}}, "a", '"type" must be one type or a list of one type and "null"'),
    ({"type": "object", "properties": {"a": {"type": "date"}}}, "a", "unsupported type"),
    ({"type": "object", "properties": {"a": {"type": "array"}}}, "a", 'an array needs "items"'),
    ({"type": "object", "properties": {"a": {}}}, "a", 'a schema needs a "type", an "enum" or a "const"'),
    ({"type": "object", "properties": {"a": "string"}}, "a", "a schema must be a JSON object"),
    ({"type": "object", "properties": {"a": {"type": "integer", "minimum": 5, "maximum": 1}}}, "a", '"minimum" must not be greater than "maximum"'),
    ({"type": "object", "properties": {"a": {"type": "string", "minLength": 3, "maxLength": 2}}}, "a", '"minLength" must not be greater than "maxLength"'),
    ({"type": "object", "properties": {"a": {"type": "string", "minLength": -1}}}, "a", '"minLength" must be a non-negative integer'),
    ({"type": "object", "properties": {"a": {"type": "number", "maximum": True}}}, "a", '"maximum" must be a finite number'),
    ({"type": "object", "properties": {"a": {"type": "string", "format": 1}}}, "a", '"format" must be a string'),
    ({"type": "object", "properties": {"a": {"type": "string", "description": 1}}}, "a", '"description" must be a string'),
    ({"type": "object", "properties": {}, "required": ["ghost"]}, "(root)", '"required" may only name properties declared in "properties"'),
    ({"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a", "a"]}, "(root)", '"required" names must be unique'),
    ({"type": "object", "required": "a"}, "(root)", '"required" must be a list of property names'),
    ({"type": "object", "additionalProperties": {"type": "string"}}, "(root)", '"additionalProperties" must be true or false'),
    ({"type": "object", "properties": []}, "(root)", '"properties" must be an object'),
    ({"type": "object", "properties": {"a": {"enum": []}}}, "a", '"enum" must be a list of 1 to 256 values'),
    ({"type": "object", "properties": {"a": {"enum": list(range(257))}}}, "a", '"enum" must be a list of 1 to 256 values'),
    ({"type": "object", "properties": {"a": {"enum": ["x", "x"]}}}, "a", '"enum" values must be unique'),
    ({"type": "object", "properties": {"a": {"enum": [{"x": 1}]}}}, "a", '"enum" values must be strings, numbers, booleans or null'),
    ({"type": "object", "properties": {"a": {"type": "string", "enum": [1]}}}, "a", '"enum" values must all be of type string'),
    ({"type": "object", "properties": {"a": {"type": "object", "enum": ["x"]}}}, "a", 'an "enum" may only have a scalar "type"'),
    ({"type": "object", "properties": {"a": {"const": [1]}}}, "a", '"const" must be a string, number, boolean or null'),
    ({"type": "object", "properties": {"a": {"type": "string", "const": 1}}}, "a", '"const" must be of type string'),
    ({"type": "object", "properties": {"a": {"type": "array", "const": 1}}}, "a", 'a "const" may only have a scalar "type"'),
    ({"type": "object", "$schema": 5}, "", '"$schema" must be a string'),
    ({"type": "object", "properties": {"": {"type": "string"}}}, "(root)", "property names must match ^[A-Za-z0-9_-]{1,64}$"),
    ({"type": "object", "properties": {"a" * 65: {"type": "string"}}}, "(root)", "property names must match ^[A-Za-z0-9_-]{1,64}$"),
    ("not a dict", "", "the schema must be a JSON object"),
    ([], "", "the schema must be a JSON object"),
    ({"type": "object", "properties": {"a": {"type": "string", "description": {"$py": 1}}}}, "", "the schema must be JSON"),
]


def _materialise(value: Any) -> Any:
    if isinstance(value, dict):
        return object() if set(value) == {"$py"} else {k: _materialise(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_materialise(v) for v in value]
    return value


@pytest.mark.parametrize(("schema", "path", "reason"), EXACT_REJECTIONS)
def test_output_subset_rejects_with_exact_path_and_message(schema, path, reason):
    with pytest.raises(SchemaSubsetInvalid) as info:
        subset.validate_output_schema(_materialise(schema))
    exc = info.value
    assert (exc.path, exc.reason) == (path, reason)
    where = f" at {path}" if path else ""
    assert str(exc) == f"the JSON Schema is invalid{where}: {reason}"
    assert exc.code == "json_schema_invalid"
    # and OutputSpec reports the same text (the ValidationError message part)
    if isinstance(schema, dict) and "$py" not in json.dumps(schema):
        with pytest.raises(ValidationError) as spec_info:
            OutputSpec.model_validate({"json_schema": schema})
        assert spec_info.value.errors()[0]["msg"] == f"Value error, {exc}"


def test_output_subset_rejections_never_echo_a_value():
    secret_schemas = [
        {"type": "object", "properties": {"a": {"type": "string", "description": SECRET, "pattern": 1}}},
        {"type": "object", "properties": {"a": {"description": 5, "type": "string", "title": SECRET}}},
        {"type": "object", "properties": {"a": {"enum": [SECRET, SECRET]}}},
        {"type": "object", "properties": {"a": {"enum": [SECRET, {"k": SECRET}]}}},
        {"type": "object", "properties": {"a": {"type": "integer", "enum": [SECRET]}}},
        {"type": "object", "properties": {"a": {"const": [SECRET]}}},
        {"type": "object", "properties": {"a": {"type": "string", "const": 1, "title": SECRET}}},
        {"type": "object", "properties": {"a": {"type": "string", "format": [SECRET]}}},
        {"type": "object", "properties": {"a": {"type": "string", "minLength": SECRET}}},
        {"type": "object", "properties": {"a": {"type": "number", "minimum": SECRET}}},
        {"type": "object", "properties": {"a": {"type": [SECRET, "null"]}}},
        {"type": "object", "properties": {"a": {"type": "array", "items": SECRET}}},
        {"type": "object", "additionalProperties": SECRET},
        {"type": "object", "required": [SECRET]},
        {"type": "object", "required": SECRET},
        {"type": "object", "properties": SECRET},
        {"type": "object", "properties": {"a": SECRET}},
        {"type": "object", "$schema": [SECRET]},
        {"type": "object", "title": [SECRET]},
        {"type": [SECRET]},
        {"type": "object", "description": SECRET * 2000},
        {"type": "object", "properties": {"a": {"type": "object", "properties": {"b": {"type": "string", "const": SECRET, "enum": [SECRET]}}}}},
    ]
    for schema in secret_schemas:
        with pytest.raises(SchemaSubsetInvalid) as info:
            subset.validate_output_schema(schema)
        exc = info.value
        assert SECRET not in str(exc) and SECRET not in exc.reason and SECRET not in exc.path
        assert SECRET not in repr(exc) and SECRET not in repr(exc.args)
        with pytest.raises(ValidationError) as spec_info:
            OutputSpec.model_validate({"json_schema": schema})
        assert all(SECRET not in e["msg"] for e in spec_info.value.errors())


def test_unsupported_type_does_not_echo_the_type_value():
    """The module promises messages that name a path and the rule, never a value."""
    for schema in ({"type": "object", "properties": {"a": {"type": SECRET}}}, {"type": SECRET}):
        with pytest.raises(SchemaSubsetInvalid) as info:
            subset.validate_output_schema(schema)
        assert SECRET not in str(info.value)


def _tree(depth_objects: int, *, via_array: bool = False) -> dict:
    """A root object with `depth_objects` nested objects in total (root = 1), optionally with an
    array between every two objects (arrays must not add a level)."""
    node: dict = {"type": "object", "properties": {"leaf": {"type": "string"}}}
    for _ in range(depth_objects - 1):
        child = {"type": "array", "items": node} if via_array else node
        node = {"type": "object", "properties": {"a": child}}
    return node


@pytest.mark.parametrize("via_array", [False, True])
def test_depth_boundary_is_five_objects_and_arrays_do_not_count(via_array):
    assert subset.validate_output_schema(_tree(5, via_array=via_array))
    with pytest.raises(SchemaSubsetInvalid) as info:
        subset.validate_output_schema(_tree(6, via_array=via_array))
    if via_array:  # the raw JSON nesting cap (16) trips first: each schema level adds 3 JSON levels
        assert info.value.reason == "the schema nests deeper than 16 levels"
    else:
        assert info.value.reason == "objects may be nested at most 5 deep"
        assert info.value.path == "a.a.a.a.a"  # the sixth object
    # an array of arrays of objects is still one object level
    deep_arrays = {"type": "array", "items": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}}}
    assert subset.validate_output_schema({"type": "object", "properties": {"a": deep_arrays}})


def test_property_count_boundary_is_100_in_total():
    def wide(*groups: int) -> dict:
        return {"type": "object", "properties": {
            f"g{i}": {"type": "object", "properties": {f"p{j}": {"type": "string"} for j in range(n)}}
            for i, n in enumerate(groups)}}

    assert subset.validate_output_schema(wide(98, 0))  # 2 groups + 98 = 100
    with pytest.raises(SchemaSubsetInvalid) as info:
        subset.validate_output_schema(wide(99, 0))  # 101 in total
    assert (info.value.reason, info.value.path) == ("at most 100 properties in total", "")
    assert str(info.value) == "the JSON Schema is invalid: at most 100 properties in total"
    flat = {"type": "object", "properties": {f"p{i}": {"type": "string"} for i in range(100)}}
    assert subset.validate_output_schema(flat)
    flat["properties"]["p100"] = {"type": "string"}
    with pytest.raises(SchemaSubsetInvalid):
        subset.validate_output_schema(flat)
    items = {"type": "object", "properties": {"l": {"type": "array", "items": {
        "type": "object", "properties": {f"p{i}": {"type": "string"} for i in range(99)}}}}}
    assert subset.validate_output_schema(items)  # 1 + 99 = 100, nested inside an array


def test_byte_boundary_is_16384_utf8_bytes_of_canonical_json():
    assert subset.json_size(_padded(16384)) == 16384
    assert subset.validate_output_schema(_padded(16384))
    with pytest.raises(SchemaSubsetInvalid) as info:
        subset.validate_output_schema(_padded(16385))
    assert str(info.value) == "the JSON Schema is invalid: the schema must be at most 16384 bytes"
    # multi-byte characters count as bytes, not characters
    room = 16384 - subset.json_size({"type": "object", "description": ""})
    at_limit = {"type": "object", "description": "é" * (room // 2) + "x" * (room % 2)}
    assert subset.json_size(at_limit) == 16384 and len(at_limit["description"]) < room
    assert subset.validate_output_schema(at_limit)
    at_limit["description"] += "é"
    assert len(json.dumps(at_limit, ensure_ascii=False)) < 16384 < subset.json_size(at_limit)
    with pytest.raises(SchemaSubsetInvalid, match="at most 16384 bytes"):
        subset.validate_output_schema(at_limit)


def test_validate_parameters_are_honoured():
    schema = _tree(3)
    assert subset.validate(schema, max_depth=3)
    with pytest.raises(SchemaSubsetInvalid, match="at most 2 deep"):
        subset.validate(schema, max_depth=2)
    with pytest.raises(SchemaSubsetInvalid, match="at most 1 properties"):
        subset.validate(_tree(3), max_properties=1)
    with pytest.raises(SchemaSubsetInvalid, match="at most 50 bytes"):
        subset.validate(_tree(3), max_bytes=50)
    assert subset.validate({"type": "string"}, root_object=False) == {"type": "string"}
    assert subset.validate({"type": "array", "items": {"type": "string"}}, root_object=False)
    with pytest.raises(SchemaSubsetInvalid, match='the root must have "type": "object"'):
        subset.validate({"type": "string"})
    # 17 levels of any nesting are refused before the walk, without recursion
    deep: Any = {"type": "string"}
    for _ in range(5000):
        deep = {"type": "array", "items": deep}
    with pytest.raises(SchemaSubsetInvalid, match="nests deeper than 16 levels"):
        subset.validate(deep, root_object=False)


ACCEPTED_TABLE = [
    {"type": "object"},
    {"type": "object", "properties": {}, "required": [], "additionalProperties": True},
    {"type": "object", "properties": {"s": {"type": "string", "minLength": 2, "maxLength": 5, "format": "date-time"}}, "required": ["s"]},
    {"type": "object", "properties": {"n": {"type": "number", "minimum": -1.5, "maximum": 2.5}, "i": {"type": "integer", "minimum": 3, "maximum": 3}}, "required": ["n", "i"]},
    {"type": "object", "properties": {"b": {"type": "boolean"}, "z": {"type": "null"}}, "required": ["b", "z"]},
    {"type": "object", "properties": {"e": {"enum": ["red", "green"]}, "f": {"enum": [1, 2.5, True, None]}}, "required": ["e", "f"]},
    {"type": "object", "properties": {"k": {"const": "fixed"}, "m": {"const": None}, "o": {"const": 0}}, "required": ["k", "m", "o"]},
    {"type": "object", "properties": {"s": {"type": ["string", "null"]}, "i": {"type": ["null", "integer"]}, "o": {"type": ["object", "null"], "properties": {"x": {"type": "string"}}, "required": ["x"]}}, "required": ["s", "i", "o"]},
    {"type": "object", "properties": {"t": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 4}}, "required": ["t"]},
    {"type": "object", "properties": {"t": {"type": "array", "items": {"type": "array", "items": {"type": "integer"}, "minItems": 1}}}, "required": ["t"]},
    {"type": "object", "properties": {"l": {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "integer"}, "tags": {"type": "array", "items": {"enum": ["a", "b"]}}}, "required": ["id", "tags"]}}}, "required": ["l"]},
    {"type": "object", "properties": {"a": {"type": "object", "properties": {"b": {"type": "object", "properties": {"c": {"type": "object", "properties": {"d": {"type": "string"}}, "required": ["d"]}}, "required": ["c"]}}, "required": ["b"]}}, "required": ["a"]},
    {"type": "object", "properties": {"p": {"type": ["string", "null"], "enum": ["x", None]}}, "required": ["p"], "additionalProperties": False},
    {"type": "object", "title": "T", "description": "D", "properties": {"x": {"type": "string", "title": "X", "description": "the x"}}, "$schema": "https://json-schema.org/draft/2020-12/schema"},
    {"type": "object", "properties": {"n": {"type": "number", "minimum": 5, "maximum": 1e3}}, "required": ["n"]},
    {"type": "object", "properties": {"s": {"type": "string", "minLength": 4}}, "required": ["s"]},
    {"type": "object", "properties": {"x": {"type": "array", "items": {"type": ["string", "null"]}, "minItems": 1}}, "required": ["x"]},
    {"type": "object", "properties": {"v": {"type": "integer", "enum": [1, 2, 3]}}, "required": ["v"]},
    {"type": "object", "properties": {f"p{i}": {"type": "string"} for i in range(100)}, "required": [f"p{i}" for i in range(100)]},
    _tree(5),
]


@pytest.mark.parametrize("schema", ACCEPTED_TABLE)
def test_accepted_schemas_pass_and_schema_instance_validates_with_jsonschema(schema):
    from eap_worker.models import schema_instance

    assert subset.validate_output_schema(schema) is schema
    assert OutputSpec.model_validate({"json_schema": schema}).json_schema == schema
    instance = schema_instance(schema)
    assert isinstance(instance, dict)
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator(schema).validate(instance)
    jsonschema.Draft7Validator(schema).validate(instance)
    # the instance is deterministic and JSON
    assert json.loads(json.dumps(instance)) == instance == schema_instance(copy.deepcopy(schema))



# --- 4. limits -----------------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("raw", "text"),
    [
        ("0.5", "0.5"), ("0.50", "0.5"), ("0.500000", "0.5"), (0.5, "0.5"), (Decimal("0.5000"), "0.5"), ("5E-1", "0.5"),
        ("1", "1"), ("1.0", "1"), ("1.00", "1"), (1, "1"), ("10", "10"), ("100", "100"), ("1E+2", "100"),
        ("1000", "1000"), ("1000.0000", "1000"), ("1E+3", "1000"), ("0.0001", "0.0001"), ("1E-4", "0.0001"),
        ("0.00010", "0.0001"), ("12.3400", "12.34"), ("999.9999", "999.9999"),
    ],
)
def test_cost_normalisation_and_hash_equality(raw, text):
    spec = AgentSpec.model_validate({**BASE, "limits": {"max_cost_usd": raw}})
    assert spec.limits.max_cost_usd == Decimal(text)
    assert str(spec.limits.max_cost_usd) == text  # no trailing zeros, no exponent
    dumped = spec.model_dump(mode="json")
    assert dumped["limits"] == {"max_cost_usd": text}
    reference = AgentSpec.model_validate({**BASE, "limits": {"max_cost_usd": text}})
    assert canonical_spec_hash(spec) == canonical_spec_hash(reference)
    assert spec.model_dump_json() == reference.model_dump_json()
    assert json.loads(spec.model_dump_json())["limits"]["max_cost_usd"] == text


def test_cost_representations_all_hash_alike_and_differ_from_other_amounts():
    hashes = {canonical_spec_hash(AgentSpec.model_validate({**BASE, "limits": {"max_cost_usd": v}}))
              for v in ("2", "2.0", "2.0000", 2, Decimal("2E+0"), "0.2E+1")}
    assert len(hashes) == 1
    assert canonical_spec_hash(AgentSpec.model_validate({**BASE, "limits": {"max_cost_usd": "2.0001"}})) not in hashes
    stored = AgentSpec.model_validate({**BASE, "limits": {"max_cost_usd": "0.5"}})
    assert AgentSpec.model_validate(stored.model_dump(mode="json")) == stored  # stored text re-reads


@pytest.mark.parametrize("bad", ["0", "0.00009", "0.0000", "-1", "1000.0001", "1001", "0.00001", "1.23456", "NaN", "Infinity", "-Infinity", "abc", "", [1], {"a": 1}])
def test_cost_bounds_reject(bad):
    with pytest.raises(ValidationError):
        UsageLimitsSpec.model_validate({"max_cost_usd": bad})


def test_cost_bound_messages():
    for bad, message in (("0", "between 0.0001 and 1000"), ("1000.0001", "between 0.0001 and 1000"),
                         ("0.00015", "at most 4 decimal places"), ("NaN", "finite")):
        with pytest.raises(ValidationError) as info:
            UsageLimitsSpec.model_validate({"max_cost_usd": bad})
        assert message in info.value.errors()[0]["msg"]


@pytest.mark.parametrize(
    ("field", "low", "high"),
    [("max_requests", 1, 200), ("max_tool_calls", 1, 500), ("max_input_tokens", 1, 10_000_000),
     ("max_output_tokens", 1, 10_000_000), ("max_total_tokens", 1, 10_000_000)],
)
def test_integer_caps_bounds(field, low, high):
    assert getattr(UsageLimitsSpec.model_validate({field: low}), field) == low
    assert getattr(UsageLimitsSpec.model_validate({field: high}), field) == high
    for bad in (0, -1, high + 1):
        with pytest.raises(ValidationError):
            UsageLimitsSpec.model_validate({field: bad})
    for bad in (1.5, "x", "1.5", [1]):
        with pytest.raises(ValidationError):
            UsageLimitsSpec.model_validate({field: bad})
    with pytest.raises(ValidationError):
        UsageLimitsSpec.model_validate({"max_nothing": 1})


def test_usage_limits_kwargs_never_includes_cost():
    spec = UsageLimitsSpec.model_validate({"max_cost_usd": "5"})
    assert spec.usage_limits_kwargs() == {} and not spec.is_empty()
    full = UsageLimitsSpec.model_validate({
        "max_requests": 3, "max_tool_calls": 4, "max_input_tokens": 5, "max_output_tokens": 6,
        "max_total_tokens": 7, "max_cost_usd": "0.25"})
    assert full.usage_limits_kwargs() == {
        "request_limit": 3, "tool_calls_limit": 4, "input_tokens_limit": 5,
        "output_tokens_limit": 6, "total_tokens_limit": 7}
    assert not any("cost" in key for key in full.usage_limits_kwargs())
    from pydantic_ai.usage import UsageLimits

    assert UsageLimits(**full.usage_limits_kwargs()).request_limit == 3  # keywords are real
    assert UsageLimitsSpec.model_validate({"max_total_tokens": 9}).usage_limits_kwargs() == {"total_tokens_limit": 9}
    assert UsageLimitsSpec().usage_limits_kwargs() == {} and UsageLimitsSpec().is_empty()


def test_empty_limits_vanish_from_the_spec():
    for raw in ({}, {"max_requests": None, "max_cost_usd": None}):
        spec = AgentSpec.model_validate({**BASE, "limits": raw})
        assert spec.limits is None and "limits" not in spec.model_dump(mode="json")
        assert canonical_spec_hash(spec) == canonical_spec_hash(AgentSpec.model_validate(BASE))
    assert UsageLimitsSpec.model_validate({"max_requests": 2}).model_dump(mode="json") == {"max_requests": 2}


# --- 5. approvals --------------------------------------------------------------------------------

TOOLED = {**BASE, "tools": ["tickets.create", "clock.now"],
          "mcp_tools": [{"server": "docs", "tool": "search"}, {"server": "gh", "tool": "issues.list"}]}


def _approval_error(spec: dict) -> str:
    with pytest.raises(ValidationError) as info:
        AgentSpec.model_validate(spec)
    assert len(info.value.errors()) == 1
    return info.value.errors()[0]["msg"]


@pytest.mark.parametrize("refs", [
    ["tickets.create"], ["clock.now"], ["docs/search"], ["gh/issues.list"],
    ["tickets.create", "docs/search", "gh/issues.list", "clock.now"],
])
def test_approval_refs_accept_builtin_and_mcp_tools_of_the_spec(refs):
    from eap_schemas import approval_required_tool_names

    spec = AgentSpec.model_validate({**TOOLED, "approvals": {"tools": refs}})
    assert spec.approvals.tools == tuple(refs)
    names = approval_required_tool_names(spec)
    assert len(names) == len(refs) and all("/" not in n and "." not in n for n in names)
    assert AgentSpec.model_validate(spec.model_dump(mode="json")) == spec


def test_approval_required_tool_names_are_model_facing():
    from eap_schemas import approval_required_tool_names, mcp_tool_name

    spec = AgentSpec.model_validate({**TOOLED, "approvals": {"tools": ["tickets.create", "gh/issues.list"]}})
    assert approval_required_tool_names(spec) == {"tickets__create", mcp_tool_name("gh", "issues.list")}
    assert approval_required_tool_names(AgentSpec.model_validate(TOOLED)) == frozenset()


@pytest.mark.parametrize("ref", ["retrieve", "update_working_memory", "final_result"])
def test_platform_tool_names_cannot_require_approval(ref):
    expected = f"Value error, approvals.tools[0]: '{ref}' is a platform tool and cannot require approval"
    assert _approval_error({**TOOLED, "approvals": {"tools": [ref]}}) == expected
    # even when a stored spec lists the reserved name as a tool, and with every spec shape around it
    listed = {**TOOLED, "tools": [*TOOLED["tools"], ref], "approvals": {"tools": [ref]}}
    assert _approval_error(listed) == expected
    with_output = {**TOOLED, "output": {"json_schema": {"type": "object"}}, "approvals": {"tools": [ref]}}
    assert _approval_error(with_output) == expected


@pytest.mark.parametrize("ref", [
    "ghost.tool", "tickets", "tickets__create", "Tickets.create", "docs", "docs/", "/search", "docs/Search",
    "gh/issues", "other/search", "docs/search/x", "clock.now ", " clock.now", "mcp__docs__search",
])
def test_approval_refs_outside_the_spec_are_rejected_with_a_message(ref):
    assert _approval_error({**TOOLED, "approvals": {"tools": [ref]}}) == (
        f"Value error, approvals.tools[0]: {ref!r} is not a tool of this agent")


def test_mcp_ref_must_match_the_spec_not_just_exist_somewhere():
    no_mcp = {**BASE, "tools": ["clock.now"], "approvals": {"tools": ["docs/search"]}}
    assert _approval_error(no_mcp).endswith("'docs/search' is not a tool of this agent")
    assert _approval_error({**BASE, "approvals": {"tools": ["clock.now"]}}).endswith("is not a tool of this agent")


@pytest.mark.parametrize("raw", [[], "tickets.create", None, ["tickets.create", "tickets.create"], [""], [1],
                                 ["x" * 230], [f"t{i}" for i in range(65)]])
def test_approval_tools_list_shape_rejected(raw):
    with pytest.raises(ValidationError):
        ApprovalPolicy.model_validate({"tools": raw})


def test_approval_tools_list_shape_accepted_edges():
    assert len(ApprovalPolicy.model_validate({"tools": [f"t{i}" for i in range(64)]}).tools) == 64
    assert ApprovalPolicy.model_validate({"tools": ["x" * 229]}).tools == ("x" * 229,)
    with pytest.raises(ValidationError, match="duplicate approval tool: a"):
        ApprovalPolicy.model_validate({"tools": ["a", "b", "a"]})
    with pytest.raises(ValidationError):
        ApprovalPolicy.model_validate({"tools": ["a"], "other": 1})


@pytest.mark.parametrize(("hours", "ok"), [(1, True), (24, True), (168, True), (0, False), (-1, False), (169, False),
                                           (1.5, False), ("x", False), ([], False)])
def test_timeout_hours_bounds(hours, ok):
    if ok:
        assert ApprovalPolicy.model_validate({"tools": ["a"], "timeout_hours": hours}).timeout_hours == hours
    else:
        with pytest.raises(ValidationError):
            ApprovalPolicy.model_validate({"tools": ["a"], "timeout_hours": hours})


def test_timeout_default_and_omission():
    policy = ApprovalPolicy.model_validate({"tools": ["a"]})
    assert policy.effective_timeout_hours() == 24
    assert policy.model_dump(mode="json") == {"tools": ["a"]}  # None timeout is never dumped
    assert ApprovalPolicy.model_validate({"tools": ["a"], "timeout_hours": None}).model_dump(mode="json") == {"tools": ["a"]}
    assert ApprovalPolicy.model_validate({"tools": ["a"], "timeout_hours": 6}).effective_timeout_hours() == 6
    a = AgentSpec.model_validate({**TOOLED, "approvals": {"tools": ["clock.now"]}})
    b = AgentSpec.model_validate({**TOOLED, "approvals": {"tools": ["clock.now"], "timeout_hours": None}})
    c = AgentSpec.model_validate({**TOOLED, "approvals": {"tools": ["clock.now"], "timeout_hours": 24}})
    assert canonical_spec_hash(a) == canonical_spec_hash(b) != canonical_spec_hash(c)


def test_message_texts():
    assert DENIED_MESSAGE == "The user denied this tool call."
    assert TIMED_OUT_MESSAGE == "No one approved this tool call in time; it was not run."
    assert EVAL_DENIED_MESSAGE == "Tool approval is not available during evaluation."
    assert denial_message() == DENIED_MESSAGE
    assert denial_message(None) == denial_message("") == DENIED_MESSAGE
    assert denial_message("too risky") == "The user denied this tool call. Comment: too risky"


# --- 6. evals ------------------------------------------------------------------------------------

EVALUATOR = TypeAdapter(EvaluatorSpec)
OBJ_SCHEMA = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}


def _ev(**raw: Any):
    return EVALUATOR.validate_python(raw)


def _bad(**raw: Any) -> str:
    with pytest.raises(ValidationError) as info:
        EVALUATOR.validate_python(raw)
    return "; ".join(e["msg"] for e in info.value.errors())


def test_every_kind_round_trips_through_the_discriminator():
    raws = [
        {"kind": "equals_expected"}, {"kind": "contains", "value": "x", "case_sensitive": False},
        {"kind": "regex", "pattern": "^a+$"}, {"kind": "schema_valid", "json_schema": OBJ_SCHEMA},
        {"kind": "schema_valid"}, {"kind": "field_equals", "path": "a.0.b", "value": [1, None, "x"]},
        {"kind": "max_duration", "seconds": 2.5}, {"kind": "max_cost", "usd": "0.05"},
        {"kind": "llm_judge", "rubric": "Be kind", "model": MODEL, "include_input": True, "pass_threshold": 0.5},
    ]
    for raw in raws:
        ev = _ev(**raw, name="n")
        assert ev.kind == raw["kind"] and ev.name == "n"
        assert EVALUATOR.validate_python(json.loads(json.dumps(ev.model_dump(mode="json")))) == ev
    for raw in ({"kind": "bogus"}, {}, {"kind": None}, {"kind": "Regex", "pattern": "a"}):
        with pytest.raises(ValidationError):
            EVALUATOR.validate_python(raw)
    with pytest.raises(ValidationError):
        _ev(kind="regex", pattern="a", value="x")  # a field of another kind
    with pytest.raises(ValidationError):
        _ev(kind="equals_expected", extra=1)


@pytest.mark.parametrize("name", ["a", "x" * 100, "ünï"])
def test_evaluator_name_accepted(name):
    assert _ev(kind="equals_expected", name=name).name == name


@pytest.mark.parametrize("name", ["", "x" * 101, 5, ["a"]])
def test_evaluator_name_rejected(name):
    with pytest.raises(ValidationError):
        _ev(kind="equals_expected", name=name)


def test_regex_pattern_length_boundary():
    assert _ev(kind="regex", pattern="a" * 200).pattern == "a" * 200
    assert _ev(kind="regex", pattern="é" * 200)  # characters, not bytes
    for bad in ("a" * 201, "", None, 5):
        with pytest.raises(ValidationError):
            _ev(kind="regex", pattern=bad)
    with pytest.raises(ValidationError):
        _ev(kind="regex")


def test_judge_rubric_size_boundary_in_utf8_bytes():
    assert _ev(kind="llm_judge", rubric="x" * 4096, model=MODEL)
    assert _ev(kind="llm_judge", rubric="é" * 2048, model=MODEL)  # 4096 bytes
    assert "4096 bytes" in _bad(kind="llm_judge", rubric="x" * 4097, model=MODEL)
    assert "4096 bytes" in _bad(kind="llm_judge", rubric="é" * 2049, model=MODEL)
    assert "blank" in _bad(kind="llm_judge", rubric="   \n", model=MODEL)
    for bad in ("", None, 5):
        with pytest.raises(ValidationError):
            _ev(kind="llm_judge", rubric=bad, model=MODEL)
    for model in (None, {}, {"provider": "test"}, "test:echo", {"provider": "", "name": "x"}):
        with pytest.raises(ValidationError):
            _ev(kind="llm_judge", rubric="r", model=model)
    with pytest.raises(ValidationError):
        _ev(kind="llm_judge", rubric="r")


@pytest.mark.parametrize(("threshold", "ok"), [(None, True), (0, True), (0.0, True), (1, True), (0.7, True),
                                               (-0.1, False), (1.01, False), ("x", False), (float("nan"), False)])
def test_judge_pass_threshold_bounds(threshold, ok):
    raw = {"kind": "llm_judge", "rubric": "r", "model": MODEL, "pass_threshold": threshold}
    if ok:
        assert _ev(**raw).pass_threshold == threshold
    else:
        with pytest.raises(ValidationError):
            _ev(**raw)
    ev = _ev(kind="llm_judge", rubric="r", model=MODEL)
    assert (ev.include_input, ev.include_expected, ev.pass_threshold) == (False, False, None)


def test_contains_value_and_size():
    assert _ev(kind="contains").value is None and _ev(kind="contains").case_sensitive is True
    for value in ("x", 1, 1.5, True, None, [1, "a"], {"k": [1]}):
        assert _ev(kind="contains", value=value).value == value
    assert _ev(kind="contains", value="x" * (4096 - 2)).value  # 4096 bytes with the quotes
    assert "4096 bytes" in _bad(kind="contains", value="x" * 4095)
    for bad in ("maybe", [], {}, 2):
        with pytest.raises(ValidationError):
            _ev(kind="contains", case_sensitive=bad)


@pytest.mark.parametrize("path", ["a", "a.b", "items.0.name", "A_b-9", "a" * 64, ".".join(["s"] * 16), "0", "a.0"])
def test_field_equals_path_accepted(path):
    assert _ev(kind="field_equals", path=path, value=1).path == path


@pytest.mark.parametrize("path", ["", ".", "a.", ".a", "a..b", "a b", "a[0]", "a/b", "a" * 65, ".".join(["s"] * 17), "é", "a\n", "$.a", 5, None])
def test_field_equals_path_rejected(path):
    with pytest.raises(ValidationError):
        _ev(kind="field_equals", path=path, value=1)


def test_field_equals_value_may_be_any_json_up_to_4096_bytes():
    for value in (None, 0, False, "", [], {}, {"a": [1, {"b": None}]}):
        assert _ev(kind="field_equals", path="a", value=value).value == value
    assert "4096 bytes" in _bad(kind="field_equals", path="a", value="x" * 4095)
    with pytest.raises(ValidationError):
        _ev(kind="field_equals", path="a")  # `value` is required (None must be explicit)


@pytest.mark.parametrize(("seconds", "ok"), [(0.001, True), (1, True), (3600, True), (3600.0, True), (0, False), (-1, False),
                                             (3600.01, False), (float("nan"), False), (float("inf"), False), ("x", False)])
def test_max_duration_bounds(seconds, ok):
    if ok:
        assert _ev(kind="max_duration", seconds=seconds).seconds == seconds
    else:
        with pytest.raises(ValidationError):
            _ev(kind="max_duration", seconds=seconds)


@pytest.mark.parametrize(("usd", "ok"), [("0.0001", True), ("0.5", True), (1, True), ("1000", True), (0.25, True), ("0", False),
                                         ("-0.1", False), ("1000.01", False), ("NaN", False), ("Infinity", False), ("x", False)])
def test_max_cost_bounds(usd, ok):
    if ok:
        assert _ev(kind="max_cost", usd=usd).usd == Decimal(str(usd))
    else:
        with pytest.raises(ValidationError):
            _ev(kind="max_cost", usd=usd)


def test_schema_valid_uses_the_output_subset():
    assert _ev(kind="schema_valid").json_schema is None
    assert _ev(kind="schema_valid", json_schema=OBJ_SCHEMA).json_schema == OBJ_SCHEMA
    assert "at most 100 properties" in _bad(kind="schema_valid", json_schema={
        "type": "object", "properties": {f"p{i}": {"type": "string"} for i in range(101)}})
    assert "unsupported keyword '$ref'" in _bad(kind="schema_valid", json_schema={"type": "object", "$ref": "x"})
    assert 'the root must have "type": "object"' in _bad(kind="schema_valid", json_schema={"type": "array", "items": {"type": "string"}})
    assert SECRET not in _bad(kind="schema_valid", json_schema={"type": "object", "properties": {"a": {"enum": [SECRET, SECRET]}}})


def _id(i: int) -> str:
    return str(uuid.UUID(int=i + 1))


def _c(i: int, **extra: Any) -> dict:
    return {"id": _id(i), "position": i, "name": f"case {i}", "input": f"q{i}", **extra}


def _snap(**patch: Any) -> EvalSuiteSnapshot:
    raw = {"suite_id": _id(99), "name": "Suite", "evaluators": [{"kind": "equals_expected"}, {"kind": "regex", "pattern": "a"}],
           "cases": [_c(0, expected_output="a"), _c(1, evaluators=[{"kind": "max_duration", "seconds": 5}]), _c(2)],
           "max_concurrency": 4}
    return EvalSuiteSnapshot.model_validate({**raw, **patch})


def test_suite_hash_ignores_name_suite_id_and_concurrency_but_not_content():
    base = _snap()
    digest = base.suite_hash()
    assert len(digest) == 64 and digest == _snap().suite_hash()
    for same in (_snap(name="Another name"), _snap(max_concurrency=1), _snap(max_concurrency=8),
                 _snap(suite_id=_id(7)), _snap(name="x", max_concurrency=2, suite_id=_id(8))):
        assert same.suite_hash() == digest
        assert same.model_dump() != base.model_dump() or same is base
    assert _snap(name="Another name").canonical_json() != base.canonical_json()
    assert _snap(max_concurrency=1).canonical_json() != base.canonical_json()
    cases = [_c(0, expected_output="a"), _c(1, evaluators=[{"kind": "max_duration", "seconds": 5}]), _c(2)]
    changes = {
        "case input": [{**cases[0], "input": "q0!"}, *cases[1:]],
        "case name": [{**cases[0], "name": "renamed case"}, *cases[1:]],
        "case expected": [{**cases[0], "expected_output": "b"}, *cases[1:]],
        "case evaluator": [cases[0], {**cases[1], "evaluators": [{"kind": "max_duration", "seconds": 6}]}, cases[2]],
        "case metadata": [{**cases[0], "metadata": {"k": 1}}, *cases[1:]],
        "case id": [{**cases[0], "id": _id(50)}, *cases[1:]],
        "case position": [cases[0], {**cases[1], "position": 5}, cases[2]],
        "fewer cases": cases[:2],
        "more cases": [*cases, _c(3)],
    }
    for label, new_cases in changes.items():
        assert _snap(cases=new_cases).suite_hash() != digest, label
    assert _snap(evaluators=[]).suite_hash() != digest
    assert _snap(evaluators=[{"kind": "equals_expected"}, {"kind": "regex", "pattern": "b"}]).suite_hash() != digest
    assert _snap(evaluators=[{"kind": "equals_expected", "name": "x"}, {"kind": "regex", "pattern": "a"}]).suite_hash() != digest


def test_suite_hash_is_sensitive_to_case_position_and_not_to_listing_order():
    cases = [_c(0), _c(1), _c(2)]
    base = _snap(cases=cases)
    # the same cases listed in another order keep their positions, so they execute identically
    shuffled = _snap(cases=[cases[2], cases[0], cases[1]])
    assert [c.id for c in shuffled.ordered_cases()] == [c.id for c in base.ordered_cases()]
    assert shuffled.suite_hash() == base.suite_hash()
    # swapping positions changes what runs first, so it changes the hash
    swapped = _snap(cases=[{**cases[0], "position": 1}, {**cases[1], "position": 0}, cases[2]])
    assert [c.name for c in swapped.ordered_cases()][:2] == ["case 1", "case 0"]
    assert swapped.suite_hash() != base.suite_hash()


def test_suite_caps_and_concurrency_bounds():
    ev = {"kind": "equals_expected"}
    assert len(_snap(evaluators=[{**ev, "name": f"e{i}"} for i in range(10)]).evaluators) == 10
    with pytest.raises(ValidationError):
        _snap(evaluators=[{**ev, "name": f"e{i}"} for i in range(11)])
    case_evs = [{**ev, "name": f"c{i}"} for i in range(5)]
    assert len(_snap(cases=[_c(0, evaluators=case_evs)]).cases[0].evaluators) == 5
    with pytest.raises(ValidationError):
        _snap(cases=[_c(0, evaluators=[*case_evs, {**ev, "name": "c5"}])])
    assert len(_snap(cases=[_c(i) for i in range(500)]).cases) == 500
    with pytest.raises(ValidationError):
        _snap(cases=[_c(i) for i in range(501)])
    for n in (1, 4, 8):
        assert _snap(max_concurrency=n).max_concurrency == n
    for n in (0, -1, 9, 2.5, "x"):
        with pytest.raises(ValidationError):
            _snap(max_concurrency=n)
    default = EvalSuiteSnapshot.model_validate({"suite_id": _id(1), "name": "S", "cases": [_c(0)]})
    assert default.max_concurrency == 4 and default.evaluators == ()


def test_evaluator_names_are_unique_across_suite_and_the_case_only():
    ev = {"kind": "equals_expected"}
    _snap(evaluators=[{**ev, "name": "same"}], cases=[_c(0), _c(1)])
    _snap(evaluators=[ev, ev, ev])  # unnamed evaluators never clash
    _snap(cases=[_c(0, evaluators=[{**ev, "name": "n"}]), _c(1, evaluators=[{**ev, "name": "n"}])])  # other case
    for evaluators, cases in (
        ([{**ev, "name": "a"}, {**ev, "name": "a"}], [_c(0)]),
        ([{**ev, "name": "a"}], [_c(0, evaluators=[{**ev, "name": "a"}])]),
        ([], [_c(0, evaluators=[{**ev, "name": "a"}, {**ev, "name": "a"}])]),
    ):
        with pytest.raises(ValidationError, match="duplicate evaluator name 'a'"):
            _snap(evaluators=evaluators, cases=cases)


def test_snapshot_rejects_duplicate_case_ids_and_names_and_round_trips():
    with pytest.raises(ValidationError, match="duplicate case id"):
        _snap(cases=[_c(0), {**_c(1), "id": _id(0)}])
    with pytest.raises(ValidationError, match="duplicate case name"):
        _snap(cases=[_c(0), {**_c(1), "name": "case 0"}])
    snap = _snap()
    again = EvalSuiteSnapshot.model_validate_json(snap.canonical_json())
    assert again == snap and again.canonical_json() == snap.canonical_json() and again.suite_hash() == snap.suite_hash()
    assert snap.canonical_json() == json.dumps(json.loads(snap.canonical_json()), sort_keys=True, separators=(",", ":"))
    # AC.1 review (2026-11-15): cases in ordered_cases() order, None model fields left out.
    assert snap.suite_hash() == hashlib.sha256(json.dumps(
        {"cases": [c.model_dump(mode="json", exclude_none=True) for c in snap.ordered_cases()],
         "evaluators": [e.model_dump(mode="json", exclude_none=True) for e in snap.evaluators]},
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def test_case_limits_boundaries():
    assert EvalCase(name="c", input="x" * 32768) and EvalCase(name="c", input="é" * 16384)
    for bad in ({"input": "é" * 16385}, {"input": ""}, {"name": ""}, {"name": "x" * 101}, {"expected_output": "x" * 32768},
                {"metadata": {"k": "x" * 1001}}, {"metadata": {"k" * 65: 1}}, {"metadata": {"": 1}}, {"metadata": {"k": [1]}},
                {"extra": 1}):
        with pytest.raises(ValidationError):
            EvalCase.model_validate({"name": "c", "input": "x", **bad})
    for good in ({"expected_output": "x" * 32766}, {"expected_output": None}, {"expected_output": {"a": [1]}},
                 {"metadata": {"k": "x" * 1000, "n": 1.5, "b": True, "z": None}}):
        EvalCase.model_validate({"name": "c", "input": "x", **good})


@pytest.mark.parametrize(
    ("value", "path", "expected"),
    [
        ({"a": {"b": 1}}, "a.b", (True, 1)), ({"a": {"b": None}}, "a.b", (True, None)),  # present but null
        ({"a": None}, "a", (True, None)), ({"a": None}, "a.b", (False, None)),
        ({"a": [10, 20, 30]}, "a.0", (True, 10)), ({"a": [10, 20, 30]}, "a.2", (True, 30)),
        ({"a": [10, 20, 30]}, "a.3", (False, None)), ({"a": [10, 20]}, "a.-1", (False, None)),
        ({"a": [[1, 2], [3]]}, "a.1.0", (True, 3)), ({"a": [{"b": "c"}]}, "a.0.b", (True, "c")),
        ({"a": [10]}, "a.x", (False, None)),
        ({"0": "zero"}, "0", (True, "zero")), ({"a": {"0": "k"}}, "a.0", (True, "k")),  # dict keys that are digits
        ([1, 2], "1", (True, 2)), ([1, 2], "2", (False, None)),
        ({"a": "text"}, "a.0", (False, None)), ({"a": 5}, "a.b", (False, None)), ({"a": True}, "a.0", (False, None)),
        ({}, "a", (False, None)), ("str", "a", (False, None)), (None, "a", (False, None)), (7, "0", (False, None)),
        ({"a": {"b": {"c": {"d": 1}}}}, "a.b.c.d", (True, 1)),
    ],
)
def test_field_path_value_cases(value, path, expected):
    assert field_path_value(value, path) == expected


def test_field_path_value_does_not_mutate_or_alias_errors():
    data = {"a": [{"b": 1}]}
    before = copy.deepcopy(data)
    field_path_value(data, "a.0.b")
    field_path_value(data, "a.1.b")
    assert data == before
    found, value = field_path_value({"a": {"b": [1]}}, "a.b")
    assert found and value == [1]


def test_judge_verdict_pass_alias_round_trip():
    verdict = JudgeVerdict.model_validate({"reason": "good", "pass": False, "score": 0.25})
    assert verdict.passed is False and verdict.score == 0.25
    dumped = verdict.model_dump()
    assert dumped == {"reason": "good", "pass": False, "score": 0.25} and "passed" not in dumped
    assert json.loads(verdict.model_dump_json()) == dumped
    assert JudgeVerdict.model_validate(dumped) == verdict
    assert JudgeVerdict.model_validate_json(verdict.model_dump_json()) == verdict
    assert JudgeVerdict(reason="good", passed=False, score=0.25) == verdict  # populate_by_name
    assert JudgeVerdict(reason="good", **{"pass": False}, score=0.25) == verdict
    schema = JudgeVerdict.model_json_schema()
    assert "pass" in schema["properties"] and "passed" not in schema["properties"]
    assert set(schema["required"]) == {"reason", "pass", "score"}
    assert JudgeVerdict.model_json_schema(mode="serialization")["properties"].keys() == {"reason", "pass", "score"}
    # the agent output schema built from it is itself inside the output subset
    assert "pass" in schema["properties"]


@pytest.mark.parametrize("bad", [
    {"reason": "r", "pass": True},  # score missing
    {"reason": "r", "score": 1.0},  # pass missing
    {"pass": True, "score": 1.0},
    {"reason": "r", "pass": True, "score": -0.01}, {"reason": "r", "pass": True, "score": 1.01},
    {"reason": "r" * 2001, "pass": True, "score": 1}, {"reason": "r", "pass": "maybe", "score": 1},
    {"reason": "r", "pass": True, "score": "high"}, {"reason": "r", "pass": True, "score": 1, "extra": 1},
    {"reason": "r", "pass": True, "score": float("nan")},
])
def test_judge_verdict_rejects(bad):
    with pytest.raises(ValidationError):
        JudgeVerdict.model_validate(bad)


def test_judge_verdict_bounds_accepted():
    assert JudgeVerdict.model_validate({"reason": "", "pass": True, "score": 0}).score == 0
    assert JudgeVerdict.model_validate({"reason": "r" * 2000, "pass": False, "score": 1}).score == 1
    with pytest.raises(ValidationError):
        JudgeVerdict.model_validate({"reason": "r", "pass": True, "score": 1}).__setattr__("score", 0.5)  # frozen


def test_field_path_value_never_raises_on_odd_segments():
    """Paths are validated by `field_equals`, but the function is public: it must answer, not raise."""
    for segment in ("\u00b2", "\u0663", "1e3", "1_0", " 1", "+1", "\u0661"):
        found, value = field_path_value({"a": [10, 20]}, f"a.{segment}")
        assert isinstance(found, bool) and (found or value is None)


# --- 7. errors -----------------------------------------------------------------------------------

ERRORS = [
    OutputInvalid(2, ("answer", "items[0].id")), OutputInvalid(0), OutputInvalid(5, ()),
    RunBudgetExceeded("0.5", "0.25"), RunBudgetExceeded(Decimal("1.2345"), Decimal("1")), RunBudgetExceeded(3, 2),
    EvalSuiteInvalid("cases are bad"), EvalSuiteInvalid("bad", "cases[2].input"), EvalSuiteInvalid("", ""),
    SchemaSubsetInvalid("unsupported keyword '$ref'", "a.b"), SchemaSubsetInvalid("the schema must be a JSON object"),
]


@pytest.mark.parametrize("exc", ERRORS, ids=lambda e: f"{type(e).__name__}-{abs(hash(str(e.args))) % 1000}")
@pytest.mark.parametrize("protocol", range(0, pickle.HIGHEST_PROTOCOL + 1))
def test_errors_pickle_round_trip_every_protocol(exc, protocol):
    clone = pickle.loads(pickle.dumps(exc, protocol=protocol))
    assert type(clone) is type(exc) and clone.args == exc.args and str(clone) == str(exc)
    assert clone.code == exc.code
    assert vars(clone) == vars(exc)
    again = copy.deepcopy(exc)
    assert vars(again) == vars(exc) and str(again) == str(exc)
    assert pickle.loads(pickle.dumps(clone)).args == exc.args


def test_error_attributes_survive_pickling():
    out = pickle.loads(pickle.dumps(OutputInvalid(3, ["a", "b.c"])))
    assert (out.max_retries, out.errors, out.code) == (3, ("a", "b.c"), "output_invalid")
    assert str(out) == "the agent's output did not match its output schema after 3 retries"
    budget = pickle.loads(pickle.dumps(RunBudgetExceeded("0.50", "0.2500")))
    assert (budget.spent_usd, budget.cap_usd, budget.code) == (Decimal("0.50"), Decimal("0.2500"), "run_budget_exceeded")
    assert isinstance(budget.spent_usd, Decimal)
    assert budget.data() == {"limit": "max_cost_usd", "value": "0.2500", "spent_usd": "0.50"}
    assert str(budget) == "the run reached its cost limit of $0.2500 (spent $0.50)"
    suite = pickle.loads(pickle.dumps(EvalSuiteInvalid("too many", "cases")))
    assert (suite.reason, suite.path, suite.code) == ("too many", "cases", "eval_suite_invalid")
    assert str(suite) == "the test suite is invalid at cases: too many"
    assert str(EvalSuiteInvalid("too many")) == "the test suite is invalid: too many"
    sub = pickle.loads(pickle.dumps(SchemaSubsetInvalid("r", "p")))
    assert (sub.reason, sub.path, sub.code) == ("r", "p", "json_schema_invalid")


def test_errors_raised_and_caught_across_a_pickle_boundary():
    for exc in ERRORS:
        try:
            raise pickle.loads(pickle.dumps(exc))
        except (OutputInvalid, RunBudgetExceeded, EvalSuiteInvalid, SchemaSubsetInvalid) as caught:
            assert type(caught) is type(exc)
    assert issubclass(OutputInvalid, RuntimeError) and issubclass(RunBudgetExceeded, RuntimeError)
    assert issubclass(EvalSuiteInvalid, ValueError) and issubclass(SchemaSubsetInvalid, ValueError)
    assert len({e.code for e in ERRORS}) == 4


def test_error_messages_never_carry_output_values():
    exc = OutputInvalid(2, ("answer",))
    assert exc.errors == ("answer",)
    assert "answer" not in str(exc)  # paths ride on `.errors`, not in the model-visible text
