"""Fixtures for the MCP control endpoint's tests: the real app in a uvicorn thread (a fake DBOS client
behind the launcher), a REST client for set-up, and a FastMCP client with a tenant API key."""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx2 as httpx
import pytest
import uvicorn
from sqlalchemy import text

from eap_api.app import create_app
from eap_api.launcher import DBOSRunLauncher, LauncherSettings, get_run_launcher

from .conftest import get_auth_header
from .run_helpers import PG_URL, cleanup_tenant, hard_timeout, requires_pg
from .test_workflow_runs_api import FakeClient

AGENT_SPEC = {"name": "Helper", "instructions": "Help.", "model": {"provider": "test", "name": "echo"}}


class McpFake(FakeClient):
    """FakeClient that also records DBOS cancels (workflow.run.cancel)."""

    def __init__(self) -> None:
        super().__init__()
        self.cancels: list[tuple[str, bool]] = []

    def cancel_workflow(self, workflow_id: str, *, cancel_children: bool = False, shard_key: str | None = None) -> None:
        self.cancels.append((workflow_id, cancel_children))


@dataclass
class Reply:
    is_error: bool
    data: Any

    @property
    def error(self) -> dict[str, Any]:
        assert self.is_error, self.data
        return self.data["error"]


@dataclass
class World:
    app: Any
    base: str
    fake: FakeClient
    tenants: list[str] = field(default_factory=list)
    seeded: list[str] = field(default_factory=list)
    runtime: Any = None

    def seed(self, *entries: Any) -> None:
        from eap_run_store.mcp_catalog import CatalogStore

        CatalogStore(self.app.state.database_engine).upsert(list(entries))
        self.seeded.extend(e.name for e in entries)

    @property
    def url(self) -> str:
        return self.base + "/api/v1/mcp"

    def tenant_of(self, user: str = "alice") -> tuple[str, dict[str, str]]:
        t = f"t{uuid.uuid4().hex[:10]}"
        self.tenants.append(t)
        return t, get_auth_header(t, user)

    def rest(self) -> httpx.Client:
        return httpx.Client(base_url=self.base + "/api/v1", timeout=30)

    def make_key(self, headers: dict[str, str], *, scopes: list[str] | None = None, **body: Any) -> str:
        with self.rest() as c:
            r = c.post("/api-keys", json={"name": "mcp", "scopes": scopes or ["agui:run"], **body}, headers=headers)
        assert r.status_code == 201, r.text
        return r.json()["key"]

    def publish_agent(self, headers: dict[str, str], spec: dict[str, Any] | None = None) -> str:
        with self.rest() as c:
            r = c.post("/agents", json={"spec": spec or AGENT_SPEC}, headers=headers)
            assert r.status_code == 201, r.text
            agent_id = r.json()["id"]
            assert c.post(f"/agents/{agent_id}/versions", headers=headers).status_code == 201
        return agent_id

    def publish_workflow(self, headers: dict[str, str], agent_id: str) -> str:
        spec = {
            "name": "Echo flow",
            "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"],
                             "additionalProperties": False},
            "start": "ask",
            "steps": [{"id": "ask", "type": "agent", "agent_id": agent_id, "input": {"p": {"$ref": "input.q"}}}],
        }
        with self.rest() as c:
            r = c.post("/workflows", json={"spec": spec}, headers=headers)
            assert r.status_code == 201, r.text
            wid = r.json()["id"]
            assert c.post(f"/workflows/{wid}/versions", headers=headers).status_code == 201, "publish"
        return wid

    def call(self, key: str | None, tool: str, args: dict[str, Any] | None = None) -> Reply:
        return asyncio.run(self._call(key, tool, args or {}))

    async def _call(self, key: str | None, tool: str, args: dict[str, Any]) -> Reply:
        from fastmcp import Client
        from fastmcp.client.transports import StreamableHttpTransport

        headers = {"Authorization": f"Bearer {key}"} if key else {}
        async with Client(StreamableHttpTransport(self.url, headers=headers)) as client:
            result = await client.call_tool(tool, args, raise_on_error=False)
        data = result.structured_content
        if data is None:
            data = json.loads(result.content[0].text)
        return Reply(bool(result.is_error), data)

    def session(self, key: str | None):
        """An async context manager for a client (list tools, read resources)."""
        from fastmcp import Client
        from fastmcp.client.transports import StreamableHttpTransport

        headers = {"Authorization": f"Bearer {key}"} if key else {}
        return Client(StreamableHttpTransport(self.url, headers=headers))

    def sql(self, query: str, **params: Any) -> list[Any]:
        with self.app.state.database_engine.begin() as conn:
            result = conn.execute(text(query), params)
            return list(result) if result.returns_rows else []


@pytest.fixture(autouse=True)
def secrets_key(monkeypatch: pytest.MonkeyPatch) -> str:
    """The platform secrets key (connection checks and managed servers need it)."""
    from eap_secrets import KEY_ENV, generate_key

    key = generate_key()
    monkeypatch.setenv(KEY_ENV, key)
    return key


def _serve(real_launcher: bool = False):
    """Start the app in a uvicorn thread; yield the World; stop it and remove the tenants' rows."""
    from eap_api.mcp_servers.managed import get_mcp_runtime
    from eap_api.routes.mcp_servers import get_outbound_policy
    from eap_secrets.outbound import OutboundPolicy

    from .mcp_runtime_helpers import FakeRuntime, _public

    app = create_app(database_url=PG_URL)
    fake = McpFake()
    if not real_launcher:
        app.dependency_overrides[get_run_launcher] = lambda: DBOSRunLauncher(
            LauncherSettings("postgresql://unused/x", "dbos", "m1-test"), client=fake
        )
    runtime = FakeRuntime()
    app.dependency_overrides[get_mcp_runtime] = lambda: runtime
    app.dependency_overrides[get_outbound_policy] = lambda: OutboundPolicy(resolver=_public)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("test server did not start")
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    w = World(app=app, base=f"http://127.0.0.1:{port}", fake=fake)
    w.runtime = runtime
    with hard_timeout(150):
        yield w
    server.should_exit = True
    thread.join(timeout=10)
    engine = app.state.database_engine
    for name in w.seeded:
        w.sql("DELETE FROM mcp_catalog_entries WHERE name = :n", n=name)
    for t in w.tenants:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM workflow_schedules WHERE tenant_id = :t"), {"t": t})
            conn.execute(text("DELETE FROM workflow_versions WHERE tenant_id = :t"), {"t": t})
            conn.execute(text("DELETE FROM workflows WHERE tenant_id = :t"), {"t": t})
            conn.execute(text("DELETE FROM api_keys WHERE tenant_id = :t"), {"t": t})
            conn.execute(text("DELETE FROM pending_approvals WHERE tenant_id = :t"), {"t": t})
            for table in ("kb_source_syncs", "kb_sources", "settings_connections", "knowledge_bases", "mcp_servers"):
                conn.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": t})
        cleanup_tenant(engine, t)
    engine.dispose()


@pytest.fixture
def world():
    yield from _serve()


@pytest.fixture
def live_world(worker_env, monkeypatch):
    """The real app on the real DBOS queue with a real `python -m eap_worker` (scripted `test:` models)."""
    for key, value in worker_env.items():
        monkeypatch.setenv(key, value)
    yield from _serve(real_launcher=True)
