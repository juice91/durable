"""Shared fixtures for the MCP catalog and managed-runtime API tests: a fake `McpRuntime` and a seeded mirror."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from eap_api.app import create_app
from eap_api.mcp_servers.managed import get_mcp_runtime
from eap_api.routes.mcp_servers import get_outbound_policy
from eap_mcp_runtime import RuntimeHandle, RuntimeSpec, RuntimeStatus
from eap_run_store.mcp_catalog import CatalogStore
from eap_schemas import CatalogEntry
from eap_secrets import KEY_ENV, OLD_KEYS_ENV, generate_key
from eap_secrets.outbound import DEV_ALLOW_HOSTS_ENV, OutboundPolicy

from .conftest import get_auth_header
from .run_helpers import PG_URL, requires_pg

__all__ = ["FakeRuntime", "requires_pg", "BASE", "CATALOG", "make_entry", "runtime_world"]

BASE = "/api/v1/mcp-servers"
CATALOG = "/api/v1/mcp-catalog"


class FakeRuntime:
    """An in-memory `McpRuntime`: records every call; objects exist from start to delete."""

    def __init__(self) -> None:
        self.specs: list[RuntimeSpec] = []
        self.calls: list[tuple[str, str]] = []
        self.objects: set[tuple[str, str]] = set()
        self.phase: dict[tuple[str, str], str] = {}
        self.log_lines: list[str] = []
        self.crash_after_delete = False
        self.fail_start = False
        self.fail_stop = False
        self.on_stop = None  # called inside stop(), to observe what the database says at that moment

    async def start(self, spec: RuntimeSpec) -> RuntimeHandle:
        from eap_mcp_runtime import handle_for

        if self.fail_start:
            raise RuntimeError("boom with token=abc123")
        h = handle_for(spec.tenant_id, spec.server_id)
        self.specs.append(spec)
        self.calls.append(("start", h.cr_name))
        self.objects.add((h.namespace, h.cr_name))
        self.phase[(h.namespace, h.cr_name)] = "starting"
        return h

    async def stop(self, handle: RuntimeHandle) -> None:
        self.calls.append(("stop", handle.cr_name))
        if self.on_stop is not None:
            self.on_stop()
        if self.fail_stop:
            raise RuntimeError("stop boom")
        if (handle.namespace, handle.cr_name) in self.objects:
            self.phase[(handle.namespace, handle.cr_name)] = "stopped"

    async def delete(self, handle: RuntimeHandle) -> None:
        self.calls.append(("delete", handle.cr_name))
        self.objects.discard((handle.namespace, handle.cr_name))  # a missing object is fine
        if self.crash_after_delete:
            self.crash_after_delete = False
            raise ConnectionError("the API server went away")

    async def status(self, handle: RuntimeHandle) -> RuntimeStatus:
        key = (handle.namespace, handle.cr_name)
        if key not in self.objects:
            return RuntimeStatus("absent", self.endpoint(handle), "not found")
        return RuntimeStatus(self.phase[key], self.endpoint(handle), "")  # type: ignore[arg-type]

    async def logs(self, handle: RuntimeHandle, tail: int = 200) -> list[str]:
        return self.log_lines[-tail:]

    def endpoint(self, handle: RuntimeHandle) -> str:
        return f"http://{handle.cr_name}-proxy.{handle.namespace}.svc.cluster.local:8080/mcp"

    def make_ready(self) -> None:
        for key in self.phase:
            self.phase[key] = "running"


def make_entry(**over: Any) -> CatalogEntry:
    base: dict[str, Any] = {
        "name": f"io.example/srv-{uuid.uuid4().hex[:8]}",
        "title": "Example server",
        "description": "Does examples",
        "version": "1.2.3",
        "packages": [
            {
                "registry_type": "npm",
                "identifier": "@example/mcp-server",
                "version": "1.2.3",
                "transport": "stdio",
                "environment_variables": [
                    {"name": "API_TOKEN", "required": True, "secret": True},
                    {"name": "REGION", "required": False, "secret": False},
                ],
            }
        ],
    }
    base.update(over)
    return CatalogEntry.model_validate(base)


@pytest.fixture(autouse=True)
def _secrets_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(KEY_ENV, generate_key())
    monkeypatch.delenv(OLD_KEYS_ENV, raising=False)
    monkeypatch.delenv(DEV_ALLOW_HOSTS_ENV, raising=False)


async def _public(host: str, port: int) -> list[str]:
    return ["93.184.215.14"]


@pytest.fixture
def runtime_world() -> Iterator[tuple[TestClient, Any, FakeRuntime, Any]]:
    """(client, app, fake runtime, tenant factory); seeded catalog rows are removed afterwards."""
    fake = FakeRuntime()
    app = create_app(database_url=PG_URL)
    app.dependency_overrides[get_outbound_policy] = lambda: OutboundPolicy(resolver=_public)
    app.dependency_overrides[get_mcp_runtime] = lambda: fake
    tenants: list[str] = []

    def tenant_of(user: str = "alice") -> tuple[str, dict[str, str]]:
        t = f"t{uuid.uuid4().hex[:10]}"
        tenants.append(t)
        return t, get_auth_header(t, user)

    seeded: list[str] = []
    try:
        with TestClient(app) as client:
            app.state.seed = lambda *entries: (
                CatalogStore(app.state.database_engine).upsert(list(entries)),
                seeded.extend(e.name for e in entries),
            )
            yield client, app, fake, tenant_of
    finally:
        with app.state.database_engine.begin() as conn:
            for name in seeded:
                conn.execute(text("DELETE FROM mcp_catalog_entries WHERE name = :n"), {"n": name})
            for t in tenants:
                conn.execute(text("DELETE FROM mcp_servers WHERE tenant_id = :t"), {"t": t})
        app.state.database_engine.dispose()
