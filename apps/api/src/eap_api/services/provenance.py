"""Provenance of the Architect's drafts and publish cards (system-aware Architect, phase C, design section 7).

A row says what the platform looked like when the Architect made a draft or a publish card: the platform version,
the manifest and capability hashes of the context the run was given, who it was built for, and the names (with
argument hashes, never arguments) of the tool calls the run made. It is stored beside the definition, keyed by the
definition's `spec_hash`; the spec, its hash and the version rows are untouched.

Everything is derived server side from the Architect's own run: the worker only says "record a draft/publish card
for this definition". The run id comes from the run token's principal, the hashes from the run's recorded
`system_context_loaded` event, the tool calls from its `tool_call` events and the spec hash from the stored draft
or version. So a model cannot write its own provenance, and no secret or tenant text can enter it.

Reads: `get` returns the rows of a definition and, for the newest draft row, which capabilities the draft uses
changed since (the stored snapshot compared with the live registry).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from eap_run_store.provenance import ProvenanceStore
from eap_schemas import AgentSpec, canonical_spec_hash
from eap_schemas.capabilities import canonical_json
from eap_schemas.system_context import DraftProvenance, PrincipalSummary, used_capability_ids
from eap_schemas.workflows import WorkflowSpec, canonical_workflow_hash
from pydantic import ValidationError


from eap_api.services.capabilities import ARCHITECT_RUN, CapabilityRegistryService, Viewer
from eap_api.services.errors import Forbidden, InvalidInput, NotFound
from eap_api.services.system_context import snapshot_body

RUN_PREFIX = "architect-run:"
MAX_TOOL_CALLS = 200
CARDS = ("draft", "publish")


def _args_hash(args: Any) -> str:
    return hashlib.sha256(canonical_json(args).encode("utf-8")).hexdigest()[:16]


def spec_hash_of(kind: str, spec: Any) -> str | None:
    """The canonical hash of a stored draft spec, or None when it does not validate."""
    try:
        if kind == "agent":
            return canonical_spec_hash(AgentSpec.model_validate(spec))
        return canonical_workflow_hash(WorkflowSpec.model_validate(spec))
    except ValidationError:
        return None


class ProvenanceService:
    def __init__(self, store: ProvenanceStore, registry: CapabilityRegistryService | None = None) -> None:
        self._store = store
        self._registry = registry

    @staticmethod
    def _kind(kind: str) -> str:
        if kind not in ("agent", "workflow"):
            raise InvalidInput("kind must be agent or workflow", code="invalid_kind")
        return kind

    # ---------------------------------------------------------------------------------------------- record

    def _tool_calls(self, tenant_id: str, run_id: str) -> list[dict[str, str]]:
        events = self._store.run_events(tenant_id, run_id, ("tool_call", "tool_result"))
        results: dict[str, str] = {}
        for ev in events:
            if ev["type"] == "tool_result":
                results[str(ev["data"].get("tool_call_id"))] = str(ev["data"].get("status") or "ok")
        calls = []
        for ev in events:
            if ev["type"] != "tool_call":
                continue
            data = ev["data"]
            call_id = str(data.get("tool_call_id"))
            calls.append({
                "name": str(data.get("tool_name"))[:120], "args_hash": _args_hash(data.get("args")),
                "outcome": results.get(call_id, "pending"),
            })
        return calls[:MAX_TOOL_CALLS]

    def _context_of(self, tenant_id: str, run_id: str) -> dict[str, Any]:
        for ev in self._store.run_events(tenant_id, run_id, ("system_context_loaded",)):
            return dict(ev["data"])
        return {}

    def record(
        self, viewer: Viewer, *, kind: str, definition_id: str, card: str = "draft", version: int | None = None,
    ) -> dict[str, Any]:
        """Append a provenance row for the definition, from the Architect's run `viewer`. `card="draft"` hashes
        the stored draft; `card="publish"` takes the published `version`'s hash. Forbidden `architect_only` for any
        other caller; NotFound for a definition the tenant does not have."""
        if viewer.kind != ARCHITECT_RUN or not (viewer.subject or "").startswith(RUN_PREFIX):
            raise Forbidden("only the Architect's own run records provenance", code="architect_only")
        kind = self._kind(kind)
        if card not in CARDS:
            raise InvalidInput(f"card must be one of {', '.join(CARDS)}", code="invalid_card")
        run_id = (viewer.subject or "").removeprefix(RUN_PREFIX)
        found = self._store.find_definition(viewer.tenant_id, kind, definition_id)
        if found is None:
            raise NotFound(f"no {kind} {definition_id!r}", code="not_found")
        if card == "publish":
            if version is None:
                raise InvalidInput("a publish card names the published version", code="version_required")
            spec_hash = self._store.version_hash(viewer.tenant_id, kind, found["id"], int(version))
        else:
            version = None
            spec_hash = spec_hash_of(kind, found["draft_spec"])
        if spec_hash is None:
            raise InvalidInput("the definition has no hashable spec to record against", code="no_spec")
        ctx = self._context_of(viewer.tenant_id, run_id)
        principal = ctx.get("principal")
        body = DraftProvenance(
            platform_version=ctx.get("platform_version"), manifest_version=ctx.get("manifest_version"),
            manifest_hash=ctx.get("manifest_hash"), capability_snapshot_hash=ctx.get("capability_snapshot_hash"),
            registry_version=ctx.get("registry_version"),
            principal=PrincipalSummary.model_validate(principal) if isinstance(principal, dict) else None,
            tool_calls=tuple(self._tool_calls(viewer.tenant_id, run_id)), degraded=tuple(ctx.get("degraded") or ()),
            truncated=tuple(ctx.get("truncated") or ()), model=self._store.run_model(viewer.tenant_id, run_id),
            run_id=run_id, spec_hash=spec_hash, card=card, version=version,
        )
        row = self._store.add(
            viewer.tenant_id, kind=kind, definition_id=found["slug"] or found["id"], spec_hash=spec_hash,
            body=json.loads(body.model_dump_json()), run_id=run_id, version=version,
        )
        return self._view(row)

    # ------------------------------------------------------------------------------------------------ read

    @staticmethod
    def _view(row: dict[str, Any]) -> dict[str, Any]:
        created = row["created_at"]
        return {
            "id": row["id"], "kind": row["kind"], "definition_id": row["definition_id"], "spec_hash": row["spec_hash"],
            "version": row["version"], "run_id": row["run_id"], "plan_id": row["plan_id"],
            "created_at": created.isoformat() if hasattr(created, "isoformat") else created,
            **{k: v for k, v in row["body"].items() if k not in ("spec_hash", "version", "run_id")},
        }

    async def _changed(self, viewer: Viewer, kind: str, definition: dict[str, Any], row: dict[str, Any]) -> list[dict[str, Any]]:
        """Capabilities the draft uses whose status or content changed since the row's snapshot."""
        if self._registry is None:
            return []
        stored = self._store.get_snapshot(str(row["body"].get("capability_snapshot_hash") or ""))
        if stored is None:
            return []
        used = used_capability_ids(kind, definition.get("draft_spec"))
        live = snapshot_body((await self._registry.snapshot(viewer)).entries)
        out = []
        for cap_id in used:
            was, now = stored.get(cap_id), live.get(cap_id)
            if was != now:
                out.append({"id": cap_id, "was": was["s"] if was else None, "now": now["s"] if now else None})
        return out

    async def get(
        self, viewer: Viewer, *, kind: str, definition_id: str, spec_hash: str | None = None, limit: int = 20
    ) -> dict[str, Any]:
        """The definition's provenance rows (newest first) and `capabilities_changed` for the newest draft row.
        NotFound for a definition the tenant does not have (the same answer for another tenant's)."""
        kind = self._kind(kind)
        found = self._store.find_definition(viewer.tenant_id, kind, definition_id)
        if found is None:
            raise NotFound(f"no {kind} {definition_id!r}", code="not_found")
        rows = self._store.list(viewer.tenant_id, kind, found["slug"] or found["id"], spec_hash=spec_hash, limit=limit)
        newest_draft = next((r for r in rows if r["body"].get("card", "draft") == "draft"), None)
        changed = await self._changed(viewer, kind, found, newest_draft) if newest_draft is not None else []
        return {
            "definition_id": found["slug"] or found["id"], "kind": kind,
            "items": [self._view(r) for r in rows], "capabilities_changed": changed,
        }


__all__ = ["ProvenanceService", "spec_hash_of"]
