"""The Architect's capability tools and the registry-based allowlist (system-aware Architect, phase A)."""

from __future__ import annotations

import asyncio
import random
from types import SimpleNamespace

import pytest

from eap_runtime._default_registry import default_registry
from eap_schemas.architect import ARCHITECT_TOOL_IDS
from eap_schemas.architect_seed import ARCHITECT_INSTRUCTIONS
from eap_schemas.capabilities import approved_tool_ids, collect_declared
from eap_worker import architect_allowlist, architect_tools, control_bridge
from eap_worker.architect_allowlist import registry_tools
from eap_worker.architect_tools import TOOL_IDS, get_capability, list_capabilities
from eap_worker.platform_tools import register_platform_tools


def run(coro):
    return asyncio.run(coro)


def worker_registry():
    registry = default_registry()
    register_platform_tools(registry)
    return registry


def test_every_worker_registered_tool_has_a_registry_entry():
    declared = {e.id for e in collect_declared()}
    registry = worker_registry()
    missing = sorted(t for t in registry.tool_ids() if f"tool.{t}" not in declared)
    assert missing == []


def test_the_architect_tool_ids_agree_between_schema_worker_and_seed():
    assert tuple(TOOL_IDS) == tuple(ARCHITECT_TOOL_IDS)
    assert {"architect.list_capabilities", "architect.get_capability"} <= set(TOOL_IDS)
    registry = worker_registry()
    assert {"architect.list_capabilities", "architect.get_capability"} <= registry.tool_ids()


def test_the_seed_says_availability_comes_only_from_the_capability_tools():
    assert "Availability comes only from the capability tools" in ARCHITECT_INSTRUCTIONS
    assert "list_capabilities" in ARCHITECT_INSTRUCTIONS and "get_capability" in ARCHITECT_INSTRUCTIONS


def test_list_approved_tools_is_a_subset_of_the_registrys_available_tools_for_any_allowed_set():
    registry = worker_registry()
    every = sorted(registry.tool_ids())
    grantable = approved_tool_ids()
    rng = random.Random(20261122)
    for _ in range(40):
        allowed = frozenset(rng.sample(every, rng.randint(0, len(every))))
        offered = {t.id for t in registry_tools(registry, allowed)}
        assert offered <= grantable, sorted(offered - grantable)
        assert offered <= allowed
    assert {t.id for t in registry_tools(registry, frozenset(every))} == grantable & set(every)


def test_a_tool_the_registry_declares_disabled_is_not_offered(monkeypatch):
    registry = worker_registry()
    monkeypatch.setattr(
        architect_allowlist, "declared_tool_status", lambda tool_id: "disabled" if tool_id == "http.request" else "available"
    )
    offered = {t.id for t in registry_tools(registry, frozenset(registry.tool_ids()))}
    assert "http.request" not in offered and "text.check" in offered


def test_workflow_history_and_the_verbatim_rule_are_available_in_the_registry():
    by_id = {e.id: e for e in collect_declared()}
    assert by_id["tool.workflow.history"].status == "available"
    assert by_id["tool_option.text.check.verbatim_exempt"].status == "available"
    assert "workflow.history" in {t.id for t in registry_tools(worker_registry(), frozenset({"workflow.history"}))}


def test_the_capability_tools_call_the_control_planes_registry(monkeypatch):
    calls = []

    async def fake(tenant_id, run_id, name, args):
        calls.append((tenant_id, run_id, name, args))
        return {"entries": [], "snapshot_hash": "h"}

    monkeypatch.setattr(control_bridge, "call_control", fake)
    assert run(list_capabilities("t1", "r1", kind="step", status="available", query="loop"))["snapshot_hash"] == "h"
    assert run(list_capabilities("t1", "r1")) == {"entries": [], "snapshot_hash": "h"}
    run(get_capability("t1", "r1", "step.foreach"))
    assert calls == [
        ("t1", "r1", "capability.list", {"kind": "step", "status": "available", "query": "loop"}),
        ("t1", "r1", "capability.list", {}),
        ("t1", "r1", "capability.get", {"capability_id": "step.foreach"}),
    ]


def test_the_registered_tool_functions_read_the_run_identity(monkeypatch):
    seen = []

    async def fake(tenant_id, run_id, name, args):
        seen.append((tenant_id, run_id, name, args))
        return {"ok": True}

    monkeypatch.setattr(control_bridge, "call_control", fake)
    ctx = SimpleNamespace(deps=SimpleNamespace(tenant_id="t9", run_id="run-9"))
    run(architect_tools._list_capabilities(ctx, query="webhook"))
    run(architect_tools._get_capability(ctx, "trigger.webhook"))
    assert seen == [
        ("t9", "run-9", "capability.list", {"query": "webhook"}),
        ("t9", "run-9", "capability.get", {"capability_id": "trigger.webhook"}),
    ]
    assert "never claim a capability" in architect_tools._list_capabilities.__doc__
