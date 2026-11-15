"""Spec-driven behaviour of `eap-agent` as one construction-time capability (agent capabilities milestone).

Pydantic AI rejects per-run capabilities inside a DBOS workflow, and an agent-level output validator
cannot be combined with a per-run `output_type`. So every behaviour that depends on the run's spec
lives in ONE capability attached when `eap-agent` is constructed, which reads the spec from
`ctx.deps.spec` on every run:

    capabilities=[ResolveModelId(...), RetrieveNumbering(), SpecCapability(), DBOSDurability(...)]

Its hooks run workflow-side (never inside a DBOS step) and are pure, so they add no durable
operation and behave identically on replay. A spec without the matching field is untouched.

AC.4 (structured output): `after_output_validate` checks a structured candidate against
`spec.output.json_schema` (Draft 2020-12 `jsonschema` with an empty registry, the same rule as
`workflow_run.final_output`). `StructuredDict` only checks for a dict, so this is the real schema
check. A failure raises `ModelRetry` with at most MAX_OUTPUT_ERRORS errors as `<json path>:
<message>`, at most MAX_OUTPUT_ERROR_BYTES, and never a value from the candidate. The hook fires
only for structured output, never for plain text.

The per-run inputs are built here too: `output_type_for(spec)` and `retries_for(spec)`. They are
passed to `agent.iter` only when `spec.output` is set, so a spec without it runs exactly as before.
"""

from __future__ import annotations

import copy
import json
import math
from functools import lru_cache
from typing import Any

from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.output import NativeOutput, PromptedOutput, StructuredDict, ToolOutput

from eap_schemas import AgentSpec
from eap_schemas.output import MAX_OUTPUT_ERROR_BYTES, MAX_OUTPUT_ERRORS, OUTPUT_TOOL_NAME

# The first line of the retry message the model gets for a schema-invalid candidate.
RETRY_HEADING = "The output does not match the required JSON schema. Fix these errors and answer again:"


# --------------------------------------------------------------------------- per-run inputs


def output_type_for(spec: AgentSpec) -> Any | None:
    """The per-run `output_type` for a structured spec (None without `spec.output`): a
    `StructuredDict` of the spec's schema, as the output tool `final_result` (tool mode, the
    default), `NativeOutput` or `PromptedOutput`."""
    out = spec.output
    if out is None:
        return None
    # StructuredDict keeps (and annotates) the schema it is given: never the spec's own dict.
    structured = StructuredDict(copy.deepcopy(out.json_schema), name=out.effective_name(), description=out.description)
    mode = out.effective_mode()
    if mode == "native":
        return NativeOutput(structured, name=out.effective_name(), description=out.description)
    if mode == "prompted":
        return PromptedOutput(structured, name=out.effective_name(), description=out.description)
    return ToolOutput(structured, name=OUTPUT_TOOL_NAME, description=out.description)


def retries_for(spec: AgentSpec) -> dict[str, int] | None:
    """The per-run `retries` for a structured spec: only the output budget (tool retries keep the
    agent's default). None without `spec.output`."""
    if spec.output is None:
        return None
    return {"output": spec.output.effective_max_retries()}


# --------------------------------------------------------------------------- schema errors


@lru_cache(maxsize=256)
def _validator(schema_json: str) -> Any:
    import jsonschema
    from referencing import Registry

    schema = json.loads(schema_json)
    cls = jsonschema.validators.validator_for(schema, default=jsonschema.Draft202012Validator)
    return cls(schema, registry=Registry())  # empty registry: no $ref is ever retrieved


def validator_for(schema: dict[str, Any]) -> Any:
    """A cached jsonschema validator for an output schema (already checked by the spec contract)."""
    return _validator(json.dumps(schema, sort_keys=True, separators=(",", ":")))


def _path(parts: Any) -> str:
    """A JSONPath-like `$.a[0].b` from path segments (strings and ints)."""
    out = "$"
    for seg in parts:
        if isinstance(seg, int):
            out += f"[{seg}]"
        elif isinstance(seg, str) and seg.isidentifier():
            out += f".{seg}"
        else:
            out += f"[{json.dumps(str(seg), ensure_ascii=False)}]"
    return out


def _schema_path(schema: Any, parts: Any) -> list[Any]:
    """`parts` cut at the first object key `schema` does not declare as a property: such a key is
    chosen by the model (`additionalProperties`) and may hold anything, so it never goes into an
    error path. Array indexes are kept."""
    out: list[Any] = []
    node = schema
    for seg in parts:
        if isinstance(seg, int):
            out.append(seg)
            items = node.get("items") if isinstance(node, dict) else None
            node = items if isinstance(items, dict) else {}
            continue
        properties = node.get("properties") if isinstance(node, dict) else None
        if not isinstance(properties, dict) or seg not in properties:
            break
        out.append(seg)
        node = properties[seg]
    return out


def _non_finite(value: Any) -> list[Any] | None:
    stack: list[tuple[Any, list[Any]]] = [(value, [])]
    while stack:
        item, path = stack.pop()
        if isinstance(item, float) and not math.isfinite(item):
            return path
        if isinstance(item, dict):
            stack.extend((v, [*path, k]) for k, v in item.items())
        elif isinstance(item, (list, tuple)):
            stack.extend((v, [*path, i]) for i, v in enumerate(item))
    return None


def _schema_value(value: Any) -> str:
    """A value FROM THE SCHEMA (enum, const, a bound), rendered for the model, bounded."""
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return text if len(text) <= 200 else text[:197] + "..."


_COMPARE = {"minimum": ">=", "maximum": "<=", "exclusiveMinimum": ">", "exclusiveMaximum": "<"}
_SIZE = {
    "minLength": ("at least", "characters"),
    "maxLength": ("at most", "characters"),
    "minItems": ("at least", "items"),
    "maxItems": ("at most", "items"),
    "minProperties": ("at least", "properties"),
    "maxProperties": ("at most", "properties"),
}


def _message(error: Any) -> str:
    """A fixed message per failed keyword, built only from the schema (never the candidate)."""
    keyword, expected = error.validator, error.validator_value
    if keyword == "type":
        types = expected if isinstance(expected, list) else [expected]
        return "expected type " + " or ".join(str(t) for t in types)
    if keyword == "required":
        missing = [p for p in expected if isinstance(error.instance, dict) and p not in error.instance]
        return "missing required properties " + _schema_value(missing) if len(missing) > 1 else (
            "missing required property " + _schema_value(missing[0] if missing else "?")
        )
    if keyword == "additionalProperties":
        return "additional properties are not allowed"
    if keyword == "enum":
        return "must be one of " + _schema_value(expected)
    if keyword == "const":
        return "must equal " + _schema_value(expected)
    if keyword in _COMPARE:
        return f"must be {_COMPARE[keyword]} {_schema_value(expected)}"
    if keyword in _SIZE:
        bound, unit = _SIZE[keyword]
        return f"must have {bound} {_schema_value(expected)} {unit}"
    return f"does not satisfy {keyword}"


def schema_errors(schema: dict[str, Any], value: Any) -> list[tuple[str, str]]:
    """`(json path, message)` for every way `value` breaks `schema`, sorted, deduplicated, at most
    MAX_OUTPUT_ERRORS. Empty when valid. No message ever contains a value from `value`, and no path
    a key the schema does not declare (the path stops at the object holding it)."""
    bad = _non_finite(value)
    if bad is not None:
        return [(_path(_schema_path(schema, bad)), "must be a finite number")]
    found: set[tuple[str, str]] = set()
    for error in validator_for(schema).iter_errors(value):
        found.add((_path(_schema_path(schema, error.absolute_path)), _message(error)))
    return sorted(found)[:MAX_OUTPUT_ERRORS]


def retry_message(errors: list[tuple[str, str]]) -> str:
    """The model's feedback for a rejected candidate: one `- <path>: <message>` line per error,
    whole lines only, at most MAX_OUTPUT_ERROR_BYTES in UTF-8."""
    lines = [RETRY_HEADING]
    size = len(RETRY_HEADING.encode("utf-8"))
    for path, message in errors:
        line = f"- {path}: {message}"
        size += 1 + len(line.encode("utf-8"))
        if size > MAX_OUTPUT_ERROR_BYTES:
            break
        lines.append(line)
    return "\n".join(lines)


def error_paths(content: Any) -> list[str]:
    """The JSON paths of a Pydantic validation error list (a retry part's content), deduplicated,
    in order; [] for anything else."""
    if not isinstance(content, list):
        return []
    paths: list[str] = []
    for detail in content:
        if isinstance(detail, dict):
            path = _path(detail.get("loc") or ())
            if path not in paths:
                paths.append(path)
    return paths[:MAX_OUTPUT_ERRORS]


# --------------------------------------------------------------------------- the capability


class SpecCapability(AbstractCapability[Any]):
    """The run spec's behaviour (see the module docstring). Workflow-side and pure: no step."""

    async def after_output_validate(
        self, ctx: RunContext[Any], *, output_context: Any, output: Any
    ) -> Any:
        spec = getattr(ctx.deps, "spec", None)
        out = getattr(spec, "output", None)
        if out is None or ctx.partial_output:
            return output
        errors = schema_errors(out.json_schema, output)
        if errors:
            raise ModelRetry(retry_message(errors))
        return output

    @classmethod
    def get_serialization_name(cls) -> str | None:
        return None


__all__ = [
    "RETRY_HEADING",
    "SpecCapability",
    "error_paths",
    "output_type_for",
    "retries_for",
    "retry_message",
    "schema_errors",
    "validator_for",
]
