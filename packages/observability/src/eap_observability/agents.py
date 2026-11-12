"""Pydantic AI instrumentation (worker only).

Kept out of the package root so the control plane, which imports `eap_observability` but must
never import pydantic_ai, does not pull it in.
"""

from __future__ import annotations

from opentelemetry.sdk.trace import TracerProvider
from pydantic_ai import Agent
from pydantic_ai.models.instrumented import InstrumentationSettings

from eap_observability import tracer_provider as _configured_provider


def instrument_pydantic_ai(*, tracer_provider: TracerProvider | None = None, include_content: bool = False) -> bool:
    """Spans for every agent run, model request and tool call; prompts and outputs left out.

    Uses the provider from setup_telemetry unless one is given. Returns False and instruments
    nothing when telemetry is off.
    """
    provider = tracer_provider or _configured_provider()
    if provider is None:
        return False
    Agent.instrument_all(InstrumentationSettings(tracer_provider=provider, include_content=include_content))
    return True


def uninstrument_pydantic_ai() -> None:
    Agent.instrument_all(False)


__all__ = ["instrument_pydantic_ai", "uninstrument_pydantic_ai"]
