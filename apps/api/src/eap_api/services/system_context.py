"""The system context for one principal (system-aware Architect, phase C): the capability, tenant and operational
layers the built-in Architect's run reads at its start, composed in ONE place so Studio, `system.context` (MCP) and
the worker's `eap.load_system_context` step see what the same principal may see.

The service composes; it renders nothing (the pure renderer is `eap_schemas.system_context.render_block`, run by the
worker, which also adds the manifest's Platform layer). It is built from collaborators: the capability registry, the
tenant reads (names and ids only), the content-addressed snapshot store and, for the tenant layer, the IAM policy
engine through `AuthzService`: ids are filtered per resource KIND once (`filter_ids`), never per row, so a resource
the principal cannot read is absent from the snapshot. Today the policy engine is `openaccess` (everything is
visible); the call exists so IAM P2 filters it later without a change here.

A layer that fails to build is omitted and named in `degraded`; the run does not fail. Nothing here holds a secret:
secrets are NAMES, tenant text is limited to names, and the failure lines are bounded and scrubbed by the renderer.

The context is not a security boundary: the API still validates and authorises every call.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from eap_iam import All, Except, IdFilter, Only
from eap_iam import Principal as IamPrincipal
from eap_observability.redaction import log_failure
from eap_schemas.capabilities import CapabilityEntry, canonical_json
from eap_schemas.system_context import (
    FAILURES_CAP,
    LIST_CAP,
    SECRETS_CAP,
    CapabilityLine,
    ContextLayers,
    FailureLine,
    Policies,
    PrincipalSummary,
    ResourceLine,
    TenantResources,
    safe_text,
)
from eap_schemas.tools import ALWAYS_GATED_TOOL_IDS

from eap_api.services.authz import AuthzService
from eap_api.services.capabilities import CapabilityRegistryService, Viewer

log = logging.getLogger("eap_api.system_context")

CANDIDATES = 200  # ids fetched per kind before the principal's filter and the cap
KIND_OF = {
    "agents": "agent", "workflows": "workflow", "knowledge_bases": "knowledge_base", "connections": "connection",
    "schedules": "schedule", "secrets": "secret",
}


@dataclass(frozen=True)
class TenantReads:
    """The tenant reads the layer needs, each a bounded, tenant-scoped, blocking call (run in a thread). Tests
    replace one to make a layer fail."""

    agents: Callable[[str], dict[str, Any]]  # {total, items: [{id, name}]}, most recently changed first
    workflows: Callable[[str], dict[str, Any]]
    knowledge_bases: Callable[[str], dict[str, Any]]  # {total, items: [{id, name, ...}]}
    schedules: Callable[[str], dict[str, Any]]  # {items: [{id, name, ...}]}
    connections: Callable[[str], dict[str, Any]]  # {items: [{id, name, ...}]}
    secret_names: Callable[[str], list[str]]
    failures: Callable[[str], dict[str, Any]]  # {items: [{name, error, finished_at, ...}]}, newest first


def snapshot_body(entries: Sequence[CapabilityEntry]) -> dict[str, Any]:
    """What `capability_snapshots` stores: `{id: {s: status, h: short content hash}}`. No description, schema or
    tenant text, so a snapshot can never hold a secret."""
    return {
        e.id: {"s": e.status, "h": hashlib.sha256(canonical_json(e.model_dump(mode="json")).encode()).hexdigest()[:12]}
        for e in sorted(entries, key=lambda e: e.id)
    }


def _apply(found: IdFilter, ids: list[str]) -> list[str]:
    if isinstance(found, Only):
        allowed = set(found.ids)
        return [i for i in ids if i in allowed]
    if isinstance(found, Except):
        denied = set(found.ids)
        return [i for i in ids if i not in denied]
    return ids


class SystemContextService:
    def __init__(
        self,
        registry: CapabilityRegistryService,
        reads: TenantReads,
        authz: AuthzService | None = None,
        snapshots: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self._registry = registry
        self._reads = reads
        self._authz = authz
        self._snapshots = snapshots

    # ------------------------------------------------------------------------------------------- the layers

    @staticmethod
    def principal_of(viewer: Viewer) -> IamPrincipal:
        """The IAM principal of a viewer. Until the run token carries `requested_by` (IAM P0 for the Architect) the
        Architect's own run is the principal; `kind="run"` is what the policy engine expects for it."""
        kind = {"architect_run": "run", "api_key": "api_key"}.get(viewer.kind, "user")
        return IamPrincipal(tenant_id=viewer.tenant_id, kind=kind, subject=viewer.subject or "unknown", provider="system-context")  # type: ignore[arg-type]

    async def _visible(self, viewer: Viewer, kind: str, ids: list[str]) -> list[str]:
        """The ids the viewer may read, from the policy engine, once per kind (never per row)."""
        if self._authz is None or not ids:
            return ids
        found = await self._authz.filter_ids(self.principal_of(viewer), "read", KIND_OF[kind], ids)
        return _apply(found, ids)

    async def _resources(self, viewer: Viewer, kind: str, answer: dict[str, Any]) -> tuple[tuple[ResourceLine, ...], int]:
        items = [i for i in answer.get("items", ()) if isinstance(i, dict) and i.get("id") is not None]
        visible = set(await self._visible(viewer, kind, [str(i["id"]) for i in items]))
        kept = [i for i in items if str(i["id"]) in visible]
        reported = int(answer.get("total", len(items)))
        total = reported if len(kept) == len(items) and reported >= len(items) else len(kept)
        lines = tuple(
            ResourceLine(id=safe_text(i["id"], 60), name=safe_text(i.get("name") or i["id"])) for i in kept[:LIST_CAP]
        )
        return lines, total

    async def _tenant(self, viewer: Viewer) -> TenantResources:
        t = viewer.tenant_id

        async def read(fn: Callable[[str], Any]) -> Any:
            return await asyncio.to_thread(fn, t)

        agents, workflows, kbs, schedules, connections, secrets = await asyncio.gather(
            read(self._reads.agents), read(self._reads.workflows), read(self._reads.knowledge_bases),
            read(self._reads.schedules), read(self._reads.connections), read(self._reads.secret_names),
        )
        counts: dict[str, int] = {}
        groups: dict[str, tuple[ResourceLine, ...]] = {}
        for kind, answer in (
            ("agents", agents), ("workflows", workflows), ("knowledge_bases", kbs), ("schedules", schedules),
            ("connections", connections),
        ):
            groups[kind], counts[kind] = await self._resources(viewer, kind, answer)
        names = sorted(str(n) for n in secrets)
        names = [n for n in names if n in set(await self._visible(viewer, "secrets", names))]
        counts["secrets"] = len(names)
        return TenantResources(
            **groups, secrets=tuple(safe_text(n) for n in names[:SECRETS_CAP]), counts=counts,
        )

    async def _policies(self, viewer: Viewer, entries: Sequence[CapabilityEntry] | None, degraded: Sequence[str]) -> Policies:
        failed = await asyncio.to_thread(self._reads.failures, viewer.tenant_id)
        failures = tuple(
            FailureLine(
                definition=safe_text(f.get("name") or "run"),
                code=safe_text(f"failed: {f.get('error') or 'no error text'}", 120),
                at=str(f.get("finished_at") or "")[:19] or None,
            )
            for f in (failed.get("items") or ())[:FAILURES_CAP]
        )
        runtime = ""
        if entries is not None:
            models = [e for e in entries if e.kind == "model"]
            servers = [e for e in entries if e.kind == "mcp_server"]
            parts = [
                f"models available {sum(e.status == 'available' for e in models)}/{len(models)}",
                f"mcp servers answering {sum(e.status == 'available' for e in servers)}/{len(servers)}",
            ]
            if degraded:
                parts.append("probes degraded: " + ", ".join(sorted(degraded)))
            runtime = "; ".join(parts)
        return Policies(
            approvals=tuple(sorted(ALWAYS_GATED_TOOL_IDS)), permissions="all", recent_failures=failures, runtime=runtime,
        )

    # --------------------------------------------------------------------------------------------- compose

    async def build(self, viewer: Viewer) -> ContextLayers:
        """The layers for `viewer`; a layer that fails is None and named in `degraded` (the caller still gets a
        context, and the worker records which layers were missing)."""
        degraded: list[str] = []
        capabilities: tuple[CapabilityLine, ...] | None = None
        gaps = None
        registry_version = snapshot_hash = None
        entries: Sequence[CapabilityEntry] | None = None
        probes_degraded: Sequence[str] = ()
        try:
            snap = await self._registry.snapshot(viewer)
            entries, gaps, probes_degraded = snap.entries, snap.gaps, snap.degraded
            registry_version, snapshot_hash = snap.registry_version, snap.snapshot_hash
            capabilities = tuple(
                CapabilityLine(
                    id=e.id, kind=e.kind, status=e.status, requires_approval=e.requires_approval,
                    writable_by_architect=e.writable_by_architect,
                    note=safe_text(e.status_reason, 120) if e.status_reason and e.status != "available" else None,
                )
                for e in snap.entries
            )
            if self._snapshots is not None:
                try:
                    await asyncio.to_thread(self._snapshots, snap.snapshot_hash, snapshot_body(snap.entries))
                except Exception as exc:  # noqa: BLE001 - the snapshot copy is for audit, never for the run
                    log_failure(log, "capability snapshot not stored", exc, code="snapshot_not_stored")
        except Exception as exc:  # noqa: BLE001 - a layer failure is reported, not raised
            log_failure(log, "capability layer unavailable", exc, code="capability_layer_unavailable", tenant_id=viewer.tenant_id)
            degraded += ["capability", "known_gaps"]
        tenant: TenantResources | None = None
        try:
            tenant = await self._tenant(viewer)
        except Exception as exc:  # noqa: BLE001
            log_failure(log, "tenant layer unavailable", exc, code="tenant_layer_unavailable", tenant_id=viewer.tenant_id)
            degraded.append("tenant")
        policies: Policies | None = None
        try:
            policies = await self._policies(viewer, entries, probes_degraded)
        except Exception as exc:  # noqa: BLE001
            log_failure(log, "operational layer unavailable", exc, code="operational_layer_unavailable", tenant_id=viewer.tenant_id)
            degraded.append("operational")
        return ContextLayers(
            registry_version=registry_version, snapshot_hash=snapshot_hash, capabilities=capabilities,
            known_gaps=gaps, tenant=tenant, policies=policies,
            principal=PrincipalSummary(tenant_id=viewer.tenant_id, subject=viewer.subject, kind=viewer.kind),
            degraded=tuple(degraded),
        )

    async def context(self, viewer: Viewer) -> dict[str, Any]:
        """`ContextLayers` as JSON: what `GET /system-context` and the `system.context` tool answer."""
        return (await self.build(viewer)).model_dump(mode="json")


__all__ = ["SystemContextService", "TenantReads", "snapshot_body"]
