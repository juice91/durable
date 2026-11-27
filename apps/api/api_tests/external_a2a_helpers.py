"""The A2A surface's test world: the external-access world, driven in-process by the official `a2a-sdk` client (the
protocol's reference client) over an ASGI transport, so the URLs it follows are the ones the Agent Card advertises."""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
from a2a.types.a2a_pb2 import (
    CancelTaskRequest,
    GetExtendedAgentCardRequest,
    GetTaskRequest,
    ListTasksRequest,
    Message,
    Part,
    Role,
    SendMessageConfiguration,
    SendMessageRequest,
    TaskState,
)
from fastapi.testclient import TestClient
from google.protobuf import json_format

from .external_world import BASE, PUBLIC, Live, World, external_app, external_env, remove_tenants  # noqa: F401

PREFIX = "/api/v1/external/a2a"
ENDPOINT = f"{BASE}/a2a-endpoint"
CARD_PATH = "/.well-known/agent-card.json"


def message(
    data: Any = None, *, text: str | None = None, message_id: str | None = None, task_id: str | None = None,
    context_id: str | None = None, metadata: dict | None = None, parts: list[Part] | None = None,
) -> Message:
    if parts is None:
        parts = []
        if data is not None:
            parts.append(json_format.ParseDict({"data": data, "mediaType": "application/json"}, Part()))
        if text is not None:
            parts.append(Part(text=text))
    built = Message(message_id=message_id or uuid.uuid4().hex, role=Role.ROLE_USER, parts=parts)
    if task_id:
        built.task_id = task_id
    if context_id:
        built.context_id = context_id
    if metadata:
        built.metadata.update(metadata)
    return built


class Agent:
    """One external agent: the SDK client of one key, on one base URL."""

    def __init__(self, client: Any, http: httpx.AsyncClient, card: Any) -> None:
        self.client, self.http, self.card = client, http, card

    async def send(self, msg: Message, *, wait: bool = False, metadata: dict | None = None, **configuration: Any) -> Any:
        request = SendMessageRequest(
            message=msg, configuration=SendMessageConfiguration(return_immediately=not wait, **configuration)
        )
        if metadata:
            request.metadata.update(metadata)
        replies = [reply async for reply in self.client.send_message(request)]
        assert len(replies) == 1 and replies[0].HasField("task"), replies
        return replies[0].task

    async def get(self, task_id: str, **kwargs: Any) -> Any:
        return await self.client.get_task(GetTaskRequest(id=task_id, **kwargs))

    async def list(self, **kwargs: Any) -> Any:
        return await self.client.list_tasks(ListTasksRequest(**kwargs))

    async def cancel(self, task_id: str) -> Any:
        return await self.client.cancel_task(CancelTaskRequest(id=task_id))

    async def extended(self) -> Any:
        return await self.client.get_extended_agent_card(GetExtendedAgentCardRequest())

    async def raw(self, payload: Any) -> Any:
        """A hand-made JSON-RPC request through the SDK's transport, which maps the error code to its exception."""
        return await self.client._transport._send_request(payload)


class A2AWorld(World):
    endpoint: dict[str, Any]

    def make_endpoint(self, headers: dict[str, str], **body: Any) -> dict[str, Any]:
        r = self.client.put(ENDPOINT, headers=headers, json={
            "card_name": "Acme automations", "card_description": "Runs the exported workflows.", "enabled": True,
        } | body)
        assert r.status_code == 200, r.text
        return r.json()["endpoint"]

    def a2a(self, name: str = "run_report", **over: Any) -> "Live":
        """A tenant with an A2A endpoint, one enabled export for A2A, one client and its key."""
        live = self.live(name=name, **({"protocols": ["a2a"]} | over))
        live.endpoint = self.make_endpoint(live.headers)  # type: ignore[attr-defined]
        return live

    def another_export(self, live: "Live", name: str, **over: Any) -> dict[str, Any]:
        export = self.export(live.headers, live.workflow, name=name, **({"protocols": ["a2a"]} | over))
        r = self.enable(live.headers, export)
        assert r.status_code == 200, r.text
        return r.json()

    def second_client(self, live: "Live") -> "Live":
        other = self.client_record(live.headers)
        key = self.key(live.headers, other["id"], [live.workflow])["key"]
        found = Live(self, live.tenant, live.headers, live.workflow, live.export, other, key)
        found.endpoint = live.endpoint  # type: ignore[attr-defined]
        return found

    @staticmethod
    def base(live: "Live", skill: str | None = None) -> str:
        url = f"{PUBLIC}{PREFIX}/{live.endpoint['endpoint_id']}"  # type: ignore[attr-defined]
        return url if skill is None else f"{url}/skills/{skill}"

    def http(self, key: str | None = None, headers: dict[str, str] | None = None) -> httpx.AsyncClient:
        sent = ({"Authorization": f"Bearer {key}"} if key else {}) | (headers or {})
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), headers=sent, timeout=90)

    @asynccontextmanager
    async def agent(self, live: "Live", key: str | None = None, skill: str | None = None,
                    headers: dict[str, str] | None = None, base: str | None = None):
        """The SDK path an external agent takes: resolve the public card, then build a client from it."""
        async with self.http(key or live.key, headers) as http:
            card = await A2ACardResolver(http, base or self.base(live, skill)).get_agent_card()
            client = ClientFactory(ClientConfig(httpx_client=http, streaming=False)).create(card)
            yield Agent(client, http, card)

    def run(self, live: "Live", body, **kwargs: Any) -> Any:
        """Run `body(agent)` as the agent of `live` and answer what it returns."""
        async def go() -> Any:
            async with self.agent(live, **kwargs) as agent:
                return await body(agent)

        return asyncio.run(go())

    def rpc(self, live: "Live", body: Any = None, key: str | None = "", method: str = "POST",
            headers: dict[str, str] | None = None, path: str | None = None):
        """One raw HTTP request to the JSON-RPC interface (what a refused or malformed caller sees)."""
        key = live.key if key == "" else key
        sent = {"Content-Type": "application/json", "A2A-Version": "1.0"}
        if key:
            sent["Authorization"] = f"Bearer {key}"
        sent |= headers or {}
        content = body if isinstance(body, (bytes, str)) else json.dumps(body if body is not None else list_request())
        path = path or f"{PREFIX}/{live.endpoint['endpoint_id']}/rpc"  # type: ignore[attr-defined]
        return self.client.request(method, path, content=content, headers={k: v for k, v in sent.items() if v})

    # -- what a worker would do to a run ------------------------------------------------------------------------

    def running(self, live: "Live", run_id: str) -> None:
        run_store, _events = self.app.state.run_stores
        run_store.mark_running(live.tenant, uuid.UUID(run_id))

    def finish(self, live: "Live", run_id: str, status: str = "completed", **kwargs: Any) -> None:
        run_store, _events = self.app.state.run_stores
        run_store.finish_with_end(live.tenant, uuid.UUID(run_id), status, **kwargs)

    def await_approval(self, live: "Live", run_id: str, step: str = "ok_gate", **data: Any) -> None:
        from datetime import datetime, timedelta, timezone

        deadline = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        self.app.state.run_stores[1].append_approval_waiting(
            live.tenant, uuid.UUID(run_id), "step", step,
            {"step_id": step, "prompt": "internal prompt", "deadline": deadline, "workflow_id": live.workflow} | data,
        )


def list_request(**params: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 1, "method": "ListTasks", "params": params}


def state(task: Any) -> str:
    return TaskState.Name(task.status.state).removeprefix("TASK_STATE_")


def as_dict(proto: Any) -> dict[str, Any]:
    return json_format.MessageToDict(proto)


@pytest.fixture
def a2a(external_env):  # noqa: F811
    app, fake = external_app(external_env)
    tenants: list[str] = []
    with TestClient(app) as client:
        yield A2AWorld(client, app, fake, tenants)
    remove_tenants(app, tenants)
