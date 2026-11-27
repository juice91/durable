"""The external MCP surface's test world: the external-access world served by uvicorn in a thread, and the official
`mcp` Python client (the protocol's reference client) talking to it with a `run` key."""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from .external_world import Live, World, external_app, external_env, remove_tenants  # noqa: F401

MCP_PATH = "/api/v1/external/mcp"
LEGACY, MODERN = "legacy", "2026-07-28"
MODES = (LEGACY, MODERN)


class Served(World):
    base: str = ""

    @property
    def url(self) -> str:
        return self.base + MCP_PATH

    @asynccontextmanager
    async def session(self, key: str | None, mode: str = MODERN, headers: dict[str, str] | None = None):
        sent = ({"Authorization": f"Bearer {key}"} if key else {}) | (headers or {})
        async with httpx2.AsyncClient(headers=sent, timeout=90) as http:
            async with Client(streamable_http_client(self.url, http_client=http), mode=mode) as client:
                yield client

    def tool(self, key: str, name: str, args: dict[str, Any] | None = None, mode: str = MODERN) -> "Reply":
        async def go() -> Reply:
            async with self.session(key, mode) as client:
                return await call(client, name, args)

        return asyncio.run(go())

    def tools(self, key: str, mode: str = MODERN) -> Any:
        async def go() -> Any:
            async with self.session(key, mode) as client:
                return await client.list_tools()

        return asyncio.run(go())

    def post(self, key: str | None, body: Any = None, method: str = "POST", headers: dict[str, str] | None = None):
        """One raw HTTP request to the endpoint (what a refused caller sees)."""
        sent = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        if key:
            sent["Authorization"] = f"Bearer {key}"
        content = body if isinstance(body, (bytes, str)) else json.dumps(body or ping())
        return self.client.request(method, MCP_PATH, content=content, headers=sent | (headers or {}))

    # -- what a worker would do to a run --------------------------------------------------------------------------------

    def running(self, live: Live, run_id: str) -> None:
        run_store, _events = self.app.state.run_stores
        run_store.mark_running(live.tenant, uuid.UUID(run_id))
        run_store.heartbeat(live.tenant, uuid.UUID(run_id))

    def finish(self, live: Live, run_id: str, status: str = "completed", **kwargs: Any) -> None:
        run_store, _events = self.app.state.run_stores
        run_store.finish_with_end(live.tenant, uuid.UUID(run_id), status, **kwargs)


class Reply:
    def __init__(self, is_error: bool, data: Any) -> None:
        self.is_error, self.data = is_error, data

    @property
    def ok(self) -> Any:
        assert not self.is_error, self.data
        return self.data

    @property
    def error(self) -> dict[str, Any]:
        assert self.is_error, self.data
        return self.data["error"]


async def call(client: Client, name: str, args: dict[str, Any] | None = None) -> Reply:
    result = await client.call_tool(name, args or {})
    data = result.structured_content
    if data is None:
        data = {"text": result.content[0].text}
    return Reply(bool(result.is_error), data)


def ping() -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 1, "method": "ping"}


@pytest.fixture
def served(external_env):  # noqa: F811
    app, fake = external_app(external_env)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("test server did not start")
        time.sleep(0.02)
    base = f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    tenants: list[str] = []
    with httpx2.Client(base_url=base, timeout=60) as client:
        found = Served(client, app, fake, tenants)
        found.base = base
        yield found
    server.should_exit = True
    thread.join(timeout=15)
    remove_tenants(app, tenants)
