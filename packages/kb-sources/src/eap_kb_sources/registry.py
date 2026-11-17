"""Which adapter serves which (kind, provider). Provider packages register a zero-argument factory;
the worker calls `adapter_for` once per segment, so a factory may return a fresh adapter (its token
cache lives for that segment) or a shared one (tests). Unknown: SourceError `source_adapter_not_found`."""

from __future__ import annotations

from collections.abc import Callable

from eap_kb_sources.adapter import SourceAdapter
from eap_kb_sources.errors import SOURCE_ADAPTER_NOT_FOUND, SourceError

_FACTORIES: dict[tuple[str, str], Callable[[], SourceAdapter]] = {}


def register_adapter(kind: str, provider: str, factory: Callable[[], SourceAdapter]) -> None:
    _FACTORIES[(kind, provider)] = factory


def adapter_for(kind: str, provider: str) -> SourceAdapter:
    factory = _FACTORIES.get((kind, provider))
    if factory is None:
        raise SourceError(SOURCE_ADAPTER_NOT_FOUND, "no adapter for this source's kind and provider")
    return factory()


def registered() -> list[tuple[str, str]]:
    return sorted(_FACTORIES)
