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
import math
import re
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from eap_schemas._omit import OmitNoneModel
from eap_schemas.embedding_policy import parse_embedding_model_id
from eap_schemas.json_schema_subset import (
    MAX_JSON_DEPTH,
    SchemaSubsetInvalid,
    is_scalar,
    json_depth_exceeds,
    json_size,
    matches_type,
    typed_key,
)
from eap_schemas.json_schema_subset import validate as validate_schema_subset
from eap_schemas.model_ref import ModelRef
from eap_schemas.processors import PiiRedactConfig

MAX_TEMPLATE_BYTES = 4 * 1024
MAX_SCHEMA_BYTES = 8 * 1024
MAX_SCHEMA_DEPTH = 3
MAX_SCHEMA_PROPERTIES = 50
MAX_SCHEMA_ENUM_VALUES = 64
MAX_WORKING_MEMORY_BYTES = 16 * 1024
# Nesting cap (objects and arrays) for content, patches and schemas: MAX_JSON_DEPTH (16), shared
# with eap_schemas.json_schema_subset and re-exported here.
DEFAULT_WORKING_MEMORY_TEMPLATE = "# User\n- Name:\n- Preferences:\n- Goals:"
# Upper bound of the "Related earlier conversations" block.
MAX_RECALL_BLOCK_BYTES = 8 * 1024

WorkingMemoryScope = Literal["conversation", "resource"]
WorkingMemoryFormat = Literal["markdown", "json"]
RecallScope = Literal["resource", "conversation"]

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


_SCALAR_TYPES = ("string", "number", "integer", "boolean")

# Value helpers shared with the subset validator (eap_schemas.json_schema_subset).
_json_len = json_size
_is_scalar = is_scalar
_typed_key = typed_key
_matches_type = matches_type


def _check_depth(value: Any, what: str, limit: int = MAX_JSON_DEPTH) -> None:
    """Raise WorkingMemoryInvalid if `value` nests objects or arrays deeper than `limit` (iterative)."""
    if json_depth_exceeds(value, limit):
        raise WorkingMemoryInvalid(f"{what} nests deeper than {limit} levels")


def _join(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


# --- schema subset ------------------------------------------------------------------------------


def validate_working_memory_schema(schema: Any) -> dict[str, Any]:
    """Check that `schema` is inside the supported JSON Schema subset; return it unchanged.

    Delegates to the shared subset validator (eap_schemas.json_schema_subset, "working_memory"
    dialect) with this module's limits. Raises WorkingMemoryInvalid naming the offending path.
    """
    try:
        return validate_schema_subset(
            schema,
            max_depth=MAX_SCHEMA_DEPTH,
            max_properties=MAX_SCHEMA_PROPERTIES,
            max_bytes=MAX_SCHEMA_BYTES,
            root_object=True,
            dialect="working_memory",
        )
    except SchemaSubsetInvalid as exc:
        raise WorkingMemoryInvalid(exc.reason, exc.path) from None


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
