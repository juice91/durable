"""MCP server registry (milestone 3, task 3.4): CRUD, tenant isolation, outbound URL policy on
registration, write-only encrypted header secrets, and the connection test with tool discovery
against a real MCP server (FastMCP over Streamable HTTP on 127.0.0.1, allowed only through
EAP_MCP_DEV_ALLOW_HOSTS)."""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import uvicorn
from fastapi.testclient import TestClient
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import text

import eap_observability as obs
from eap_api.app import create_app
from eap_api.routes.mcp_servers import get_outbound_policy
from eap_secrets import KEY_ENV, OLD_KEYS_ENV, Keyring, generate_key
from eap_secrets.outbound import DEV_ALLOW_HOSTS_ENV, OutboundPolicy

from .conftest import get_auth_header
from .run_helpers import PG_URL, hard_timeout, requires_pg

pytestmark = requires_pg

SENTINEL = "sntl-9b1e4c7d2f-secret"
BASE = "/api/v1/mcp-servers"
PUBLIC_URL = "https://mcp.example.com/mcp"


def public_resolver(*addresses: str):
    async def resolve(host: str, port: int) -> list[str]:
        return list(addresses or ["93.184.215.14"])

    return resolve


@pytest.fixture(autouse=True)
def secrets_key(monkeypatch: pytest.MonkeyPatch) -> str:
    key = generate_key()
    monkeypatch.setenv(KEY_ENV, key)
    monkeypatch.delenv(OLD_KEYS_ENV, raising=False)
    monkeypatch.delenv(DEV_ALLOW_HOSTS_ENV, raising=False)
    return key


@pytest.fixture
def app_client() -> Iterator[tuple[TestClient, Any]]:
    app = create_app(database_url=PG_URL)
    app.dependency_overrides[get_outbound_policy] = lambda: OutboundPolicy(resolver=public_resolver())
    try:
        with TestClient(app) as client:
            yield client, app
    finally:
        app.state.database_engine.dispose()


@pytest.fixture
def tenant_headers():
    def make() -> tuple[str, dict[str, str]]:
        tenant = f"t{uuid.uuid4().hex[:10]}"
        return tenant, get_auth_header(tenant, "alice")

    return make


def register(client: TestClient, h: dict[str, str], **body: Any):
    body.setdefault("name", f"srv-{uuid.uuid4().hex[:6]}")
    body.setdefault("url", PUBLIC_URL)
    return client.post(BASE, json=body, headers=h)


# --- a real MCP server on 127.0.0.1 -------------------------------------------------------------


class LocalMcpServer:
    """FastMCP over Streamable HTTP in a uvicorn thread. Records request headers; with
    `required_auth` set it answers 401 unless Authorization matches. With `echo` set it also lists
    a tool `leaky` whose description (plain and base64) and input-schema default contain `echo`
    (a server echoing a header value into tools/list). With `forms` set, `leaky`'s description is
    only a base64 blob of JSON containing the secret, and its schema defaults are the two halves
    of the secret plus its hex, percent-encoded and base64 forms (no plaintext secret)."""

    def __init__(self, required_auth: str | None = None, echo: str | None = None, *, forms: str | None = None) -> None:
        from fastmcp import FastMCP

        mcp = FastMCP("local-test")

        @mcp.tool
        def add(a: int, b: int) -> int:
            """Add two integers."""
            return a + b

        @mcp.tool
        def search_docs(query: str, limit: int = 5) -> list[str]:
            """Search the documentation."""
            return [query][:limit]

        if echo is not None:
            import base64

            def leaky(token: str = echo) -> str:
                return token

            encoded = base64.b64encode(echo.encode()).decode()
            mcp.tool(leaky, description=f"Calls upstream with key {echo} (b64 {encoded}).")

        if forms is not None:
            import base64
            from urllib.parse import quote

            secret = forms
            mid = len(secret) // 2
            left, right = secret[:mid], secret[mid:]
            blob = base64.b64encode(json.dumps({"authorization": secret}).encode()).decode()
            as_hex = secret.encode().hex()
            as_pct = quote(secret, safe="").lower()
            as_b64 = base64.b64encode(secret.encode()).decode()
            self.echoed_forms = {
                "secret": secret, "left": left, "right": right, "blob": blob,
                "hex": as_hex, "pct": as_pct, "b64": as_b64,
            }

            def leaky_forms(
                part_a: str = left, part_b: str = right, as_hex: str = as_hex, as_pct: str = as_pct, as_b64: str = as_b64
            ) -> str:
                return "ok"

            mcp.tool(leaky_forms, name="leaky", description=blob)

        inner = mcp.http_app(path="/mcp")
        self.seen_headers: list[dict[str, str]] = []
        seen = self.seen_headers

        async def app(scope, receive, send):
            if scope["type"] == "http":
                headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
                seen.append(headers)
                if required_auth is not None and headers.get("authorization") != required_auth:
                    await send({"type": "http.response.start", "status": 401, "headers": [(b"content-length", b"0")]})
                    await send({"type": "http.response.body", "body": b""})
                    return
            await inner(scope, receive, send)

        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> LocalMcpServer:
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("local MCP server did not start")
            time.sleep(0.02)
        self.port = self.server.servers[0].sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}/mcp"
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


class RawHttp:
    """Loopback HTTP that is not a working MCP server. `jsonrpc` answers HTTP 200 with a JSON-RPC
    error whose id is the request's. Otherwise every method gets `status` (and `location`)."""

    def __init__(self, status: int, *, location: str | None = None, jsonrpc: bool = False) -> None:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        outer = self
        self.requests = 0

        class Handler(BaseHTTPRequestHandler):
            def _reply(self) -> None:
                outer.requests += 1
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                if jsonrpc:
                    try:
                        req = json.loads(raw or b"{}")
                    except json.JSONDecodeError:
                        req = {}
                    payload = json.dumps({
                        "jsonrpc": "2.0",
                        "id": req.get("id", 1),
                        "error": {"code": -32600, "message": "Invalid Request"},
                    }).encode()
                    code = 200
                else:
                    payload = b""
                    code = status
                self.send_response(code)
                if location:
                    self.send_header("Location", location)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if payload:
                    self.wfile.write(payload)

            do_GET = do_POST = do_DELETE = _reply  # noqa: N815 - http.server API

            def log_message(self, *args: object) -> None:
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}/mcp"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self) -> RawHttp:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


def _joined_strings(obj: Any, *, keys: bool) -> str:
    """String leaves of a JSON value, optionally including dict keys, concatenated in order."""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        parts = []
        for key, value in obj.items():
            if keys:
                parts.append(str(key))
            parts.append(_joined_strings(value, keys=keys))
        return "".join(parts)
    if isinstance(obj, list):
        return "".join(_joined_strings(item, keys=keys) for item in obj)
    return ""


@pytest.fixture
def dev_allow_loopback(monkeypatch: pytest.MonkeyPatch, app_client):
    """Use the real policy from the environment, with 127.0.0.1 on the dev allowlist."""
    monkeypatch.setenv(DEV_ALLOW_HOSTS_ENV, "127.0.0.1")
    _, app = app_client
    app.dependency_overrides.pop(get_outbound_policy, None)


# --- CRUD -----------------------------------------------------------------------------------------


class TestCrud:
    def test_create_list_get_patch_delete(self, app_client, tenant_headers):
        client, _ = app_client
        _, h = tenant_headers()
        r = register(client, h, name="docs", headers={"Authorization": f"Bearer {SENTINEL}", "X-Api-Key": SENTINEL})
        assert r.status_code == 201, r.text
        server = r.json()
        assert SENTINEL not in r.text
        assert server["name"] == "docs" and server["url"] == PUBLIC_URL
        assert server["transport"] == "streamable_http" and server["enabled"] is True
        assert server["header_names"] == ["authorization", "x-api-key"]
        assert server["last_status"] is None and server["last_checked_at"] is None
        assert server["last_error_code"] is None
        assert {"id", "created_at", "updated_at"} <= set(server)
        assert "headers" not in server and "tenant_id" not in server

        listed = client.get(BASE, headers=h)
        assert listed.status_code == 200 and [s["id"] for s in listed.json()] == [server["id"]]
        assert SENTINEL not in listed.text

        got = client.get(f"{BASE}/{server['id']}", headers=h)
        assert got.status_code == 200 and got.json() == server

        patched = client.patch(f"{BASE}/{server['id']}", json={"name": "docs2", "enabled": False}, headers=h)
        assert patched.status_code == 200, patched.text
        assert patched.json()["name"] == "docs2" and patched.json()["enabled"] is False
        assert patched.json()["header_names"] == ["authorization", "x-api-key"]  # untouched

        replaced = client.patch(f"{BASE}/{server['id']}", json={"headers": {"X-Other": SENTINEL}}, headers=h)
        assert replaced.status_code == 200 and replaced.json()["header_names"] == ["x-other"]
        assert SENTINEL not in replaced.text
        cleared = client.patch(f"{BASE}/{server['id']}", json={"headers": {}}, headers=h)
        assert cleared.status_code == 200 and cleared.json()["header_names"] == []

        assert client.delete(f"{BASE}/{server['id']}", headers=h).status_code == 204
        assert client.get(f"{BASE}/{server['id']}", headers=h).status_code == 404
        assert client.delete(f"{BASE}/{server['id']}", headers=h).status_code == 404

    def test_list_is_sorted_by_name(self, app_client, tenant_headers):
        client, _ = app_client
        _, h = tenant_headers()
        for name in ("zeta", "alpha", "mid"):
            assert register(client, h, name=name).status_code == 201
        assert [s["name"] for s in client.get(BASE, headers=h).json()] == ["alpha", "mid", "zeta"]

    def test_duplicate_name_in_a_tenant_is_409(self, app_client, tenant_headers):
        client, _ = app_client
        _, h = tenant_headers()
        assert register(client, h, name="docs").status_code == 201
        assert register(client, h, name="docs").status_code == 409
        other = register(client, h, name="other").json()
        assert client.patch(f"{BASE}/{other['id']}", json={"name": "docs"}, headers=h).status_code == 409

    @pytest.mark.parametrize(
        "body",
        [
            {"name": "", "url": PUBLIC_URL},
            {"name": "has/slash", "url": PUBLIC_URL},
            {"name": "mcp:colon", "url": PUBLIC_URL},
            {"name": "x" * 65, "url": PUBLIC_URL},
            {"name": "ok", "url": PUBLIC_URL, "transport": "stdio"},
            {"name": "ok", "url": PUBLIC_URL, "command": "rm -rf /"},
        ],
    )
    def test_invalid_bodies_are_422(self, app_client, tenant_headers, body):
        client, _ = app_client
        _, h = tenant_headers()
        assert client.post(BASE, json=body, headers=h).status_code == 422
        assert client.get(BASE, headers=h).json() == []

    def test_requires_auth(self, app_client):
        client, _ = app_client
        assert client.get(BASE).status_code == 401


class TestTenantIsolation:
    def test_other_tenant_gets_404_everywhere(self, app_client, tenant_headers):
        client, _ = app_client
        _, a = tenant_headers()
        _, b = tenant_headers()
        server = register(client, a, name="docs", headers={"Authorization": SENTINEL}).json()
        sid = server["id"]
        assert client.get(f"{BASE}/{sid}", headers=b).status_code == 404
        assert client.patch(f"{BASE}/{sid}", json={"enabled": False}, headers=b).status_code == 404
        assert client.patch(f"{BASE}/{sid}", json={"headers": {"x": "y"}}, headers=b).status_code == 404
        assert client.post(f"{BASE}/{sid}/test", headers=b).status_code == 404
        assert client.delete(f"{BASE}/{sid}", headers=b).status_code == 404
        assert client.get(BASE, headers=b).json() == []
        # Unchanged for its owner; the same name is free in the other tenant.
        assert client.get(f"{BASE}/{sid}", headers=a).json() == server
        assert register(client, b, name="docs").status_code == 201

    def test_unknown_or_malformed_id_is_404(self, app_client, tenant_headers):
        client, _ = app_client
        _, h = tenant_headers()
        assert client.get(f"{BASE}/{uuid.uuid4()}", headers=h).status_code == 404
        assert client.get(f"{BASE}/not-a-uuid", headers=h).status_code in (404, 422)


# --- URL policy at registration ---------------------------------------------------------------------


class TestUrlPolicy:
    @pytest.mark.parametrize(
        ("url", "code"),
        [
            ("http://mcp.example.com/mcp", "url_scheme_not_allowed"),
            ("https://user:pw@mcp.example.com/mcp", "url_credentials"),
            ("https://127.0.0.1/mcp", "address_blocked"),
            ("https://[::1]/mcp", "address_blocked"),
            ("https://[::ffff:169.254.169.254]/mcp", "address_blocked"),
            ("https://10.0.0.8/mcp", "address_blocked"),
            ("https://100.64.0.1/mcp", "address_blocked"),
            ("https://mcp.example.com/" + "a" * 3000, "url_too_long"),
        ],
    )
    def test_refused_on_create_and_patch(self, app_client, tenant_headers, url, code):
        client, _ = app_client
        _, h = tenant_headers()
        r = register(client, h, url=url)
        assert r.status_code == 422 and code in r.json()["detail"]
        assert "pw" not in r.text
        server = register(client, h).json()
        r = client.patch(f"{BASE}/{server['id']}", json={"url": url}, headers=h)
        assert r.status_code == 422 and code in r.json()["detail"]
        assert client.get(f"{BASE}/{server['id']}", headers=h).json()["url"] == PUBLIC_URL

    def test_name_resolving_to_private_address_is_refused(self, app_client, tenant_headers):
        client, app = app_client
        _, h = tenant_headers()
        app.dependency_overrides[get_outbound_policy] = lambda: OutboundPolicy(resolver=public_resolver("10.1.2.3"))
        r = register(client, h, url="https://internal.example.com/mcp")
        assert r.status_code == 422 and "address_blocked" in r.json()["detail"]
        assert client.get(BASE, headers=h).json() == []

    def test_http_loopback_needs_the_dev_allowlist(self, app_client, tenant_headers, monkeypatch):
        client, app = app_client
        _, h = tenant_headers()
        app.dependency_overrides.pop(get_outbound_policy, None)
        assert register(client, h, url="http://127.0.0.1:9/mcp").status_code == 422
        monkeypatch.setenv(DEV_ALLOW_HOSTS_ENV, "127.0.0.1")
        assert register(client, h, url="http://127.0.0.1:9/mcp").status_code == 201


# --- header secrets ---------------------------------------------------------------------------------


class TestHeaderSecrets:
    def test_values_are_encrypted_at_rest_and_bound_to_the_row(self, app_client, tenant_headers, secrets_key):
        client, app = app_client
        tenant, h = tenant_headers()
        server = register(client, h, headers={"Authorization": f"Bearer {SENTINEL}"}).json()
        with app.state.database_engine.connect() as conn:
            rows = conn.execute(
                text("SELECT tenant_id, server_id, header_name, ciphertext, key_id FROM mcp_server_secrets "
                     "WHERE server_id = :s"), {"s": server["id"]}
            ).all()
        assert len(rows) == 1
        row = rows[0]
        assert (row.tenant_id, row.header_name) == (tenant, "authorization")
        assert SENTINEL.encode() not in bytes(row.ciphertext)
        ring = Keyring.from_env({KEY_ENV: secrets_key})
        assert row.key_id == ring.current_key_id
        assert ring.decrypt(bytes(row.ciphertext), row.key_id,
                            context=f"{tenant}/{server['id']}/authorization".encode()) == f"Bearer {SENTINEL}"

    @pytest.mark.parametrize(
        "headers",
        [
            {"bad header": SENTINEL},
            {"Host": SENTINEL},
            {"Content-Length": SENTINEL},
            {"Mcp-Session-Id": SENTINEL},
            {"X-Ok": f"{SENTINEL}\r\nX-Injected: 1"},
            {"X-Ok": SENTINEL * 1000},
            {"A": SENTINEL, "a": SENTINEL},
            {f"x-{i}": SENTINEL for i in range(25)},
            {"X-Ok": 12345},
            [SENTINEL],
            SENTINEL,
        ],
    )
    def test_invalid_headers_are_refused_without_echoing_values(self, app_client, tenant_headers, headers):
        client, _ = app_client
        _, h = tenant_headers()
        r = register(client, h, headers=headers)
        assert r.status_code == 422
        assert SENTINEL not in r.text
        server = register(client, h).json()
        r = client.patch(f"{BASE}/{server['id']}", json={"headers": headers}, headers=h)
        assert r.status_code == 422 and SENTINEL not in r.text

    def test_missing_platform_key_is_a_clear_503_only_when_secrets_are_used(
        self, app_client, tenant_headers, monkeypatch
    ):
        client, _ = app_client
        _, h = tenant_headers()
        server = register(client, h, headers={"Authorization": SENTINEL}).json()
        monkeypatch.delenv(KEY_ENV)
        r = register(client, h, headers={"Authorization": SENTINEL})
        assert r.status_code == 503 and KEY_ENV in r.json()["detail"] and SENTINEL not in r.text
        assert register(client, h, name="no-secrets").status_code == 201
        t = client.post(f"{BASE}/{server['id']}/test", headers=h)
        assert t.status_code == 503 and KEY_ENV in t.json()["detail"]

    def test_malformed_platform_key_fails_api_startup(self, monkeypatch):
        monkeypatch.setenv(KEY_ENV, "definitely-not-a-key")
        with pytest.raises(RuntimeError) as err:
            create_app(database_url=PG_URL)
        assert KEY_ENV in str(err.value) and "definitely-not-a-key" not in str(err.value)


# --- connection test ------------------------------------------------------------------------------


class TestConnection:
    def test_lists_tools_of_a_local_mcp_server_with_the_decrypted_header(
        self, app_client, tenant_headers, dev_allow_loopback
    ):
        client, _ = app_client
        _, h = tenant_headers()
        with hard_timeout(60), LocalMcpServer(required_auth=f"Bearer {SENTINEL}") as mcp:
            server = register(client, h, url=mcp.url, headers={"Authorization": f"Bearer {SENTINEL}"}).json()
            r = client.post(f"{BASE}/{server['id']}/test", headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        assert SENTINEL not in r.text
        assert body["status"] == "ok" and body.get("error_code") is None
        tools = {t["name"]: t for t in body["tools"]}
        assert set(tools) == {"add", "search_docs"}
        assert tools["add"]["description"] == "Add two integers."
        assert tools["add"]["input_schema"]["properties"].keys() == {"a", "b"}
        assert set(tools["add"]["input_schema"]["required"]) == {"a", "b"}
        assert "query" in tools["search_docs"]["input_schema"]["properties"]
        assert any(seen.get("authorization") == f"Bearer {SENTINEL}" for seen in mcp.seen_headers)

        stored = client.get(f"{BASE}/{server['id']}", headers=h).json()
        assert stored["last_status"] == "ok" and stored["last_error_code"] is None
        assert stored["last_checked_at"] is not None

    def test_tool_listing_echoing_the_header_secret_is_redacted(
        self, app_client, tenant_headers, dev_allow_loopback
    ):
        """A server that echoes its header value into tools/list (description, schema default) never
        gets it into the connection test's response: the listing is scrubbed like tool results."""
        import base64

        client, _ = app_client
        _, h = tenant_headers()
        with hard_timeout(60), LocalMcpServer(required_auth=f"Bearer {SENTINEL}", echo=SENTINEL) as mcp:
            server = register(client, h, url=mcp.url, headers={"Authorization": f"Bearer {SENTINEL}"}).json()
            r = client.post(f"{BASE}/{server['id']}/test", headers=h)
        assert r.status_code == 200, r.text
        assert SENTINEL not in r.text
        assert base64.b64encode(SENTINEL.encode()).decode().rstrip("=") not in r.text
        body = r.json()
        assert body["status"] == "ok"
        leaky = next(t for t in body["tools"] if t["name"] == "leaky")
        assert "[REDACTED]" in leaky["description"]
        assert leaky["input_schema"]["properties"]["token"]["default"] == "[REDACTED]"
        assert {t["name"] for t in body["tools"]} == {"add", "search_docs", "leaky"}

    def test_split_or_encoded_secret_is_absent_from_the_listing(
        self, app_client, tenant_headers, dev_allow_loopback
    ):
        """The listing echoes the header secret only as two halves, or as hex / percent-encoding /
        base64 / a base64 JSON blob. None of those forms, and no join of the returned strings, is
        in the connection-test response."""
        client, _ = app_client
        _, h = tenant_headers()
        secret = "sntl-Form-4e8c-Encoded-91ab"
        with hard_timeout(60), LocalMcpServer(forms=secret) as mcp:
            server = register(client, h, url=mcp.url, headers={"Authorization": f"Bearer {secret}"}).json()
            r = client.post(f"{BASE}/{server['id']}/test", headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "ok", body
        forms = mcp.echoed_forms
        for label, value in forms.items():
            if label in ("left", "right"):
                continue  # each half is absent only once split masking joins them; checked below
            assert value not in r.text, label
            assert value.upper() not in r.text, label
        leaked = next(t for t in body["tools"] if t["name"] == "leaky")
        assert "[REDACTED]" in json.dumps(leaked)
        for keys in (True, False):
            joined = _joined_strings(body, keys=keys)
            assert secret.lower() not in joined.lower()
            assert forms["left"].lower() not in joined.lower()
            assert forms["right"].lower() not in joined.lower()

    def test_redirect_is_redirect_refused(self, app_client, tenant_headers, dev_allow_loopback):
        client, _ = app_client
        _, h = tenant_headers()
        with hard_timeout(60), RawHttp(302, location="http://127.0.0.1:9/mcp") as http:
            server = register(client, h, url=http.url).json()
            r = client.post(f"{BASE}/{server['id']}/test", headers=h)
            assert http.requests >= 1
        assert r.status_code == 200, r.text
        assert r.json() == {"status": "error", "error_code": "redirect_refused", "tools": []}
        assert "127.0.0.1:9" not in r.text

    def test_jsonrpc_error_is_protocol_error(self, app_client, tenant_headers, dev_allow_loopback):
        client, _ = app_client
        _, h = tenant_headers()
        with hard_timeout(60), RawHttp(200, jsonrpc=True) as http:
            server = register(client, h, url=http.url).json()
            r = client.post(f"{BASE}/{server['id']}/test", headers=h)
            assert http.requests >= 1
        assert r.status_code == 200, r.text
        assert r.json() == {"status": "error", "error_code": "protocol_error", "tools": []}

    def test_rejected_credentials_are_recorded_as_a_safe_code(self, app_client, tenant_headers, dev_allow_loopback):
        client, _ = app_client
        _, h = tenant_headers()
        with hard_timeout(60), LocalMcpServer(required_auth="Bearer right") as mcp:
            server = register(client, h, url=mcp.url, headers={"Authorization": f"Bearer {SENTINEL}"}).json()
            r = client.post(f"{BASE}/{server['id']}/test", headers=h)
        assert r.status_code == 200
        assert r.json() == {"status": "error", "error_code": "unauthorized", "tools": []}
        stored = client.get(f"{BASE}/{server['id']}", headers=h).json()
        assert (stored["last_status"], stored["last_error_code"]) == ("error", "unauthorized")

    def test_nothing_listening_is_connect_failed(self, app_client, tenant_headers, dev_allow_loopback):
        client, _ = app_client
        _, h = tenant_headers()
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        server = register(client, h, url=f"http://127.0.0.1:{port}/mcp").json()
        with hard_timeout(60):
            r = client.post(f"{BASE}/{server['id']}/test", headers=h)
        assert r.json() == {"status": "error", "error_code": "connect_failed", "tools": []}

    def test_unresponsive_server_times_out_within_the_bound(
        self, app_client, tenant_headers, dev_allow_loopback, monkeypatch
    ):
        client, _ = app_client
        _, h = tenant_headers()
        monkeypatch.setenv("EAP_MCP_TEST_TIMEOUT_SECONDS", "1")
        silent = socket.socket()
        silent.bind(("127.0.0.1", 0))
        silent.listen()  # accepts TCP, never answers HTTP
        try:
            server = register(client, h, url=f"http://127.0.0.1:{silent.getsockname()[1]}/mcp").json()
            started = time.monotonic()
            with hard_timeout(60):
                r = client.post(f"{BASE}/{server['id']}/test", headers=h)
            assert time.monotonic() - started < 10
        finally:
            silent.close()
        assert r.json() == {"status": "error", "error_code": "timeout", "tools": []}

    def test_dns_rebinding_after_registration_is_refused_at_connect(self, app_client, tenant_headers):
        """Registered while the name resolved publicly; at test time it resolves to 127.0.0.1."""
        client, app = app_client
        _, h = tenant_headers()
        with LocalMcpServer() as mcp:
            server = register(client, h, url=f"https://rebind.example.com:{mcp.port}/mcp").json()
            app.dependency_overrides[get_outbound_policy] = lambda: OutboundPolicy(resolver=public_resolver("127.0.0.1"))
            with hard_timeout(60):
                r = client.post(f"{BASE}/{server['id']}/test", headers=h)
            assert mcp.seen_headers == []
        assert r.json() == {"status": "error", "error_code": "address_blocked", "tools": []}


# --- secrets never leave through responses, logs or spans ---------------------------------------------


@pytest.fixture
def traced_client(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.setenv(DEV_ALLOW_HOSTS_ENV, "127.0.0.1")
    obs.shutdown_telemetry()
    span_exporter = InMemorySpanExporter()
    log_exporter = InMemoryLogRecordExporter()
    provider = obs.setup_telemetry("eap-api", exporter=span_exporter)
    obs.setup_logging("eap-api", exporter=log_exporter, level=logging.DEBUG)
    app = create_app(database_url=PG_URL)

    def spans():
        provider.force_flush()
        return span_exporter.get_finished_spans()

    def logs():
        obs._state.logger_provider.force_flush()
        return [r.log_record for r in log_exporter.get_finished_logs()]

    try:
        with TestClient(app) as client:
            yield client, spans, logs
    finally:
        app.state.database_engine.dispose()
        obs.shutdown_telemetry()


def test_sentinel_secret_never_appears_in_responses_logs_or_spans(traced_client, tenant_headers, caplog):
    client, spans, logs = traced_client
    _, h = tenant_headers()
    caplog.set_level(logging.DEBUG)
    responses = []
    with hard_timeout(90):
        with LocalMcpServer(required_auth=f"Bearer {SENTINEL}") as good, LocalMcpServer(required_auth="nope") as bad:
            ok = register(client, h, name="good", url=good.url, headers={"Authorization": f"Bearer {SENTINEL}"})
            fail = register(client, h, name="bad", url=bad.url, headers={"Authorization": f"Bearer {SENTINEL}"})
            responses += [ok, fail]
            sid, bid = ok.json()["id"], fail.json()["id"]
            responses.append(client.post(f"{BASE}/{sid}/test", headers=h))
            responses.append(client.post(f"{BASE}/{bid}/test", headers=h))
            responses.append(client.get(BASE, headers=h))
            responses.append(client.get(f"{BASE}/{sid}", headers=h))
            responses.append(client.patch(f"{BASE}/{sid}", json={"headers": {"X-Key": SENTINEL}}, headers=h))
            responses.append(client.patch(f"{BASE}/{sid}", json={"headers": {"bad name": SENTINEL}}, headers=h))
            responses.append(client.post(BASE, json={"name": "x", "url": PUBLIC_URL, "headers": [SENTINEL]}, headers=h))
            # A missing field or a non-object body must not echo the request (FastAPI's 422 would).
            responses.append(client.post(BASE, json={"url": PUBLIC_URL, "headers": {"A": SENTINEL}}, headers=h))
            responses.append(client.post(BASE, json=[SENTINEL], headers=h))
            responses.append(client.patch(f"{BASE}/{sid}", json={"bogus": 1, "headers": {"A": SENTINEL}}, headers=h))
            responses.append(client.delete(f"{BASE}/{bid}", headers=h))
    assert responses[2].json()["status"] == "ok"
    assert responses[3].json()["error_code"] == "unauthorized"

    for r in responses:
        assert SENTINEL not in r.text, r.request.url
        assert all(SENTINEL not in v for v in r.headers.values())

    for record in caplog.records:
        assert SENTINEL not in record.getMessage()
        assert SENTINEL not in repr(record.__dict__)
    assert SENTINEL not in caplog.text

    exported = logs()
    assert exported, "telemetry log pipeline captured nothing"
    for record in exported:
        assert SENTINEL not in str(record.body)
        assert SENTINEL not in json.dumps(dict(record.attributes or {}), default=str)

    finished = spans()
    assert any(s.name.startswith("POST /api/v1/mcp-servers") for s in finished)
    for span in finished:
        assert SENTINEL not in span.name
        assert SENTINEL not in json.dumps(dict(span.attributes or {}), default=str)
        for event in span.events:
            assert SENTINEL not in json.dumps(dict(event.attributes or {}), default=str)
        assert SENTINEL not in (span.status.description or "")
