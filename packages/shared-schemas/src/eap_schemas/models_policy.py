"""Platform model allowlist, shared by the control plane (publish) and the worker (before any model call).

Configured with `EAP_ALLOWED_MODELS`: a comma-separated list of `provider:name` entries, where
`name` may be `*` to allow every model of that provider, e.g.

    EAP_ALLOWED_MODELS="test:*,litellm:qwen3.8-thinking"

Unset (or blank) means `test:*` only: no real model is reachable until an operator opts in.
Matching is exact and case-sensitive; the model id is split on its first `:`, as the worker does.

Dependency-free on purpose (stdlib only) so eap_api and eap_worker can both import it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

ALLOWED_MODELS_ENV = "EAP_ALLOWED_MODELS"
DEFAULT_ALLOWED_MODELS = "test:*"

# What Anthropic offers when its own `GET /v1/models` cannot be read (a small, current set; the listing is the
# truth when it answers). The API's discovery and the worker's Architect allowlist both use it as the fallback.
ANTHROPIC_FALLBACK: tuple[str, ...] = ("claude-haiku-5-5", "claude-opus-5-5", "claude-sonnet-5-5")

__all__ = (
    "ANTHROPIC_FALLBACK",
    "ALLOWED_MODELS_ENV",
    "DEFAULT_ALLOWED_MODELS",
    "ModelNotAllowed",
    "ModelPolicy",
)


class ModelNotAllowed(ValueError):
    """A spec names a model that the platform allowlist does not permit."""

    def __init__(self, provider: str, name: str) -> None:
        self.provider = provider
        self.name = name
        # args are the constructor arguments, so the error survives pickling (DBOS step errors).
        super().__init__(provider, name)

    def __str__(self) -> str:
        return (
            f"model '{self.provider}:{self.name}' is not allowed on this platform "
            f"(not in the {ALLOWED_MODELS_ENV} allowlist)"
        )


@dataclass(frozen=True)
class ModelPolicy:
    """An allowlist of `provider:name` / `provider:*` entries."""

    entries: tuple[str, ...]

    @classmethod
    def parse(cls, value: str | None) -> "ModelPolicy":
        """Parse a comma-separated allowlist. None or blank -> the default (`test:*`).

        Raises ValueError for a malformed entry, so a typo fails at startup/publish instead of
        silently denying (or allowing) models.
        """
        if value is None or not value.strip():
            value = DEFAULT_ALLOWED_MODELS
        entries: list[str] = []
        for raw in value.split(","):
            entry = raw.strip()
            if not entry:
                continue
            provider, sep, name = entry.partition(":")
            if not sep or not provider or not name or provider == "*" or "*" in provider:
                raise ValueError(
                    f"invalid {ALLOWED_MODELS_ENV} entry {entry!r}: expected 'provider:name' or 'provider:*'"
                )
            if "*" in name and name != "*":
                raise ValueError(
                    f"invalid {ALLOWED_MODELS_ENV} entry {entry!r}: only a whole-name '*' wildcard is supported"
                )
            entries.append(f"{provider}:{name}")
        return cls(tuple(entries))

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "ModelPolicy":
        env = os.environ if environ is None else environ
        return cls.parse(env.get(ALLOWED_MODELS_ENV))

    def allows(self, provider: str, name: str) -> bool:
        if not provider or not name:
            return False
        return f"{provider}:{name}" in self.entries or f"{provider}:*" in self.entries

    def check(self, provider: str, name: str) -> None:
        """Raise ModelNotAllowed unless `provider:name` is allowed."""
        if not self.allows(provider, name):
            raise ModelNotAllowed(provider, name)

    def __str__(self) -> str:
        return ",".join(self.entries)
