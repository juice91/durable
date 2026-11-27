"""The Agent Cards (design 5.2). The public card lists no workflow (decision Q9): a name and description the tenant's
admin chose, the security scheme and one generic skill. The extended card, behind the key, lists the exports that key
reaches as skills, with each one's input schema in a declared, optional extension (a card has no field for it).

Streaming and push notifications are declared off: they are a later release."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry

from eap_api.external_a2a.protocol import PROTOCOL_VERSION
from eap_api.services.external_runs import text_property

SKILL_INPUT_EXTENSION = "urn:eap:a2a:skill-input-schema:v1"
GENERIC_SKILL = "run_exported_workflow"
JSON, TEXT = "application/json", "text/plain"
DEFAULT_TAG = "workflow"
EXTENSION_DESCRIPTION = (
    "params maps each skill id of the extended card to the JSON Schema of its input. A message may set the metadata "
    "key deadlineSeconds to cancel its task if it has not ended in that many seconds."
)
SEND_HINT = (
    "Send one data part with the input, or {\"skill\": id, \"input\": {...}}. Every send creates a task: set "
    "returnImmediately and follow it with GetTask. A blocking send waits at most 60 seconds and then answers the task "
    "that is still working."
)
STATUS_ONLY_NOTE = "The task reports its state only; the result stays in the platform."
_PLACEHOLDERS = {"string": "text", "integer": 0, "number": 0, "boolean": False, "array": [], "null": None}


def _card(
    base: str, endpoint: dict[str, str], skills: list[dict[str, Any]], params: dict[str, Any] | None
) -> dict[str, Any]:
    extension: dict[str, Any] = {"uri": SKILL_INPUT_EXTENSION, "description": EXTENSION_DESCRIPTION, "required": False}
    if params is not None:
        extension["params"] = params
    card = {
        "name": endpoint["name"],
        "description": endpoint["description"],
        "supportedInterfaces": [
            {"url": f"{base}/rpc", "protocolBinding": "JSONRPC", "protocolVersion": PROTOCOL_VERSION}
        ],
        "capabilities": {
            "streaming": False, "pushNotifications": False, "extendedAgentCard": True, "extensions": [extension],
        },
        "securitySchemes": {"runKey": {"httpAuthSecurityScheme": {"scheme": "Bearer"}}},
        "securityRequirements": [{"schemes": {"runKey": {"list": []}}}],
        "defaultInputModes": [JSON, TEXT],
        "defaultOutputModes": [JSON],
        "skills": skills,
    }
    content = json.dumps(card, sort_keys=True, default=str)
    return {**card, "version": hashlib.sha256(content.encode()).hexdigest()[:12]}  # changes with the content


def public_card(base: str, endpoint: dict[str, str]) -> dict[str, Any]:
    """The card of `base` anyone may read. A per-skill base gets the same card: it never says whether a skill exists."""
    generic = {
        "id": GENERIC_SKILL, "name": "Run an exported workflow",
        "description": f"Runs a workflow this key was given; the extended card lists them. {SEND_HINT}",
        "tags": [DEFAULT_TAG], "inputModes": [JSON, TEXT], "outputModes": [JSON],
    }
    return _card(base, endpoint, [generic], None)


def extended_card(base: str, endpoint: dict[str, str], exports: list[dict[str, Any]]) -> dict[str, Any]:
    """The card of `base` for one key: `exports` are the ones it may start there (`ExternalRunService.exports`)."""
    skills = []
    for export in exports:
        description = export["description"] or export["title"]
        if export["result"] != "output":
            description = f"{description} {STATUS_ONLY_NOTE}"
        example = _example(export["input_schema"])
        examples = [] if example is None else [json.dumps({"skill": export["name"], "input": example})]
        skills.append({
            "id": export["name"], "name": export["title"], "description": description,
            "tags": export["tags"] or [DEFAULT_TAG], "examples": examples + export["examples"],
            "inputModes": [JSON] if text_property(export["input_schema"]) is None else [JSON, TEXT],
            "outputModes": [JSON],
        })
    return _card(base, endpoint, skills, {e["name"]: e["input_schema"] for e in exports})


def _sample(schema: Any, depth: int = 0) -> Any:
    if not isinstance(schema, dict) or depth > 5:
        return None
    for key in ("const", "default"):
        if key in schema:
            return schema[key]
    for key in ("examples", "enum"):
        if isinstance(schema.get(key), list) and schema[key]:
            return schema[key][0]
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = kind[0] if kind else None
    if kind == "object" or "properties" in schema:
        properties = schema.get("properties") or {}
        return {name: _sample(properties.get(name), depth + 1) for name in schema.get("required") or ()}
    return _PLACEHOLDERS.get(kind) if isinstance(kind, str) else None


def _example(schema: dict[str, Any]) -> dict[str, Any] | None:
    """One input the schema accepts, from its defaults, examples and types; None when that simple reading finds none."""
    value = _sample(schema)
    if not isinstance(value, dict):
        return None
    try:
        # An empty registry: no remote $ref is ever retrieved.
        valid = Draft202012Validator(schema, registry=Registry()).is_valid(value)
    except Exception:  # noqa: BLE001 - an unresolvable reference: no example rather than no card
        return None
    return value if valid else None
