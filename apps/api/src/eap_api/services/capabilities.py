"""The capability registry service: what the platform can do now, for one tenant and one viewer.

The registry is the declared inventory (`eap_schemas.capabilities.collect_declared`, plus the MCP control tools the
control plane registers) with the statuses and dynamic entries the probes report (`capability_probes`), filtered
for the viewer. The same service answers `GET /api/v1/capabilities`, the MCP read tools `capability.list` /
`capability.get` and, through the control bridge, the Architect's `architect.list_capabilities` /
`get_capability`, so every reader sees one truth.

Hashes: `registry_version` is the content hash of the declared (code-derived) part; `snapshot_hash` is the hash of
the entries the viewer can see, dynamic ones included, so two viewers can differ and a change of an MCP server or a
provider changes it. It is the route's ETag.

Who sees what is a pluggable `EntryPolicy` (today: nothing is hidden except the control tools only a person may
use, from the Architect's own run). When IAM's PolicyEngine arrives, a policy that calls it replaces the default;
nothing else here changes.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from eap_api.services.capability_probes import ProbeContext, ProbeRegistry, merge
from eap_api.services.errors import InvalidInput, NotFound
from eap_schemas.capabilities import (
    KINDS,
    STATUSES,
    CapabilityEntry,
    ControlToolDecl,
    collect_declared,
    entries_hash,
)
from eap_schemas.capability_gaps import GapLine, known_gaps

MAX_QUERY_CHARS = 100
NEAREST = 5
ARCHITECT_RUN = "architect_run"


@dataclass(frozen=True)
class Viewer:
    """Whose view of the registry: the tenant, the kind of caller (`user`, `api_key`, `architect_run`) and who."""

    tenant_id: str
    kind: str = "user"
    subject: str | None = None


class EntryPolicy(Protocol):
    """Which entries a viewer may see. The default hides nothing a viewer could use; IAM supplies another."""

    def visible(self, viewer: Viewer, entry: CapabilityEntry) -> bool: ...


class DefaultEntryPolicy:
    """Pre-IAM: every entry is visible, except a control tool only a person may call, from the Architect's run
    (the control bridge hides the same tools)."""

    def __init__(self, human_only: frozenset[str] = frozenset()) -> None:
        self._human_only = human_only

    def visible(self, viewer: Viewer, entry: CapabilityEntry) -> bool:
        return not (viewer.kind == ARCHITECT_RUN and entry.id in self._human_only)


@dataclass(frozen=True)
class Snapshot:
    """The registry as one viewer sees it."""

    entries: tuple[CapabilityEntry, ...]
    gaps: tuple[GapLine, ...]
    registry_version: str
    snapshot_hash: str
    degraded: tuple[str, ...] = ()


def _words(text: str) -> list[str]:
    return [w for w in re.split(r"[^a-z0-9]+", text.lower()) if w]


class CapabilityRegistryService:
    def __init__(
        self,
        probes: ProbeRegistry,
        control_tools: Callable[[], Sequence[ControlToolDecl]] = lambda: (),
        policy: EntryPolicy | None = None,
    ) -> None:
        self._probes = probes
        self._control_tools = control_tools
        self._policy = policy

    # --- building --------------------------------------------------------------------------------------

    def _policy_for(self, controls: Sequence[ControlToolDecl]) -> EntryPolicy:
        return self._policy or DefaultEntryPolicy(
            frozenset(f"control.{c.name}" for c in controls if c.human_only or c.internal)
        )

    async def snapshot(self, viewer: Viewer) -> Snapshot:
        controls = tuple(self._control_tools())
        declared = collect_declared(controls)
        outcome = await self._probes.run(ProbeContext(viewer.tenant_id, {e.id: e for e in declared}))
        merged = merge(declared, outcome)
        policy = self._policy_for(controls)
        visible = tuple(e for e in merged if policy.visible(viewer, e))
        return Snapshot(
            entries=visible, gaps=known_gaps(), registry_version=entries_hash(declared),
            snapshot_hash=entries_hash(visible), degraded=outcome.degraded,
        )

    def invalidate(self, tenant_id: str | None = None) -> None:
        """Forget cached probe results (a provider or MCP server change calls this)."""
        self._probes.invalidate(tenant_id)

    # --- reads -----------------------------------------------------------------------------------------

    async def list(
        self, viewer: Viewer, *, kind: str | None = None, status: str | None = None, query: str | None = None,
        include_schemas: bool = False,
    ) -> dict[str, Any]:
        """Entries (compact unless `include_schemas`), the known gaps, the hashes and any degraded probe.

        `kind` and `status` filter exactly; `query` keeps entries whose id, title or description contain every
        word. InvalidInput for an unknown kind or status."""
        if kind is not None and kind not in KINDS:
            raise InvalidInput(f"unknown kind {kind!r}; one of {', '.join(KINDS)}", code="invalid_kind")
        if status is not None and status not in STATUSES:
            raise InvalidInput(f"unknown status {status!r}; one of {', '.join(STATUSES)}", code="invalid_status")
        words = _words((query or "")[:MAX_QUERY_CHARS])
        snap = await self.snapshot(viewer)
        chosen = [
            e for e in snap.entries
            if (kind is None or e.kind == kind) and (status is None or e.status == status)
            and all(w in f"{e.id} {e.title} {e.description}".lower() for w in words)
        ]
        shown = [e.model_dump(mode="json") if include_schemas else e.compact() for e in chosen]
        return {
            "registry_version": snap.registry_version, "snapshot_hash": snap.snapshot_hash,
            "total": len(snap.entries), "matched": len(chosen), "entries": shown,
            "known_gaps": [g.model_dump(mode="json") for g in snap.gaps],
            "degraded": list(snap.degraded),
        }

    async def get(self, viewer: Viewer, entry_id: str) -> dict[str, Any]:
        """One entry with its schemas, and the gaps that concern it. NotFound `unknown_capability` (with the
        nearest ids) for an id the viewer cannot see, which is also the answer for an id that does not exist."""
        snap = await self.snapshot(viewer)
        found = next((e for e in snap.entries if e.id == entry_id), None)
        if found is None:
            near = self._nearest(entry_id, [e.id for e in snap.entries])
            raise NotFound(
                f"no capability {entry_id!r}" + (f"; nearest: {', '.join(near)}" if near else ""),
                code="unknown_capability", details=[{"nearest": near}],
            )
        gaps = [g.model_dump(mode="json") for g in snap.gaps if found.id in g.entry_ids]
        return {
            "snapshot_hash": snap.snapshot_hash, "registry_version": snap.registry_version,
            "entry": found.model_dump(mode="json"), "gaps": gaps,
        }

    @staticmethod
    def _nearest(wanted: str, ids: list[str]) -> list[str]:
        import difflib

        close = difflib.get_close_matches(wanted, ids, n=NEAREST, cutoff=0.5)
        if close:
            return close
        words = _words(wanted)
        return [i for i in ids if words and all(w in i.lower() for w in words)][:NEAREST]


def etag_of(snapshot_hash: str) -> str:
    """The strong ETag of a listing: the snapshot hash, shortened (collisions are not a concern at 128 bits)."""
    return '"' + hashlib.sha256(snapshot_hash.encode()).hexdigest()[:32] + '"'


__all__ = [
    "ARCHITECT_RUN",
    "CapabilityRegistryService",
    "DefaultEntryPolicy",
    "EntryPolicy",
    "Snapshot",
    "Viewer",
    "etag_of",
]
