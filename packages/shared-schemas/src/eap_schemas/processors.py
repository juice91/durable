"""Declarative input and output processors (guardrails) for agent specs (milestone P0, task P0.2).

An agent spec lists `input_processors` and `output_processors`, at most `MAX_PROCESSORS_PER_PHASE`
each, run in list order. A processor is data, never code: one of the built-in types below, with a
bounded config. This module only declares the contracts; the deterministic implementations live in
`eap_processors`, and moderation runs in the worker.

Regex patterns are only bounded here (count and length). Their RE2 syntax is checked by
`eap_processors` at publish and draft-run validation, so `eap_schemas` needs no compiled dependency.

Wire shape of one processor: `{"type": "<type>", "config": {...}}`.
"""

from __future__ import annotations

import copy
from typing import Annotated, Any, Literal, Tuple, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from eap_schemas.model_ref import ModelRef

MAX_PROCESSORS_PER_PHASE = 8
MAX_REGEX_PATTERNS = 20
MAX_REGEX_PATTERN_LENGTH = 200
MAX_LENGTH_LIMIT = 1_000_000

ProcessorPhase = Literal["input", "step", "stream", "output"]
PROCESSOR_PHASES: tuple[ProcessorPhase, ...] = ("input", "step", "stream", "output")
# Moderation is a model call, so it runs only before the model and on the final answer.
MODEL_CALL_PHASES: tuple[ProcessorPhase, ...] = ("input", "output")

ProcessorType = Literal["pii_redact", "regex_blocklist", "max_length", "moderation"]

PiiKind = Literal["email", "phone", "credit_card", "iban", "ipv4", "us_ssn"]
PII_KINDS: tuple[PiiKind, ...] = ("email", "phone", "credit_card", "iban", "ipv4", "us_ssn")

# The fixed category set the moderation verdict is expressed in. Changing it changes the
# moderation prompt contract, so it is versioned with the platform, not per tenant.
ModerationCategory = Literal[
    "hate",
    "harassment",
    "self_harm",
    "sexual",
    "sexual_minors",
    "violence",
    "illicit",
]
MODERATION_CATEGORIES: tuple[ModerationCategory, ...] = (
    "hate",
    "harassment",
    "self_harm",
    "sexual",
    "sexual_minors",
    "violence",
    "illicit",
)

# Every action a processor outcome can report ("allow" = ran, nothing to do).
ProcessorAction = Literal["allow", "redact", "block", "truncate"]


def _unique(values: tuple[Any, ...], what: str) -> tuple[Any, ...]:
    seen: set[Any] = set()
    for value in values:
        if value in seen:
            raise ValueError(f"duplicate {what}: {value}")
        seen.add(value)
    return values


class PiiRedactConfig(BaseModel):
    """Find personal data of the chosen kinds; redact it (`[REDACTED:<kind>]`) or block the text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kinds: Tuple[PiiKind, ...] = Field(default=PII_KINDS, min_length=1, description="Kinds of personal data to detect")
    action: Literal["redact", "block"] = Field(default="redact", description="Redact matches or block the text")

    @field_validator("kinds")
    @classmethod
    def _unique_kinds(cls, v: tuple[PiiKind, ...]) -> tuple[PiiKind, ...]:
        return _unique(v, "PII kind")


class RegexBlocklistConfig(BaseModel):
    """Block (or redact) text matching any of the patterns (RE2 syntax, checked at publish)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    patterns: Tuple[Annotated[str, Field(min_length=1, max_length=MAX_REGEX_PATTERN_LENGTH)], ...] = Field(
        min_length=1,
        max_length=MAX_REGEX_PATTERNS,
        description=f"RE2 patterns, at most {MAX_REGEX_PATTERNS} of {MAX_REGEX_PATTERN_LENGTH} characters",
    )
    action: Literal["block", "redact"] = Field(default="block", description="Block the text or redact matches")

    @field_validator("patterns")
    @classmethod
    def _unique_patterns(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(v, "pattern")


class MaxLengthConfig(BaseModel):
    """Block or truncate text longer than `max_chars` characters (Unicode code points)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_chars: int = Field(ge=1, le=MAX_LENGTH_LIMIT, description="Maximum length in characters")
    action: Literal["block", "truncate"] = Field(default="block", description="Block or truncate longer text")


class ModerationConfig(BaseModel):
    """Ask a moderation model for a verdict over the fixed category set; block on a flagged category."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: ModelRef = Field(description="Model that answers the moderation check (must be allowlisted)")
    categories: Tuple[ModerationCategory, ...] = Field(
        default=MODERATION_CATEGORIES, min_length=1, description="Categories that block the text"
    )
    action: Literal["block"] = Field(default="block", description="Moderation can only block")

    @field_validator("categories")
    @classmethod
    def _unique_categories(cls, v: tuple[ModerationCategory, ...]) -> tuple[ModerationCategory, ...]:
        return _unique(v, "moderation category")


class PiiRedactProcessor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["pii_redact"] = "pii_redact"
    config: PiiRedactConfig = Field(default_factory=PiiRedactConfig)


class RegexBlocklistProcessor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["regex_blocklist"] = "regex_blocklist"
    config: RegexBlocklistConfig


class MaxLengthProcessor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["max_length"] = "max_length"
    config: MaxLengthConfig


class ModerationProcessor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["moderation"] = "moderation"
    config: ModerationConfig


ProcessorSpec = Annotated[
    Union[PiiRedactProcessor, RegexBlocklistProcessor, MaxLengthProcessor, ModerationProcessor],
    Field(discriminator="type"),
]
PROCESSOR_SPEC_ADAPTER: TypeAdapter[Any] = TypeAdapter(ProcessorSpec)


class ModerationVerdict(BaseModel):
    """Structured answer of the moderation model. Carries no quoted text, only categories."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    flagged: bool
    categories: Tuple[ModerationCategory, ...] = ()

    @field_validator("categories")
    @classmethod
    def _unique_categories(cls, v: tuple[ModerationCategory, ...]) -> tuple[ModerationCategory, ...]:
        return _unique(v, "moderation category")

    @model_validator(mode="after")
    def _consistent(self) -> "ModerationVerdict":
        if self.categories and not self.flagged:
            raise ValueError("categories are only allowed on a flagged verdict")
        return self

    def blocks(self, config: ModerationConfig) -> bool:
        """True when the verdict flags a category the config blocks. A flagged verdict without any
        category fails closed (blocks)."""
        if not self.flagged:
            return False
        if not self.categories:
            return True
        return any(category in config.categories for category in self.categories)


def _inline_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Resolve local `$ref`s, drop titles and turn `const` into a one-value `enum`, so the schema fits Studio's JsonSchemaForm subset."""
    defs = schema.get("$defs", {})

    def walk(node: Any, properties_map: bool = False) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                name = node["$ref"].rsplit("/", 1)[-1]
                resolved = walk(copy.deepcopy(defs[name]))
                extra = {k: walk(v) for k, v in node.items() if k != "$ref" and k != "title"}
                return {**resolved, **extra}
            out = {
                key: walk(value, properties_map=key == "properties")
                for key, value in node.items()
                if key != "$defs" and (properties_map or key != "title")
            }
            if not properties_map and "const" in out and "enum" not in out:
                out["enum"] = [out.pop("const")]
            return out
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    return walk(schema)


class ProcessorDeclaration(BaseModel):
    """Catalog entry for one processor type (served by `GET /processors`)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: ProcessorType
    title: str
    description: str
    phases: Tuple[ProcessorPhase, ...]
    actions: Tuple[ProcessorAction, ...]
    # True when the type runs without a model call (testable in the processors test console).
    deterministic: bool
    config_model: type[BaseModel]

    def config_json_schema(self) -> dict[str, Any]:
        """The config as JSON Schema without `$ref`s or titles (Studio form subset)."""
        schema = _inline_schema(self.config_model.model_json_schema())
        schema.pop("description", None)
        return schema


PROCESSOR_DECLARATIONS: dict[str, ProcessorDeclaration] = {
    "pii_redact": ProcessorDeclaration(
        id="pii_redact",
        title="PII redaction",
        description="Detects email addresses, phone numbers, credit card numbers (Luhn-checked), IBANs, "
        "IPv4 addresses and US social security numbers; redacts them or blocks the text.",
        phases=PROCESSOR_PHASES,
        actions=("redact", "block"),
        deterministic=True,
        config_model=PiiRedactConfig,
    ),
    "regex_blocklist": ProcessorDeclaration(
        id="regex_blocklist",
        title="Regex blocklist",
        description="Blocks text matching any of up to 20 RE2 patterns (linear time), or redacts the matches.",
        phases=PROCESSOR_PHASES,
        actions=("block", "redact"),
        deterministic=True,
        config_model=RegexBlocklistConfig,
    ),
    "max_length": ProcessorDeclaration(
        id="max_length",
        title="Maximum length",
        description="Blocks or truncates text longer than a number of characters.",
        phases=PROCESSOR_PHASES,
        actions=("block", "truncate"),
        deterministic=True,
        config_model=MaxLengthConfig,
    ),
    "moderation": ProcessorDeclaration(
        id="moderation",
        title="Moderation",
        description="Asks an allowlisted model for a verdict over a fixed category set and blocks flagged "
        "text. On output it checks the final answer once.",
        phases=MODEL_CALL_PHASES,
        actions=("block",),
        deterministic=False,
        config_model=ModerationConfig,
    ),
}


def processor_models(processors: tuple[BaseModel, ...]) -> tuple[ModelRef, ...]:
    """Every model a processor list calls (moderation), for the allowlist check at publish and run."""
    return tuple(p.config.model for p in processors if isinstance(p, ModerationProcessor))


__all__ = [
    "MAX_LENGTH_LIMIT",
    "MODEL_CALL_PHASES",
    "MAX_PROCESSORS_PER_PHASE",
    "MAX_REGEX_PATTERNS",
    "MAX_REGEX_PATTERN_LENGTH",
    "MODERATION_CATEGORIES",
    "PII_KINDS",
    "PROCESSOR_DECLARATIONS",
    "PROCESSOR_PHASES",
    "PROCESSOR_SPEC_ADAPTER",
    "MaxLengthConfig",
    "MaxLengthProcessor",
    "ModerationCategory",
    "ModerationConfig",
    "ModerationProcessor",
    "ModerationVerdict",
    "PiiKind",
    "PiiRedactConfig",
    "PiiRedactProcessor",
    "ProcessorAction",
    "ProcessorDeclaration",
    "ProcessorPhase",
    "ProcessorSpec",
    "ProcessorType",
    "RegexBlocklistConfig",
    "RegexBlocklistProcessor",
    "processor_models",
]
