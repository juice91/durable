"""Embedding model allowlist (RAG and memory milestone, task RM.2).

A sibling of `ModelPolicy`, shared by the control plane (knowledge base create, reindex, publish of
semantic recall) and the worker (before every embedding call). Configured with
`EAP_ALLOWED_EMBEDDING_MODELS`: a comma-separated list of entries

    provider:name=<dims>     an exact model with its output dimensions, e.g.
                             litellm:Qwen3-VL-Embedding-8B-mlx-8bit=1024
    provider:*               every model of that provider; dimensions come from the built-in
                             table (`test:hash`, `test:hash768`) or are unknown
    provider:*=<dims>        every model of that provider, all at <dims>

where <dims> is one of 768, 1024 or 1536 (the partial HNSW indexes). The model name may hold any
character except whitespace, `,` and `=` (dots, dashes and mixed case parse); matching is exact and
case-sensitive, and a model id is split on its first `:`, as for chat models. An exact entry wins
over a wildcard. Unset or blank means `test:*`.

Optional per-model query instructions (Qwen3 embedding models want `Instruct: <task>\\nQuery: <text>`
on queries only; documents are embedded as-is) come from a separate variable,
`EAP_EMBEDDING_QUERY_INSTRUCTIONS`, a JSON object `{"<provider:name>": "<task>"}`, so the
allowlist keeps the entry shape the decisions table fixes. Every key must be allowed by the policy.

Dependency-free (stdlib only) so eap_api and eap_worker can both import it.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

ALLOWED_EMBEDDING_MODELS_ENV = "EAP_ALLOWED_EMBEDDING_MODELS"
EMBEDDING_QUERY_INSTRUCTIONS_ENV = "EAP_EMBEDDING_QUERY_INSTRUCTIONS"
DEFAULT_ALLOWED_EMBEDDING_MODELS = "test:*"
ALLOWED_EMBEDDING_DIMENSIONS: tuple[int, ...] = (768, 1024, 1536)
# Scripted embedders of the worker (RM.6) and their fixed dimensions, used for `test:*`.
BUILTIN_EMBEDDING_DIMENSIONS: Mapping[str, int] = {"test:hash": 1536, "test:hash768": 768}
MAX_QUERY_INSTRUCTION_LENGTH = 1024

_PROVIDER_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_NAME_RE = re.compile(r"^[^\s,=]{1,128}$")

__all__ = (
    "ALLOWED_EMBEDDING_DIMENSIONS",
    "ALLOWED_EMBEDDING_MODELS_ENV",
    "BUILTIN_EMBEDDING_DIMENSIONS",
    "DEFAULT_ALLOWED_EMBEDDING_MODELS",
    "EMBEDDING_QUERY_INSTRUCTIONS_ENV",
    "EmbeddingDimensionMismatch",
    "EmbeddingModel",
    "EmbeddingModelNotAllowed",
    "EmbeddingModelPolicy",
    "MAX_QUERY_INSTRUCTION_LENGTH",
    "format_embedding_model_id",
    "parse_embedding_model_id",
)


class EmbeddingModelNotAllowed(ValueError):
    """An embedding model is not in the allowlist, or its dimensions are unknown."""

    code = "embedding_model_not_allowed"

    def __init__(self, model_id: str, reason: str = "not_allowed") -> None:
        self.model_id = model_id
        self.reason = reason
        # args are the constructor arguments, so the error survives pickling (DBOS step errors).
        super().__init__(model_id, reason)

    def __str__(self) -> str:
        if self.reason == "unknown_dimensions":
            return (
                f"embedding model '{self.model_id}' is allowed by a wildcard but its dimensions are unknown; "
                f"add it to {ALLOWED_EMBEDDING_MODELS_ENV} as '{self.model_id}=<768|1024|1536>'"
            )
        return (
            f"embedding model '{self.model_id}' is not allowed on this platform "
            f"(not in the {ALLOWED_EMBEDDING_MODELS_ENV} allowlist)"
        )


class EmbeddingDimensionMismatch(ValueError):
    """The provider returned vectors of another length than the allowlist declares."""

    code = "embedding_dimension_mismatch"

    def __init__(self, model_id: str, expected: int, actual: int) -> None:
        self.model_id = model_id
        self.expected = expected
        self.actual = actual
        super().__init__(model_id, expected, actual)

    def __str__(self) -> str:
        return (
            f"embedding model '{self.model_id}' returned {self.actual}-dimensional vectors, "
            f"but {ALLOWED_EMBEDDING_MODELS_ENV} declares {self.expected}"
        )


def parse_embedding_model_id(model_id: str) -> tuple[str, str]:
    """Split `provider:name` on its first `:`. Raises ValueError when malformed."""
    provider, sep, name = model_id.partition(":")
    if not sep or not _PROVIDER_RE.fullmatch(provider) or not _NAME_RE.fullmatch(name) or name == "*":
        raise ValueError(f"invalid embedding model id {model_id!r}: expected 'provider:name'")
    return provider, name


def format_embedding_model_id(provider: str, name: str) -> str:
    return f"{provider}:{name}"


@dataclass(frozen=True)
class EmbeddingModel:
    """One resolved, allowed embedding model."""

    provider: str
    name: str
    dimensions: int
    query_instruction: str | None = None

    @property
    def id(self) -> str:
        return format_embedding_model_id(self.provider, self.name)

    def query_text(self, text: str) -> str:
        """The text to embed for a search query (documents are embedded as-is)."""
        if self.query_instruction is None:
            return text
        return f"Instruct: {self.query_instruction}\nQuery: {text}"

    def check_dimensions(self, actual: int) -> None:
        """Raise EmbeddingDimensionMismatch unless `actual` equals the declared dimensions."""
        if actual != self.dimensions:
            raise EmbeddingDimensionMismatch(self.id, self.dimensions, actual)


def _parse_dims(entry: str, raw: str) -> int:
    try:
        dims = int(raw)
    except ValueError:
        dims = 0
    if str(dims) != raw.strip() or dims not in ALLOWED_EMBEDDING_DIMENSIONS:
        raise ValueError(
            f"invalid {ALLOWED_EMBEDDING_MODELS_ENV} entry {entry!r}: dimensions must be one of "
            f"{', '.join(map(str, ALLOWED_EMBEDDING_DIMENSIONS))}"
        )
    return dims


@dataclass(frozen=True)
class EmbeddingModelPolicy:
    """Allowlist of embedding models with declared dimensions.

    `exact` maps `provider:name` to dimensions; `wildcards` maps a provider to its dimensions or
    None (dimensions from BUILTIN_EMBEDDING_DIMENSIONS, else unknown); `query_instructions` maps
    `provider:name` to an instruction.
    """

    exact: Mapping[str, int]
    wildcards: Mapping[str, int | None]
    query_instructions: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def parse(cls, value: str | None, query_instructions: str | Mapping[str, str] | None = None) -> "EmbeddingModelPolicy":
        """Parse the allowlist (and optional query instructions, a JSON object string or a mapping).

        None or blank -> `test:*`. Raises ValueError on any malformed entry, so a typo fails at
        startup instead of silently denying or allowing a model.
        """
        if value is None or not value.strip():
            value = DEFAULT_ALLOWED_EMBEDDING_MODELS
        exact: dict[str, int] = {}
        wildcards: dict[str, int | None] = {}
        for raw in value.split(","):
            entry = raw.strip()
            if not entry:
                continue
            spec, eq, dims_raw = entry.partition("=")
            provider, sep, name = spec.strip().partition(":")
            if not sep or not _PROVIDER_RE.fullmatch(provider) or not name:
                raise ValueError(
                    f"invalid {ALLOWED_EMBEDDING_MODELS_ENV} entry {entry!r}: "
                    "expected 'provider:name=<dims>' or 'provider:*'"
                )
            dims = _parse_dims(entry, dims_raw) if eq else None
            if name == "*":
                if provider in wildcards:
                    raise ValueError(f"duplicate {ALLOWED_EMBEDDING_MODELS_ENV} entry {entry!r}")
                wildcards[provider] = dims
                continue
            if not _NAME_RE.fullmatch(name) or "*" in name:
                raise ValueError(
                    f"invalid {ALLOWED_EMBEDDING_MODELS_ENV} entry {entry!r}: the model name may not contain "
                    "whitespace, ',', '=' or a partial '*' wildcard"
                )
            if dims is None:
                raise ValueError(
                    f"invalid {ALLOWED_EMBEDDING_MODELS_ENV} entry {entry!r}: an exact model needs its "
                    "dimensions, e.g. 'provider:name=1536'"
                )
            model_id = format_embedding_model_id(provider, name)
            if model_id in exact:
                raise ValueError(f"duplicate {ALLOWED_EMBEDDING_MODELS_ENV} entry {entry!r}")
            exact[model_id] = dims
        if not exact and not wildcards:
            raise ValueError(f"{ALLOWED_EMBEDDING_MODELS_ENV} has no entries: {value!r}")
        policy = cls(exact, wildcards)
        instructions = _parse_query_instructions(query_instructions)
        for model_id in instructions:
            provider, name = parse_embedding_model_id(model_id)
            if not policy.allows(provider, name):
                raise ValueError(
                    f"{EMBEDDING_QUERY_INSTRUCTIONS_ENV} names '{model_id}', which "
                    f"{ALLOWED_EMBEDDING_MODELS_ENV} does not allow"
                )
        return cls(exact, wildcards, instructions)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "EmbeddingModelPolicy":
        env = os.environ if environ is None else environ
        return cls.parse(env.get(ALLOWED_EMBEDDING_MODELS_ENV), env.get(EMBEDDING_QUERY_INSTRUCTIONS_ENV))

    def allows(self, provider: str, name: str) -> bool:
        if not provider or not name:
            return False
        return format_embedding_model_id(provider, name) in self.exact or provider in self.wildcards

    def dimensions(self, provider: str, name: str) -> int | None:
        """Declared dimensions of an allowed model; None if not allowed or unknown."""
        model_id = format_embedding_model_id(provider, name)
        if model_id in self.exact:
            return self.exact[model_id]
        if provider in self.wildcards:
            dims = self.wildcards[provider]
            return dims if dims is not None else BUILTIN_EMBEDDING_DIMENSIONS.get(model_id)
        return None

    def resolve(self, model_id: str) -> EmbeddingModel:
        """The allowed model with its dimensions and query instruction.

        Raises EmbeddingModelNotAllowed when the id is malformed, not allowed, or allowed by a
        wildcard without known dimensions (a knowledge base cannot be indexed without them).
        """
        try:
            provider, name = parse_embedding_model_id(model_id)
        except ValueError:
            raise EmbeddingModelNotAllowed(model_id) from None
        if not self.allows(provider, name):
            raise EmbeddingModelNotAllowed(model_id)
        dims = self.dimensions(provider, name)
        if dims is None:
            raise EmbeddingModelNotAllowed(model_id, "unknown_dimensions")
        return EmbeddingModel(provider, name, dims, self.query_instructions.get(model_id))

    def check(self, model_id: str) -> None:
        """Raise EmbeddingModelNotAllowed unless the model resolves."""
        self.resolve(model_id)

    def listed(self) -> tuple[EmbeddingModel, ...]:
        """Every model the policy names concretely (exact entries plus built-in wildcard matches),
        sorted by id: what `GET /embedding-models` can offer."""
        ids = set(self.exact)
        ids.update(m for m in BUILTIN_EMBEDDING_DIMENSIONS if m.partition(":")[0] in self.wildcards)
        return tuple(self.resolve(model_id) for model_id in sorted(ids))

    def __str__(self) -> str:
        parts = [f"{model_id}={dims}" for model_id, dims in self.exact.items()]
        parts += [f"{p}:*" if d is None else f"{p}:*={d}" for p, d in self.wildcards.items()]
        return ",".join(parts)


def _parse_query_instructions(value: str | Mapping[str, str] | None) -> dict[str, str]:
    if value is None or (isinstance(value, str) and not value.strip()):
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{EMBEDDING_QUERY_INSTRUCTIONS_ENV} is not valid JSON: {exc.msg}") from None
    if not isinstance(value, Mapping):
        raise ValueError(f"{EMBEDDING_QUERY_INSTRUCTIONS_ENV} must be a JSON object of model id to instruction")
    out: dict[str, str] = {}
    for model_id, instruction in value.items():
        if not isinstance(model_id, str) or not isinstance(instruction, str):
            raise ValueError(f"{EMBEDDING_QUERY_INSTRUCTIONS_ENV} keys and values must be strings")
        parse_embedding_model_id(model_id)
        instruction = instruction.strip()
        if not instruction or len(instruction) > MAX_QUERY_INSTRUCTION_LENGTH:
            raise ValueError(
                f"{EMBEDDING_QUERY_INSTRUCTIONS_ENV}['{model_id}'] must be 1 to "
                f"{MAX_QUERY_INSTRUCTION_LENGTH} characters"
            )
        out[model_id] = instruction
    return out
