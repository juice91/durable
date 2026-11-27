"""The tool contract of the external MCP server (design 5.1): names, descriptions and argument schemas. A call is
checked against the same schema `tools/list` advertises; the workflow's input itself is checked by the service."""

from __future__ import annotations

from typing import Any

MAX_WAIT_SECONDS = 25  # a start never holds the connection longer
DEFAULT_WAIT_SECONDS = 10
MAX_TIMEOUT_SECONDS = 60  # runs.wait
DEFAULT_TIMEOUT_SECONDS = 30
MAX_PAGE = 50
DEFAULT_PAGE = 20
RUN_PREFIX = "run_"
EXAMPLE_CHARS = 1000

_RUN_ID = {"type": "string", "description": "The run id a start answered."}
_STATES = "queued, running, input_required, completed, failed or cancelled"


def _object(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    schema = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema


# name -> (description, argument schema, read only)
FIXED_TOOLS: dict[str, tuple[str, dict[str, Any], bool]] = {
    "exports.list": (
        "The workflows this key may run: name, title, description, input schema and result mode. Start one with the "
        "tool `run_<name>`.",
        _object({}), True,
    ),
    "runs.get": (
        "The current view of one of your runs: state, whether it is done, and its result or error.",
        _object({"run_id": _RUN_ID}, ["run_id"]), True,
    ),
    "runs.wait": (
        "Wait for one of your runs: answers as soon as it ends or needs a person, else after `timeout_seconds` with "
        "`done` false (call again). A run in `input_required` is decided by a person in the platform, never from here.",
        _object({
            "run_id": _RUN_ID,
            "timeout_seconds": {"type": "integer", "minimum": 0, "maximum": MAX_TIMEOUT_SECONDS,
                                "default": DEFAULT_TIMEOUT_SECONDS},
        }, ["run_id"]), True,
    ),
    "runs.list": (
        f"Your runs, the latest status change first, without results. `status` is one of {_STATES}.",
        _object({
            "status": {"type": "string"},
            "cursor": {"type": "string", "description": "`next_cursor` of the page before."},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_PAGE, "default": DEFAULT_PAGE},
        }), True,
    ),
    "runs.cancel": (
        "Cancel one of your runs. Repeating it is harmless; a run that had already ended is answered as it is.",
        _object({"run_id": _RUN_ID}, ["run_id"]), False,
    ),
}


def run_tool_arguments(input_schema: dict[str, Any]) -> dict[str, Any]:
    """The argument schema of a `run_<export>` tool around `input_schema` (`{"type": "object"}` for the check of a
    call, the version's own schema for the listing)."""
    return _object({
        "input": input_schema,
        "idempotency_key": {
            "type": "string",
            "description": "1 to 128 characters. Repeating a call with the same key and input answers the same run.",
        },
        "deadline_seconds": {
            "type": "integer", "minimum": 1,
            "description": "Cancel the run if it has not ended after this long (the export may set a shorter limit).",
        },
        "wait_seconds": {
            "type": "integer", "minimum": 0, "maximum": MAX_WAIT_SECONDS, "default": DEFAULT_WAIT_SECONDS,
            "description": "How long this call waits for the run to end before answering its current view.",
        },
    }, ["input"])


def run_tool_schema(export: dict[str, Any]) -> dict[str, Any]:
    """The advertised schema of the export's tool: the version's input schema embedded under `input`, with its
    definitions moved to the root and its local references re-pointed so that they still resolve there."""
    embedded = {k: v for k, v in export["input_schema"].items() if k not in ("$schema", "$id")}
    definitions = {k: embedded.pop(k) for k in ("$defs", "definitions") if k in embedded}
    schema = run_tool_arguments(_repoint(embedded) if embedded else {"type": "object"})
    for key, value in definitions.items():
        schema[key] = {name: _repoint(sub) for name, sub in value.items()} if isinstance(value, dict) else value
    if export["require_idempotency_key"]:
        schema["required"] = ["input", "idempotency_key"]
    return schema


_LITERALS = frozenset({"const", "enum", "default", "examples"})  # data, not schemas
_NAMED = frozenset({"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"})


def _repoint(node: Any) -> Any:
    if isinstance(node, list):
        return [_repoint(item) for item in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in _LITERALS:
            out[key] = value
        elif key in _NAMED and isinstance(value, dict):
            out[key] = {name: _repoint(sub) for name, sub in value.items()}
        elif key == "$ref" and isinstance(value, str):
            moved = value == "#" or (value.startswith("#/") and not value.startswith(("#/$defs/", "#/definitions/")))
            out[key] = "#/properties/input" + value[1:] if moved else value
        else:
            out[key] = _repoint(value)
    return out


def run_tool_description(export: dict[str, Any]) -> str:
    lines = [export["description"] or export["title"], ""]
    lines.append(
        f"Starts a run of this workflow and waits at most `wait_seconds` (up to {MAX_WAIT_SECONDS}) for it. The answer "
        "is the run view; while `done` is false, call `runs.wait` with its `run_id`."
    )
    if export["require_idempotency_key"]:
        lines.append("Send an `idempotency_key`: repeating a call with the same key and input answers the same run.")
    else:
        lines.append(
            "Without an `idempotency_key` every call starts a new run; with one, repeating the call answers the same run."
        )
    if export["result"] == "status_only":
        lines.append("The run answers its status only, no result.")
    for example in export["examples"]:
        lines.append(f"Example: {str(example)[:EXAMPLE_CHARS]}")
    return "\n".join(lines)
