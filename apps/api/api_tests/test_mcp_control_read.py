"""The MCP control endpoint, read side: authentication, scopes, every read tool, resources, tenant
isolation and the audit record. A FastMCP client talks to the real app over Streamable HTTP."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

import pytest

from .mcp_control_helpers import secrets_key, world  # noqa: F401 - fixtures
from .run_helpers import requires_pg

pytestmark = requires_pg

READ_TOOLS = {
    "agent.list", "agent.get", "agent.versions", "workflow.list", "workflow.get", "workflow.versions",
    "workflow.run.get", "workflow.run.wait", "workflow.run.result", "workflow.schedule.list", "kb.list", "kb.get",
    "kb.source.list", "kb.source.get", "kb.source.syncs", "settings.connection.list", "settings.connection.check",
    "mcp.server.list", "mcp.catalog.search", "approvals.list",
}


def test_without_a_key_the_endpoint_is_401(world):
    import httpx2 as httpx

    r = httpx.post(world.url, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"
    r = httpx.post(world.url, json={}, headers={"Authorization": "Bearer pos_" + "0" * 24 + "_nope"})
    assert r.status_code == 401 and r.json()["error"]["message"] == "Invalid API key"
    # No traceback, no key echo.
    assert "pos_" not in r.text


def test_a_key_lists_every_read_tool(world):
    _, h = world.tenant_of()
    key = world.make_key(h)

    async def names():
        async with world.session(key) as c:
            return {t.name for t in await c.list_tools()}

    assert READ_TOOLS <= asyncio.run(names())


def test_agent_tools_read_the_tenants_agents(world):
    _, h = world.tenant_of()
    key = world.make_key(h)
    agent_id = world.publish_agent(h)
    listed = world.call(key, "agent.list")
    assert not listed.is_error and [a["id"] for a in listed.data["items"]] == [agent_id]
    assert listed.data["total"] == 1 and listed.data["next_offset"] is None
    got = world.call(key, "agent.get", {"agent_id": agent_id})
    assert got.data["id"] == agent_id and got.data["latest_version"] == 1
    versions = world.call(key, "agent.versions", {"agent_id": agent_id})
    assert [v["version"] for v in versions.data["items"]] == [1]
    assert world.call(key, "agent.get", {"agent_id": "not-a-uuid"}).error["code"] == "invalid"


def test_lists_are_paged(world):
    _, h = world.tenant_of()
    key = world.make_key(h)
    ids = [world.publish_agent(h) for _ in range(3)]
    first = world.call(key, "agent.list", {"limit": 2})
    assert len(first.data["items"]) == 2 and first.data["next_offset"] == 2 and first.data["total"] == 3
    rest = world.call(key, "agent.list", {"limit": 2, "offset": 2})
    assert len(rest.data["items"]) == 1 and rest.data["next_offset"] is None
    assert {a["id"] for a in first.data["items"] + rest.data["items"]} == set(ids)
    assert len(world.call(key, "agent.list", {"limit": 5000}).data["items"]) == 3  # capped at 100, not an error


def test_workflow_tools_and_run_reads(world):
    _, h = world.tenant_of()
    key = world.make_key(h)
    agent_id = world.publish_agent(h)
    wid = world.publish_workflow(h, agent_id)
    assert [w["id"] for w in world.call(key, "workflow.list").data["items"]] == [wid]
    assert world.call(key, "workflow.get", {"workflow_id": wid}).data["latest_version"] == 1
    assert [v["version"] for v in world.call(key, "workflow.versions", {"workflow_id": wid}).data["items"]] == [1]

    run_id = str(uuid.uuid4())
    with world.rest() as c:
        r = c.post(f"/workflows/{wid}/runs", json={"input": {"q": "hi"}, "run_id": run_id}, headers=h)
    assert r.status_code == 202, r.text
    got = world.call(key, "workflow.run.get", {"run_id": run_id})
    assert got.data["status"] == "queued" and got.data["workflow_id"] == wid
    waited = world.call(key, "workflow.run.wait", {"run_id": run_id, "timeout_seconds": 0})
    assert waited.data["done"] is False and waited.data["status"] == "queued"
    world.sql("UPDATE runs SET status='completed', output=:o WHERE id = :r", o="x" * 40_000, r=run_id)
    done = world.call(key, "workflow.run.wait", {"run_id": run_id, "timeout_seconds": 5})
    assert done.data["done"] is True and done.data["status"] == "completed"
    result = world.call(key, "workflow.run.result", {"run_id": run_id})
    assert result.data["status"] == "completed"
    assert len(result.data["output"]) == 32 * 1024 and result.data["output_truncated"] is True
    assert result.data["input"] == '{"q":"hi"}' and result.data["input_truncated"] is False


def test_schedules_kb_connections_mcp_and_approvals_answer_empty_for_a_new_tenant(world):
    _, h = world.tenant_of()
    key = world.make_key(h)
    agent_id = world.publish_agent(h)
    wid = world.publish_workflow(h, agent_id)
    assert world.call(key, "workflow.schedule.list", {"workflow_id": wid}).data["items"] == []
    assert world.call(key, "workflow.schedule.list").data["items"] == []
    assert world.call(key, "kb.list").data["items"] == []
    assert world.call(key, "settings.connection.list").data["items"] == []
    assert world.call(key, "mcp.server.list").data["items"] == []
    assert world.call(key, "approvals.list").data["items"] == []
    assert "items" in world.call(key, "mcp.catalog.search", {"query": "x"}).data
    assert world.call(key, "kb.get", {"kb_id": str(uuid.uuid4())}).error["code"] == "not_found"
    assert world.call(key, "kb.source.list", {"kb_id": str(uuid.uuid4())}).error["code"] == "knowledge_base_not_found"
    assert world.call(key, "settings.connection.check", {"connection_id": str(uuid.uuid4())}).error["code"] == "connection_not_found"


def test_another_tenant_reads_nothing_by_id(world):
    _, a = world.tenant_of()
    _, b = world.tenant_of("mallory")
    key_b = world.make_key(b)
    agent_id = world.publish_agent(a)
    wid = world.publish_workflow(a, agent_id)
    run_id = str(uuid.uuid4())
    with world.rest() as c:
        assert c.post(f"/workflows/{wid}/runs", json={"input": {"q": "hi"}, "run_id": run_id}, headers=a).status_code == 202
    for tool, args in [
        ("agent.get", {"agent_id": agent_id}), ("agent.versions", {"agent_id": agent_id}),
        ("workflow.get", {"workflow_id": wid}), ("workflow.versions", {"workflow_id": wid}),
        ("workflow.run.get", {"run_id": run_id}), ("workflow.run.wait", {"run_id": run_id, "timeout_seconds": 0}),
        ("workflow.run.result", {"run_id": run_id}), ("workflow.schedule.list", {"workflow_id": wid}),
    ]:
        reply = world.call(key_b, tool, args)
        assert reply.is_error and reply.error["code"] == "not_found", (tool, reply)
    assert world.call(key_b, "agent.list").data["items"] == []
    assert world.call(key_b, "workflow.list").data["items"] == []


def test_allowlisted_keys_see_only_their_agents(world):
    _, h = world.tenant_of()
    mine, other = world.publish_agent(h), world.publish_agent(h)
    key = world.make_key(h, agent_ids=[mine])
    assert [a["id"] for a in world.call(key, "agent.list").data["items"]] == [mine]
    assert world.call(key, "agent.get", {"agent_id": other}).error["code"] == "not_found"


def test_resources_are_tenant_scoped(world):
    _, a = world.tenant_of()
    _, b = world.tenant_of("mallory")
    key_a, key_b = world.make_key(a), world.make_key(b)
    agent_id = world.publish_agent(a)
    wid = world.publish_workflow(a, agent_id)

    async def read(key, uri):
        async with world.session(key) as c:
            blocks = await c.read_resource(uri)
            return json.loads(blocks[0].text)

    assert asyncio.run(read(key_a, f"pydanticos://agents/{agent_id}"))["id"] == agent_id
    assert asyncio.run(read(key_a, f"pydanticos://workflows/{wid}"))["id"] == wid
    with pytest.raises(Exception):
        asyncio.run(read(key_b, f"pydanticos://agents/{agent_id}"))
    with pytest.raises(Exception):
        asyncio.run(read(key_b, f"pydanticos://workflows/{wid}"))


def test_every_call_is_audited_without_arguments(world, caplog):
    _, h = world.tenant_of()
    key = world.make_key(h)
    agent_id = world.publish_agent(h)
    with caplog.at_level(logging.INFO, logger="eap_api.mcp_control"):
        world.call(key, "agent.get", {"agent_id": agent_id})
        world.call(key, "agent.get", {"agent_id": str(uuid.uuid4())})
    records = [r for r in caplog.records if getattr(r, "eap.audit", None) == "mcp_call"]
    assert [(getattr(r, "eap.tool"), getattr(r, "eap.outcome")) for r in records] == [("agent.get", "ok"), ("agent.get", "error")]
    assert all(agent_id not in r.getMessage() for r in records)
    assert getattr(records[1], "eap.code") == "not_found"


def test_the_dev_bearer_connects_in_dev_mode_and_a_revoked_key_stops(world):
    tenant, h = world.tenant_of()
    agent_id = world.publish_agent(h)
    dev = h["Authorization"].removeprefix("Bearer ")
    assert [a["id"] for a in world.call(dev, "agent.list").data["items"]] == [agent_id]
    key = world.make_key(h)
    assert not world.call(key, "agent.list").is_error
    with world.rest() as c:
        key_id = c.get("/api-keys", headers=h).json()["items"][0]["id"]
        assert c.delete(f"/api-keys/{key_id}", headers=h).status_code == 204
    with pytest.raises(Exception):  # the revoked key is refused at the transport (401)
        world.call(key, "agent.list")


def test_the_manage_scope_is_a_key_scope_and_unknown_scopes_still_fail(world):
    _, h = world.tenant_of()
    with world.rest() as c:
        ok = c.post("/api-keys", json={"name": "m", "scopes": ["agui:run", "mcp", "manage"]}, headers=h)
        assert ok.status_code == 201 and ok.json()["scopes"] == ["agui:run", "mcp", "manage"]
        assert c.post("/api-keys", json={"name": "m", "scopes": ["admin"]}, headers=h).status_code == 422


def test_a_run_only_key_is_refused_at_the_endpoint_and_scopes_stack(world):
    import httpx2 as httpx

    _, h = world.tenant_of()
    run_only = world.make_key(h, scopes=["agui:run"])
    r = httpx.post(world.url, json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers={"Authorization": f"Bearer {run_only}"})
    assert r.status_code == 403 and r.json()["error"] == {"code": "forbidden", "message": "API key lacks the mcp scope"}
    assert run_only not in r.text
    agent_id = world.publish_agent(h)
    reader = world.make_key(h, scopes=["mcp"])
    assert [a["id"] for a in world.call(reader, "agent.list").data["items"]] == [agent_id]
    assert world.call(reader, "agent.publish", {"agent_id": agent_id}).error["code"] == "scope_required"
    manager = world.make_key(h, scopes=["manage"])  # manage implies mcp: reads and mutates
    assert len(world.call(manager, "agent.list").data["items"]) == 1
    assert world.call(manager, "agent.publish", {"agent_id": agent_id}).error["code"] == "conflict"  # reached the service


def test_a_manage_only_key_can_read_but_cannot_run_over_ag_ui(world):
    _, h = world.tenant_of()
    key = world.make_key(h, scopes=["manage"])
    world.publish_agent(h)
    assert len(world.call(key, "agent.list").data["items"]) == 1


def test_calls_over_the_per_key_concurrency_cap_are_refused(world, monkeypatch):
    from eap_api.sse import StreamLimiter

    _, h = world.tenant_of()
    key = world.make_key(h)
    world.app.state.mcp_limiter = StreamLimiter(max_global=10, max_per_principal=0)
    reply = world.call(key, "agent.list")
    assert reply.is_error and reply.error["code"] == "rate_limited"
