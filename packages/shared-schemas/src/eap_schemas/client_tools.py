"""Frontend (client) tool contracts and AG-UI run options (AG-UI milestone, AGUI.1; plan D10, D18, D19).

These are OUR models for data that reaches the run row and the worker; the AG-UI wire types
(`ag_ui.core`) stay inside `eap_api.ag_ui` (D19), which maps them onto these.

- `ClientToolDecl`: a tool the client (browser) executes. The name is `^[A-Za-z0-9_-]{1,64}$` and
  never one of `RESERVED_TOOL_NAMES`; `parameters` is an object schema that passes the JSON Schema
  Draft 2020-12 meta-schema; references are `#/$defs/<name>` only, each naming a root `$defs`
  member, and `$id`/`$anchor`/dynamic references and `pattern`/`patternProperties`/`format: regex`
  (ReDoS) are refused in every subschema. A clash
  with a server tool of the spec is checked by the API against the spec (`client_tool_clashes`).
- `ClientToolResult`: the client's answer to one pending client tool call (`content`, or `error`
  for a failure reported back to the model as a retry).
- `check_client_tools`: the list rules (at most MAX_CLIENT_TOOLS, unique names, parameters schemas
  at most MAX_CLIENT_TOOL_SCHEMAS_BYTES in total).
- `AgUiRunOptions`: `forwardedProps.pydanticos` (camelCase on the wire: `version`, `model`,
  `requestContext`, `resourceId`, workflow `input`).

Errors carry a stable code as the Pydantic error type (or `ClientToolsInvalid.code`) and name a
path and the rule, never a value. Not re-exported from `eap_schemas` on purpose: this module
imports `jsonschema`, which `eap_processors` (an `eap_schemas` importer) must never reach.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pydantic.alias_generators import to_camel
from pydantic_core import PydanticCustomError

from eap_schemas.json_schema_subset import DRAFT_2020_12, json_depth_exceeds, json_size
from eap_schemas.model_ref import ModelRef
from eap_schemas.tools import RESERVED_TOOL_NAMES

CLIENT_TOOL_NAME_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
MAX_CLIENT_TOOLS = 32
MAX_CLIENT_TOOL_DESCRIPTION_CHARS = 4096
MAX_CLIENT_TOOL_SCHEMA_BYTES = 64 * 1024  # one tool's parameters (canonical JSON, UTF-8)
MAX_CLIENT_TOOL_SCHEMAS_BYTES = 64 * 1024  # all tools' parameters together
MAX_CLIENT_TOOL_SCHEMA_DEPTH = 32  # objects/arrays; the AG-UI body itself is capped at 64
# JSON objects in parameters, all tools together (so per tool too). Meta-validation costs about
# 0.1 ms per subschema, so this bounds one run's schema work to well under 100 ms; the byte caps
# alone (64 KiB of tiny properties is ~1,500 subschemas) do not.
MAX_CLIENT_TOOL_SCHEMA_NODES = 256
# JSON objects with every `$ref` expanded (memoised per `$defs` member): what validation can visit.
# The raw count alone misses a chain of defs that each `anyOf` two refs to the next (2^N work for
# ~3N raw nodes); a `$ref` cycle (unbounded expansion, RecursionError in jsonschema) is refused.
MAX_CLIENT_TOOL_SCHEMA_EXPANDED_NODES = 4096
MAX_TOOL_CALL_ID_CHARS = 256
MAX_CLIENT_TOOL_RESULT_BYTES = 32 * 1024  # content + error, UTF-8
MAX_AGUI_INPUT_BYTES = 32 * 1024  # the new user input of one AG-UI run (as RunCreate.input)

# Reference keywords whose value must be a local JSON pointer / anchor ("#...").
_REF_KEYWORDS = ("$ref", "$dynamicRef")
# The only reference form accepted: a direct member of the root `$defs`. With no `$id` anywhere,
# jsonschema resolves it against the root exactly as `_check_refs_and_keywords` does.
LOCAL_REF_PATTERN = re.compile(r"^#/\$defs/[A-Za-z0-9_-]{1,64}$")
# AC.8's decide route uses the same rule (eap_api.routes.approvals.TOOL_CALL_ID_PATTERN).
TOOL_CALL_ID_PATTERN = r"^[\x21-\x7e]{1,256}$"
MAX_VERSION = 2**31 - 1  # Postgres integer

# Keywords refused in any subschema:
# - `pattern`/`patternProperties` (and `format: regex`): Python `re` in jsonschema is a ReDoS vector,
#   the same rule as AC.1's subset (json_schema_subset);
# - `$id`/`$anchor`/dynamic and recursive references: they change how references resolve, so the
#   contract could accept a schema that fails at validation time in the worker.
_FORBIDDEN_KEYWORDS = {
    "pattern": "client_tool_schema_pattern_unsupported",
    "patternProperties": "client_tool_schema_pattern_unsupported",
    "$id": "client_tool_schema_id_unsupported",
    "$anchor": "client_tool_schema_id_unsupported",
    "$dynamicAnchor": "client_tool_schema_id_unsupported",
    "$dynamicRef": "client_tool_schema_id_unsupported",
    "$recursiveAnchor": "client_tool_schema_id_unsupported",
    "$recursiveRef": "client_tool_schema_id_unsupported",
}
# Draft 2020-12 applicators: where subschemas live (everything else is a value: const, enum,
# default, examples, property names...).
_SCHEMA_KEYWORDS = ("items", "contains", "additionalProperties", "propertyNames", "not", "if", "then", "else",
                    "unevaluatedItems", "unevaluatedProperties", "contentSchema")
_SCHEMA_MAP_KEYWORDS = ("properties", "$defs", "definitions", "dependentSchemas", "patternProperties")
_SCHEMA_LIST_KEYWORDS = ("allOf", "anyOf", "oneOf", "prefixItems")


def _schema_error(code: str, message: str) -> PydanticCustomError:
    return PydanticCustomError(code, message)


def _iter_refs(schema: Any) -> Iterable[tuple[str, str]]:
    """(keyword, value) of every string `$ref`/`$dynamicRef` anywhere in `schema` (iterative). A dict
    under `const`/`enum`/`default` that looks like a reference counts too: conservative on purpose."""
    stack = [schema]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                if key in _REF_KEYWORDS and isinstance(value, str):
                    yield key, value
                elif isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(node, list):
            stack.extend(item for item in node if isinstance(item, (dict, list)))


# Stands for a client-chosen name (a property, a `$defs` member, any other object key of the
# schema) in an error's subschema path: error messages name keywords and indexes only.
SCHEMA_NAME_PLACEHOLDER = "*"


def _schema_keywords() -> frozenset[str]:
    import jsonschema

    return frozenset(jsonschema.Draft202012Validator.VALIDATORS) | {
        "$schema", "$defs", "definitions", "$id", "$anchor", "$comment", "title", "description", "default",
        "examples", "deprecated", "readOnly", "writeOnly", "format", "contentMediaType", "contentEncoding",
        "contentSchema",
    }


def _safe_schema_path(parts: Iterable[Any]) -> list[str]:
    """A path into the client's schema with every client-chosen name replaced by
    SCHEMA_NAME_PLACEHOLDER: indexes and JSON Schema keywords are kept, except a member name of a
    name-keyed keyword (`properties`, `$defs`...), which is always a placeholder."""
    keywords = _schema_keywords()
    out: list[str] = []
    previous: Any = None
    for part in parts:
        if isinstance(part, int):
            out.append(str(part))
        elif part in keywords and previous not in _SCHEMA_MAP_KEYWORDS:
            out.append(part)
        else:
            out.append(SCHEMA_NAME_PLACEHOLDER)
        previous = part
    return out


def schema_node_count(value: Any, limit: int | None = None) -> int:
    """The number of JSON objects in `value` (iterative). With `limit`, stops early and returns
    limit + 1 once the count exceeds it."""
    count = 0
    stack = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            count += 1
            if limit is not None and count > limit:
                return limit + 1
            stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
        elif isinstance(node, list):
            stack.extend(v for v in node if isinstance(v, (dict, list)))
    return count


def _raw_nodes_and_refs(value: Any, *, skip_root_defs: bool = False) -> tuple[int, list[str]]:
    """(JSON objects in `value`, the root-`$defs` member names its `$ref`s name, with repeats),
    iteratively. `skip_root_defs` leaves out the root's `$defs` (counted through the refs)."""
    count = 0
    refs: list[str] = []
    stack = [value]
    first = True
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            count += 1
            ref = node.get("$ref")
            if isinstance(ref, str) and LOCAL_REF_PATTERN.match(ref):
                refs.append(ref[len("#/$defs/"):])
            for key, child in node.items():
                if first and skip_root_defs and key == "$defs":
                    continue
                if isinstance(child, (dict, list)):
                    stack.append(child)
        elif isinstance(node, list):
            stack.extend(v for v in node if isinstance(v, (dict, list)))
        first = False
    return count, refs


def expanded_node_count(schema: dict[str, Any], limit: int = MAX_CLIENT_TOOL_SCHEMA_EXPANDED_NODES) -> int:
    """The JSON objects of `schema` with every `#/$defs/<name>` reference expanded, memoised per
    member and saturating at limit + 1 (so the work is linear in the schema size). Raises
    ValueError("cycle") for a `$ref` cycle. Iterative: no recursion depth to exhaust."""
    defs = schema.get("$defs") if isinstance(schema.get("$defs"), dict) else {}
    local: dict[str, tuple[int, list[str]]] = {}
    memo: dict[str, int] = {}

    def info(name: str) -> tuple[int, list[str]]:
        if name not in local:
            local[name] = _raw_nodes_and_refs(defs.get(name))
        return local[name]

    for start in dict.fromkeys(_raw_nodes_and_refs(schema, skip_root_defs=True)[1]):
        if start in memo:
            continue
        visiting: set[str] = {start}
        stack: list[str] = [start]
        while stack:
            name = stack[-1]
            pending = [r for r in dict.fromkeys(info(name)[1]) if r not in memo]
            cyclic = [r for r in pending if r in visiting]
            if cyclic:
                raise ValueError("cycle")
            if pending:
                stack.append(pending[0])
                visiting.add(pending[0])
                continue
            raw, refs = info(name)
            memo[name] = min(limit + 1, raw + sum(memo[r] for r in refs))
            visiting.discard(name)
            stack.pop()
    raw, refs = _raw_nodes_and_refs(schema, skip_root_defs=True)
    return min(limit + 1, raw + sum(memo[r] for r in refs))


def _check_parameters_schema(schema: Any) -> dict[str, Any]:
    if not isinstance(schema, dict):
        raise _schema_error("client_tool_schema_not_object", "parameters must be a JSON Schema object")
    if json_depth_exceeds(schema, MAX_CLIENT_TOOL_SCHEMA_DEPTH):
        raise _schema_error(
            "client_tool_schema_too_deep",
            f"parameters nests deeper than {MAX_CLIENT_TOOL_SCHEMA_DEPTH} levels",
        )
    if schema_node_count(schema, MAX_CLIENT_TOOL_SCHEMA_NODES) > MAX_CLIENT_TOOL_SCHEMA_NODES:
        raise _schema_error(
            "client_tool_schema_too_complex",
            f"parameters may hold at most {MAX_CLIENT_TOOL_SCHEMA_NODES} JSON objects",
        )
    try:
        # Strict JSON: NaN and Infinity are not JSON, and must never reach the stored row or the wire.
        json.dumps(schema, allow_nan=False)
        size = json_size(schema)
    except (TypeError, ValueError):
        raise _schema_error("client_tool_schema_invalid", "parameters must be JSON") from None
    if size > MAX_CLIENT_TOOL_SCHEMA_BYTES:
        raise _schema_error(
            "client_tool_schema_too_large",
            f"parameters must be at most {MAX_CLIENT_TOOL_SCHEMA_BYTES} bytes as JSON",
        )
    dialect = schema.get("$schema", DRAFT_2020_12)
    if dialect != DRAFT_2020_12:
        raise _schema_error("client_tool_schema_invalid", "parameters '$schema' must be JSON Schema Draft 2020-12")
    if schema.get("type") != "object":
        raise _schema_error("client_tool_schema_not_object", "parameters must have \"type\": \"object\" at the root")
    refs = list(_iter_refs(schema))
    if any(not value.startswith("#") for _, value in refs):
        raise _schema_error("client_tool_schema_remote_ref", "parameters may use local '#...' references only")

    _check_refs_and_keywords(schema)
    try:
        expanded = expanded_node_count(schema)
    except ValueError:
        raise _schema_error(
            "client_tool_schema_too_complex", "parameters may not use '$ref' cycles (a definition that refers back to itself)"
        ) from None
    if expanded > MAX_CLIENT_TOOL_SCHEMA_EXPANDED_NODES:
        raise _schema_error(
            "client_tool_schema_too_complex",
            f"parameters may expand to at most {MAX_CLIENT_TOOL_SCHEMA_EXPANDED_NODES} JSON objects with '$ref's resolved",
        )

    import jsonschema

    try:
        jsonschema.Draft202012Validator.check_schema(schema)
    except jsonschema.SchemaError as err:
        where = "/".join(_safe_schema_path(err.absolute_path)) or "(root)"
        raise _schema_error(
            "client_tool_schema_invalid",
            f"parameters is not a valid JSON Schema (Draft 2020-12) at {where}: '{err.validator}' keyword",
        ) from None
    return schema


def _check_refs_and_keywords(root: dict[str, Any]) -> None:
    """Walk every subschema (iteratively, through the 2020-12 applicators only): refuse the
    forbidden keywords, a nested `$schema`, and any `$ref` other than `#/$defs/<name>` naming a
    member of the root `$defs`. Errors name the subschema path and the keyword, never a value."""
    defs = root.get("$defs")
    stack: list[tuple[Any, str]] = [(root, "")]
    while stack:
        node, path = stack.pop()
        if not isinstance(node, dict):
            continue  # boolean schemas (and junk the meta-schema check reports)
        where = path or "(root)"
        for keyword, code in _FORBIDDEN_KEYWORDS.items():
            if keyword in node:
                raise _schema_error(code, f"parameters may not use '{keyword}' (at {where})")
        if node.get("format") == "regex":
            raise _schema_error("client_tool_schema_pattern_unsupported",
                                f"parameters may not use 'format: regex' (at {where})")
        if path and "$schema" in node:
            raise _schema_error("client_tool_schema_invalid", f"parameters may declare '$schema' only at the root (at {where})")
        if "$ref" in node:
            ref = node["$ref"]
            target = None
            if isinstance(ref, str) and LOCAL_REF_PATTERN.match(ref) and isinstance(defs, dict):
                target = defs.get(ref[len("#/$defs/"):])
            if not isinstance(target, (dict, bool)):
                raise _schema_error(
                    "client_tool_schema_invalid",
                    f"parameters '$ref' must name a member of the root '$defs' as '#/$defs/<name>' (at {where})",
                )
        for keyword in _SCHEMA_KEYWORDS:
            if keyword in node:
                stack.append((node[keyword], f"{path}/{keyword}"))
        for keyword in _SCHEMA_MAP_KEYWORDS:
            members = node.get(keyword)
            if isinstance(members, dict):
                # Member names are the client's: never part of an error message.
                stack.extend((sub, f"{path}/{keyword}/{SCHEMA_NAME_PLACEHOLDER}") for sub in members.values())
        for keyword in _SCHEMA_LIST_KEYWORDS:
            members = node.get(keyword)
            if isinstance(members, list):
                stack.extend((sub, f"{path}/{keyword}/{i}") for i, sub in enumerate(members))


class ClientToolDecl(BaseModel):
    """A frontend tool declared by the client for one AG-UI run (stored on `runs.client_tools`).

    `parameters` must be a dict with `"type": "object"`: an AG-UI client's empty or null
    `parameters` (a zero-argument tool) is refused here, and AGUI.5's input mapping normalises it
    to `{"type": "object", "properties": {}}` before building this model."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    name: str = Field(pattern=CLIENT_TOOL_NAME_PATTERN)
    description: str = Field(default="", max_length=MAX_CLIENT_TOOL_DESCRIPTION_CHARS)
    parameters: dict[str, Any]

    @field_validator("name")
    @classmethod
    def _not_reserved(cls, value: str) -> str:
        if value in RESERVED_TOOL_NAMES:
            raise _schema_error(
                "client_tool_shadows_server_tool", "a client tool may not use a reserved platform tool name"
            )
        return value

    @field_validator("parameters", mode="before")
    @classmethod
    def _valid_schema(cls, value: Any) -> dict[str, Any]:
        return _check_parameters_schema(value)


class ClientToolResult(BaseModel):
    """The client's result for one pending client tool call. `error` set means the call failed on
    the client; the worker hands it to the model as a retry. Untrusted tool output, size-capped.

    `content` is text only: an AG-UI 1.0 `ToolMessage.content` given as a list of parts is
    refused here, and AGUI.5 maps it (text parts joined, anything else 422) before building this
    model. `tool_call_id` is 1-256 printable ASCII characters without spaces (as AC.8)."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    tool_call_id: str = Field(pattern=TOOL_CALL_ID_PATTERN, max_length=MAX_TOOL_CALL_ID_CHARS)
    content: str = ""
    error: str | None = None

    @field_validator("error")
    @classmethod
    def _error_not_empty(cls, value: str | None) -> str | None:
        if value is not None and not value:
            raise _schema_error("client_tool_result_invalid", "error must not be empty when set")
        return value

    @model_validator(mode="after")
    def _size(self) -> ClientToolResult:
        size = len(self.content.encode("utf-8")) + len((self.error or "").encode("utf-8"))
        if size > MAX_CLIENT_TOOL_RESULT_BYTES:
            raise _schema_error(
                "client_tool_result_too_large",
                f"a client tool result must be at most {MAX_CLIENT_TOOL_RESULT_BYTES} bytes in UTF-8",
            )
        return self


class ClientToolsInvalid(ValueError):
    """The client tool list breaks a list rule. Names the rule (and a tool index), never a value.
    Pickle-safe."""

    def __init__(self, code: str, reason: str) -> None:
        self.code = code
        self.reason = reason
        super().__init__(code, reason)

    def __str__(self) -> str:
        return f"client tools are invalid: {self.reason}"


def check_client_tools(tools: Sequence[ClientToolDecl]) -> None:
    """The list rules for one run's client tools: at most MAX_CLIENT_TOOLS, unique names, and the
    parameters schemas at most MAX_CLIENT_TOOL_SCHEMAS_BYTES and MAX_CLIENT_TOOL_SCHEMA_NODES
    together. A caller holding raw input should apply the two totals before building the decls
    (eap_api.ag_ui.input does), so the per-tool JSON Schema work is bounded too."""
    if len(tools) > MAX_CLIENT_TOOLS:
        raise ClientToolsInvalid("client_tools_too_many", f"at most {MAX_CLIENT_TOOLS} client tools are allowed")
    seen: set[str] = set()
    for index, tool in enumerate(tools):
        if tool.name in seen:
            raise ClientToolsInvalid("client_tool_duplicate", f"tools[{index}] repeats an earlier tool name")
        seen.add(tool.name)
    total = sum(json_size(tool.parameters) for tool in tools)
    if total > MAX_CLIENT_TOOL_SCHEMAS_BYTES:
        raise ClientToolsInvalid(
            "client_tools_schemas_too_large",
            f"client tool parameters must be at most {MAX_CLIENT_TOOL_SCHEMAS_BYTES} bytes in total",
        )
    if sum(schema_node_count(tool.parameters) for tool in tools) > MAX_CLIENT_TOOL_SCHEMA_NODES:
        raise ClientToolsInvalid(
            "client_tools_schemas_too_complex",
            f"client tool parameters may hold at most {MAX_CLIENT_TOOL_SCHEMA_NODES} JSON objects in total",
        )


def client_tool_clashes(tools: Iterable[ClientToolDecl], server_tool_names: Iterable[str]) -> list[str]:
    """Names of client tools that shadow a server tool of the spec (built-in or `mcp__*`), in
    declaration order. The API answers any as 422 `client_tool_shadows_server_tool`."""
    server = set(server_tool_names) | set(RESERVED_TOOL_NAMES)
    return [tool.name for tool in tools if tool.name in server]


RunVersionOption = Annotated[StrictInt, Field(ge=1, le=MAX_VERSION)] | Literal["draft"] | None


class AgUiRunOptions(BaseModel):
    """`forwardedProps.pydanticos` of an AG-UI run (camelCase keys on the wire).

    - `version`: a published version number, "draft" (dev bearer only; the API enforces), or null
      for the latest published version.
    - `model`: a per-run model override (allowlist and provider rules apply in the API; API keys
      may not override).
    - `requestContext`: values for the spec's `context_fields`, validated like
      `RunCreate.request_context` by the API.
    - `resourceId`: the resource of a conversation derived from a `threadId`, set once at
      creation (D3); same rule as `ConversationCreate.resource_id`.
    - `input`: a workflow run's input (validated against the workflow's input schema by the API).
      Absent and `null` differ: use `has_input`.
    """

    model_config = ConfigDict(
        extra="forbid", frozen=True, alias_generator=to_camel, populate_by_name=True, hide_input_in_errors=True
    )

    version: RunVersionOption = None
    model: ModelRef | None = None
    request_context: dict[str, Any] | None = None
    resource_id: str | None = Field(default=None, max_length=128, pattern=r"^[A-Za-z0-9_.:@-]{1,128}$")
    input: Any = None

    @property
    def has_input(self) -> bool:
        """True when the caller sent `input` (even `null`)."""
        return "input" in self.model_fields_set


__all__ = [
    "AgUiRunOptions",
    "CLIENT_TOOL_NAME_PATTERN",
    "LOCAL_REF_PATTERN",
    "MAX_VERSION",
    "SCHEMA_NAME_PLACEHOLDER",
    "TOOL_CALL_ID_PATTERN",
    "ClientToolDecl",
    "ClientToolResult",
    "ClientToolsInvalid",
    "MAX_AGUI_INPUT_BYTES",
    "MAX_CLIENT_TOOLS",
    "MAX_CLIENT_TOOL_DESCRIPTION_CHARS",
    "MAX_CLIENT_TOOL_RESULT_BYTES",
    "MAX_CLIENT_TOOL_SCHEMAS_BYTES",
    "MAX_CLIENT_TOOL_SCHEMA_BYTES",
    "MAX_CLIENT_TOOL_SCHEMA_DEPTH",
    "MAX_CLIENT_TOOL_SCHEMA_EXPANDED_NODES",
    "MAX_CLIENT_TOOL_SCHEMA_NODES",
    "MAX_TOOL_CALL_ID_CHARS",
    "check_client_tools",
    "expanded_node_count",
    "client_tool_clashes",
    "schema_node_count",
]
