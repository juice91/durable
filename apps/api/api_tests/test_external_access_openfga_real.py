"""External access over a REAL OpenFGA (CI Heavy): the relations `can_manage_external_access` and
`can_read_external_access` as the bootstrapped model answers them for `zitadel:<sub>` people, and a `run` key, which
the engine abstains on, bounded by its scope and allowlist.

Needs Postgres and an OpenFGA: skipped unless `EAP_TEST_OPENFGA_URL` or `EAP_TEST_OPENFGA_DOCKER=1`."""

from __future__ import annotations

import asyncio
import dataclasses

import httpx
import httpx2
import pytest
from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
from a2a.types.a2a_pb2 import (
    CancelTaskRequest,
    GetExtendedAgentCardRequest,
    ListTasksRequest,
    Message,
    Part,
    Role,
    SendMessageConfiguration,
    SendMessageRequest,
    TaskState,
)
from a2a.utils.errors import InvalidParamsError
from eap_iam import RequestView
from google.protobuf import json_format
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import text
from sqlalchemy.orm import Session

from eap_api.deps import iam_of
from eap_api.launcher import get_run_launcher
from eap_api.service_deps.external_access import external_run_service_of
from eap_api.services.errors import NotFound

from .run_helpers import requires_pg
from .test_agent_grants_api import world as _grants_world  # noqa: F401
from .test_iam_access_openfga_real import _env, server, world  # noqa: F401 - fixtures: a real OpenFGA behind the app
from .test_iam_p2b_wave1 import API, World
from .test_iam_p2b_wave1 import world as _wave1_world  # noqa: F401

pytestmark = requires_pg

BASE = f"{API}/agent-access"


@pytest.fixture(autouse=True)
def _external(monkeypatch):
    monkeypatch.setenv("EAP_EXTERNAL_BASE_URL", "https://durable.example.test")
    monkeypatch.setenv("EAP_AGENT_GRANTS_ENFORCE", "1")


@pytest.fixture
def external(world: World):  # noqa: F811
    # `external_run_service_of` has no Request: it reads the launcher from app.state, past the world's override.
    world.app.state.run_launcher = world.app.dependency_overrides[get_run_launcher]()
    yield world
    with world.app.state.database_engine.begin() as conn:
        for table in ("external_runs", "external_endpoints", "api_keys", "workflow_exports", "external_clients"):
            conn.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": world.tenant})


def publish(world: World, name: str) -> str:
    spec = {"name": name, "input_schema": {"type": "object"}, "start": "now",
            "steps": [{"id": "now", "type": "tool", "tool_id": "clock.now", "input": {}}]}
    r = world.client.post(f"{API}/workflows", json={"spec": spec}, headers=world.h("ann"))
    assert r.status_code == 201, r.text
    wid = r.json()["id"]
    assert world.client.post(f"{API}/workflows/{wid}/versions", headers=world.h("ann")).status_code == 201
    return wid


def test_admins_manage_editors_read_and_a_run_key_stays_inside_its_allowlist(external: World, monkeypatch):
    world = external
    one, two = publish(world, "one"), publish(world, "two")
    export_body = lambda name, wid: {"name": name, "title": name, "workflow_id": wid, "protocols": ["mcp"]}  # noqa: E731
    for user in ("eve", "vic"):
        r = world.client.post(f"{BASE}/clients", json={"name": "x"}, headers=world.h(user))
        assert r.status_code == 403, (user, r.text)
        r = world.client.post(f"{BASE}/exports", json=export_body("x", one), headers=world.h(user))
        assert r.status_code == 403, (user, r.text)
    client = world.client.post(f"{BASE}/clients", json={"name": "claude"}, headers=world.h("ann"))
    assert client.status_code == 201, client.text
    key = world.client.post(f"{BASE}/clients/{client.json()['id']}/keys", json={"name": "k", "workflow_ids": [one]},
                            headers=world.h("ann"))
    assert key.status_code == 201, key.text
    exports = {}
    for name, wid in (("first", one), ("second", two)):
        made = world.client.post(f"{BASE}/exports", json=export_body(name, wid), headers=world.h("ann"))
        assert made.status_code == 201, made.text
        body = {"approved_hash": made.json()["approval_hash"], "confirm": True}
        url = f"{BASE}/exports/{made.json()['id']}/enable"
        assert world.client.post(url, json=body, headers=world.h("eve")).status_code == 403
        on = world.client.post(url, json=body, headers=world.h("ann"))
        assert on.status_code == 200, on.text
        exports[name] = on.json()
    for path in ("/clients", "/exports"):
        assert world.client.get(f"{BASE}{path}", headers=world.h("eve")).status_code == 200
        assert world.client.get(f"{BASE}{path}", headers=world.h("vic")).status_code == 403
    assert world.client.get(BASE, headers=world.h("eve")).json()["can_manage"] is False
    assert world.client.get(BASE, headers=world.h("ann")).json()["can_manage"] is True

    # The external side: the key is decided by its constraints; the real engine abstains.
    monkeypatch.setenv("EAP_EXTERNAL_ACCESS_ENABLED", "true")
    chain = iam_of(world.app).chain.chain  # the real chain under the test's ZITADEL wrapper (a key is not a person)
    principal = asyncio.run(chain.authenticate(RequestView(
        headers={"authorization": f"Bearer {key.json()['key']}"}, accepts=frozenset({"api_key"}), action="run.external")))
    with Session(world.app.state.database_engine) as session:
        service = external_run_service_of(world.app, session)
        assert [e["name"] for e in service.exports(principal, "mcp")] == ["first"]
        started = service.start(principal, "mcp", "first", {}, idempotency_key="k")
        assert started["state"] == "queued"
        with pytest.raises(NotFound):
            service.start(principal, "mcp", "second", {}, idempotency_key="k")
    assert len(world.fake.calls) == 1  # the allowed start was handed off, the refused one was not


def test_the_mcp_surface_over_the_real_engine(external: World, monkeypatch):
    """The official client on `/api/v1/external/mcp`: the real engine abstains for the `run` key, whose scope and
    allowlist decide; the control endpoint refuses it."""
    world = external
    one, two = publish(world, "one"), publish(world, "two")
    client = world.client.post(f"{BASE}/clients", json={"name": "claude"}, headers=world.h("ann")).json()
    key = world.client.post(f"{BASE}/clients/{client['id']}/keys", json={"name": "k", "workflow_ids": [one]},
                            headers=world.h("ann")).json()["key"]
    for name, wid in (("first", one), ("second", two)):
        made = world.client.post(
            f"{BASE}/exports", json={"name": name, "title": name, "workflow_id": wid, "protocols": ["mcp"]},
            headers=world.h("ann")).json()
        on = world.client.post(f"{BASE}/exports/{made['id']}/enable",
                               json={"approved_hash": made["approval_hash"], "confirm": True}, headers=world.h("ann"))
        assert on.status_code == 200, on.text
    monkeypatch.setenv("EAP_EXTERNAL_ACCESS_ENABLED", "true")
    world.fake.cancel_workflow = lambda *args, **kwargs: None  # what a DBOS client also does
    iam = iam_of(world.app)
    # The people of this world sign in through a ZITADEL stand-in; a key is not a person: the real chain under it.
    world.app.state.iam = dataclasses.replace(iam, chain=iam.chain.chain)

    async def go() -> None:
        headers = {"Authorization": f"Bearer {key}"}
        transport = httpx2.ASGITransport(app=world.app)
        # The app's own lifespan is not running under this world's client: run the surface's here.
        inner = world.app.state.external_mcp_app
        async with inner.router.lifespan_context(inner):
            async with httpx2.AsyncClient(transport=transport, base_url="http://testserver", headers=headers) as http:
                refused = await http.post("/api/v1/mcp", json={})
                assert refused.status_code == 403, refused.text
                async with Client(streamable_http_client("http://testserver/api/v1/external/mcp", http_client=http)) as mcp:
                    names = [t.name for t in (await mcp.list_tools()).tools]
                    assert "run_first" in names and "run_second" not in names
                    started = await mcp.call_tool("run_first", {"input": {}, "idempotency_key": "k", "wait_seconds": 0})
                    assert not started.is_error, started.structured_content
                    run_id = started.structured_content["run_id"]
                    other = await mcp.call_tool("run_second", {"input": {}, "idempotency_key": "k"})
                    assert other.structured_content["error"]["code"] == "export_not_found"
                    cancelled = await mcp.call_tool("runs.cancel", {"run_id": run_id})
                    assert not cancelled.is_error, cancelled.structured_content
                    assert cancelled.structured_content["state"] == "cancelled"

    asyncio.run(go())
    assert len(world.fake.calls) == 1


def test_the_a2a_surface_over_the_real_engine(external: World, monkeypatch):
    """Only an admin makes the endpoint (editors read it); then the official A2A client on it: the real engine abstains
    for the `run` key, whose scope and allowlist decide."""
    world = external
    one, two = publish(world, "one"), publish(world, "two")
    endpoint = f"{BASE}/a2a-endpoint"
    body = {"card_name": "Acme", "card_description": "Workflows.", "enabled": True}
    for user in ("eve", "vic"):
        assert world.client.put(endpoint, json=body, headers=world.h(user)).status_code == 403, user
        assert world.client.post(f"{endpoint}/rotate", headers=world.h(user)).status_code == 403, user
    assert world.client.get(endpoint, headers=world.h("vic")).status_code == 403
    made = world.client.put(endpoint, json=body, headers=world.h("ann"))
    assert made.status_code == 200, made.text
    assert world.client.get(endpoint, headers=world.h("eve")).json() == made.json()
    client = world.client.post(f"{BASE}/clients", json={"name": "graph"}, headers=world.h("ann")).json()
    key = world.client.post(f"{BASE}/clients/{client['id']}/keys", json={"name": "k", "workflow_ids": [one]},
                            headers=world.h("ann")).json()["key"]
    for name, wid in (("first", one), ("second", two)):
        export = world.client.post(
            f"{BASE}/exports", json={"name": name, "title": name, "workflow_id": wid, "protocols": ["a2a"]},
            headers=world.h("ann")).json()
        on = world.client.post(f"{BASE}/exports/{export['id']}/enable",
                               json={"approved_hash": export["approval_hash"], "confirm": True}, headers=world.h("ann"))
        assert on.status_code == 200, on.text
    monkeypatch.setenv("EAP_EXTERNAL_ACCESS_ENABLED", "true")
    world.fake.cancel_workflow = lambda *args, **kwargs: None  # what a DBOS client also does
    iam = iam_of(world.app)
    # The people of this world sign in through a ZITADEL stand-in; a key is not a person: the real chain under it.
    world.app.state.iam = dataclasses.replace(iam, chain=iam.chain.chain)

    async def go() -> None:
        headers = {"Authorization": f"Bearer {key}"}
        transport = httpx.ASGITransport(app=world.app)
        async with httpx.AsyncClient(transport=transport, headers=headers, timeout=90) as http:
            card = await A2ACardResolver(http, made.json()["endpoint"]["url"]).get_agent_card()
            a2a = ClientFactory(ClientConfig(httpx_client=http, streaming=False)).create(card)
            extended = await a2a.get_extended_agent_card(GetExtendedAgentCardRequest())
            assert [skill.id for skill in extended.skills] == ["first"]

            def send(skill: str, message_id: str) -> SendMessageRequest:
                part = json_format.ParseDict({"data": {"skill": skill, "input": {}}}, Part())
                message = Message(message_id=message_id, role=Role.ROLE_USER, parts=[part])
                return SendMessageRequest(
                    message=message, configuration=SendMessageConfiguration(return_immediately=True))

            replies = [reply async for reply in a2a.send_message(send("first", "m-1"))]
            task = replies[0].task
            assert task.status.state == TaskState.TASK_STATE_SUBMITTED
            with pytest.raises(InvalidParamsError):
                async for _ in a2a.send_message(send("second", "m-2")):
                    pass
            assert [t.id for t in (await a2a.list_tasks(ListTasksRequest())).tasks] == [task.id]
            cancelled = await a2a.cancel_task(CancelTaskRequest(id=task.id))
            assert cancelled.status.state == TaskState.TASK_STATE_CANCELED

    asyncio.run(go())
    assert len(world.fake.calls) == 1
