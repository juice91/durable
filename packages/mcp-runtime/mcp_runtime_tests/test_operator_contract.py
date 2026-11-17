"""Contract test against the real ToolHive operator on the local colima-pydanticos k3s."""

from __future__ import annotations

import base64
import json
import os
import re
import socket
import subprocess
import time
import urllib.request
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from eap_mcp_runtime import OperatorRuntime, RuntimeSpec
from eap_mcp_runtime.operator import LightkubeApi

from mcp_runtime_tests.conftest import CONTEXT

pytestmark = [pytest.mark.requires_k3s, pytest.mark.anyio]

ECHO_IMAGE = "mcp/everything"  # stdio MCP server; has `echo` and `get-env` tools
CANARY = "canary-" + uuid.uuid4().hex


def kubectl(*args: str, check: bool = True) -> str:
    r = subprocess.run(["kubectl", "--context", CONTEXT, *args], capture_output=True, text=True, timeout=60)
    if check and r.returncode != 0:
        raise AssertionError(f"kubectl {' '.join(args)}: {r.stderr}")
    return r.stdout


def api_server_cidrs() -> list[str]:
    svc = kubectl("get", "svc", "kubernetes", "-n", "default", "-o", "jsonpath={.spec.clusterIP}")
    eps = kubectl("get", "endpoints", "kubernetes", "-n", "default", "-o", "jsonpath={.subsets[*].addresses[*].ip}")
    return [f"{ip}/32" for ip in [svc, *eps.split()] if ip]


def make_runtime() -> OperatorRuntime:
    from lightkube import AsyncClient, KubeConfig

    path = os.environ.get("KUBECONFIG") or os.path.expanduser("~/.kube/config")
    cfg = KubeConfig.from_file(path).get(context_name=CONTEXT)
    return OperatorRuntime(LightkubeApi(AsyncClient(config=cfg)), api_server_cidrs=api_server_cidrs())


class PortForward:
    def __init__(self, namespace: str, service: str) -> None:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.proc = subprocess.Popen(
            ["kubectl", "--context", CONTEXT, "-n", namespace, "port-forward", f"svc/{service}", f"{self.port}:8080"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=1).close()
                return
            except OSError:
                time.sleep(0.5)
        self.close()
        raise AssertionError("port-forward did not open")

    def close(self) -> None:
        self.proc.terminate()
        self.proc.wait(timeout=10)


def rpc(url: str, body: dict[str, Any], session: str | None = None) -> tuple[dict[str, Any] | None, str | None]:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **({"Mcp-Session-Id": session} if session else {}),
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        text = resp.read().decode()
        sid = resp.headers.get("Mcp-Session-Id")
    if not text.strip():
        return None, sid
    m = re.search(r"^data: (.*)$", text, re.M)
    return json.loads(m.group(1) if m else text), sid


@pytest.fixture
def tenant() -> Iterator[str]:
    tenant_id = f"contract-{uuid.uuid4().hex[:8]}"
    yield tenant_id
    from eap_mcp_runtime import k8s_models as m

    kubectl("delete", "namespace", m.namespace_name(tenant_id), "--wait=false", "--ignore-not-found", check=False)


async def test_start_serves_mcp_stops_and_deletes_every_object(tenant: str) -> None:
    rt = make_runtime()
    server_id = str(uuid.uuid4())
    spec = RuntimeSpec(
        tenant_id=tenant,
        server_id=server_id,
        name="Echo",
        image=ECHO_IMAGE,
        secret_refs={"CONTRACT_TOKEN": CANARY},
    )
    h = await rt.start(spec)
    ns, cr = h.namespace, h.cr_name

    st = await rt.wait_for(h, "running", timeout=240)
    assert st.state == "running", st
    assert st.endpoint == rt.endpoint(h)

    # The operator's own URL matches the endpoint pattern we hand out (host part: .svc vs .svc.cluster.local).
    url = kubectl("-n", ns, "get", "mcpserver", cr, "-o", "jsonpath={.status.url}")
    assert url.replace(".svc.cluster.local", ".svc") == rt.endpoint(h)
    assert kubectl("-n", ns, "get", "svc", f"mcp-{cr}-proxy", "-o", "name").strip() == f"service/mcp-{cr}-proxy"

    # The secret reaches the container through `secrets:` and never appears in the CR or the pod specs.
    assert CANARY not in kubectl("-n", ns, "get", "mcpserver", cr, "-o", "json")
    assert CANARY not in kubectl("-n", ns, "get", "pods", "-o", "json")

    pf = PortForward(ns, f"mcp-{cr}-proxy")
    try:
        local = f"http://127.0.0.1:{pf.port}/mcp"
        init, sid = rpc(
            local,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "contract", "version": "0"},
                },
            },
        )
        assert init and "protocolVersion" in init["result"]
        rpc(local, {"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
        listed, _ = rpc(local, {"jsonrpc": "2.0", "id": 3, "method": "tools/list"}, sid)
        names = [t["name"] for t in listed["result"]["tools"]]
        env_tool = next(n for n in names if "env" in n.lower())
        call, _ = rpc(
            local,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": env_tool, "arguments": {}}},
            sid,
        )
        assert call and "result" in call, call
        assert CANARY in json.dumps(call["result"]), "the secret was not injected as CONTRACT_TOKEN"
    finally:
        pf.close()

    assert isinstance(await rt.logs(h, tail=20), list)

    await rt.stop(h)
    st = await rt.wait_for(h, "stopped", timeout=120)
    assert st.state == "stopped", st

    await rt.start(spec)  # transparent restart of a stopped server
    st = await rt.wait_for(h, "running", timeout=240)
    assert st.state == "running", st

    await rt.delete(h)
    await rt.delete(h)  # idempotent
    assert (await rt.status(h)).state == "absent"
    for kind, name in [("mcpserver", cr), ("secret", f"{cr}-env"), ("networkpolicy", f"{cr}-isolation")]:
        out = kubectl("-n", ns, "get", kind, name, check=False)
        assert out.strip() == "", f"{kind}/{name} survived delete"
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline and kubectl("-n", ns, "get", "pods", "-o", "name").strip():
        time.sleep(2)
    assert kubectl("-n", ns, "get", "pods", "-o", "name").strip() == ""
    assert kubectl("-n", ns, "get", "svc,deploy,sts", "-o", "name").strip() == ""


def _first_pod(ns: str, cr: str) -> str:
    return f"{cr}-0"  # the backend StatefulSet pod


# Prints `<name> ok|fail:<code>` per target; every connect has a 4 s timeout.
PROBE = """
const net = require('net');
const targets = JSON.parse(process.argv[1]);
let left = targets.length;
for (const t of targets) {
  const s = net.connect({host: t.host, port: t.port});
  const done = (r) => { console.log(t.name + ' ' + r); s.destroy(); if (--left === 0) process.exit(0); };
  s.setTimeout(4000, () => done('fail:timeout'));
  s.on('connect', () => done('ok'));
  s.on('error', (e) => done('fail:' + (e.code || 'error')));
}
""".strip()
LISTENER = "require('net').createServer(s => s.end('hi')).listen(9999)"


def probe(ns: str, pod: str, targets: list[dict[str, Any]]) -> dict[str, str]:
    r = subprocess.run(
        ["kubectl", "--context", CONTEXT, "-n", ns, "exec", pod, "-c", "mcp" if pod.startswith("s-") else pod,
         "--", "node", "-e", PROBE, json.dumps(targets)],
        capture_output=True,
        text=True,
        timeout=90,
    )
    out: dict[str, str] = {}
    for line in r.stdout.splitlines():
        name, _, res = line.partition(" ")
        out[name] = res
    assert out, r.stderr
    return out


@pytest.fixture
def control_listener() -> Iterator[tuple[str, str]]:
    """A listener in an unrestricted namespace: reachable from its own namespace, so a timeout from
    the MCP pod can only come from the pod's egress policy, not from a closed port or ingress rule."""
    ns = f"mcp-iso-ctl-{uuid.uuid4().hex[:8]}"
    kubectl("create", "namespace", ns)
    try:
        kubectl("-n", ns, "run", "ctl", f"--image={ECHO_IMAGE}", "--image-pull-policy=IfNotPresent",
                "--command", "--", "node", "-e", LISTENER)
        kubectl("-n", ns, "wait", "--for=condition=Ready", "pod/ctl", "--timeout=120s")
        ip = kubectl("-n", ns, "get", "pod", "ctl", "-o", "jsonpath={.status.podIP}").strip()
        time.sleep(1)
        yield ns, ip
    finally:
        kubectl("delete", "namespace", ns, "--wait=false", "--ignore-not-found", check=False)


async def test_platform_is_unreachable_from_the_pod(tenant: str, control_listener: tuple[str, str]) -> None:
    """Review Focus 1: from a started pod the Postgres, API and worker addresses and any other pod time out
    while a public HTTPS host succeeds."""
    ctl_ns, ctl_ip = control_listener
    platform = "pydanticos"
    targets: list[dict[str, Any]] = []
    pg_ip = kubectl("-n", platform, "get", "pod", "postgres-0", "-o", "jsonpath={.status.podIP}", check=False).strip()
    assert pg_ip, "the platform Postgres pod must be running in namespace pydanticos for this test"
    targets.append({"name": "postgres", "host": pg_ip, "port": 5432})
    for svc, port in (("api", 8000), ("worker", 8080), ("postgres", 5432)):
        ip = kubectl("-n", platform, "get", "svc", svc, "-o", "jsonpath={.spec.clusterIP}", check=False).strip()
        if ip and ip != "None":
            targets.append({"name": f"{svc}-svc", "host": ip, "port": port})
    for i, ip in enumerate(
        kubectl("-n", platform, "get", "pods", "-l", "app.kubernetes.io/name in (api,worker)", "-o",
                "jsonpath={.items[*].status.podIP}", check=False).split()
    ):
        targets.append({"name": f"platform-pod-{i}", "host": ip, "port": 8080})
    targets.append({"name": "other-pod", "host": ctl_ip, "port": 9999})

    # Control: the listener works from inside its own namespace.
    assert probe(ctl_ns, "ctl", [{"name": "self", "host": ctl_ip, "port": 9999}])["self"] == "ok"

    rt = make_runtime()
    h = await rt.start(RuntimeSpec(tenant_id=tenant, server_id=str(uuid.uuid4()), name="Iso", image=ECHO_IMAGE))
    st = await rt.wait_for(h, "running", timeout=240)
    assert st.state == "running", st
    results = probe(
        h.namespace,
        _first_pod(h.namespace, h.cr_name),
        [*targets, {"name": "public-https", "host": "registry.npmjs.org", "port": 443}],
    )
    assert results["public-https"] == "ok", results
    # k3s's kube-router rejects a denied connection (ECONNREFUSED, ICMP port unreachable) where other
    # CNIs drop it (timeout); both mean "not delivered". `ok` would mean reachable.
    for t in targets:
        assert results[t["name"]] in {"fail:timeout", "fail:ECONNREFUSED", "fail:EHOSTUNREACH"}, (
            f"{t['name']} not blocked from the pod: {results}"
        )

    # The block is the pod's NetworkPolicy and nothing else: the same live listener answers once it is gone.
    kubectl("-n", h.namespace, "delete", "networkpolicy", f"{h.cr_name}-isolation")
    time.sleep(5)
    after = probe(h.namespace, _first_pod(h.namespace, h.cr_name), [{"name": "other-pod", "host": ctl_ip, "port": 9999}])
    assert after["other-pod"] == "ok", after


async def test_secret_values_appear_only_in_the_kubernetes_secret(tenant: str, caplog: pytest.LogCaptureFixture) -> None:
    """Review Focus 2: the CR, the pod specs and the adapter/operator logs contain no secret value."""
    import logging

    caplog.set_level(logging.DEBUG)
    value = "sv-" + uuid.uuid4().hex
    rt = make_runtime()
    h = await rt.start(
        RuntimeSpec(
            tenant_id=tenant,
            server_id=str(uuid.uuid4()),
            name="Leak",
            image=ECHO_IMAGE,
            env={"PLAIN": "visible"},
            secret_refs={"LEAK_TOKEN": value},
        )
    )
    st = await rt.wait_for(h, "running", timeout=240)
    assert st.state == "running", st
    ns, cr = h.namespace, h.cr_name

    b64 = base64.b64encode(value.encode()).decode()
    assert b64 in kubectl("-n", ns, "get", "secret", f"{cr}-env", "-o", "json")  # the one place it lives
    for what in ("mcpserver", "pods", "statefulsets", "deployments", "events", "configmaps", "networkpolicies"):
        out = kubectl("-n", ns, "get", what, "-o", "json", check=False)
        assert value not in out and b64 not in out, f"secret value present in {what}"

    operator_logs = ""
    for sel in ("app.kubernetes.io/name=toolhive-operator", "app.kubernetes.io/name=toolhive"):
        operator_logs += kubectl("-n", "toolhive-system", "logs", "-l", sel, "--tail=2000", check=False)
    assert operator_logs, "no operator log lines were read"
    assert value not in operator_logs and b64 not in operator_logs
    assert value not in "\n".join(await rt.logs(h, tail=500))
    assert value not in caplog.text
    assert value not in repr(h) and value not in repr(st)


# --- platform runner images (Task 4): npm and pypi servers installed at pod start ---------------


async def _run_package_and_list_tools(tenant: str, package: Any, expect_tool: str) -> float:
    """Start `package` on its runner image, wait for it to serve MCP, list tools; return cold-start seconds."""
    from eap_mcp_runtime.images import image_for

    rt = make_runtime()
    image, args, env = image_for(package)
    spec = RuntimeSpec(
        tenant_id=tenant, server_id=str(uuid.uuid4()), name="Runner", image=image, args=args, env=env
    )
    started = time.monotonic()
    h = await rt.start(spec)
    st = await rt.wait_for(h, "running", timeout=300)
    assert st.state == "running", st
    pf = PortForward(h.namespace, f"mcp-{h.cr_name}-proxy")
    try:
        local = f"http://127.0.0.1:{pf.port}/mcp"
        init, sid = None, None
        deadline = time.monotonic() + 120  # the runner installs the package before it answers
        while time.monotonic() < deadline:
            try:
                init, sid = rpc(
                    local,
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-03-26",
                            "capabilities": {},
                            "clientInfo": {"name": "contract", "version": "0"},
                        },
                    },
                )
                if init and "result" in init:
                    break
            except OSError:
                pass
            time.sleep(2)
        assert init and "protocolVersion" in init["result"], init
        cold_start = time.monotonic() - started
        rpc(local, {"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
        listed, _ = rpc(local, {"jsonrpc": "2.0", "id": 3, "method": "tools/list"}, sid)
        assert expect_tool in [t["name"] for t in listed["result"]["tools"]]
    finally:
        pf.close()
        await rt.delete(h)
    print(f"COLD_START {package.registry_type} {package.identifier}: {cold_start:.1f}s")
    return cold_start


async def test_npm_server_runs_on_the_node_runner(tenant: str) -> None:
    from eap_mcp_runtime.images import PackageSpec

    pkg = PackageSpec("npm", "@modelcontextprotocol/server-memory", "2.1.1")
    await _run_package_and_list_tools(tenant, pkg, "create_entities")


async def test_pypi_server_runs_on_the_python_runner(tenant: str) -> None:
    from eap_mcp_runtime.images import PackageSpec

    pkg = PackageSpec("pypi", "mcp-server-time", "2026.11.11")
    await _run_package_and_list_tools(tenant, pkg, "get_current_time")
