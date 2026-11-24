"""Probes for the capability registry: what is true right now (system-aware Architect, phase A).

The declared inventory (eap_schemas.capabilities) says what the platform has. A probe says what is on, off or
down at this moment, and lists what only exists at run time (models, a tenant's MCP servers and their tools).
The one rule: **a probe may change an entry's `status` / `status_reason` and add entries of the dynamic kinds
(`model`, `mcp_server`, `mcp_tool`); it can never add or remove a built-in entry.** `merge` enforces it, so a tool
missing from the declarations is a failing test, never something a probe quietly papers over.

`ProbeRegistry` runs the registered probes (in parallel, each bounded), collects their results and reports a
probe that failed in `degraded` instead of failing the listing. Probe results that are expensive (an MCP
server's `tools/list`, a provider's model list) are cached in a `TtlCache` for 60 s (the registry's TTL); the
tenant's server list is read every time, so adding a server shows at once.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import anyio

from eap_schemas.architect import BUILTIN_ONLY_TOOL_PREFIXES
from eap_schemas.capabilities import DYNAMIC_KINDS, CapabilityEntry

log = logging.getLogger("eap_api.capabilities")

PROBE_TIMEOUT_S = 15.0
CACHE_TTL_S = 60.0
MAX_MODELS_PER_PROVIDER = 100
MAX_MCP_TOOLS_PER_SERVER = 200

Override = tuple[str, str | None]  # (status, reason)


@dataclass(frozen=True)
class ProbeContext:
    tenant_id: str
    declared: Mapping[str, CapabilityEntry]


@dataclass
class ProbeResult:
    overrides: dict[str, Override] = field(default_factory=dict)  # entry id -> (status, reason)
    extra: tuple[CapabilityEntry, ...] = ()  # dynamic entries


Probe = Callable[[ProbeContext], Awaitable[ProbeResult]]


class TtlCache:
    """A small thread-safe TTL map (in-process; the registry's dynamic part, section 2.5 of the design)."""

    def __init__(self, ttl_s: float = CACHE_TTL_S, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl, self._clock = ttl_s, clock
        self._items: dict[Any, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: Any) -> Any | None:
        with self._lock:
            hit = self._items.get(key)
            if hit is None or self._clock() - hit[0] >= self._ttl:
                self._items.pop(key, None)
                return None
            return hit[1]

    def put(self, key: Any, value: Any) -> None:
        with self._lock:
            self._items[key] = (self._clock(), value)

    def invalidate(self, tenant_id: str | None = None) -> None:
        """Forget everything, or every key that starts with `tenant_id` (keys are `(tenant, ...)` tuples)."""
        with self._lock:
            if tenant_id is None:
                self._items.clear()
            else:
                for key in [k for k in self._items if isinstance(k, tuple) and k and k[0] == tenant_id]:
                    del self._items[key]


# Shared by every request in the process; a test passes its own.
SHARED_CACHE = TtlCache()


@dataclass(frozen=True)
class ProbeOutcome:
    overrides: dict[str, Override]
    extra: tuple[CapabilityEntry, ...]
    degraded: tuple[str, ...]  # names of the probes that failed


class ProbeRegistry:
    """The named probes of one registry service."""

    def __init__(self, cache: TtlCache | None = None) -> None:
        self.cache = cache if cache is not None else SHARED_CACHE
        self._probes: dict[str, Probe] = {}

    def register(self, name: str, probe: Probe) -> None:
        self._probes[name] = probe

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._probes)

    def invalidate(self, tenant_id: str | None = None) -> None:
        self.cache.invalidate(tenant_id)

    async def run(self, ctx: ProbeContext) -> ProbeOutcome:
        """Every probe, concurrently; a probe that raises or times out is `degraded`, its answer dropped."""
        results: dict[str, ProbeResult | None] = {}

        async def one(name: str, probe: Probe) -> None:
            try:
                with anyio.fail_after(PROBE_TIMEOUT_S):
                    results[name] = await probe(ctx)
            except BaseException as exc:  # noqa: BLE001 - a probe must never fail the listing
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                log.warning("capability probe %s failed: %s", name, type(exc).__name__)
                results[name] = None

        async with anyio.create_task_group() as tg:
            for name, probe in self._probes.items():
                tg.start_soon(one, name, probe)
        overrides: dict[str, Override] = {}
        extra: list[CapabilityEntry] = []
        degraded: list[str] = []
        for name in self._probes:  # registration order: a later probe wins an override
            result = results.get(name)
            if result is None:
                degraded.append(name)
                continue
            overrides.update(result.overrides)
            extra.extend(result.extra)
        return ProbeOutcome(overrides, tuple(extra), tuple(degraded))


def merge(declared: Sequence[CapabilityEntry], outcome: ProbeOutcome) -> tuple[CapabilityEntry, ...]:
    """The registry: declared entries with probe statuses applied, plus the dynamic entries probes added.

    A probe's override of an id that is not declared and not one of its own extras is ignored; an extra whose
    kind is not dynamic, or whose id is declared, is ignored. Sorted by id."""
    merged: dict[str, CapabilityEntry] = {e.id: e for e in declared}
    extras: dict[str, CapabilityEntry] = {}
    for entry in outcome.extra:
        if entry.kind in DYNAMIC_KINDS and entry.id not in merged:
            extras[entry.id] = entry
    merged.update(extras)
    for entry_id, (status, reason) in outcome.overrides.items():
        current = merged.get(entry_id)
        if current is None or current.status == "designed_not_built":
            continue  # a built-in cannot be created, and a design cannot be switched on, by a probe
        merged[entry_id] = current.model_copy(update={"status": status, "status_reason": reason})
    return tuple(sorted(merged.values(), key=lambda e: e.id))


# ------------------------------------------------------------------------------------------------ probes

def tool_allowlist_probe(allowed: Callable[[], frozenset[str] | None]) -> Probe:
    """A tool outside the deployment's `EAP_ALLOWED_TOOLS` is `disabled` (the Architect's own and the platform's
    tools are always allowed to the built-in Architect)."""

    async def probe(ctx: ProbeContext) -> ProbeResult:
        permitted = allowed()
        if permitted is None:
            return ProbeResult()
        overrides: dict[str, Override] = {}
        for entry in ctx.declared.values():
            if entry.kind != "tool" or entry.status != "available":
                continue
            tool_id = entry.id.removeprefix("tool.")
            if tool_id.startswith(BUILTIN_ONLY_TOOL_PREFIXES) or tool_id in permitted:
                continue
            overrides[entry.id] = ("disabled", "not in this deployment's EAP_ALLOWED_TOOLS")
        return ProbeResult(overrides)

    return probe


ProviderStatuses = Callable[[], list[Any]]  # eap_api.model_discovery.ProviderStatus-like: id, configured, status, error
ModelLister = Callable[[], list[dict[str, Any]]]  # eap_api.services.model_catalog.model_items-like
PolicyProviders = Callable[[], frozenset[str]]  # providers the model allowlist names


def provider_probe(statuses: ProviderStatuses, models: ModelLister, allowed_providers: PolicyProviders) -> Probe:
    """One `model.<provider>` entry per provider the allowlist names, `disabled` with a reason when it has no
    credentials or does not answer, and the concrete models of the allowlist under it (inheriting its status)."""

    def build() -> ProbeResult:
        named = allowed_providers()
        by_id = {s.id: s for s in statuses()}
        entries: list[CapabilityEntry] = []
        state: dict[str, tuple[str, str | None]] = {}
        for provider in sorted(named):
            known = by_id.get(provider)
            if known is None:  # `test` (scripted) and any provider without discovery: nothing to check
                state[provider] = ("available", None)
            elif not known.configured:
                state[provider] = ("disabled", "the provider has no credentials configured")
            elif known.status != "ok":
                state[provider] = ("disabled", f"the provider does not answer ({known.error or known.status})")
            else:
                state[provider] = ("available", None)
        listed: dict[str, list[dict[str, Any]]] = {p: [] for p in named}
        for item in models():
            if not item.get("wildcard") and item.get("provider") in listed:
                listed[item["provider"]].append(item)
        for provider in sorted(named):
            status, reason = state[provider]
            rows = sorted(listed[provider], key=lambda m: m["id"])
            entries.append(CapabilityEntry(
                id=f"model.{provider}", kind="model", title=f"Model provider {provider}",
                description=f"The {provider} model provider and the {len(rows)} of its models the allowlist lists.",
                status=status, status_reason=reason, scope="agent",
                limits={"models_listed": min(len(rows), MAX_MODELS_PER_PROVIDER), "models_total": len(rows)},
            ))
            for row in rows[:MAX_MODELS_PER_PROVIDER]:
                entries.append(CapabilityEntry(
                    id=f"model.{row['id']}", kind="model", title=row["id"], description=f"Model {row['id']} (provider {provider}).",
                    status=status, status_reason=reason, needs=(f"model.{provider}",),
                ))
        return ProbeResult(extra=tuple(entries))

    async def probe(ctx: ProbeContext) -> ProbeResult:
        del ctx
        return await anyio.to_thread.run_sync(build)

    return probe


McpServers = Callable[[str], list[dict[str, Any]]]  # tenant -> server rows (McpServerService.list)
McpToolLister = Callable[[str, str], Awaitable[dict[str, Any]]]  # (tenant, server id) -> {status, tools, error_code}
STARTABLE_IDLE = ("idle", "stopped")  # a managed runtime the first call starts: not started by a read


def mcp_probe(servers: McpServers, lister: McpToolLister, cache: TtlCache) -> Probe:
    """The tenant's MCP servers as `mcp_server.<name>` and, for a server that answers a `tools/list`, one
    `mcp_tool.<server>/<tool>` each (every MCP tool asks a person). A server that is off, failed or silent
    has no tool entries. A read never starts a stopped managed runtime: it is listed `available` with no tools
    (it starts on its first call)."""

    async def probe(ctx: ProbeContext) -> ProbeResult:
        rows = await anyio.to_thread.run_sync(servers, ctx.tenant_id)
        entries: list[CapabilityEntry] = []
        for row in rows:
            name = str(row["name"])
            sid = str(row["id"])
            server_id = f"mcp_server.{name}"
            state = row.get("runtime_state")
            if not row.get("enabled", True):
                entries.append(_server(server_id, name, "disabled", "the server is switched off"))
                continue
            if row.get("runtime") == "managed" and state in STARTABLE_IDLE:
                entries.append(_server(server_id, name, "available", f"the managed runtime is {state}; it starts on first use, so its tools are not listed"))
                continue
            if row.get("runtime") == "managed" and state not in ("running",):
                entries.append(_server(server_id, name, "disabled", f"the managed runtime is {state or 'not running'}"))
                continue
            key = (ctx.tenant_id, "mcp_tools", sid, row.get("revision"))
            answer = cache.get(key)
            if answer is None:
                try:
                    answer = await lister(ctx.tenant_id, sid)
                except Exception as exc:  # noqa: BLE001 - a silent server is a status, not an error
                    log.warning("capability mcp tools/list failed: %s", type(exc).__name__)
                    answer = {"status": "error", "error_code": "connection_failed", "tools": []}
                cache.put(key, answer)
            if answer.get("status") != "ok":
                entries.append(_server(server_id, name, "disabled", f"the server did not answer ({answer.get('error_code') or 'error'})"))
                continue
            tools = sorted({str(t["name"]) for t in answer.get("tools", []) if isinstance(t, dict) and t.get("name")})
            entries.append(_server(server_id, name, "available", None, tools=len(tools)))
            for tool in tools[:MAX_MCP_TOOLS_PER_SERVER]:
                entries.append(CapabilityEntry(
                    id=f"mcp_tool.{name}/{tool}", kind="mcp_tool", title=f"{name}: {tool}",
                    description=f"The tool {tool} of the MCP server {name}.", requires_approval=True,
                    needs=(server_id,), writable_by_architect=True,
                ))
        return ProbeResult(extra=tuple(entries))

    return probe


def _server(entry_id: str, name: str, status: str, reason: str | None, *, tools: int | None = None) -> CapabilityEntry:
    return CapabilityEntry(
        id=entry_id, kind="mcp_server", title=f"MCP server {name}", description=f"The tenant's MCP server {name}.",
        status=status, status_reason=reason,  # type: ignore[arg-type]
        limits={} if tools is None else {"tools": tools},
    )


__all__ = [
    "SHARED_CACHE",
    "Probe",
    "ProbeContext",
    "ProbeOutcome",
    "ProbeRegistry",
    "ProbeResult",
    "TtlCache",
    "mcp_probe",
    "merge",
    "provider_probe",
    "tool_allowlist_probe",
]
