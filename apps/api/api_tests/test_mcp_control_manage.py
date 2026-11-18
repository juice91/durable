"""The MCP control endpoint, mutation side: the `manage` scope, every mutation tool over the shared
services, cross-tenant and allowlist limits, no secrets surface, and a real worker for agent.chat and
approvals.decide."""

from __future__ import annotations

import asyncio
import json
import time
import uuid

from eap_kb_sources.fake import FakeCatalogAdapter
from eap_kb_sources.registry import register_adapter

from .mcp_control_helpers import live_world, secrets_key, world  # noqa: F401 - fixtures
from .mcp_runtime_helpers import make_entry
from .run_helpers import requires_pg
from .test_run_e2e import worker_env  # noqa: F401 - fixture

pytestmark = requires_pg

MANAGE_TOOLS = {
    "agent.save_draft", "agent.publish", "agent.archive", "agent.chat", "workflow.save_draft", "workflow.publish",
    "workflow.run.start", "workflow.run.cancel", "workflow.schedule.set", "kb.create", "kb.source.create",
    "kb.source.sync", "settings.connection.create", "mcp.server.install", "mcp.server.runtime", "approvals.decide",
}
SPEC = {"name": "Drafted", "instructions": "Help.", "model": {"provider": "test", "name": "echo"}}


def manage_key(world, h, **body):
    return world.make_key(h, scopes=["agui:run", "manage"], **body)


async def read_resource(world, key, uri):
    async with world.session(key) as c:
        return json.loads((await c.read_resource(uri))[0].text)


def test_every_mutation_tool_is_listed_and_no_secret_tool_exists(world):
    _, h = world.tenant_of()
    key = manage_key(world, h)

    async def names():
        async with world.session(key) as c:
            return {t.name for t in await c.list_tools()}

    got = asyncio.run(names())
    assert MANAGE_TOOLS <= got
    assert not [n for n in got if "secret" in n.lower()], got  # the data-source credentials rule stands


def test_every_mutation_is_refused_without_the_manage_scope(world):
    _, h = world.tenant_of()
    read_key = world.make_key(h)  # mcp (read) only
    agent_id = world.publish_agent(h)
    some = str(uuid.uuid4())
    calls = {
        "agent.save_draft": {"spec": SPEC}, "agent.publish": {"agent_id": agent_id}, "agent.archive": {"agent_id": agent_id},
        "agent.chat": {"agent_id": agent_id, "message": "hi"}, "workflow.save_draft": {"spec": {}},
        "workflow.publish": {"workflow_id": some}, "workflow.run.start": {"workflow_id": some, "input": {}},
        "workflow.run.cancel": {"run_id": some}, "workflow.schedule.set": {"workflow_id": some},
        "kb.create": {"name": "x", "embedding_model": "test:hash"},
        "kb.source.create": {"kb_id": some, "connection_id": some, "name": "x", "provider": "fake"},
        "kb.source.sync": {"kb_id": some, "source_id": some, "mode": "full"},
        "settings.connection.create": {"name": "x", "config": {"provider": "fake"}},
        "mcp.server.install": {"catalog_name": "x"}, "mcp.server.runtime": {"server_id": some, "action": "stop"},
        "approvals.decide": {"run_id": some, "kind": "tool", "key": "k", "decision": "approve"},
    }
    assert set(calls) == MANAGE_TOOLS
    for tool, args in calls.items():
        reply = world.call(read_key, tool, args)
        assert reply.is_error and reply.error["code"] == "scope_required", (tool, reply)
    with world.rest() as c:  # nothing changed
        assert [a["id"] for a in c.get("/agents", headers=h).json()] == [agent_id]
        assert [v["version"] for v in c.get(f"/agents/{agent_id}/versions", headers=h).json()] == [1]


def test_a_created_agent_appears_in_studios_list_and_publishes_with_the_studio_checks(world):
    _, h = world.tenant_of()
    key = manage_key(world, h)
    created = world.call(key, "agent.save_draft", {"spec": SPEC})
    assert not created.is_error, created.data
    agent_id = created.data["id"]
    with world.rest() as c:
        assert [a["id"] for a in c.get("/agents", headers=h).json()] == [agent_id]
    replaced = world.call(key, "agent.save_draft", {"agent_id": agent_id, "spec": {**SPEC, "name": "Renamed"}})
    assert replaced.data["name"] == "Renamed"
    published = world.call(key, "agent.publish", {"agent_id": agent_id})
    assert not published.is_error and published.data["version"] == 1 and "warnings" in published.data
    # The publish-time refusals are Studio's: an always-gated tool without its approval entry.
    bad = world.call(key, "agent.save_draft", {"spec": {**SPEC, "tools": ["secrets.request"]}})
    assert bad.is_error and bad.error["code"] == "approval_required", bad.data
    assert world.call(key, "agent.save_draft", {"spec": {"name": ""}}).error["code"] == "invalid"
    assert world.call(key, "agent.save_draft", {"spec": {**SPEC, "instructions": "x\u0000y"}}).error["code"] == "invalid"
    archived = world.call(key, "agent.archive", {"agent_id": agent_id})
    assert archived.data["archived"] is True
    assert world.call(key, "agent.archive", {"agent_id": agent_id, "archived": False}).data["archived"] is False


def test_workflow_save_publish_run_cancel_and_schedule(world):
    _, h = world.tenant_of()
    key = manage_key(world, h)
    agent_id = world.publish_agent(h)
    spec = {"name": "Flow", "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"],
                                              "additionalProperties": False},
            "start": "ask", "steps": [{"id": "ask", "type": "agent", "agent_id": agent_id, "input": {"p": {"$ref": "input.q"}}}]}
    saved = world.call(key, "workflow.save_draft", {"spec": spec})
    assert not saved.is_error, saved.data
    wid = saved.data["id"]
    assert world.call(key, "workflow.run.start", {"workflow_id": wid, "input": {"q": "x"}}).error["code"] == "conflict"
    published = world.call(key, "workflow.publish", {"workflow_id": wid})
    assert not published.is_error and published.data["version"] == 1, published.data
    assert world.call(key, "workflow.publish", {"workflow_id": wid}).error["code"] == "conflict"  # unchanged draft

    run = world.call(key, "workflow.run.start", {"workflow_id": wid, "input": {"q": "hi"}})
    assert not run.is_error and run.data["kind"] == "workflow" and run.data["status"] == "queued", run.data
    (options, _args), = world.fake.calls
    assert options["workflow_name"] == "eap.workflow_run" and options["workflow_id"] == run.data["run_id"]
    assert world.call(key, "workflow.run.start", {"workflow_id": wid, "input": {"q": 1}}).error["code"] == "invalid"
    cancelled = world.call(key, "workflow.run.cancel", {"run_id": run.data["run_id"]})
    assert not cancelled.is_error, cancelled.data
    assert world.call(key, "workflow.run.get", {"run_id": run.data["run_id"]}).data["status"] in ("failed", "needs_attention")

    sched = world.call(key, "workflow.schedule.set", {"workflow_id": wid, "name": "nightly", "cron": "0 3 * * *",
                                                      "input": {"q": "scheduled"}})
    assert not sched.is_error, sched.data
    listed = world.call(key, "workflow.schedule.list", {"workflow_id": wid})
    assert [s["name"] for s in listed.data["items"]] == ["nightly"]
    paused = world.call(key, "workflow.schedule.set", {"workflow_id": wid, "schedule_id": sched.data["id"], "active": False})
    assert paused.data["active"] is False, paused.data
    assert world.call(key, "workflow.schedule.set", {"workflow_id": wid, "name": "bad", "cron": "nope"}).is_error


def test_kb_connection_and_source_tools(world, monkeypatch):
    monkeypatch.setenv("EAP_ALLOWED_EMBEDDING_MODELS", "test:*")
    register_adapter("product_catalog", "fake", lambda: FakeCatalogAdapter())
    _, h = world.tenant_of()
    key = manage_key(world, h)
    kb = world.call(key, "kb.create", {"name": "docs", "embedding_model": "test:hash"})
    assert not kb.is_error, kb.data
    assert world.call(key, "kb.create", {"name": "docs", "embedding_model": "test:hash"}).error["code"] == "conflict"
    assert world.call(key, "kb.create", {"name": "x", "embedding_model": "nope:model"}).is_error
    assert [k["id"] for k in world.call(key, "kb.list").data["items"]] == [kb.data["id"]]

    # A credential value is refused and never echoed; a reference is accepted and shown as a reference.
    value = world.call(key, "settings.connection.create", {
        "name": "ti", "config": {"provider": "fake"}, "kind": "api_token", "credential_refs": {"token": "sentinel-VALUE-0001"}})
    assert value.is_error and value.error["code"] == "secret_ref_required" and "sentinel" not in str(value.data)
    conn = world.call(key, "settings.connection.create", {
        "name": "ti", "config": {"provider": "fake"}, "kind": "api_token", "base_url": "https://example.com",
        "credential_refs": {"token": "secret:ti-token"}})
    assert not conn.is_error, conn.data
    assert conn.data["credential_refs"] == {"token": "secret:ti-token"}
    listed = world.call(key, "settings.connection.list")
    assert listed.data["items"][0]["credential_refs"] == {"token": "secret:ti-token"}

    src = world.call(key, "kb.source.create", {"kb_id": kb.data["id"], "connection_id": conn.data["id"], "name": "parts",
                                               "provider": "fake", "kind": "product_catalog", "config": {}})
    assert not src.is_error, src.data
    sync = world.call(key, "kb.source.sync", {"kb_id": kb.data["id"], "source_id": src.data["id"], "mode": "full"})
    assert not sync.is_error, sync.data
    (options, _args), = [c for c in world.fake.calls if c[0]["workflow_name"] == "eap.kb_source_sync"]
    assert options["workflow_id"] == sync.data["run_id"]
    syncs = world.call(key, "kb.source.syncs", {"kb_id": kb.data["id"], "source_id": src.data["id"]})
    assert [s["run_id"] for s in syncs.data["items"]] == [sync.data["run_id"]]


def test_mcp_server_install_never_takes_a_secret_value_and_runtime_controls_a_managed_pod(world):
    from eap_run_store.secrets import SecretStore
    from eap_secrets import load_keyring

    tenant, h = world.tenant_of()
    key = manage_key(world, h)
    entry = make_entry()
    world.seed(entry)
    assert world.call(key, "mcp.server.install", {"catalog_name": "io.example/none"}).error["code"] == "not_found"
    refused = world.call(key, "mcp.server.install", {"catalog_name": entry.name, "env": {"API_TOKEN": "tok-VALUE-123"}})
    assert refused.is_error and refused.error["code"] == "secret_value_refused" and "tok-VALUE" not in str(refused.data)
    assert world.sql("SELECT count(*) FROM mcp_servers WHERE tenant_id = :t", t=tenant)[0][0] == 0
    # A server installs once the secret exists in the store (Studio's job) and is passed as a reference.
    SecretStore(world.app.state.database_engine, load_keyring()).put(tenant, "tok", "tok-VALUE-123", created_by="studio")
    ok = world.call(key, "mcp.server.install", {"catalog_name": entry.name, "env": {"API_TOKEN": "secret:tok", "REGION": "eu"}})
    assert not ok.is_error, ok.data
    server_id = ok.data["id"]
    assert "tok-VALUE" not in str(ok.data)
    assert world.call(key, "mcp.server.list").data["items"][0]["id"] == server_id
    stopped = world.call(key, "mcp.server.runtime", {"server_id": server_id, "action": "stop"})
    assert not stopped.is_error, stopped.data
    assert world.runtime.calls[-1][0] == "stop"
    restarted = world.call(key, "mcp.server.runtime", {"server_id": server_id, "action": "restart"})
    assert not restarted.is_error, restarted.data


def test_another_tenants_manage_key_changes_nothing_of_mine(world):
    _, a = world.tenant_of()
    _, b = world.tenant_of("mallory")
    key_b = manage_key(world, b)
    agent_id = world.publish_agent(a)
    wid = world.publish_workflow(a, agent_id)
    run_id = str(uuid.uuid4())
    with world.rest() as c:
        assert c.post(f"/workflows/{wid}/runs", json={"input": {"q": "hi"}, "run_id": run_id}, headers=a).status_code == 202
    attempts = [
        ("agent.save_draft", {"agent_id": agent_id, "spec": {**SPEC, "name": "Hijacked"}}),
        ("agent.publish", {"agent_id": agent_id}), ("agent.archive", {"agent_id": agent_id}),
        ("agent.chat", {"agent_id": agent_id, "message": "hi"}),
        ("workflow.publish", {"workflow_id": wid}), ("workflow.run.start", {"workflow_id": wid, "input": {"q": "x"}}),
        ("workflow.run.cancel", {"run_id": run_id}),
        ("workflow.schedule.set", {"workflow_id": wid, "name": "n", "cron": "0 3 * * *"}),
        ("approvals.decide", {"run_id": run_id, "kind": "step", "key": "ask", "decision": "approve"}),
        ("kb.source.sync", {"kb_id": str(uuid.uuid4()), "source_id": str(uuid.uuid4()), "mode": "full"}),
        ("mcp.server.runtime", {"server_id": str(uuid.uuid4()), "action": "stop"}),
    ]
    for tool, args in attempts:
        reply = world.call(key_b, tool, args)
        assert reply.is_error and reply.error["code"].endswith("not_found"), (tool, reply.data)
    with world.rest() as c:
        agent = c.get(f"/agents/{agent_id}", headers=a).json()
        assert agent["name"] == "Helper" and agent["archived"] is False
        assert [v["version"] for v in c.get(f"/agents/{agent_id}/versions", headers=a).json()] == [1]
        assert c.get(f"/runs/{run_id}", headers=a).json()["status"] == "queued"
    assert len(world.fake.calls) == 1  # only my own run was ever enqueued


def test_an_allowlisted_manage_key_cannot_create_or_touch_other_resources(world):
    _, h = world.tenant_of()
    mine, other = world.publish_agent(h), world.publish_agent(h)
    key = manage_key(world, h, agent_ids=[mine])
    assert world.call(key, "agent.save_draft", {"spec": SPEC}).error["code"] == "forbidden"
    assert world.call(key, "agent.publish", {"agent_id": other}).error["code"] == "not_found"
    assert world.call(key, "agent.publish", {"agent_id": mine}).error["code"] == "conflict"  # allowed in: nothing new to publish
    assert world.call(key, "workflow.save_draft", {"spec": {}}).error["code"] == "forbidden"


def test_agent_chat_and_approvals_decide_through_a_real_worker(live_world):
    w = live_world
    _tenant, h = w.tenant_of()
    key = manage_key(w, h)
    spec = {"name": "Ticketer", "instructions": "Open a ticket for the user's problem, then confirm it.",
            "model": {"provider": "test", "name": "ticket"}, "tools": ["tickets.create"],
            "approvals": {"tools": ["tickets.create"]}}
    agent_id = w.call(key, "agent.save_draft", {"spec": spec}).data["id"]
    assert not w.call(key, "agent.publish", {"agent_id": agent_id}).is_error

    turn = w.call(key, "agent.chat", {"agent_id": agent_id, "message": "The printer is on fire", "wait_seconds": 90})
    assert not turn.is_error, turn.data
    assert turn.data["done"] is False and turn.data["display_status"] == "waiting_approval", turn.data
    (approval,) = turn.data["pending_approvals"]
    assert approval["kind"] == "tool" and approval["tool_name"] == "tickets__create"
    listed = w.call(key, "approvals.list")
    assert [i["approval_key"] for i in listed.data["items"]] == [approval["approval_key"]]

    decided = w.call(key, "approvals.decide", {"run_id": turn.data["run_id"], "kind": "tool", "key": approval["approval_key"],
                                               "decision": "approve", "comment": "ok"})
    assert not decided.is_error and decided.data["decision"] == "approved", decided.data
    again = w.call(key, "approvals.decide", {"run_id": turn.data["run_id"], "kind": "tool", "key": approval["approval_key"],
                                             "decision": "deny"})
    assert again.is_error and again.error["code"] == "conflict"

    deadline = time.monotonic() + 90
    while True:  # an agent run: read it through the report resource (workflow.run.* is for workflow runs only)
        report = asyncio.run(read_resource(w, key, f"pydanticos://runs/{turn.data['run_id']}/report"))
        if report["status"] in ("completed", "failed"):
            break
        assert time.monotonic() < deadline, report
        time.sleep(0.5)
    assert report["status"] == "completed" and "ticket" in report["output"].lower(), report
    assert w.call(key, "workflow.run.get", {"run_id": turn.data["run_id"]}).error["code"] == "not_found"
