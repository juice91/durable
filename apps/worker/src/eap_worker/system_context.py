"""`eap.load_system_context`: the built-in Architect's view of the platform, recorded once per run
(system-aware Architect, phase C; design sections 2 and 7).

    eap.load_memory ... (when the spec has memory)
    eap.load_system_context       NEW, built-in Architect runs only: the layers from the API, the manifest's
                                  Platform layer, the rendered block and the provenance, as ONE recorded output
    eap.append_event  system_context   system_context_loaded: provenance and hashes, never the text

The block is appended to the Architect's instructions as a delimited "platform facts" block, fixed for the run (the
same pattern as the working-memory block: `eap_worker.memory`). The BUILD is the step (blocking HTTP to the control
plane and a file read, so it belongs in a step); the RENDER is pure (`eap_schemas.system_context.render_block`)
and is part of the recorded output, so a run recovered after a crash gets the same bytes back even if the
registry changed in between. The block is never stored: `runs.messages` keep the base instructions only
(`strip_system_context_messages`).

Which runs: only a built-in spec that lists a tool of the built-in-only prefixes (`architect.`, `platform.`).
Every other spec's step layout and instructions are exactly what they were.

A layer that cannot be built does not fail the run: the API composes what it can, the control plane being down
degrades every layer, a missing manifest degrades the Platform layer; the block says so ("do not claim any
capability is present") and the event lists the degraded layers.

No secret enters: the API sends names and counts only (see `eap_api.services.system_context`), the renderer escapes
and scrubs them, and the event holds hashes and layer names.
"""

from __future__ import annotations

import dataclasses
import logging
import re
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

from dbos import DBOS
from pydantic import ValidationError

from eap_observability import log_failure
from eap_schemas.architect import BUILTIN_ONLY_TOOL_PREFIXES
from eap_schemas.system_context import (
    BLOCK_CLOSE,
    BLOCK_OPEN,
    CONTEXT_BUDGET_TOKENS,
    ContextLayers,
    PrincipalSummary,
    render_block,
)
from eap_schemas.system_manifest import LoadedManifest, load_manifest, platform_version, render_platform_layer

from eap_worker import control_bridge
from eap_worker.crash import crash_point

log = logging.getLogger("eap_worker.system_context")

LOAD_SYSTEM_CONTEXT_STEP = "eap.load_system_context"
EVENT_KEY = "system_context"
EVENT_TYPE = "system_context_loaded"
ALL_LAYERS = ("capability", "known_gaps", "tenant", "operational")


def wants_system_context(spec: Any, builtin: bool) -> bool:
    """True for a built-in agent that lists an Architect or platform tool: the built-in Architect, no other."""
    return builtin and any(str(t).startswith(BUILTIN_ONLY_TOOL_PREFIXES) for t in getattr(spec, "tools", ()) or ())


@lru_cache(maxsize=1)
def _manifest() -> LoadedManifest | None:
    """The packaged manifest, loaded once per process (None when it cannot be read)."""
    try:
        return load_manifest()
    except Exception as exc:  # noqa: BLE001 - a missing or invalid manifest degrades the Platform layer
        log_failure(log, "system manifest unavailable", exc, code="system_manifest_unavailable")
        return None


def reset_cache() -> None:
    _manifest.cache_clear()


def _budgets(loaded: LoadedManifest | None) -> dict[str, int]:
    if loaded is None:
        return dict(CONTEXT_BUDGET_TOKENS)
    return {**CONTEXT_BUDGET_TOKENS, **loaded.manifest.generated.context_budget}


async def _layers(tenant_id: str, run_id: str) -> ContextLayers:
    """The API's layers for this run; the control plane being unreachable degrades every layer."""
    answer = await control_bridge.call_control(tenant_id, run_id, "system.context", {})
    if "error" in answer:
        log.warning("run %s: system context unavailable (%s)", run_id, answer["error"].get("code"))
    else:
        try:
            return ContextLayers.model_validate(answer)
        except ValidationError as exc:
            log_failure(log, "system context malformed", exc, code="system_context_malformed", run_id=run_id)
    return ContextLayers(principal=PrincipalSummary(tenant_id=tenant_id), degraded=ALL_LAYERS)


@DBOS.step(name=LOAD_SYSTEM_CONTEXT_STEP)
async def load_system_context_step(run_id: str, tenant_id: str) -> dict[str, Any]:
    """Build and render the context: `{"block": str, "provenance": {...}}`, JSON only (DBOS checkpoints it). Never
    raises for a missing layer; the run goes on with a degraded block."""
    layers = await _layers(tenant_id, run_id)
    loaded = _manifest()
    budgets = _budgets(loaded)
    version = platform_version()
    platform_text = render_platform_layer(loaded.manifest) if loaded is not None else None
    rendered = render_block(platform_text, version, layers, budgets)
    provenance = {
        "platform_version": version,
        "manifest_version": loaded.manifest_version if loaded else None,
        "manifest_hash": loaded.manifest_hash if loaded else None,
        "registry_version": layers.registry_version,
        "capability_snapshot_hash": layers.snapshot_hash,
        "principal": layers.principal.model_dump(mode="json"),
        "degraded": list(rendered.degraded),
        "truncated": list(rendered.truncated),
        "sizes": rendered.sizes,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    # Crash-injection tests: the context was built, the step's output is not yet checkpointed.
    crash_point("load_system_context")
    return {"block": rendered.text, "provenance": provenance}


def with_system_context(deps: Any, loaded: dict[str, Any]) -> Any:
    """`deps` carrying the recorded step output (the dataclass's own class is kept)."""
    return dataclasses.replace(deps, system_context=loaded)


def block_of(deps: Any) -> str | None:
    data = getattr(deps, "system_context", None)
    block = data.get("block") if isinstance(data, dict) else None
    return block if isinstance(block, str) and block else None


def instructions_with_system_context(base: str, deps: Any) -> str:
    block = block_of(deps)
    if block is None:
        return base
    return f"{base}\n\n{block}" if base else block


def snapshot_hash_of(deps: Any) -> str | None:
    data = getattr(deps, "system_context", None)
    prov = data.get("provenance") if isinstance(data, dict) else None
    return prov.get("capability_snapshot_hash") if isinstance(prov, dict) else None


_BLOCK_RE = re.compile(r"(?:\n\n)?" + re.escape(BLOCK_OPEN) + r".*?" + re.escape(BLOCK_CLOSE), re.DOTALL)


def strip_system_context_messages(messages: Any) -> Any:
    """Stored messages with the platform-facts block removed from every request's `instructions` (the block is
    rebuilt each run; history keeps the base instructions only)."""
    if not isinstance(messages, list):
        return messages
    out: list[Any] = []
    for message in messages:
        if isinstance(message, dict) and message.get("kind") == "request" and isinstance(message.get("instructions"), str):
            stripped = _BLOCK_RE.sub("", message["instructions"])
            if stripped != message["instructions"]:
                message = {**message, "instructions": stripped or None}
        out.append(message)
    return out


__all__ = [
    "EVENT_KEY",
    "EVENT_TYPE",
    "LOAD_SYSTEM_CONTEXT_STEP",
    "block_of",
    "instructions_with_system_context",
    "load_system_context_step",
    "reset_cache",
    "snapshot_hash_of",
    "strip_system_context_messages",
    "wants_system_context",
    "with_system_context",
]
