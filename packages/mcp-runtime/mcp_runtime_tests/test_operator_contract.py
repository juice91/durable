"""Contract test against the real ToolHive operator on the local colima-pydanticos k3s."""

from __future__ import annotations

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
