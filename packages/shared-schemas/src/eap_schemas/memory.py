"""Agent memory contracts (RAG and memory milestone, task RM.2).

`AgentSpec.memory: MemoryConfig | None`. A spec without `memory` behaves exactly as before (all
completed turns up to EAP_MAX_HISTORY_BYTES). With `memory` set, each feature is on only when its
sub-config is set, except `history`, whose defaults (last 20 turns) apply whenever memory is set
(`MemoryConfig.effective_history()`).

Every sub-config and every optional field is dropped from dumps while None, so `memory: {}` hashes
the same as explicit Nones and a later optional field keeps every stored hash.

For the agent editor (RM.17): when memory is off, send `memory: null` (or leave it out), never
`memory: {}`. `{}` is "memory on with default history limits": it changes the hash and limits
history to the last 20 turns. `redact: null` means "inherit the agent's input `pii_redact`
processor config, if any"; there is no way to switch inherited redaction off while that input
processor exists (by design: memory never stores what the input path redacts).

Working memory is shaped by a Markdown template (replace semantics) or by a JSON Schema subset
(merge semantics: objects deep-merge, `null` deletes, arrays are replaced). The subset:

- the root is `{"type": "object", "properties": {...}}`;
- a property is `type` string, number, integer, boolean, object (nested `properties`) or array
  (`items` a scalar schema), or an `enum` of scalars (optionally with a scalar `type`);
- keywords: `type`, `properties`, `items`, `enum`, `description`, `title`, `required`,
  `additionalProperties` (boolean), and `$schema` at the root;
- object depth at most 3 (the root counts as 1), at most 50 properties in total, at most 8 KiB.

`required` is accepted and ignored: the note starts empty and fills over time, so every property
is optional. `additionalProperties` defaults to true (standard JSON Schema); false rejects unknown
keys. Validation messages name the path and the expected type, never a value and never a
model-written key: a path stops at the first key that is not a schema property (memory text never
goes to logs). Content, patches and schemas nest at most MAX_JSON_DEPTH levels, checked without
recursion before anything else walks them.

No I/O here.
"""

from __future__ import annotations

import copy
import json
import math
import re
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from eap_schemas._omit import OmitNoneModel
from eap_schemas.embedding_policy import parse_embedding_model_id
from eap_schemas.model_ref import ModelRef
from eap_schemas.processors import PiiRedactConfig

MAX_TEMPLATE_BYTES = 4 * 1024
MAX_SCHEMA_BYTES = 8 * 1024
MAX_SCHEMA_DEPTH = 3
MAX_SCHEMA_PROPERTIES = 50
MAX_SCHEMA_ENUM_VALUES = 64
MAX_WORKING_MEMORY_BYTES = 16 * 1024
# Nesting cap (objects and arrays) for content, patches and schemas.
MAX_JSON_DEPTH = 16
DEFAULT_WORKING_MEMORY_TEMPLATE = "# User\n- Name:\n- Preferences:\n- Goals:"
# Upper bound of the "Related earlier conversations" block.
MAX_RECALL_BLOCK_BYTES = 8 * 1024

WorkingMemoryScope = Literal["conversation", "resource"]
WorkingMemoryFormat = Literal["markdown", "json"]
RecallScope = Literal["resource", "conversation"]

_SCALAR_TYPES = ("string", "number", "integer", "boolean")
_ALLOWED_KEYWORDS = frozenset(
    {"type", "properties", "items", "enum", "description", "title", "required", "additionalProperties"}
)


class WorkingMemoryInvalid(ValueError):
    """A working memory schema, patch or content breaks the rules. Model-visible; never echoes values."""

    code = "working_memory_invalid"

    def __init__(self, reason: str, path: str = "") -> None:
        self.reason = reason
        self.path = path
        super().__init__(reason, path)

    def __str__(self) -> str:
        where = f" at {self.path}" if self.path else ""
        return f"working memory is invalid{where}: {self.reason}"


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def _json_len(value: Any) -> int:
    return _utf8_len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True))


def _check_depth(value: Any, what: str, limit: int = MAX_JSON_DEPTH) -> None:
    """Raise WorkingMemoryInvalid if `value` nests objects or arrays deeper than `limit` (iterative)."""
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, dict):
            children = node.values()
        elif isinstance(node, list):
            children = node
        else:
            continue
        if depth > limit:
            raise WorkingMemoryInvalid(f"{what} nests deeper than {limit} levels")
        stack.extend((child, depth + 1) for child in children)


def _join(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


# --- schema subset ------------------------------------------------------------------------------


def _is_scalar(value: Any) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    return isinstance(value, (str, bool, int))


def validate_working_memory_schema(schema: Any) -> dict[str, Any]:
    """Check that `schema` is inside the supported JSON Schema subset; return it unchanged.

    Raises WorkingMemoryInvalid naming the offending path.
    """
    if not isinstance(schema, dict):
        raise WorkingMemoryInvalid("the schema must be a JSON object")
    _check_depth(schema, "the schema")
    try:
        size = _json_len(schema)
    except (TypeError, ValueError):
        raise WorkingMemoryInvalid("the schema must be JSON") from None
    if size > MAX_SCHEMA_BYTES:
        raise WorkingMemoryInvalid(f"the schema must be at most {MAX_SCHEMA_BYTES} bytes")
    if schema.get("type") != "object":
        raise WorkingMemoryInvalid('the root must have "type": "object"')
    root = {k: v for k, v in schema.items() if k != "$schema"}
    if "$schema" in schema and not isinstance(schema["$schema"], str):
        raise WorkingMemoryInvalid('"$schema" must be a string')
    count = [0]
    _check_node(root, "", depth=1, count=count)
    return schema


def _check_node(node: Any, path: str, *, depth: int, count: list[int], in_array: bool = False) -> None:
    where = path or "(root)"
    if not isinstance(node, dict):
        raise WorkingMemoryInvalid("a schema must be a JSON object", where)
    unknown = sorted(set(node) - _ALLOWED_KEYWORDS)
    if unknown:
        raise WorkingMemoryInvalid(f"unsupported keyword {unknown[0]!r}", where)
    for key in ("description", "title"):
        if key in node and not isinstance(node[key], str):
            raise WorkingMemoryInvalid(f'"{key}" must be a string', where)
    type_ = node.get("type")
    if "enum" in node:
        values = node["enum"]
        if not isinstance(values, list) or not 1 <= len(values) <= MAX_SCHEMA_ENUM_VALUES:
            raise WorkingMemoryInvalid(f'"enum" must be a list of 1 to {MAX_SCHEMA_ENUM_VALUES} values', where)
        if not all(_is_scalar(v) for v in values):
            raise WorkingMemoryInvalid('"enum" values must be strings, numbers or booleans', where)
        if len({_typed_key(v) for v in values}) != len(values):
            raise WorkingMemoryInvalid('"enum" values must be unique', where)
        if type_ is not None:
            if type_ not in _SCALAR_TYPES:
                raise WorkingMemoryInvalid('an "enum" may only have a scalar "type"', where)
            if not all(_matches_type(v, type_) for v in values):
                raise WorkingMemoryInvalid(f'"enum" values must all be of type {type_}', where)
        extra = set(node) - {"enum", "type", "description", "title"}
        if extra:
            raise WorkingMemoryInvalid(f"unsupported keyword {sorted(extra)[0]!r} on an enum", where)
        return
    if type_ in _SCALAR_TYPES:
        extra = set(node) - {"type", "description", "title"}
        if extra:
            raise WorkingMemoryInvalid(f"unsupported keyword {sorted(extra)[0]!r} on a {type_}", where)
        return
    if in_array:
        raise WorkingMemoryInvalid("array items must be a string, number, integer, boolean or enum", where)
    if type_ == "array":
        extra = set(node) - {"type", "items", "description", "title"}
        if extra:
            raise WorkingMemoryInvalid(f"unsupported keyword {sorted(extra)[0]!r} on an array", where)
        if "items" not in node:
            raise WorkingMemoryInvalid('an array needs "items"', where)
        _check_node(node["items"], f"{path}[]", depth=depth, count=count, in_array=True)
        return
    if type_ == "object":
        if depth > MAX_SCHEMA_DEPTH:
            raise WorkingMemoryInvalid(f"objects may be nested at most {MAX_SCHEMA_DEPTH} deep", where)
        if "items" in node:
            raise WorkingMemoryInvalid('unsupported keyword "items" on an object', where)
        properties = node.get("properties", {})
        if not isinstance(properties, dict):
            raise WorkingMemoryInvalid('"properties" must be an object', where)
        additional = node.get("additionalProperties", True)
        if not isinstance(additional, bool):
            raise WorkingMemoryInvalid('"additionalProperties" must be true or false', where)
        required = node.get("required", [])
        if not isinstance(required, list) or not all(isinstance(r, str) for r in required):
            raise WorkingMemoryInvalid('"required" must be a list of property names', where)
        for name, child in properties.items():
            if not name or len(name) > 64:
                raise WorkingMemoryInvalid("property names must be 1 to 64 characters", where)
            count[0] += 1
            if count[0] > MAX_SCHEMA_PROPERTIES:
                raise WorkingMemoryInvalid(f"at most {MAX_SCHEMA_PROPERTIES} properties in total")
            _check_node(child, _join(path, name), depth=depth + 1, count=count)
        return
    if type_ is None:
        raise WorkingMemoryInvalid('a schema needs a "type" or an "enum"', where)
    raise WorkingMemoryInvalid(f"unsupported type {type_!r}", where)


def _typed_key(value: Any) -> tuple[str, Any]:
    # Python treats True == 1 == 1.0; JSON does not distinguish 1 and 1.0 but does distinguish booleans.
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, (int, float)):
        return ("number", value)
    return ("string", value)


def _matches_type(value: Any, type_: str) -> bool:
    if type_ == "string":
        return isinstance(value, str)
    if type_ == "boolean":
        return isinstance(value, bool)
    if type_ == "integer":
        if isinstance(value, bool):
            return False
        return isinstance(value, int) or (isinstance(value, float) and math.isfinite(value) and value.is_integer())
    if type_ == "number":
        if isinstance(value, bool):
            return False
        return isinstance(value, int) or (isinstance(value, float) and math.isfinite(value))
    return False


# --- content ------------------------------------------------------------------------------------


def validate_working_memory_content(schema: dict[str, Any], content: Any) -> dict[str, Any]:
    """Validate schema-mode content against a (subset-valid) schema and the 16 KiB cap; return it."""
    if not isinstance(content, dict):
        raise WorkingMemoryInvalid("the content must be a JSON object")
    _check_depth(content, "the content")
    try:
        size = _json_len(content)
    except (TypeError, ValueError):
        raise WorkingMemoryInvalid("the content must be JSON") from None
    if size > MAX_WORKING_MEMORY_BYTES:
        raise WorkingMemoryInvalid(f"the content must be at most {MAX_WORKING_MEMORY_BYTES} bytes")
    _check_value(schema, content, "")
    return content


def validate_working_memory_text(content: Any) -> str:
    """Validate template-mode (Markdown) content: a string of at most 16 KiB."""
    if not isinstance(content, str):
        raise WorkingMemoryInvalid("the content must be a string")
    if _utf8_len(content) > MAX_WORKING_MEMORY_BYTES:
        raise WorkingMemoryInvalid(f"the content must be at most {MAX_WORKING_MEMORY_BYTES} bytes")
    return content


def _check_value(schema: dict[str, Any], value: Any, path: str) -> None:
    where = path or "(root)"
    if "enum" in schema:
        if not _is_scalar(value) or _typed_key(value) not in {_typed_key(v) for v in schema["enum"]}:
            raise WorkingMemoryInvalid("must be one of the allowed values", where)
        return
    type_ = schema.get("type")
    if type_ in _SCALAR_TYPES:
        if not _matches_type(value, type_):
            raise WorkingMemoryInvalid(f"must be of type {type_}", where)
        return
    if type_ == "array":
        if not isinstance(value, list):
            raise WorkingMemoryInvalid("must be an array", where)
        for i, item in enumerate(value):
            _check_value(schema["items"], item, f"{path}[{i}]")
        return
    if type_ == "object":
        if not isinstance(value, dict):
            raise WorkingMemoryInvalid("must be an object", where)
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)
        for key, item in value.items():
            if not isinstance(key, str):
                raise WorkingMemoryInvalid("object keys must be strings", where)
            if key in properties:
                _check_value(properties[key], item, _join(path, key))
            elif not additional:
                raise WorkingMemoryInvalid("unknown property", where)
            else:
                # Not a schema property: the key is model-written, so the path stops here.
                _check_json(item, where)
        return
    raise WorkingMemoryInvalid("unsupported schema", where)


def _check_json(value: Any, path: str) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise WorkingMemoryInvalid("numbers must be finite", path)
        return
    if isinstance(value, list):
        for item in value:
            _check_json(item, path)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise WorkingMemoryInvalid("object keys must be strings", path)
            _check_json(item, path)
        return
    raise WorkingMemoryInvalid("must be JSON", path)


def _strip_nulls(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip_nulls(v) for k, v in value.items() if v is not None}
    return copy.deepcopy(value)


def merge_working_memory(content: dict[str, Any] | None, patch: Any) -> dict[str, Any]:
    """Deep-merge `patch` into `content` and return a new dict (neither input is mutated).

    Objects merge key by key; `null` deletes the key; arrays and scalars replace. A nested patch
    object for a missing or non-object key becomes that object with its nulls removed.
    """
    if not isinstance(patch, dict):
        raise WorkingMemoryInvalid("the patch must be a JSON object")
    _check_depth(patch, "the patch")
    if isinstance(content, dict):
        _check_depth(content, "the content")
    base = copy.deepcopy(content) if isinstance(content, dict) else {}
    return _merge(base, patch)


def _merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    for key, value in patch.items():
        if value is None:
            base.pop(key, None)
        elif isinstance(value, dict):
            current = base.get(key)
            base[key] = _merge(current, value) if isinstance(current, dict) else _strip_nulls(value)
        else:
            base[key] = copy.deepcopy(value)
    return base


# --- configs ------------------------------------------------------------------------------------


class HistoryConfig(OmitNoneModel):
    """Conversation history limits: whole turns, oldest dropped first."""

    last_turns: int = Field(default=20, ge=1, le=200, description="Completed turns kept in context")
    max_tokens: int | None = Field(default=None, ge=1, le=1_000_000, description="Token budget (chars / 4)")


class WorkingMemoryConfig(OmitNoneModel):
    """A structured note the agent reads and updates. Exactly one of template and json_schema (both
    None means DEFAULT_WORKING_MEMORY_TEMPLATE)."""

    scope: WorkingMemoryScope = "resource"
    template: str | None = Field(default=None, description="Markdown template (replace semantics)")
    json_schema: dict[str, Any] | None = Field(default=None, description="JSON Schema subset (merge semantics)")
    read_only: bool = False

    @field_validator("template")
    @classmethod
    def _template(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not v.strip():
            raise ValueError("template cannot be blank")
        if _utf8_len(v) > MAX_TEMPLATE_BYTES:
            raise ValueError(f"template must be at most {MAX_TEMPLATE_BYTES} bytes in UTF-8")
        return v

    @field_validator("json_schema")
    @classmethod
    def _schema(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        if v is None:
            return v
        try:
            return validate_working_memory_schema(v)
        except WorkingMemoryInvalid as exc:
            raise ValueError(str(exc)) from None

    @model_validator(mode="after")
    def _one_shape(self) -> "WorkingMemoryConfig":
        if self.template is not None and self.json_schema is not None:
            raise ValueError("set either template or json_schema, not both")
        return self

    @property
    def format(self) -> WorkingMemoryFormat:
        return "json" if self.json_schema is not None else "markdown"

    def effective_template(self) -> str | None:
        """The Markdown template in force (None in schema mode)."""
        if self.json_schema is not None:
            return None
        return self.template if self.template is not None else DEFAULT_WORKING_MEMORY_TEMPLATE


class ObservationalMemoryConfig(OmitNoneModel):
    """Compress older turns into observations (conversation scope only)."""

    model: ModelRef = Field(description="Observer and reflector model (must be allowlisted)")
    observe_after_tokens: int = Field(default=12_000, ge=1_000, le=200_000)
    keep_recent_turns: int = Field(default=4, ge=0, le=50)
    reflect_after_tokens: int = Field(default=8_000, ge=1_000, le=200_000)


class SemanticRecallConfig(OmitNoneModel):
    """Bring back similar turns from the same resource's (or conversation's) history."""

    embedding_model: str = Field(description="`provider:name` from the embedding allowlist")
    top_k: int = Field(default=4, ge=1, le=10)
    message_range: int = Field(default=1, ge=0, le=3, description="Neighbouring turns around each hit")
    scope: RecallScope = "resource"
    min_score: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator("embedding_model")
    @classmethod
    def _model_id(cls, v: str) -> str:
        parse_embedding_model_id(v)
        return v


class MemoryConfig(OmitNoneModel):
    """Agent memory. Every sub-config is dropped from dumps while None."""

    history: HistoryConfig | None = None
    working: WorkingMemoryConfig | None = None
    observational: ObservationalMemoryConfig | None = None
    semantic_recall: SemanticRecallConfig | None = None
    # PII redaction applied to every memory write. None = the agent's input pii_redact processor
    # config if it has one, otherwise no redaction (see effective_redact).
    redact: PiiRedactConfig | None = None

    @field_validator("redact")
    @classmethod
    def _redact_only(cls, v: PiiRedactConfig | None) -> PiiRedactConfig | None:
        if v is not None and v.action != "redact":
            raise ValueError("memory.redact must use action 'redact' (memory writes are never blocked)")
        return v

    def effective_history(self) -> HistoryConfig:
        return self.history if self.history is not None else HistoryConfig()

    def effective_redact(self, input_processors: tuple[Any, ...] = ()) -> PiiRedactConfig | None:
        """The redaction config for memory writes: `redact`, else the kinds of EVERY input
        `pii_redact` processor merged (first appearance order, action forced to redact), else None."""
        if self.redact is not None:
            return self.redact
        kinds: list[Any] = []
        for processor in input_processors:
            if getattr(processor, "type", None) == "pii_redact":
                kinds.extend(k for k in processor.config.kinds if k not in kinds)
        if not kinds:
            return None
        return PiiRedactConfig(kinds=tuple(kinds), action="redact")

    def chat_models(self) -> tuple[ModelRef, ...]:
        """Chat models memory calls (observer), for the model allowlist check."""
        return (self.observational.model,) if self.observational is not None else ()

    def embedding_models(self) -> tuple[str, ...]:
        """Embedding models memory calls, for the embedding allowlist check."""
        return (self.semantic_recall.embedding_model,) if self.semantic_recall is not None else ()


# ------------------------------------------------------------------ the instructions block

# The worker (eap_worker.memory.render_memory_instructions) appends this block to a memory turn's
# instructions: it starts with these two lines and ends with the closing tag (note content escapes
# `<`, so the tag cannot occur inside it).
WORKING_MEMORY_BLOCK_HEADING = "## Working memory"
WORKING_MEMORY_BLOCK_LEAD = "Your persistent notes, kept across "
WORKING_MEMORY_BLOCK_END = "</working_memory>"
_WM_BLOCK_RE = re.compile(
    r"(?:\n\n)?" + re.escape(f"{WORKING_MEMORY_BLOCK_HEADING}\n{WORKING_MEMORY_BLOCK_LEAD}") + r".*?"
    + re.escape(WORKING_MEMORY_BLOCK_END),
    re.S,
)


def strip_working_memory_block(instructions: str | None) -> str | None:
    """`instructions` without the working memory block(s); the base instructions stay."""
    if not instructions or WORKING_MEMORY_BLOCK_HEADING not in instructions:
        return instructions
    return _WM_BLOCK_RE.sub("", instructions)


def strip_working_memory_messages(messages: Any) -> Any:
    """Stored messages (ModelMessage JSON) with the working memory block removed from every
    request's `instructions`. Returns a new list (unchanged messages are shared); anything that is
    not a list is returned as is."""
    if not isinstance(messages, list):
        return messages
    out: list[Any] = []
    for message in messages:
        if isinstance(message, dict) and message.get("kind") == "request" and isinstance(message.get("instructions"), str):
            stripped = strip_working_memory_block(message["instructions"])
            if stripped != message["instructions"]:
                message = {**message, "instructions": stripped or None}
        out.append(message)
    return out


__all__ = [
    "WORKING_MEMORY_BLOCK_END",
    "WORKING_MEMORY_BLOCK_HEADING",
    "WORKING_MEMORY_BLOCK_LEAD",
    "strip_working_memory_block",
    "strip_working_memory_messages",
    "DEFAULT_WORKING_MEMORY_TEMPLATE",
    "HistoryConfig",
    "MAX_JSON_DEPTH",
    "MAX_RECALL_BLOCK_BYTES",
    "MAX_SCHEMA_BYTES",
    "MAX_SCHEMA_DEPTH",
    "MAX_SCHEMA_ENUM_VALUES",
    "MAX_SCHEMA_PROPERTIES",
    "MAX_TEMPLATE_BYTES",
    "MAX_WORKING_MEMORY_BYTES",
    "MemoryConfig",
    "ObservationalMemoryConfig",
    "RecallScope",
    "SemanticRecallConfig",
    "WorkingMemoryConfig",
    "WorkingMemoryFormat",
    "WorkingMemoryInvalid",
    "WorkingMemoryScope",
    "merge_working_memory",
    "validate_working_memory_content",
    "validate_working_memory_schema",
    "validate_working_memory_text",
]
