"""Which adapter serves which (kind, provider). Adapters are plug-ins: a distribution declares an entry
point in the group `eap.kb_sources.adapters` whose target is a module (importing it registers) or a
zero-argument callable that calls `register_adapter(kind, provider, factory)`. The GitHub adapter is
built in. Discovery runs once, lazily, on first registry use; a plug-in that fails to load is logged
(with its distribution name) and skipped, a duplicate (kind, provider) raises `DuplicateAdapterError`.
The worker calls `adapter_for` once per segment, so a factory may return a fresh adapter (its token
cache lives for that segment) or a shared one (tests). Unknown: SourceError `source_adapter_not_found`."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.metadata import entry_points

from eap_kb_sources.adapter import SourceAdapter
from eap_kb_sources.errors import SOURCE_ADAPTER_NOT_FOUND, SourceError

log = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "eap.kb_sources.adapters"
BUILTIN_DISTRIBUTION = "eap-kb-sources"
LOCAL = "local"  # registered directly in code (tests), not by a distribution


class DuplicateAdapterError(RuntimeError):
    """Two distributions registered the same (kind, provider)."""


@dataclass(frozen=True)
class AdapterInfo:
    kind: str
    provider: str
    distribution: str  # the distribution that registered it; "local" when registered directly in code


_FACTORIES: dict[tuple[str, str], Callable[[], SourceAdapter]] = {}
_ORIGINS: dict[tuple[str, str], str] = {}
_discovered = False
_discovery_error: DuplicateAdapterError | None = None
_loading: str | None = None  # the distribution whose entry point is being loaded right now


def register_adapter(kind: str, provider: str, factory: Callable[[], SourceAdapter]) -> None:
    key = (kind, provider)
    if _loading is None:
        _FACTORIES[key] = factory  # a direct registration (tests) replaces whatever was there
        _ORIGINS[key] = LOCAL
        return
    existing = _ORIGINS.get(key)
    if existing == LOCAL:
        return  # a direct registration made before discovery wins
    if existing is not None:
        raise DuplicateAdapterError(
            f"kb source adapter ({kind}, {provider}) is registered by both {existing!r} and {_loading!r}"
        )
    _FACTORIES[key] = factory
    _ORIGINS[key] = _loading


@contextmanager
def _as_distribution(name: str) -> Iterator[None]:
    global _loading
    _loading = name
    try:
        yield
    finally:
        _loading = None


def _discover() -> None:
    global _discovered, _discovery_error
    from eap_kb_sources.providers import register_builtin_adapters

    try:
        with _as_distribution(BUILTIN_DISTRIBUTION):
            register_builtin_adapters()
        for ep in sorted(entry_points(group=ENTRY_POINT_GROUP), key=lambda e: (e.dist.name if e.dist else "", e.name)):
            dist = ep.dist.name if ep.dist else "unknown"
            try:
                with _as_distribution(dist):
                    target = ep.load()
                    if callable(target):
                        target()
            except DuplicateAdapterError:
                raise
            except Exception as exc:  # a plug-in must never crash the worker or the API
                log.warning("kb source plug-in %s (entry point %s) failed to load and is skipped: %s", dist, ep.name, exc)
    except DuplicateAdapterError as exc:
        _discovery_error = exc
    _discovered = True


def _ensure_discovered() -> None:
    if not _discovered:
        _discover()
    if _discovery_error is not None:
        raise _discovery_error


def adapter_for(kind: str, provider: str) -> SourceAdapter:
    _ensure_discovered()
    factory = _FACTORIES.get((kind, provider))
    if factory is None:
        raise SourceError(SOURCE_ADAPTER_NOT_FOUND, "no adapter for this source's kind and provider")
    return factory()


def registered() -> list[tuple[str, str]]:
    _ensure_discovered()
    return sorted(_FACTORIES)


def list_adapters() -> list[AdapterInfo]:
    """Every registered adapter with the distribution it came from."""
    _ensure_discovered()
    return [AdapterInfo(kind, provider, _ORIGINS[(kind, provider)]) for kind, provider in sorted(_FACTORIES)]


def adapter_for_provider(provider: str) -> SourceAdapter:
    """The adapter of a provider whatever its source kind (a connection names its provider, not a kind:
    `check_connection` needs only the provider). Unknown: SourceError `source_adapter_not_found`."""
    _ensure_discovered()
    for (_kind, name), factory in sorted(_FACTORIES.items()):
        if name == provider:
            return factory()
    raise SourceError(SOURCE_ADAPTER_NOT_FOUND, "no adapter for this connection's provider")


def _snapshot() -> tuple:
    return dict(_FACTORIES), dict(_ORIGINS), _discovered, _discovery_error


def _restore(state: tuple) -> None:
    global _discovered, _discovery_error
    _FACTORIES.clear(), _ORIGINS.clear()
    _FACTORIES.update(state[0]), _ORIGINS.update(state[1])
    _discovered, _discovery_error = state[2], state[3]


def _reset() -> None:
    """Forget every registration and the discovery state (tests)."""
    _restore(({}, {}, False, None))
