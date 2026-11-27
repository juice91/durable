"""The world of the external-access tests: the real app on Postgres, the real identity chain, a fake DBOS client behind
the real launcher, and helpers that make an exported workflow, a client and its key the way an admin does."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import contextmanager
from typing import Any

import pytest
from eap_iam import RequestView
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from eap_api.app import create_app
from eap_api.deps import iam_of
from eap_api.launcher import DBOSRunLauncher, LauncherSettings, get_run_launcher
from eap_api.service_deps.external_access import external_run_service_of

from .conftest import get_auth_header
from .run_helpers import PG_URL, cleanup_tenant, hard_timeout
from .test_runs import FakeDBOSClient

BASE = "/api/v1/agent-access"
PUBLIC = "https://durable.example.test"
INPUT_SCHEMA = {
    "type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"], "additionalProperties": False,
}
QUIET_STEPS = [{"id": "now", "type": "tool", "tool_id": "clock.now", "input": {}}]
NOISY_STEPS = [{
    "id": "post", "type": "tool", "tool_id": "http.request",
    "input": {"url": "https://example.com/hook", "method": "POST"},
}]


@pytest.fixture
def external_env(monkeypatch):
    monkeypatch.setenv("EAP_EXTERNAL_ACCESS_ENABLED", "false")
    monkeypatch.setenv("EAP_EXTERNAL_BASE_URL", PUBLIC)
    monkeypatch.setenv("EAP_AGENT_GRANTS_ENFORCE", "1")
    with hard_timeout(180):
        yield monkeypatch


@pytest.fixture
def world(external_env):
    app, fake = external_app(external_env)
    tenants: list[str] = []
    with TestClient(app) as client:
        yield World(client, app, fake, tenants)
    remove_tenants(app, tenants)


def external_app(env) -> tuple[Any, FakeDBOSClient]:
    """The real app with a fake DBOS client and a grant-keeping engine, external access switched on."""
    app = create_app(database_url=PG_URL)
    fake = FakeDBOSClient()
    fake.cancel_workflow = lambda *args, **kwargs: None  # type: ignore[attr-defined]
    launcher = DBOSRunLauncher(LauncherSettings("postgresql://unused/x", "dbos", "m1-test"), client=fake)
    app.dependency_overrides[get_run_launcher] = lambda: launcher
    app.state.run_launcher = launcher
    # The deployed engine keeps agent-bound grants; this one allows everything and says it keeps them.
    use_engine(app, GrantKeepingAllowAll(iam_of(app).policy))
    # The surface is switched on for the service after startup (a dev-mode app refuses to start with it on).
    env.setenv("EAP_EXTERNAL_ACCESS_ENABLED", "true")
    return app, fake


def remove_tenants(app: Any, tenants: list[str]) -> None:
    engine = app.state.database_engine
    with engine.begin() as conn:
        for t in tenants:
            for table in ("external_runs", "external_endpoints", "api_keys", "workflow_exports", "external_clients",
                          "audit_log"):
                conn.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": t})
    for t in tenants:
        cleanup_tenant(engine, t)
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM workflow_versions WHERE tenant_id = :t"), {"t": t})
            conn.execute(text("DELETE FROM workflows WHERE tenant_id = :t"), {"t": t})
    engine.dispose()


class GrantKeepingAllowAll:
    """The transitional allow-all engine, declaring the agent-bound grants feature (as `openfga` does)."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.features = frozenset(getattr(inner, "features", ())) | {"agent_grants"}

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def used_by(self, subject: Any, kind: str) -> list[str]:
        return []

    async def grantees(self, resource: Any) -> list[Any]:
        return []

    async def grant_use(self, grant: Any) -> None:
        return None

    async def revoke_use(self, grant: Any) -> None:
        return None


def use_engine(app: Any, engine: Any) -> None:
    import dataclasses

    app.state.iam = dataclasses.replace(iam_of(app), policy=engine)


class World:
    def __init__(self, client: TestClient, app: Any, fake: FakeDBOSClient, tenants: list[str]) -> None:
        self.client, self.app, self.fake, self.tenants = client, app, fake, tenants

    @property
    def engine(self):
        return self.app.state.database_engine

    def tenant(self) -> tuple[str, dict[str, str]]:
        t = f"t{uuid.uuid4().hex[:10]}"
        self.tenants.append(t)
        return t, get_auth_header(t, "alice")

    # -- workflows ---------------------------------------------------------------------------------------------------

    def spec(self, steps: list | None = None, input_schema: dict | None = None) -> dict[str, Any]:
        steps = steps or QUIET_STEPS
        return {"name": f"wf-{uuid.uuid4().hex[:6]}", "input_schema": input_schema or INPUT_SCHEMA,
                "start": steps[0]["id"], "steps": steps}

    def workflow(self, headers: dict[str, str], **spec: Any) -> str:
        r = self.client.post("/api/v1/workflows", json={"spec": self.spec(**spec)}, headers=headers)
        assert r.status_code == 201, r.text
        wid = r.json()["id"]
        r = self.client.post(f"/api/v1/workflows/{wid}/versions", headers=headers)
        assert r.status_code == 201, r.text
        return wid

    def publish(self, headers: dict[str, str], workflow_id: str, **spec: Any):
        r = self.client.patch(f"/api/v1/workflows/{workflow_id}", json={"spec": self.spec(**spec)}, headers=headers)
        assert r.status_code == 200, r.text
        r = self.client.post(f"/api/v1/workflows/{workflow_id}/versions", headers=headers)
        assert r.status_code == 201, r.text
        return r.json()

    # -- clients, keys, exports --------------------------------------------------------------------------------------

    def client_record(self, headers: dict[str, str], name: str | None = None, **body: Any) -> dict[str, Any]:
        r = self.client.post(f"{BASE}/clients", json={"name": name or f"agent-{uuid.uuid4().hex[:6]}", **body}, headers=headers)
        assert r.status_code == 201, r.text
        return r.json()

    def key(self, headers: dict[str, str], client_id: str, workflow_ids: list[str], **body: Any) -> dict[str, Any]:
        r = self.client.post(f"{BASE}/clients/{client_id}/keys",
                             json={"name": "k", "workflow_ids": workflow_ids, **body}, headers=headers)
        assert r.status_code == 201, r.text
        return r.json()

    def export_body(self, wf: str, /, **over: Any) -> dict[str, Any]:
        return {"name": "run_report", "title": "Run the report", "description": "Builds the report.",
                "workflow_id": wf, "version": "latest", "protocols": ["mcp"]} | over

    def export(self, headers: dict[str, str], workflow_id: str, **over: Any) -> dict[str, Any]:
        r = self.client.post(f"{BASE}/exports", json=self.export_body(workflow_id, **over), headers=headers)
        assert r.status_code == 201, r.text
        return r.json()

    def enable(self, headers: dict[str, str], export: dict[str, Any]):
        return self.client.post(f"{BASE}/exports/{export['id']}/enable",
                                json={"approved_hash": export["approval_hash"], "confirm": True}, headers=headers)

    def live(self, name: str = "run_report", steps: list | None = None, **over: Any) -> "Live":
        """A tenant with one enabled export, one client and its key."""
        tenant, headers = self.tenant()
        workflow = self.workflow(headers, steps=steps)
        export = self.export(headers, workflow, name=name, **over)
        client = self.client_record(headers)
        key = self.key(headers, client["id"], [workflow])
        r = self.enable(headers, export)
        assert r.status_code == 200, r.text
        return Live(self, tenant, headers, workflow, r.json(), client, key["key"])

    # -- the external side (the service, as the controllers call it) ------------------------------

    def principal(self, key: str, action: str = "run.external"):
        view = RequestView(headers={"authorization": f"Bearer {key}"}, accepts=frozenset({"api_key"}), action=action)
        return asyncio.run(iam_of(self.app).chain.authenticate(view))

    @contextmanager
    def service(self):
        with Session(self.engine) as session:
            yield external_run_service_of(self.app, session)

    def call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        with self.service() as service:
            return getattr(service, method)(*args, **kwargs)

    def sql(self, statement: str, **params: Any) -> list[Any]:
        with self.engine.begin() as conn:
            result = conn.execute(text(statement), params)
            return list(result.mappings().all()) if result.returns_rows else []


class Live:
    def __init__(self, world: World, tenant: str, headers: dict[str, str], workflow: str, export: dict, client: dict,
                 key: str) -> None:
        self.world, self.tenant, self.headers, self.workflow = world, tenant, headers, workflow
        self.export, self.client, self.key = export, client, key

    def principal(self):
        return self.world.principal(self.key)

    def start(self, q: str = "hello", key: str | None = "k-1", **kwargs: Any) -> dict[str, Any]:
        return self.world.call("start", self.principal(), "mcp", self.export["name"], {"q": q}, idempotency_key=key, **kwargs)

    def runs(self) -> list[Any]:
        return self.world.sql(
            "SELECT id, workflow_version, principal, started_by_kind, on_behalf_of, status, input FROM runs"
            " WHERE tenant_id = :t ORDER BY created_at", t=self.tenant)
