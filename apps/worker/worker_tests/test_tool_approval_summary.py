"""Approval summaries from the registry, and the architect.* guard for non-builtin agents."""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from worker_tests.conftest import APP_VERSION, enqueue_run, ticket_spec, wait_terminal
from worker_tests.test_tool_approval import TICKET_ARGS, gated_spec, prompt, start, wait_waiting


def _echo(note: str) -> str:
    return note


def _insert_agent(worker, tenant, agent_id, spec, *, builtin: bool) -> None:
    with worker.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agents (id, tenant_id, name, draft_spec, archived, builtin, created_at, updated_at) "
                "VALUES (:id, :t, :n, cast(:s as jsonb), false, :b, now(), now())"
            ),
            {"id": str(agent_id), "t": tenant, "n": f"agent_{agent_id}", "s": json.dumps(spec), "b": builtin},
        )


def test_waiting_payload_carries_the_registered_summary(worker, client, tenant, monkeypatch):
    if "test.summarised" not in worker.registry.tool_ids():
        worker.registry.register(
            "test.summarised", _echo, description="Echo", approval_summary=lambda a: f"Will echo: {a['note']}"
        )
    monkeypatch.setitem(worker.__dict__, "allowed_tools", frozenset(worker.registry.tool_ids()))
    spec = gated_spec(tools=("test.summarised",), gate=("test.summarised",))
    run_id = start(worker, client, tenant, spec, prompt("test.summarised", {"note": "hi"}))
    waiting = wait_waiting(worker, run_id)
    assert waiting[0]["summary"] == "Will echo: hi"
    assert waiting[0]["args"] == {"note": "hi"}


def test_waiting_payload_has_no_summary_without_a_summariser(worker, client, tenant):
    run_id = start(worker, client, tenant, gated_spec(), prompt())
    waiting = wait_waiting(worker, run_id)
    assert "summary" not in waiting[0]
    assert waiting[0]["args"] == TICKET_ARGS


def test_allowed_tools_for_hides_architect_tools_from_non_builtin_agents(worker, monkeypatch):
    monkeypatch.setitem(worker.__dict__, "allowed_tools", frozenset({"clock.now", "architect.save_agent"}))
    architect = {t for t in worker.registry.tool_ids() if t.startswith(("architect.", "platform."))}
    assert worker.allowed_tools_for(builtin=False) == frozenset({"clock.now"})
    assert worker.allowed_tools_for(builtin=True) == frozenset({"clock.now", "architect.save_agent"}) | architect


def test_builtin_agents_keep_architect_tools_under_a_deployment_allowlist(worker, monkeypatch):
    """EAP_ALLOWED_TOOLS names tenant-grantable tools (a deployment may allow only ticket.create); the
    seed's platform tools for the built-in Architect must not be cut by it, or every Architect run fails
    with tool_not_allowed before the model is called (deploy-host, 2026-11-17)."""
    monkeypatch.setitem(worker.__dict__, "allowed_tools", frozenset({"ticket.create"}))
    architect = {t for t in worker.registry.tool_ids() if t.startswith(("architect.", "platform."))}
    assert architect, "the registry registers the Architect's tools"
    assert worker.allowed_tools_for(builtin=True) == frozenset({"ticket.create"}) | architect
    assert worker.allowed_tools_for(builtin=False) == frozenset({"ticket.create"})


@pytest.mark.parametrize("builtin", [False, True])
def test_a_non_builtin_agent_cannot_see_architect_tools(worker, client, tenant, monkeypatch, builtin):
    if "architect.save_agent" not in worker.registry.tool_ids():
        worker.registry.register("architect.save_agent", _echo, description="Save a draft")
    monkeypatch.setitem(worker.__dict__, "allowed_tools", frozenset(worker.registry.tool_ids()))
    spec = ticket_spec(tools=("architect.save_agent",), model="approval")
    agent_id = uuid.uuid4()
    _insert_agent(worker, tenant, agent_id, spec, builtin=builtin)
    run_id = enqueue_run(
        client, worker.run_store, tenant_id=tenant, spec=spec, input_text=prompt("architect.save_agent", {"note": "x"}),
        app_version=APP_VERSION, agent_id=agent_id,
    )
    run = wait_terminal(worker.run_store, tenant, run_id)
    if builtin:
        assert run["status"] == "completed"
    else:
        assert run["status"] == "failed"
