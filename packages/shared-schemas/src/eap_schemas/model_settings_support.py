"""Which model settings a provider cannot accept (the Pydantic AI request mapping of each provider).

Anthropic: current models reject sampling parameters with a 400 (temperature, top_p, top_k), and
the API has no frequency_penalty, presence_penalty or seed. OpenAI-compatible APIs have no top_k.
Every other provider (the gateway, the scripted test models) passes all of them. The API refuses a
spec or a run option that sets one of these on such a provider (422), instead of failing on every run.
"""

from __future__ import annotations

from eap_schemas import ModelSettings

# Settings checked here, in the order the first refusal is named.
CHECKED_SETTINGS = ("temperature", "top_p", "top_k", "frequency_penalty", "presence_penalty", "seed")

_UNSUPPORTED: dict[str, frozenset[str]] = {
    "anthropic": frozenset(CHECKED_SETTINGS),
    "openai": frozenset({"top_k"}),
}


def unsupported_settings(provider: str, settings: ModelSettings) -> list[str]:
    """The names of the settings that are set on `settings` and that `provider` does not accept."""
    refused = _UNSUPPORTED.get(provider, frozenset())
    return [name for name in CHECKED_SETTINGS if name in refused and getattr(settings, name) is not None]


def merge_model_settings(base: ModelSettings, override: ModelSettings | None) -> ModelSettings:
    """`base` with every setting that is set on `override` replacing it (a per-run override)."""
    if override is None:
        return base
    return ModelSettings(**{**base.set_values(), **override.set_values()})


__all__ = ["CHECKED_SETTINGS", "merge_model_settings", "unsupported_settings"]
