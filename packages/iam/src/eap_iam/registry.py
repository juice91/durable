"""Plug-in discovery for the three IAM entry-point groups (the pattern of `eap_kb_sources.registry`).

An entry point's name is the plug-in's name; its target is a callable `create(ctx: PluginContext) -> instance`.
`register_local` adds a factory directly in code (tests, fakes); it wins over an installed distribution of the same
name. `EAP_IAM_IGNORE_PLUGINS` (comma list) hides installed plug-ins, which is how the suite runs with them absent."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import Any

from eap_iam.types import IamConfigError

log = logging.getLogger(__name__)

Factory = Callable[["PluginContext"], Any]
_LOCAL: dict[str, dict[str, Factory]] = {}


@dataclass(frozen=True)
class PluginContext:
    """What a plug-in factory receives. `engine` is the platform's SQLAlchemy engine (typed Any: core imports no
    database library); `env` is the process environment (plug-ins read their own `EAP_IAM_*` / `EAP_<NAME>_*`);
    `membership` is the configured membership source (None without one), built before the identity providers so a
    provider that must know a person's current roles (personal API keys) asks it without importing another plug-in."""

    engine: Any = None
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    membership: Any = None


def register_local(group: str, name: str, factory: Factory) -> None:
    _LOCAL.setdefault(group, {})[name] = factory


def clear_local() -> None:
    _LOCAL.clear()


def _ignored(env: Mapping[str, str]) -> set[str]:
    return {n.strip() for n in env.get("EAP_IAM_IGNORE_PLUGINS", "").split(",") if n.strip()}


def installed(group: str, env: Mapping[str, str] | None = None) -> dict[str, Factory]:
    """Name -> factory of every plug-in registered for `group` (distributions, then local registrations)."""
    env = os.environ if env is None else env
    hidden = _ignored(env)
    found: dict[str, Factory] = {}
    origins: dict[str, str] = {}
    for ep in sorted(entry_points(group=group), key=lambda e: (e.dist.name if e.dist else "", e.name)):
        dist = ep.dist.name if ep.dist else "unknown"
        if ep.name in hidden:
            continue
        if ep.name in found:
            raise IamConfigError(f"IAM plug-in {ep.name!r} ({group}) is registered by both {origins[ep.name]!r} and {dist!r}")
        try:
            found[ep.name] = ep.load()
        except Exception as exc:  # a broken plug-in must not take the API down silently: it is simply absent
            log.warning("IAM plug-in %s (%s) failed to load: %s", ep.name, dist, type(exc).__name__)
            continue
        origins[ep.name] = dist
    found.update(_LOCAL.get(group, {}))
    return found


def parse_names(raw: str | None) -> list[str]:
    return [part.strip() for part in (raw or "").split(",") if part.strip()]
