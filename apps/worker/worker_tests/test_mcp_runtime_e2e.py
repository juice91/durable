"""End-to-end suite for the local MCP runtime (Task 9 of the MCP local runtime plan).

The real thing, with nothing faked but the registry: the API's `POST /mcp-servers/from-catalog` route
installs an entry from a seeded mirror (the stand-in for the hourly registry mirror), the real
`OperatorRuntime` starts it through the ToolHive operator on the local `colima-pydanticos` k3s, and the
real in-process worker runs agent runs against it. One real npm server (`server-memory` on the platform
node runner, installed from npm at pod start) and one `oci` server (`mcp/everything`).

The pods' proxy Services have cluster DNS names only, so the test's runtime adapter hands out
`http://127.0.0.1:<port>/...` endpoints behind one `kubectl proxy` (the API server's service proxy, which
picks the pod per request, so stopped and restarted pods need no care) instead; the outbound policy still
admits exactly that endpoint by runtime identity (`for_managed_runtime`), as in production, and the dev
allow-list is not set.

Needs the cluster and the operator: `requires_k3s` (skipped without them; failing under
`EAP_REQUIRE_K3S=1`). Cleanup deletes the tenants' namespaces, rows and seeded catalog entries.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import threading
import time
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from eap_mcp_runtime import OperatorRuntime, RuntimeHandle, RuntimeSpec, RuntimeStatus, handle_for
from eap_mcp_runtime import k8s_models as k8s
from eap_mcp_runtime.operator import LightkubeApi
from eap_run_store.mcp_catalog import CatalogStore
from eap_schemas import CatalogEntry
from eap_secrets import KEY_ENV, OLD_KEYS_ENV, generate_key
from eap_secrets.outbound import DEV_ALLOW_HOSTS_ENV
from eap_worker import mcp as worker_mcp
from eap_worker import mcp_runtime_sweep as sweep
from worker_tests.conftest import APP_DB_URL, APP_VERSION, cleanup_tenant, enqueue_run, event_rows, wait_terminal
from worker_tests.mcp_helpers import mcp_spec
from worker_tests.test_mcp_tools import _insert_agent_and_version

CONTEXT = "colima-pydanticos"
CRD = "mcpservers.toolhive.stacklok.dev"
BASE = "/api/v1/mcp-servers"
START_TIMEOUT = 360.0  # an npm server installs its package at pod start

pytestmark = pytest.mark.requires_k3s


# --- the cluster -------------------------------------------------------------------------------------


def kubectl(*args: str, check: bool = True, timeout: int = 90) -> str:
    r = subprocess.run(["kubectl", "--context", CONTEXT, *args], capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise AssertionError(f"kubectl {' '.join(args)}: {r.stderr.strip()[:500]}")
    return r.stdout


def _k3s_unavailable() -> str | None:
    try:
        r = subprocess.run(["kubectl", "--context", CONTEXT, "get", "crd", CRD, "-o", "name"],
                           capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"kubectl is not usable: {exc}"
    return None if r.returncode == 0 else f"cluster {CONTEXT} or CRD {CRD} is unreachable: {r.stderr.strip()[:200]}"


@pytest.fixture(autouse=True)
def _require_cluster() -> None:
    reason = _k3s_unavailable()
    if reason is None:
        return
    if os.environ.get("EAP_REQUIRE_K3S") == "1":
        pytest.fail(f"EAP_REQUIRE_K3S=1 and {reason}")
    pytest.skip(f"{reason} (set EAP_REQUIRE_K3S=1 to fail instead)")


class ClusterRuntime:
    """The real `OperatorRuntime` on the local cluster, reachable from the host: `endpoint()` is a
    the proxy Service reached through one `kubectl proxy` (the API server's service proxy)."""

    def __init__(self) -> None:
        import yaml
        from lightkube import KubeConfig

        raw = yaml.safe_load(kubectl("config", "view", "--raw", "--minify"))
        self._config = KubeConfig.from_dict(raw).get()
        svc = kubectl("get", "svc", "kubernetes", "-n", "default", "-o", "jsonpath={.spec.clusterIP}")
        eps = kubectl("get", "endpoints", "kubernetes", "-n", "default", "-o", "jsonpath={.subsets[*].addresses[*].ip}")
        self._cidrs = [f"{ip}/32" for ip in [svc, *eps.split()] if ip]
        port = kubectl("get", "endpoints", "kubernetes", "-n", "default", "-o", "jsonpath={.subsets[0].ports[0].port}")
        self._ports = [443, int(port)]
        self._by_loop: dict[int, OperatorRuntime] = {}  # an httpx client belongs to one event loop
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        self._port = 0

    def _rt(self) -> OperatorRuntime:
        from lightkube import AsyncClient

        key = id(asyncio.get_running_loop())
        with self._lock:
            if key not in self._by_loop:
                self._by_loop[key] = OperatorRuntime(
                    LightkubeApi(AsyncClient(config=self._config)), api_server_cidrs=self._cidrs, api_server_ports=self._ports
                )
            return self._by_loop[key]

    async def start(self, spec: RuntimeSpec) -> RuntimeHandle:
        return await self._rt().start(spec)

    async def stop(self, handle: RuntimeHandle) -> None:
        await self._rt().stop(handle)

    async def delete(self, handle: RuntimeHandle) -> None:
        await self._rt().delete(handle)

    async def status(self, handle: RuntimeHandle) -> RuntimeStatus:
        return await self._rt().status(handle)

    async def logs(self, handle: RuntimeHandle, tail: int = 200) -> list[str]:
        return await self._rt().logs(handle, tail)

    def endpoint(self, handle: RuntimeHandle) -> str:
        with self._lock:
            if self._proc is None:
                self._proc = subprocess.Popen(
                    ["kubectl", "--context", CONTEXT, "proxy", "--port=0", "--address=127.0.0.1"],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                )
                assert self._proc.stdout is not None
                m = re.search(r"127\.0\.0\.1:(\d+)", self._proc.stdout.readline())
                assert m, "kubectl proxy did not start"
                self._port = int(m.group(1))
        return (f"http://127.0.0.1:{self._port}/api/v1/namespaces/{handle.namespace}"
                f"/services/mcp-{handle.cr_name}-proxy:8080/proxy/mcp")

    def close(self) -> None:
        if self._proc is not None:
            self._proc.terminate()
            self._proc.wait(timeout=10)


# --- the world: API in front of the real worker, a seeded mirror, the cluster ---------------------------


class World:
    def __init__(self, client: TestClient, app: Any, runtime: ClusterRuntime, worker: Any) -> None:
        self.client, self.app, self.runtime, self.worker = client, app, runtime, worker
        self.tenants: list[str] = []
        self.seeded: list[str] = []
        self.api_bodies: list[str] = []  # every API response body, for the leak scan

    @property
    def engine(self) -> Any:
        return self.app.state.database_engine

    def tenant(self, user: str = "alice") -> tuple[str, dict[str, str]]:
        t = f"e2e{uuid.uuid4().hex[:10]}"
        self.tenants.append(t)
        return t, {"Authorization": f"Bearer dev:{t}:{user}"}

    def seed(self, **over: Any) -> CatalogEntry:
        base: dict[str, Any] = {"name": f"io.example/e2e-{uuid.uuid4().hex[:8]}", "title": "e2e", "description": "e2e", "version": "1"}
        base.update(over)
        entry = CatalogEntry.model_validate(base)
        CatalogStore(self.engine).upsert([entry])
        self.seeded.append(entry.name)
        return entry

    def npm_entry(self) -> CatalogEntry:
        return self.seed(packages=[{
            "registry_type": "npm", "identifier": "@modelcontextprotocol/server-memory", "version": "2.1.1",
            "transport": "stdio", "environment_variables": [{"name": "API_TOKEN", "required": True, "secret": True}]}])

    def oci_entry(self) -> CatalogEntry:
        return self.seed(packages=[{
            "registry_type": "oci", "identifier": "mcp/everything", "version": "latest", "transport": "stdio",
            "environment_variables": [{"name": "API_TOKEN", "required": True, "secret": True}]}])

    def call(self, method: str, path: str, headers: dict[str, str], **kw: Any) -> Any:
        r = self.client.request(method, path, headers=headers, **kw)
        self.api_bodies.append(r.text)
        return r

    def install(self, headers: dict[str, str], entry: CatalogEntry, name: str, secret: str) -> str:
        r = self.call("POST", f"{BASE}/from-catalog", headers,
                      json={"catalog_name": entry.name, "name": name, "env": {"API_TOKEN": secret}})
        assert r.status_code == 201, r.text
        assert r.json()["runtime"] == "managed" and r.json()["runtime_state"] in ("starting", "running"), r.json()
        return r.json()["id"]

    def runtime_state(self, headers: dict[str, str], server_id: str) -> dict[str, Any]:
        r = self.call("GET", f"{BASE}/{server_id}/runtime", headers)
        assert r.status_code == 200, r.text
        return r.json()

    def wait_running(self, headers: dict[str, str], server_id: str, timeout: float = START_TIMEOUT) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        body: dict[str, Any] = {}
        while time.monotonic() < deadline:
            body = self.runtime_state(headers, server_id)
            if body["state"] == "running":
                return body
            assert body["state"] in ("pending", "starting"), body
            time.sleep(2)
        raise AssertionError(f"the server did not reach running in {timeout}s: {body}")

    def discover(self, headers: dict[str, str], server_id: str) -> list[str]:
        """`POST /test` until the proxy answers (the package may still be installing behind a ready pod)."""
        deadline = time.monotonic() + 180
        body: dict[str, Any] = {}
        while time.monotonic() < deadline:
            r = self.call("POST", f"{BASE}/{server_id}/test", headers)
            assert r.status_code == 200, r.text
            body = r.json()
            if body["status"] == "ok":
                return sorted(t["name"] for t in body["tools"])
            time.sleep(3)
        raise AssertionError(f"tool discovery never succeeded: {body}")

    def run_tool(self, tenant: str, server: str, tool: str, args: dict[str, Any], timeout: float = 240) -> tuple[uuid.UUID, dict[str, Any]]:
        """An agent run whose scripted model calls `mcp__<server>__<tool>`; returns (run id, the run)."""
        agent_id = uuid.uuid4()
        spec = mcp_spec((server, tool))
        spec["spec_version"] = 1
        spec["name"] = "e2e MCP agent"
        with self.worker.engine.begin() as conn:
            _insert_agent_and_version(conn, agent_id, tenant, spec)
        run_id = enqueue_run(
            self.client_for_worker, self.worker.run_store, tenant_id=tenant, spec=spec,
            input_text=json.dumps({"tool": f"mcp__{server}__{tool}", "args": args}),
            app_version=APP_VERSION, agent_id=agent_id, version=1,
        )
        return run_id, wait_terminal(self.worker.run_store, tenant, run_id, timeout=timeout)

    client_for_worker: Any = None


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, worker: Any, client: Any) -> Iterator[World]:
    from eap_api.app import create_app
    from eap_api.mcp_servers.managed import get_mcp_runtime

    monkeypatch.setenv("EAP_AUTH_MODE", "dev")
    monkeypatch.setenv("EAP_ALLOWED_MODELS", "test:*")
    monkeypatch.setenv(KEY_ENV, generate_key())
    monkeypatch.delenv(OLD_KEYS_ENV, raising=False)
    monkeypatch.delenv(DEV_ALLOW_HOSTS_ENV, raising=False)  # managed endpoints are admitted by identity only
    monkeypatch.setenv("EAP_MCP_START_TIMEOUT_SECONDS", "300")
    monkeypatch.setenv("EAP_MCP_TEST_TIMEOUT_SECONDS", "30")
    runtime = ClusterRuntime()
    worker_mcp.set_mcp_runtime(runtime)  # type: ignore[arg-type]
    app = create_app(database_url=APP_DB_URL)
    app.dependency_overrides[get_mcp_runtime] = lambda: runtime
    w: World | None = None
    try:
        with TestClient(app) as test_client:
            w = World(test_client, app, runtime, worker)
            w.client_for_worker = client
            yield w
    finally:
        worker_mcp.set_mcp_runtime(None)
        if w is not None:
            for t in w.tenants:
                kubectl("delete", "namespace", k8s.namespace_name(t), "--wait=false", "--ignore-not-found", check=False)
                cleanup_tenant(w.worker.engine, t)
                with w.engine.begin() as conn:
                    conn.execute(text("DELETE FROM mcp_servers WHERE tenant_id = :t"), {"t": t})
            with w.engine.begin() as conn:
                for n in w.seeded:
                    conn.execute(text("DELETE FROM mcp_catalog_entries WHERE name = :n"), {"n": n})
        runtime.close()
        app.state.database_engine.dispose()


def tool_result(world: World, run_id: uuid.UUID) -> dict[str, Any]:
    results = [e[3] for e in event_rows(world.worker.engine, run_id) if e[2] == "tool_result"]
    assert len(results) == 1, results
    return results[0]


def pods(namespace: str) -> list[str]:
    return kubectl("get", "pods", "-n", namespace, "-o", "name").split()


# --- scenarios -----------------------------------------------------------------------------------------


def test_npm_server_installs_from_the_mirror_and_its_tools_are_discovered(world: World) -> None:
    tenant, h = world.tenant()
    entry = world.npm_entry()
    sid = world.install(h, entry, "memory", "npm-secret-1")
    body = world.wait_running(h, sid)
    assert body["package"]["registry_type"] == "npm" and body["catalog_name"] == entry.name
    assert "pydanticos-mcp-runner-node" in body["image"]
    tools = world.discover(h, sid)
    assert {"create_entities", "read_graph"} <= set(tools), tools
    server = world.call("GET", f"{BASE}/{sid}", h).json()
    assert server["runtime"] == "managed" and "url" not in server or not server.get("url")
    # The pod lives in the tenant's own namespace, one per server.
    ns = k8s.namespace_name(tenant)
    assert any(handle_for(tenant, sid).cr_name in p for p in kubectl("get", "mcpserver", "-n", ns, "-o", "name").split())


def test_oci_server_installs_from_the_mirror_and_its_tools_are_discovered(world: World) -> None:
    tenant, h = world.tenant()
    sid = world.install(h, world.oci_entry(), "everything", "oci-secret-1")
    body = world.wait_running(h, sid)
    assert body["package"]["registry_type"] == "oci" and body["image"] == "mcp/everything:latest"
    tools = world.discover(h, sid)
    assert {"echo", "add"} <= set(tools), tools


def test_an_agent_run_calls_a_tool_of_a_managed_server(world: World) -> None:
    tenant, h = world.tenant()
    sid = world.install(h, world.oci_entry(), "everything", "run-secret-1")
    world.wait_running(h, sid)
    run_id, run = world.run_tool(tenant, "everything", "echo", {"message": "hello from the e2e suite"})
    assert run["status"] == "completed", run.get("error")
    result = tool_result(world, run_id)
    assert result["tool_name"] == "mcp__everything__echo" and result["status"] == "ok"
    assert "hello from the e2e suite" in json.dumps(result["content"])
    assert world.runtime_state(h, sid)["last_used_at"] is not None


def test_an_idle_server_is_stopped_and_restarts_on_demand(world: World) -> None:
    tenant, h = world.tenant()
    sid = world.install(h, world.oci_entry(), "everything", "idle-secret-1")
    world.wait_running(h, sid)
    ns, cr = k8s.namespace_name(tenant), handle_for(tenant, sid).cr_name
    # Idle for two hours: only this tenant's row is that old, so the sweep touches nothing else.
    with world.engine.begin() as conn:
        conn.execute(text("UPDATE mcp_runtimes SET last_used_at = now() - interval '2 hours' WHERE server_id = :s"), {"s": sid})
    counts = asyncio.run(sweep.sweep_runtimes(world.engine, world.runtime))  # type: ignore[arg-type]
    assert counts["stopped"] >= 1 and counts["stop_failed"] == 0, counts
    assert world.runtime_state(h, sid)["state"] == "stopped"
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline and kubectl("get", "pods", "-n", ns, "-l", f"toolhive-name={cr}", "-o", "name", check=False).strip():
        time.sleep(2)
    # The next agent run starts it again transparently and waits for it.
    run_id, run = world.run_tool(tenant, "everything", "echo", {"message": "back again"}, timeout=360)
    assert run["status"] == "completed", (run.get("error"), [e[2:] for e in event_rows(world.worker.engine, run_id)])
    assert "back again" in json.dumps(tool_result(world, run_id)["content"])
    assert world.runtime_state(h, sid)["state"] == "running"


def test_a_starting_row_left_by_a_crash_is_failed_by_the_sweep_and_can_be_started_again(world: World) -> None:
    tenant, h = world.tenant()
    sid = world.install(h, world.oci_entry(), "everything", "crash-secret-1")
    world.wait_running(h, sid)
    ns, cr = k8s.namespace_name(tenant), handle_for(tenant, sid).cr_name
    # The API died after it claimed `starting` and before the adapter created anything: the row says
    # `starting` for eleven minutes, the cluster has no MCPServer for it.
    kubectl("delete", "mcpserver", cr, "-n", ns, "--wait=true", "--timeout=120s")
    with world.engine.begin() as conn:
        conn.execute(text("UPDATE mcp_runtimes SET state = 'starting', updated_at = now() - interval '11 minutes' WHERE server_id = :s"), {"s": sid})
    counts = asyncio.run(sweep.sweep_runtimes(world.engine, world.runtime))  # type: ignore[arg-type]
    assert counts["failed_starting"] >= 1, counts
    body = world.runtime_state(h, sid)
    assert body["state"] == "failed" and body["last_error"], body
    # Recovery is the normal start control: it recreates the objects from the stored row and secrets.
    r = world.call("POST", f"{BASE}/{sid}/runtime/start", h)
    assert r.status_code == 200 and r.json()["state"] == "starting", r.text
    world.wait_running(h, sid)
    assert "echo" in world.discover(h, sid)


def test_two_tenants_cannot_see_or_reach_each_others_servers(world: World) -> None:
    ta, ha = world.tenant("alice")
    tb, hb = world.tenant("mallory")
    entry = world.oci_entry()
    sa = world.install(ha, entry, "everything", "tenant-a-secret")
    sb = world.install(hb, entry, "everything", "tenant-b-secret")
    world.wait_running(ha, sa)
    world.wait_running(hb, sb)
    nsa, nsb = k8s.namespace_name(ta), k8s.namespace_name(tb)
    assert nsa != nsb
    crs_a = kubectl("get", "mcpserver", "-n", nsa, "-o", "name").split()
    crs_b = kubectl("get", "mcpserver", "-n", nsb, "-o", "name").split()
    assert [c.split("/")[1] for c in crs_a] == [handle_for(ta, sa).cr_name]
    assert [c.split("/")[1] for c in crs_b] == [handle_for(tb, sb).cr_name]
    assert world.call("GET", f"{BASE}", hb).json() and sa not in world.call("GET", f"{BASE}", hb).text
    # The API: B cannot read, control, test, run or delete A's server.
    for method, path in (("GET", f"{BASE}/{sa}"), ("GET", f"{BASE}/{sa}/runtime"), ("POST", f"{BASE}/{sa}/runtime/stop"),
                         ("POST", f"{BASE}/{sa}/runtime/restart"), ("GET", f"{BASE}/{sa}/runtime/logs"),
                         ("POST", f"{BASE}/{sa}/test"), ("DELETE", f"{BASE}/{sa}")):
        assert world.call(method, path, hb).status_code == 404, (method, path)
    assert world.call("POST", f"{BASE}/{sa}/tools/echo/runs", hb, json={"input": {"message": "x"}}).status_code == 404
    assert world.runtime_state(ha, sa)["state"] == "running"
    # The worker: B's agent run names a server B does not have under A's name; only B's own pod answers.
    _r, run_b = world.run_tool(tb, "everything", "echo", {"message": "from tenant b"})
    assert run_b["status"] == "completed", run_b.get("error")
    # Network: a pod of B cannot reach A's proxy (ingress is for the platform's worker and API only).
    target = f"http://mcp-{handle_for(ta, sa).cr_name}-proxy.{nsa}.svc:8080/mcp"
    script = ("const c=new AbortController();setTimeout(()=>c.abort(),8000);"
              f"fetch('{target}',{{method:'POST',signal:c.signal}}).then(r=>{{console.log('REACHED '+r.status);process.exit(0)}})"
              ".catch(e=>{console.log('BLOCKED '+e.name);process.exit(0)})")
    outputs = []
    for pod in pods(nsb):
        r = subprocess.run(["kubectl", "--context", CONTEXT, "-n", nsb, "exec", pod.split("/")[1], "--", "node", "-e", script],
                           capture_output=True, text=True, timeout=60)
        outputs.append(r.stdout.strip())
    assert any(o.startswith("BLOCKED") for o in outputs), outputs
    assert not any(o.startswith("REACHED") for o in outputs), outputs


def test_secret_values_leak_into_no_cr_pod_log_run_event_or_response(world: World) -> None:
    secrets = {"npm": "canary-npm-" + uuid.uuid4().hex, "oci": "canary-oci-" + uuid.uuid4().hex}
    tenant, h = world.tenant()
    sn = world.install(h, world.npm_entry(), "memory", secrets["npm"])
    so = world.install(h, world.oci_entry(), "everything", secrets["oci"])
    world.wait_running(h, sn)
    world.wait_running(h, so)
    world.discover(h, sn)
    world.discover(h, so)
    run_oci, r1 = world.run_tool(tenant, "everything", "echo", {"message": "scan me"})
    run_npm, r2 = world.run_tool(tenant, "memory", "read_graph", {})
    assert r1["status"] == r2["status"] == "completed", (r1.get("error"), r2.get("error"))
    ns = k8s.namespace_name(tenant)

    # Positive control: the value is in the Kubernetes Secret (so a clean scan below means something).
    secret_dump = kubectl("get", "secrets", "-n", ns, "-o", "json")
    import base64

    decoded = " ".join(base64.b64decode(v).decode("utf-8", "replace")
                       for item in json.loads(secret_dump)["items"] for v in (item.get("data") or {}).values())
    assert all(v in decoded for v in secrets.values()), "the secrets are not in the Kubernetes Secret"

    haystacks: dict[str, str] = {}
    for kind in ("mcpserver", "pods", "deployments", "statefulsets", "services", "networkpolicies", "configmaps", "events"):
        haystacks[kind] = kubectl("get", kind, "-n", ns, "-o", "json", check=False)
    for pod in pods(ns):
        haystacks[pod] = kubectl("logs", "-n", ns, pod.split("/")[1], "--all-containers", "--tail=-1", check=False)
    for sid in (sn, so):
        r = world.call("GET", f"{BASE}/{sid}/runtime/logs?tail=1000", h)
        assert r.status_code == 200, r.text
    haystacks["api"] = "\n".join(world.api_bodies)
    haystacks["events"] = json.dumps([[e[2], e[3]] for rid in (run_oci, run_npm) for e in event_rows(world.worker.engine, rid)], default=str)
    with world.engine.connect() as conn:
        rows = conn.execute(text("SELECT to_jsonb(r)::text FROM runs r WHERE tenant_id = :t"), {"t": tenant}).scalars().all()
        rows += conn.execute(text("SELECT to_jsonb(s)::text FROM mcp_servers s WHERE tenant_id = :t"), {"t": tenant}).scalars().all()
        rows += conn.execute(text("SELECT to_jsonb(m)::text FROM mcp_runtimes m WHERE tenant_id = :t"), {"t": tenant}).scalars().all()
    haystacks["rows"] = "\n".join(rows)
    for place, blob in haystacks.items():
        assert blob.strip() or place in ("events",), f"empty scan target {place}"
        for label, value in secrets.items():
            assert value not in blob, f"the {label} secret value leaked into {place}"
            assert base64.b64encode(value.encode()).decode() not in blob, f"the {label} secret (base64) leaked into {place}"
