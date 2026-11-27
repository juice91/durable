"""Webhook trigger tools on the MCP control endpoint: references only, requesting is not enabling, scopes, tenants."""

from __future__ import annotations

import asyncio
import json
import uuid

import httpx2
import pytest

from .mcp_control_helpers import secrets_key, world  # noqa: F401 - fixtures
from .run_helpers import requires_pg

pytestmark = requires_pg

SENTINEL = "mcp-sentinel-signing-secret-91c"
ACTIONS = {"start": {"payload_schema": {"type": "object"}, "input_map": {"repo": {"$pointer": "/repo"}}}}
WEBHOOK_TOOLS = {
    "webhook.trigger.list", "webhook.trigger.get", "webhook.trigger.events", "webhook.delivery.list", "webhook.delivery.get",
    "webhook.trigger.create", "webhook.trigger.update", "webhook.trigger.request_enable", "webhook.trigger.disable",
    "webhook.trigger.rotate", "webhook.trigger.archive",
}


@pytest.fixture(autouse=True)
def _base_url(monkeypatch):
    monkeypatch.setenv("EAP_WEBHOOK_BASE_URL", "https://hooks.example.test")


def refused(world, key, tool, args) -> bool:
    """True when the endpoint refused the call, including a refusal at argument validation (plain text, not a tool error object)."""
    async def call():
        from fastmcp import Client
        from fastmcp.client.transports import StreamableHttpTransport

        async with Client(StreamableHttpTransport(world.url, headers={"Authorization": f"Bearer {key}"})) as client:
            return (await client.call_tool(tool, args, raise_on_error=False)).is_error

    return asyncio.run(call())


def workflow(world, headers):
    spec = {
        "name": "Hook", "input_schema": {"type": "object", "properties": {"repo": {"type": "string"}}, "required": ["repo"]},
        "start": "now", "steps": [{"id": "now", "type": "tool", "tool_id": "clock.now", "input": {}}],
    }
    with world.rest() as c:
        wid = c.post("/workflows", json={"spec": spec}, headers=headers).json()["id"]
        assert c.post(f"/workflows/{wid}/versions", headers=headers).status_code == 201
    return wid


def put_secret(world, tenant, name="hook", value=SENTINEL):
    from eap_run_store.secrets import SecretStore
    from eap_secrets import load_keyring

    SecretStore(world.app.state.database_engine, load_keyring()).put(tenant, name, value, "test")
    return f"secret:{name}"


def create_args(wid, ref, **over):
    return {"name": f"t{uuid.uuid4().hex[:5]}", "workflow_id": wid, "version": 1, "preset": "github", "secrets": [ref],
            "actions": ACTIONS} | over


def test_the_tools_are_listed_and_none_takes_or_returns_a_secret_value(world, secrets_key):  # noqa: F811
    _t, h = world.tenant_of()
    key = world.make_key(h, scopes=["agui:run", "manage"])

    async def tools():
        async with world.session(key) as c:
            return {t.name: t for t in await c.list_tools()}

    got = asyncio.run(tools())
    assert WEBHOOK_TOOLS <= set(got)
    schema = json.dumps(got["webhook.trigger.create"].inputSchema)
    assert "generate" not in schema and "value" not in schema and "enable" not in {n.split(".")[-1] for n in got if n.startswith("webhook.")}
    assert not [n for n in got if n.startswith("webhook.") and n.endswith(".enable")]


def test_create_request_enable_and_never_enable(world, secrets_key):  # noqa: F811
    tenant, h = world.tenant_of()
    wid = workflow(world, h)
    ref = put_secret(world, tenant)
    key = world.make_key(h, scopes=["agui:run", "manage"])
    created = world.call(key, "webhook.trigger.create", create_args(wid, ref))
    assert not created.is_error, created.data
    trig = created.data
    assert trig["status"] == "disabled" and trig["secrets"][0]["ref"] == ref and SENTINEL not in json.dumps(created.data)
    assert "generated_secret" not in trig
    pending = world.call(key, "webhook.trigger.request_enable", {"trigger_id": trig["id"]})
    assert pending.data["status"] == "pending_enable"
    # approvals.decide does not take this kind, and nothing in MCP changes the status to enabled
    assert refused(world, key, "approvals.decide", {"run_id": str(uuid.uuid4()), "kind": "webhook_trigger", "key": trig["id"], "decision": "approve"})
    assert world.call(key, "webhook.trigger.get", {"trigger_id": trig["id"]}).data["status"] == "pending_enable"
    # a change returns it to disabled; disable and archive work; the audit rows name the actor
    changed = world.call(key, "webhook.trigger.update", {"trigger_id": trig["id"], "changes": {"limits": {"rate_per_minute": 5}}})
    assert changed.data["status"] == "pending_enable" or changed.data["status"] == "disabled"
    events = world.call(key, "webhook.trigger.events", {"trigger_id": trig["id"]}).data["items"]
    assert {"created", "enable_requested"} <= {e["kind"] for e in events}
    assert all(e["actor"].startswith("apikey:") for e in events)
    assert world.call(key, "webhook.trigger.disable", {"trigger_id": trig["id"]}).data["status"] == "disabled"
    assert world.call(key, "webhook.trigger.archive", {"trigger_id": trig["id"]}).data["status"] == "archived"
    listed = world.call(key, "webhook.trigger.list", {"include_archived": True}).data["items"]
    assert [i["id"] for i in listed] == [trig["id"]]


def test_a_value_instead_of_a_reference_is_refused_without_echo(world, secrets_key):  # noqa: F811
    tenant, h = world.tenant_of()
    wid = workflow(world, h)
    key = world.make_key(h, scopes=["agui:run", "manage"])
    as_value = world.call(key, "webhook.trigger.create", create_args(wid, SENTINEL))
    assert as_value.is_error and SENTINEL not in json.dumps(as_value.data)
    missing = world.call(key, "webhook.trigger.create", create_args(wid, "secret:nothere"))
    assert missing.is_error and missing.error["code"] == "secret_not_found"
    assert refused(world, key, "webhook.trigger.create", create_args(wid, "secret:x", generate=True))  # not an argument


def test_scopes_restricted_keys_and_tenants(world, secrets_key):  # noqa: F811
    tenant, h = world.tenant_of()
    other_tenant, other_h = world.tenant_of()
    wid = workflow(world, h)
    ref = put_secret(world, tenant)
    manage = world.make_key(h, scopes=["agui:run", "manage"])
    read_only = world.make_key(h, scopes=["mcp"])
    restricted = world.make_key(h, scopes=["agui:run", "manage"], workflow_ids=[wid], agent_ids=[])
    trig = world.call(manage, "webhook.trigger.create", create_args(wid, ref)).data
    assert world.call(read_only, "webhook.trigger.list").data["items"][0]["id"] == trig["id"]
    denied = world.call(read_only, "webhook.trigger.create", create_args(wid, ref))
    assert denied.is_error and denied.error["code"] == "scope_required"
    assert world.call(read_only, "webhook.trigger.request_enable", {"trigger_id": trig["id"]}).error["code"] == "scope_required"
    forbidden = world.call(restricted, "webhook.trigger.create", create_args(wid, ref))
    assert forbidden.is_error and forbidden.error["code"] == "forbidden"
    other = world.make_key(other_h, scopes=["agui:run", "manage"])
    assert world.call(other, "webhook.trigger.get", {"trigger_id": trig["id"]}).error["code"] == "webhook_trigger_not_found"
    assert world.call(other, "webhook.trigger.list").data["items"] == []
    assert world.call(other, "webhook.trigger.archive", {"trigger_id": trig["id"]}).is_error


def test_rotate_over_mcp_takes_references_only(world, secrets_key):  # noqa: F811
    tenant, h = world.tenant_of()
    wid = workflow(world, h)
    ref = put_secret(world, tenant)
    ref2 = put_secret(world, tenant, "hook2", "second-sentinel-xyz")
    key = world.make_key(h, scopes=["agui:run", "manage"])
    trig = world.call(key, "webhook.trigger.create", create_args(wid, ref)).data
    added = world.call(key, "webhook.trigger.rotate", {"trigger_id": trig["id"], "add": ref2})
    assert [s["ref"] for s in added.data["secrets"]] == [ref, ref2] and "second-sentinel-xyz" not in json.dumps(added.data)
    assert world.call(key, "webhook.trigger.rotate", {"trigger_id": trig["id"], "retire": ref}).data["secrets"][0]["ref"] == ref2
    assert refused(world, key, "webhook.trigger.rotate", {"trigger_id": trig["id"]})


def test_mcp_never_opens_an_enabled_trigger_to_another_secret(world, secrets_key):  # noqa: F811
    tenant, h = world.tenant_of()
    wid = workflow(world, h)
    ref = put_secret(world, tenant)
    ref2 = put_secret(world, tenant, "hook2", "second-sentinel-xyz")
    key = world.make_key(h, scopes=["agui:run", "manage"])
    trig = world.call(key, "webhook.trigger.create", create_args(wid, ref)).data
    r = httpx2.post(
        f"{world.base}/api/v1/webhook-triggers/{trig['id']}/enable",
        json={"config_hash": trig["config_hash"], "confirm": True}, headers=h,
    )
    assert r.status_code == 200, r.text
    assert refused(world, key, "webhook.trigger.rotate", {"trigger_id": trig["id"], "add": ref2})
    got = world.call(key, "webhook.trigger.get", {"trigger_id": trig["id"]}).data
    assert [s["ref"] for s in got["secrets"]] == [ref]
