"""Running a registered MCP server's tool: POST /mcp-servers/{id}/tools/{tool}/runs records a run on
the same ledger as the built-in tool runs (GET /runs, GET /runs/{id}), tenant-scoped, against a real
FastMCP server on 127.0.0.1 (allowed only through EAP_MCP_DEV_ALLOW_HOSTS)."""

from __future__ import annotations

import threading
import time
import uuid

import pytest
import uvicorn

from .run_helpers import hard_timeout
from .test_mcp_servers import (  # noqa: F401 - fixtures
    BASE,
    SENTINEL,
    app_client,
    dev_allow_loopback,
    register,
    secrets_key,
    tenant_headers,
)

pytestmark = pytest.mark.usefixtures("secrets_key")


class ToolServer:
    """FastMCP over Streamable HTTP in a uvicorn thread with `add`, `boom` (the tool raises) and
    `leak` (returns the Authorization header value it was called with)."""

    def __init__(self, required_auth: str | None = None) -> None:
        from fastmcp import FastMCP
        from fastmcp.exceptions import ToolError
        from fastmcp.server.dependencies import get_http_headers

        mcp = FastMCP("run-test")
        self.calls = 0

        @mcp.tool
        def add(a: int, b: int = 1) -> int:
            """Add two integers."""
            self.calls += 1
            return a + b

        @mcp.tool
        def boom() -> str:
            """Always fails."""
            self.calls += 1
            raise ToolError("internal detail that must not be stored")

        @mcp.tool
        def leak() -> str:
            """Echoes the header it was called with."""
            return get_http_headers(include={"authorization"}).get("authorization", "")

        inner = mcp.http_app(path="/mcp")

        async def app(scope, receive, send):
            await inner(scope, receive, send)

        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> ToolServer:
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("local MCP server did not start")
            time.sleep(0.02)
        self.url = f"http://127.0.0.1:{self.server.servers[0].sockets[0].getsockname()[1]}/mcp"
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


def runs_url(server_id: str, tool: str) -> str:
    return f"{BASE}/{server_id}/tools/{tool}/runs"


def ledger(client, h, tool_id: str) -> list[dict]:
    r = client.get("/api/v1/runs", params={"kind": "tool", "tool_id": tool_id}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    return body["items"] if isinstance(body, dict) else body


def test_success_is_recorded_on_the_ledger(app_client, tenant_headers, dev_allow_loopback):
    client, _ = app_client
    _, h = tenant_headers()
    with hard_timeout(60), ToolServer() as mcp:
        server = register(client, h, url=mcp.url).json()
        r = client.post(runs_url(server["id"], "add"), json={"input": {"a": 2, "b": 3}}, headers=h)
        assert r.status_code == 200, r.text
        accepted = r.json()
        tool_id = f"mcp:{server['id']}/add"
        assert accepted["kind"] == "tool" and accepted["tool_id"] == tool_id
        assert accepted["status"] == "completed"

        run = client.get(f"/api/v1/runs/{accepted['run_id']}", headers=h).json()
        assert run["kind"] == "tool" and run["tool_id"] == tool_id
        assert run["status"] == "completed" and run["error"] is None
        assert run["input"] == '{"a":2,"b":3}'
        assert run["output"] == '{"result":5}'
        assert [x["run_id"] for x in ledger(client, h, tool_id)] == [accepted["run_id"]]

        events = client.get(f"/api/v1/runs/{accepted['run_id']}/events", headers=h).json()
        types = [e["type"] for e in (events["items"] if isinstance(events, dict) else events)]
        assert types == ["run_started", "tool_call", "tool_result", "run_completed"]


def test_repeated_run_id_returns_the_recorded_run_without_calling_again(app_client, tenant_headers, dev_allow_loopback):
    client, _ = app_client
    _, h = tenant_headers()
    run_id = str(uuid.uuid4())
    with hard_timeout(60), ToolServer() as mcp:
        server = register(client, h, url=mcp.url).json()
        first = client.post(runs_url(server["id"], "add"), json={"input": {"a": 1}, "run_id": run_id}, headers=h)
        again = client.post(runs_url(server["id"], "add"), json={"input": {"a": 1}, "run_id": run_id}, headers=h)
        other = client.post(runs_url(server["id"], "add"), json={"input": {"a": 9}, "run_id": run_id}, headers=h)
        assert first.status_code == again.status_code == 200
        assert again.json()["run_id"] == run_id
        assert other.status_code == 409
        assert mcp.calls == 1


def test_a_tool_error_is_recorded_as_a_failed_run_without_the_servers_text(
    app_client, tenant_headers, dev_allow_loopback
):
    client, _ = app_client
    _, h = tenant_headers()
    with hard_timeout(60), ToolServer() as mcp:
        server = register(client, h, url=mcp.url).json()
        r = client.post(runs_url(server["id"], "boom"), json={"input": {}}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "failed"
    run = client.get(f"/api/v1/runs/{r.json()['run_id']}", headers=h).json()
    assert run["status"] == "failed" and run["output"] is None
    assert "internal detail" not in str(run) and run["error"]
    assert len(ledger(client, h, f"mcp:{server['id']}/boom")) == 1


def test_an_unreachable_server_is_recorded_as_a_failed_run(app_client, tenant_headers, dev_allow_loopback):
    client, _ = app_client
    _, h = tenant_headers()
    with hard_timeout(60), ToolServer() as mcp:
        url = mcp.url
    server = register(client, h, url=url).json()  # nothing listens there any more
    r = client.post(runs_url(server["id"], "add"), json={"input": {"a": 1}}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "failed"
    run = client.get(f"/api/v1/runs/{r.json()['run_id']}", headers=h).json()
    assert run["status"] == "failed" and "unavailable" in run["error"]
    assert len(ledger(client, h, f"mcp:{server['id']}/add")) == 1


def test_bad_arguments_are_422_without_values_and_record_nothing(app_client, tenant_headers, dev_allow_loopback):
    client, _ = app_client
    _, h = tenant_headers()
    with hard_timeout(60), ToolServer() as mcp:
        server = register(client, h, url=mcp.url).json()
        wrong_type = client.post(runs_url(server["id"], "add"), json={"input": {"a": "sntl-typed-value"}}, headers=h)
        missing = client.post(runs_url(server["id"], "add"), json={"input": {}}, headers=h)
        assert mcp.calls == 0
    assert wrong_type.status_code == 422 and "sntl-typed-value" not in wrong_type.text
    assert wrong_type.json()["detail"][0]["loc"] == ["body", "input", "a"]
    assert missing.status_code == 422
    assert missing.json()["detail"][0]["loc"] == ["body", "input", "a"]
    assert ledger(client, h, f"mcp:{server['id']}/add") == []


def test_unknown_tool_is_404(app_client, tenant_headers, dev_allow_loopback):
    client, _ = app_client
    _, h = tenant_headers()
    with hard_timeout(60), ToolServer() as mcp:
        server = register(client, h, url=mcp.url).json()
        r = client.post(runs_url(server["id"], "nope"), json={"input": {}}, headers=h)
    assert r.status_code == 404


def test_other_tenants_missing_and_disabled_servers(app_client, tenant_headers, dev_allow_loopback):
    client, _ = app_client
    _, h = tenant_headers()
    _, other = tenant_headers()
    with hard_timeout(60), ToolServer() as mcp:
        server = register(client, h, url=mcp.url).json()
        assert client.post(runs_url(server["id"], "add"), json={"input": {"a": 1}}, headers=other).status_code == 404
        assert client.post(runs_url(str(uuid.uuid4()), "add"), json={"input": {"a": 1}}, headers=h).status_code == 404
        assert client.patch(f"{BASE}/{server['id']}", json={"enabled": False}, headers=h).status_code == 200
        r = client.post(runs_url(server["id"], "add"), json={"input": {"a": 1}}, headers=h)
        assert r.status_code == 422 and "disabled" in r.json()["detail"]
        assert mcp.calls == 0
    assert ledger(client, other, f"mcp:{server['id']}/add") == []


def test_ledger_is_tenant_scoped(app_client, tenant_headers, dev_allow_loopback):
    client, _ = app_client
    _, h = tenant_headers()
    _, other = tenant_headers()
    with hard_timeout(60), ToolServer() as mcp:
        server = register(client, h, url=mcp.url).json()
        run_id = client.post(runs_url(server["id"], "add"), json={"input": {"a": 1}}, headers=h).json()["run_id"]
    assert client.get(f"/api/v1/runs/{run_id}", headers=other).status_code == 404
    assert ledger(client, other, f"mcp:{server['id']}/add") == []


def test_header_secret_is_sent_but_never_stored_in_the_run(app_client, tenant_headers, dev_allow_loopback):
    client, _ = app_client
    _, h = tenant_headers()
    with hard_timeout(60), ToolServer() as mcp:
        server = register(client, h, url=mcp.url, headers={"Authorization": f"Bearer {SENTINEL}"}).json()
        r = client.post(runs_url(server["id"], "leak"), json={"input": {}}, headers=h)
        assert r.status_code == 200, r.text
        run_id = r.json()["run_id"]
        run = client.get(f"/api/v1/runs/{run_id}", headers=h).json()
        events = client.get(f"/api/v1/runs/{run_id}/events", headers=h).text
    assert run["status"] == "completed"
    assert SENTINEL not in r.text and SENTINEL not in str(run) and SENTINEL not in events
    assert "[REDACTED]" in run["output"]


def test_mcp_tool_run_marks_ledger_failed_when_the_call_raises(
    app_client, tenant_headers, dev_allow_loopback, monkeypatch
):
    from eap_api.routes import mcp_servers

    async def explode(*args, **kwargs):
        raise RuntimeError("transport blew up")

    monkeypatch.setattr(mcp_servers, "call_server_tool", explode)
    client, _ = app_client
    _, h = tenant_headers()
    with hard_timeout(60), ToolServer() as mcp:
        server = register(client, h, url=mcp.url).json()
        r = client.post(runs_url(server["id"], "add"), json={"input": {"a": 1}}, headers=h)
    assert r.status_code == 502, r.text
    tool_id = f"mcp:{server['id']}/add"
    rows = ledger(client, h, tool_id)
    assert len(rows) == 1 and rows[0]["status"] == "failed"
    run = client.get(f"/api/v1/runs/{rows[0]['run_id']}", headers=h).json()
    assert run["status"] == "failed" and "transport blew up" in run["error"]
