"""The seam between the Architect's `control__*` bridge (worker) and the MCP control plane (API): a run token
authenticates a live run of the built-in Architect, the audit record names that run, and the tools that would
let the Architect approve itself are refused."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

import pytest

from eap_run_store import RunStore
from eap_run_store.run_tokens import mint_run_token, signing_key

from .mcp_control_helpers import secrets_key, world  # noqa: F401 - fixtures
from .run_helpers import requires_pg

pytestmark = requires_pg


def seed_architect(world, tenant: str, *, slug: str = "architect", builtin: bool = True, status: str = "running") -> str:  # noqa: F811
    """An agent row and a run of it; returns the run id."""
    agent_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    world.sql(
        "INSERT INTO agents (id, tenant_id, name, draft_spec, archived, created_at, updated_at, builtin, slug) "
        "VALUES (:i, :t, 'Architect', NULL, false, :n, :n, :b, :s)",
        i=agent_id, t=tenant, n=now, b=builtin, s=slug,
    )
    run_id = uuid.uuid4()
    RunStore(world.app.state.database_engine).create_run(tenant, agent_id, 1, "hi", run_id)  # type: ignore[arg-type]
    if status != "running":
        world.sql("UPDATE runs SET status = :s WHERE id = :r", s=status, r=str(run_id))
    return str(run_id)


def token(secret: str, tenant: str, run_id: str, **kw) -> str:
    return mint_run_token(signing_key(secret), tenant, run_id, **kw)


@pytest.fixture
def cleanup_agents(world):  # noqa: F811
    yield
    for t in world.tenants:
        world.sql("DELETE FROM runs WHERE tenant_id = :t", t=t)
        world.sql("DELETE FROM agents WHERE tenant_id = :t", t=t)


def test_a_live_architect_run_reads_and_manages_and_the_audit_names_the_run(world, secrets_key, cleanup_agents, caplog):  # noqa: F811
    tenant, _ = world.tenant_of()
    run_id = seed_architect(world, tenant)
    bearer = token(secrets_key, tenant, run_id)
    with caplog.at_level(logging.INFO, logger="eap_api.mcp_control"):
        read = world.call(bearer, "scorer.list")
        made = world.call(bearer, "scorer.create", {"name": "No refunds", "evaluator": {"kind": "regex", "pattern": "refund", "negate": True}})
    assert not read.is_error, read.data
    assert not made.is_error, made.data
    audit = [r for r in caplog.records if getattr(r, "eap.audit", None) == "mcp_call"]
    assert {(r.__dict__["eap.tool"], r.__dict__["eap.principal"], r.__dict__["eap.outcome"]) for r in audit} >= {
        ("scorer.list", f"architect-run:{run_id}", "ok"),
        ("scorer.create", f"architect-run:{run_id}", "ok"),
    }
    assert all(r.__dict__["eap.tenant_id"] == tenant for r in audit)


def test_the_tools_that_would_let_the_architect_approve_itself_are_refused_and_tagged(world, secrets_key, cleanup_agents):  # noqa: F811
    import asyncio

    tenant, _ = world.tenant_of()
    run_id = seed_architect(world, tenant)
    bearer = token(secrets_key, tenant, run_id)
    for name, args in (("approvals.decide", {"run_id": run_id, "kind": "tool", "key": "x", "decision": "approve"}),
                       ("agent.chat", {"agent_id": "x", "message": "hi"})):
        reply = world.call(bearer, name, args)
        assert reply.is_error and reply.error["code"] == "human_only", reply.data

    async def tags():
        async with world.session(bearer) as client:
            return {t.name: set((t.meta or {}).get("fastmcp", {}).get("tags", ())) for t in await client.list_tools()}

    listed = asyncio.run(tags())
    assert len(listed) >= 40 and "human_only" in listed["approvals.decide"] and "manage" in listed["eval.suite.create"]
    assert "human_only" not in listed["eval.suite.create"] and listed["eval.suite.list"] == {"read"}


@pytest.mark.parametrize("case", ["ended", "not_architect", "not_builtin", "other_tenant", "expired", "bad_signature", "no_secret"])
def test_a_token_is_not_enough_on_its_own(world, secrets_key, cleanup_agents, monkeypatch, case):  # noqa: F811
    tenant, _ = world.tenant_of()
    other, _ = world.tenant_of("bob")
    run_id = seed_architect(
        world, tenant, status="completed" if case == "ended" else "running",
        slug="helper" if case == "not_architect" else "architect", builtin=case != "not_builtin",
    )
    bearer = {
        "other_tenant": lambda: token(secrets_key, other, run_id),
        "expired": lambda: token(secrets_key, tenant, run_id, now=1),
        "bad_signature": lambda: token("another-secrets-key", tenant, run_id),
    }.get(case, lambda: token(secrets_key, tenant, run_id))()
    if case == "no_secret":
        monkeypatch.delenv("EAP_SECRETS_KEY")
    import httpx2 as httpx

    r = httpx.post(world.url, headers={"Authorization": f"Bearer {bearer}", "Accept": "application/json, text/event-stream"},
                   json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401, (case, r.text)
