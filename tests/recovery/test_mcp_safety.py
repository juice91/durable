"""MCP SSRF and secret-leak safety, whole-system (milestone 3, task 3.10).

The real API and worker run in their own processes and everything goes over real HTTP: servers are
registered, tested and changed through /api/v1/mcp-servers, agents using MCP tools are published and
run through the API, and an MCP server (worker_tests.mcp_helpers.LocalMcpServer, in this process on
127.0.0.1) answers the worker. Milestone 3 acceptance: "a private or loopback URL is refused; header
secrets never appear in API responses, logs or traces".

- M7a: without EAP_MCP_DEV_ALLOW_HOSTS, registering a loopback, private, link-local, CGNAT,
  unspecified or IPv4-in-IPv6 address (literal, by name or obfuscated) is refused with a 422 that
  does not echo the URL, and nothing is stored. With 127.0.0.1 allowed, other private targets
  are still refused at registration and on PATCH.
- M7b: the worker enforces the policy again on its own: a server the API accepted (127.0.0.1 on the
  API's dev allow list) is never contacted by a worker that does not allow it; the run fails
  mcp_tool_unavailable.
- M8: a header-secret sentinel reaches the MCP server but appears in none of: every API response
  (create, get, list, test connection, a 422 for a bad header, the runs, their events, SSE), the API
  and worker process logs (stdout+stderr files), the OTLP traces and logs both processes exported to
  a local collector, the runs / execution_events / side_effects rows, or this test's DBOS system
  schema (every table: inputs, outputs, step checkpoints). The server echoing the headers back (as a
  result and as a tool error) is redacted. Checked forms: plain, URL-encoded, hex and base64 at every
  alignment.
- M9: containment against a hostile server over real processes: a gzip-encoded result and an
  oversized result each end the run needs_attention after exactly one tools/call (no retry), and a
  remote $ref in a listed tool schema is never fetched.
- M10: kill -9 right after the MCP server answered a side-effecting call: the recovered run ends
  needs_attention without calling the server again, and the secret is in neither worker's log.
"""

from __future__ import annotations

import base64
import gzip
import importlib.util
import json
import math
import socket
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

from conftest import REPO_ROOT, Harness, wait_for


def _load_mcp_helpers() -> Any:
    """apps/worker/worker_tests/mcp_helpers.py, loaded by path (no sys.path change, no worker conftest)."""
    name = "recovery_mcp_helpers"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "apps/worker/worker_tests/mcp_helpers.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


LocalMcpServer = _load_mcp_helpers().LocalMcpServer
REDACTED = "[REDACTED]"
TERMINAL = {"completed", "failed", "needs_attention"}


def mcp_agent_spec(*pairs: tuple[str, str]) -> dict[str, Any]:
    return {
        "name": "MCP agent",
        "instructions": "Use the MCP tool.",
        "model": {"provider": "test", "name": "mcp"},
        "tools": [],
        "mcp_tools": [{"server": s, "tool": t} for s, t in pairs],
    }


def secrets_env(*, allow: str | None = "127.0.0.1", **extra: str | None) -> dict[str, str | None]:
    from eap_secrets import KEY_ENV, OLD_KEYS_ENV, generate_key
    from eap_secrets.outbound import DEV_ALLOW_HOSTS_ENV

    env: dict[str, str | None] = {
        KEY_ENV: generate_key(),
        OLD_KEYS_ENV: None,
        DEV_ALLOW_HOSTS_ENV: allow,
        "EAP_MCP_TIMEOUT_SECONDS": "20",
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
        "OTEL_EXPORTER_OTLP_ENDPOINT": None,  # only M8 exports, to its own collector
    }
    env.update(extra)
    return env


def leaked_forms(secret: str) -> list[bytes]:
    """The forms a secret could be written in: plain, URL-encoded, hex (both cases), and base64 of
    the secret inside any byte stream (one fully-determined slice per alignment)."""
    raw = secret.encode()
    forms = {raw, quote(secret, safe="").encode(), raw.hex().encode(), raw.hex().upper().encode()}
    for offset in range(3):
        encoded = base64.b64encode(b"\0" * offset + raw)
        start = math.ceil(8 * offset / 6)
        end = (8 * (offset + len(raw))) // 6
        forms.add(encoded[start:end])
        forms.add(base64.urlsafe_b64encode(b"\0" * offset + raw)[start:end])
    return sorted(forms)


def assert_no_secret(where: str, blob: str | bytes, secrets: list[str]) -> None:
    data = blob.encode() if isinstance(blob, str) else blob
    for secret in secrets:
        for form in leaked_forms(secret):
            assert form not in data, f"secret {secret[:8]}... found in {where} as {form[:40]!r}"


def run_agent(h: Harness, agent_id: str, request: dict[str, Any], *, timeout: float = 90) -> tuple[str, dict[str, Any]]:
    run_id = str(uuid.uuid4())
    r = h.post_run(agent_id, run_id, json.dumps(request))
    assert r.status_code == 201, r.text
    return run_id, h.wait_terminal(run_id, timeout=timeout)


def tool_results(h: Harness, run_id: str) -> list[dict[str, Any]]:
    return [e.data for e in h.events(run_id) if e.type == "tool_result"]


def test_leak_detector_finds_every_form() -> None:
    """Positive control for assert_no_secret: each encoding the checks rely on is actually caught."""
    import pickle

    secret = f"sntl{uuid.uuid4().hex}ctl"
    samples = [
        f"log line: Bearer {secret}",
        quote(f"Bearer {secret}", safe=""),
        f"Bearer {secret}".encode().hex(),
        *(base64.b64encode(b"p" * n + secret.encode() + b"q").decode() for n in range(6)),
        base64.b64encode(pickle.dumps({"authorization": f"Bearer {secret}"})).decode(),
        base64.urlsafe_b64encode(b"\xfb\xff" + secret.encode()).decode(),
    ]
    for sample in samples:
        with pytest.raises(AssertionError):
            assert_no_secret("sample", sample, [secret])
    assert_no_secret("clean", "Bearer [REDACTED] " + base64.b64encode(b"unrelated").decode(), [secret])


# --------------------------------------------------------------------------- an OTLP/HTTP collector


class OtlpCollector:
    """Accepts OTLP/HTTP (protobuf) exports on 127.0.0.1 and keeps every body (gunzipped)."""

    def __init__(self) -> None:
        self.payloads: list[tuple[str, bytes]] = []
        lock = threading.Lock()
        payloads = self.payloads

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server API
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                with lock:
                    payloads.append((self.path, body))
                self.send_response(200)
                self.send_header("Content-Type", "application/x-protobuf")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> OtlpCollector:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()

    def bodies(self, signal: str) -> list[bytes]:
        return [body for path, body in list(self.payloads) if path.endswith(f"/v1/{signal}")]

    def spans(self) -> list[tuple[str, str, dict[str, str]]]:
        """(service.name, span name, string attributes) of every exported span."""
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

        out = []
        for body in self.bodies("traces"):
            request = ExportTraceServiceRequest.FromString(body)
            for resource_spans in request.resource_spans:
                service = next(
                    (a.value.string_value for a in resource_spans.resource.attributes if a.key == "service.name"), ""
                )
                for scope in resource_spans.scope_spans:
                    for span in scope.spans:
                        attrs = {a.key: a.value.string_value for a in span.attributes}
                        out.append((service, span.name, attrs))
        return out


# --------------------------------------------------------------------------- a $ref-rewriting proxy


@contextmanager
def counting_listener() -> Iterator[tuple[int, dict[str, int]]]:
    """A 127.0.0.1 listener that only counts accepted connections (a $ref fetch would be one)."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    sock.settimeout(0.2)
    state = {"connections": 0, "stop": False}

    def accept() -> None:
        while not state["stop"]:
            try:
                conn, _ = sock.accept()
            except OSError:
                continue
            state["connections"] += 1
            conn.close()

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    try:
        yield sock.getsockname()[1], state
    finally:
        state["stop"] = True
        thread.join(timeout=2)
        sock.close()


class RefProxy:
    """Forwards to an MCP server (json_response mode) and rewrites tools/list so lookup_order's
    schema points at a remote $ref and requires an argument the model never sends. FastMCP strips
    a non-local $ref itself, so the hostile schema has to be introduced on the wire."""

    def __init__(self, upstream: str, ref: str) -> None:
        import uvicorn

        self.rewritten = 0

        async def app(scope: dict, receive: Any, send: Any) -> None:
            import httpx2

            if scope["type"] != "http":
                return
            body, more = b"", True
            while more:
                message = await receive()
                body += message.get("body", b"")
                more = message.get("more_body", False)
            headers = {k.decode(): v.decode() for k, v in scope["headers"]
                       if k.decode().lower() not in {"host", "content-length"}}
            async with httpx2.AsyncClient(timeout=30.0) as client:
                response = await client.request(scope["method"], upstream, content=body, headers=headers)
            out = response.content
            try:
                payload = json.loads(out) if out else None
            except ValueError:
                payload = None
            tools = (payload.get("result") or {}).get("tools") if isinstance(payload, dict) else None
            if isinstance(tools, list):
                for tool in tools:
                    if tool.get("name") == "lookup_order":
                        tool["inputSchema"] = {"type": "object", "properties": {"order_id": {"$ref": ref}},
                                               "required": ["order_id", "must_be_present"]}
                        self.rewritten += 1
                out = json.dumps(payload).encode()
            hop = {"content-length", "transfer-encoding", "connection", "content-encoding"}
            resp_headers = [(k.encode(), v.encode()) for k, v in response.headers.multi_items() if k.lower() not in hop]
            resp_headers.append((b"content-length", str(len(out)).encode()))
            await send({"type": "http.response.start", "status": response.status_code, "headers": resp_headers})
            await send({"type": "http.response.body", "body": out})

        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> RefProxy:
        self.thread.start()
        wait_for(lambda: self.server.started, timeout=10, what="the $ref proxy to start", interval=0.02)
        self.url = f"http://127.0.0.1:{self.server.servers[0].sockets[0].getsockname()[1]}/mcp"
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


# --------------------------------------------------------------------------- M7: SSRF


BLOCKED_URLS = [
    "https://127.0.0.1/mcp",
    "https://localhost/mcp",
    "https://[::1]/mcp",
    "https://10.0.0.5/mcp",
    "https://172.16.0.1/mcp",
    "https://192.168.1.10/mcp",
    "https://169.254.169.254/latest/meta-data/",
    "https://[fe80::1]/mcp",
    "https://100.64.0.1/mcp",
    "https://0.0.0.0/mcp",
    "https://[::ffff:127.0.0.1]/mcp",
    "https://[::ffff:169.254.169.254]/mcp",
    "https://[64:ff9b::a9fe:a9fe]/mcp",
    "https://127.1/mcp",
    "https://2130706433/mcp",
    "https://0x7f000001/mcp",
    "https://[fd00::1]/mcp",
    "https://[fc00::5]/mcp",
    "https://[2002:a00:5::1]/mcp",
    "https://[2002:7f00:1::1]/mcp",
    "http://127.0.0.1:8000/mcp",
    "https://user:pw@10.0.0.5/mcp",
]
# Non-localhost DNS names resolving to private addresses. The API process resolves them through a
# test-only getaddrinfo stub (api_server.py, EAP_TEST_DNS_STUB): no /etc/hosts edit, no real DNS.
# Without the stub they would be refused as host_unresolvable, so the address_blocked code below also
# proves the stub answered.
DNS_STUB = {
    "intranet.ssrf-test.example": ["10.0.0.5"],
    "metadata.ssrf-test.example": ["169.254.169.254"],
    "ula.ssrf-test.example": ["fd00::5"],
    "mixed.ssrf-test.example": ["93.184.216.34", "10.0.0.5"],  # one public record does not help
}
BLOCKED_NAMES = [f"https://{name}/mcp" for name in DNS_STUB]


def test_m7a_private_loopback_and_link_local_urls_are_refused(harness: Harness, report: Callable[[str], None]) -> None:
    h = harness
    stub = ",".join(f"{name}={'|'.join(addrs)}" for name, addrs in DNS_STUB.items())
    api = h.start_api(extra_env=secrets_env(allow=None, EAP_TEST_DNS_STUB=stub))
    assert "EAP_TEST_DNS_STUB active" in api.log()
    refused: dict[str, str] = {}
    for url in [*BLOCKED_URLS, *BLOCKED_NAMES]:
        r = h.client.post("/api/v1/mcp-servers", json={"name": "evil", "url": url}, headers=h.headers)
        assert r.status_code == 422, (url, r.status_code, r.text)
        detail = r.json()["detail"]
        assert isinstance(detail, str) and detail.startswith("url refused by the outbound policy: "), (url, detail)
        assert url not in r.text and "10.0.0.5" not in r.text and "169.254" not in r.text, r.text
        assert "ssrf-test" not in r.text and "fd00" not in r.text, r.text
        refused[url] = detail.rsplit(": ", 1)[1]
    listed = h.client.get("/api/v1/mcp-servers", headers=h.headers)
    assert listed.status_code == 200 and listed.json() in ([], {"items": []}), listed.text
    assert h.sql("SELECT count(*) FROM mcp_servers WHERE tenant_id = :t", t=h.tenant)[0][0] == 0
    for url in ("https://127.0.0.1/mcp", "https://10.0.0.5/mcp", "https://169.254.169.254/latest/meta-data/",
                "https://[fd00::1]/mcp", "https://[fc00::5]/mcp", "https://[2002:a00:5::1]/mcp",
                "https://[2002:7f00:1::1]/mcp", *BLOCKED_NAMES):
        assert refused[url] == "address_blocked", (url, refused[url])
    assert refused["http://127.0.0.1:8000/mcp"] == "url_scheme_not_allowed"
    assert refused["https://user:pw@10.0.0.5/mcp"] == "url_credentials"
    report(f"M7a {len(BLOCKED_URLS)} URLs + {len(BLOCKED_NAMES)} DNS names (stubbed to private) refused (422, URL not echoed, nothing stored): "
           f"codes {sorted(set(refused.values()))}")


def test_m7b_worker_refuses_a_url_its_own_policy_blocks(harness: Harness, report: Callable[[str], None]) -> None:
    """The API allows 127.0.0.1 (dev allow list); the worker does not. The worker re-checks the URL
    itself, so the MCP server never receives a single request and the run fails cleanly."""
    h = harness
    env = secrets_env()
    with LocalMcpServer() as server:
        h.start_api(extra_env=env)
        h.start_worker("worker-no-allow", extra_env={**env, "EAP_MCP_DEV_ALLOW_HOSTS": None})
        r = h.client.post("/api/v1/mcp-servers", json={"name": "orders", "url": server.url}, headers=h.headers)
        assert r.status_code == 201, r.text
        agent_id = h.create_published_agent(mcp_agent_spec(("orders", "lookup_order")))
        run_id, run = run_agent(h, agent_id, {"tool": "lookup_order", "args": {"order_id": "S1"}})
        time.sleep(0.5)
        assert server.seen_headers == [], server.seen_headers
        assert server.call_count() == 0
    assert run["status"] == "failed", run
    end = h.events(run_id)[-1]
    assert end.type == "run_failed" and end.data["error_code"] == "mcp_tool_unavailable", end.data
    assert server.url not in str(run["error"])
    report(f"M7b API-accepted 127.0.0.1 server, worker without the allow host: 0 requests reached the server, "
           f"run failed {end.data['error_code']}")


class Redirector:
    """An allowlisted 'MCP server' (127.0.0.1) that answers every request with a redirect to
    `location` and counts the requests it got."""

    def __init__(self, status: int, location: str) -> None:
        self.requests = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _redirect(self) -> None:
                outer.requests += 1
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                self.send_response(status)
                self.send_header("Location", location)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_GET = do_POST = do_DELETE = _redirect  # noqa: N815 - http.server API

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/mcp"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> Redirector:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()


def test_m11_redirects_are_never_followed(harness: Harness, report: Callable[[str], None]) -> None:
    """An allowlisted MCP server answers with 302 / 307 to a second listener on another port. That
    target is itself on the dev allow list (127.0.0.1), so only the no-redirect rule can stop it: the
    listener must accept no connection through the API's connection test, nor through a real agent
    run's tool discovery and call."""
    h = harness
    env = secrets_env()
    outcomes: dict[int, tuple[str, str]] = {}
    with counting_listener() as (port, target):
        location = f"http://127.0.0.1:{port}/mcp"
        with Redirector(302, location) as r302, Redirector(307, location) as r307:
            h.start_api(extra_env=env)
            h.start_worker("worker", extra_env=env)
            for status, redirector in ((302, r302), (307, r307)):
                name = f"redir{status}"
                created = h.client.post("/api/v1/mcp-servers", json={"name": name, "url": redirector.url},
                                        headers=h.headers)
                assert created.status_code == 201, created.text
                tested = h.client.post(f"/api/v1/mcp-servers/{created.json()['id']}/test", headers=h.headers)
                assert tested.status_code == 200, tested.text
                assert tested.json()["status"] == "error" and tested.json()["tools"] == [], tested.text
                test_code = tested.json()["error_code"]
                assert test_code == "redirect_refused" and location not in tested.text, tested.text
                assert redirector.requests >= 1
                assert target["connections"] == 0, f"API connection test followed a {status} redirect"

                # Publishing only checks the server row; the worker meets the redirect at discovery.
                agent_id = h.create_published_agent(mcp_agent_spec((name, "lookup_order")))
                run_id, run = run_agent(h, agent_id, {"tool": "lookup_order", "args": {"order_id": "X1"}})
                time.sleep(0.5)
                assert target["connections"] == 0, f"worker followed a {status} redirect"
                assert run["status"] == "failed", run
                end = h.events(run_id)[-1]
                assert end.type == "run_failed" and end.data["error_code"] == "mcp_tool_unavailable", end.data
                assert location not in str(run["error"]) and str(port) not in str(run["error"])
                assert tool_results(h, run_id) == []  # never got as far as a call
                inner = str(run["error"]).rsplit(": ", 1)[-1]
                # The worker names the refused redirect redirect_refused, like the API's connection
                # test; nothing was followed and no call was sent.
                assert inner.endswith("(redirect_refused)"), inner
                assert any("error_code=redirect_refused" in w.log() for w in h.workers), h.worker_logs()
                outcomes[status] = (test_code, end.data["error_code"], inner)
    report(f"M11 302/307 to an allowlisted second port: 0 connections followed; "
           f"(connection test code, run error_code, worker's inner code) = {outcomes}")


# --------------------------------------------------------------------------- M8: secrets never leak


def test_m8_header_secret_never_appears_in_responses_logs_traces_or_storage(
    harness: Harness, report: Callable[[str], None]
) -> None:
    h = harness
    sentinel, sentinel2 = f"sntl{uuid.uuid4().hex}auth", f"sntl{uuid.uuid4().hex}akey"
    secrets = [sentinel, sentinel2]
    secret_headers = {"Authorization": f"Bearer {sentinel}", "X-Api-Key": sentinel2}
    responses: list[tuple[str, str]] = []

    def keep(what: str, response: Any) -> Any:
        responses.append((what, response.text))
        return response

    with OtlpCollector() as collector, LocalMcpServer(required_auth=f"Bearer {sentinel}", echo_headers=True) as server:
        env = secrets_env(
            OTEL_EXPORTER_OTLP_ENDPOINT=collector.url,
            OTEL_BSP_SCHEDULE_DELAY="200",
            OTEL_BLRP_SCHEDULE_DELAY="200",
        )
        api = h.start_api(extra_env=env)
        worker = h.start_worker("worker", extra_env=env)

        created = keep("create", h.client.post(
            "/api/v1/mcp-servers", json={"name": "orders", "url": server.url, "headers": secret_headers},
            headers=h.headers,
        ))
        assert created.status_code == 201, created.text
        server_id = created.json()["id"]
        assert sorted(created.json()["header_names"]) == ["authorization", "x-api-key"], created.json()
        assert keep("get", h.client.get(f"/api/v1/mcp-servers/{server_id}", headers=h.headers)).status_code == 200
        assert keep("list", h.client.get("/api/v1/mcp-servers", headers=h.headers)).status_code == 200
        tested = keep("test", h.client.post(f"/api/v1/mcp-servers/{server_id}/test", headers=h.headers))
        assert tested.status_code == 200 and tested.json()["status"] == "ok", tested.text
        assert {"echo_headers", "fail_echo_headers"} <= {t["name"] for t in tested.json()["tools"]}
        # A rejected header value is not echoed by the 422 either.
        bad = keep("bad-header", h.client.patch(
            f"/api/v1/mcp-servers/{server_id}", json={"headers": {"X-Api-Key": sentinel2 + "\n"}}, headers=h.headers
        ))
        assert bad.status_code == 422, bad.text
        # SSRF on update: 127.0.0.1 is allowed for development, nothing else private is.
        for url in ("https://10.0.0.5/mcp", f"http://localhost:{server.port}/mcp", "https://169.254.169.254/"):
            moved = keep("patch-url", h.client.patch(f"/api/v1/mcp-servers/{server_id}", json={"url": url},
                                                     headers=h.headers))
            assert moved.status_code == 422 and "outbound policy" in moved.text, (url, moved.text)
        assert h.sql("SELECT url FROM mcp_servers WHERE id = :i", i=server_id)[0][0] == server.url

        agent_id = h.create_published_agent(
            mcp_agent_spec(("orders", "echo_headers"), ("orders", "fail_echo_headers"), ("orders", "lookup_order"))
        )
        runs: dict[str, str] = {}
        for tool in ("echo_headers", "fail_echo_headers", "lookup_order"):
            args = {"order_id": "A1"} if tool == "lookup_order" else {}
            run_id, run = run_agent(h, agent_id, {"tool": tool, "args": args})
            assert run["status"] == "completed", (tool, run, h.worker_logs())
            runs[tool] = run_id
            keep(f"run {tool}", h.client.get(f"/api/v1/runs/{run_id}", headers=h.headers))
            keep(f"events {tool}", h.client.get(f"/api/v1/runs/{run_id}/events", headers=h.headers))
            frames = h.sse(run_id, after_seq=-1)
            run_events = [f for f in frames if f.event == "run_event"]
            assert run_events and run_events[-1].data["type"] == "run_completed", frames
            assert len(run_events) == len(h.events(run_id))
            responses.append((f"sse {tool}", repr([(f.event, f.id, f.data, f.comments) for f in frames])))
        keep("runs list", h.client.get("/api/v1/runs?limit=200", headers=h.headers))

        # The secrets did reach the MCP server, on every request (discovery, calls, the API's test).
        with_secrets = [s for s in server.seen_headers
                        if s.get("authorization") == f"Bearer {sentinel}" and s.get("x-api-key") == sentinel2]
        assert with_secrets and len(with_secrets) == len(server.seen_headers)
        echoed = tool_results(h, runs["echo_headers"])
        assert len(echoed) == 1 and echoed[0]["status"] == "ok", echoed
        assert echoed[0]["content"]["authorization"] == REDACTED and echoed[0]["content"]["x-api-key"] == REDACTED
        failed = tool_results(h, runs["fail_echo_headers"])
        assert len(failed) == 1 and failed[0]["status"] == "error", failed
        assert server.call_count("echo_headers") == 1 and server.call_count("fail_echo_headers") == 1

        # Wait until the worker has exported the runs' spans, then stop both processes (flush).
        wait_for(lambda: all(r in {s[2].get("eap.run_id") for s in collector.spans()} for r in runs.values()),
                 timeout=30, what="the worker's eap.run spans at the collector")
        worker.stop()
        api.stop()

    # 1. API responses.
    for what, text_ in responses:
        assert_no_secret(f"API response ({what})", text_, secrets)
    # 2. Process logs (stdout+stderr of the API and the worker).
    api_log, worker_log = api.log(), worker.log()
    assert "POST /api/v1/mcp-servers" in api_log and "EAP_WORKER_READY" in worker_log
    assert_no_secret("API process log", api_log, secrets)
    assert_no_secret("worker process log", worker_log, secrets)
    # 3. OTLP traces and logs exported by both processes.
    spans = collector.spans()
    services = {s[0] for s in spans}
    assert {"eap-api", "eap-worker"} <= services, services
    assert any(s[1].startswith("POST") for s in spans if s[0] == "eap-api")
    for path, body in collector.payloads:
        assert_no_secret(f"OTLP export {path}", body, secrets)
    assert collector.bodies("logs"), "no OTLP log export arrived"
    # 4. Storage: the runs, their events and side effects, and the DBOS system schema.
    stored = {
        "runs": h.sql("SELECT row_to_json(r)::text FROM runs r WHERE tenant_id = :t", t=h.tenant),
        "execution_events": h.sql(
            "SELECT row_to_json(e)::text FROM execution_events e WHERE run_id IN "
            "(SELECT id FROM runs WHERE tenant_id = :t)", t=h.tenant),
        "side_effects": h.sql("SELECT row_to_json(s)::text FROM side_effects s WHERE tenant_id = :t", t=h.tenant),
        "mcp_server_secrets": h.sql("SELECT encode(ciphertext, 'escape') FROM mcp_server_secrets WHERE tenant_id = :t",
                                    t=h.tenant),
    }
    assert len(stored["runs"]) == 3 and len(stored["side_effects"]) == 3 and len(stored["mcp_server_secrets"]) == 2
    for table, rows in stored.items():
        assert_no_secret(f"table {table}", repr([r[0] for r in rows]), secrets)
    dbos_rows = _dbos_schema_dump(h)
    assert any(r in dbos_rows for r in runs.values())
    assert_no_secret("DBOS system schema", dbos_rows, secrets)
    report(
        f"M8 sentinel (2 header secrets) reached the server on {len(with_secrets)} requests; absent from "
        f"{len(responses)} API responses, API log ({len(api_log)} B), worker log ({len(worker_log)} B), "
        f"{len(collector.payloads)} OTLP exports ({len(spans)} spans from {sorted(services)}), "
        f"runs/events/side_effects and {dbos_rows.count(chr(10))} DBOS rows"
    )


def _dbos_schema_dump(h: Harness) -> str:
    """Every row of every table in this test's DBOS system schema, as text."""
    import psycopg

    from conftest import DBOS_DB_URL, _libpq

    lines: list[str] = []
    with psycopg.connect(_libpq(DBOS_DB_URL), connect_timeout=5) as conn:
        tables = [r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s", (h.schema,))]
        for table in tables:
            for row in conn.execute(f'SELECT row_to_json(t)::text FROM "{h.schema}"."{table}" t'):
                lines.append(row[0])
    return "\n".join(lines)


# --------------------------------------------------------------------------- M9: containment


def test_m9_hostile_server_responses_are_contained(harness: Harness, report: Callable[[str], None]) -> None:
    h = harness
    env = secrets_env()
    sentinel = f"sntl{uuid.uuid4().hex}m9"
    with (
        LocalMcpServer(big_results=True, json_response=True, gzip_results={"cancel_order": 4096}) as server,
        counting_listener() as (ref_port, ref_state),
        LocalMcpServer(json_response=True) as plain,
        RefProxy(plain.url, f"http://127.0.0.1:{ref_port}/schema.json") as proxy,
    ):
        h.start_api(extra_env=env)
        worker = h.start_worker("worker", extra_env=env)
        for name, url in (("hostile", server.url), ("refd", proxy.url)):
            r = h.client.post("/api/v1/mcp-servers", json={"name": name, "url": url,
                                                           "headers": {"X-Api-Key": sentinel}}, headers=h.headers)
            assert r.status_code == 201, r.text
        hostile = h.create_published_agent(mcp_agent_spec(("hostile", "cancel_order"), ("hostile", "big_result")))
        refd = h.create_published_agent(mcp_agent_spec(("refd", "lookup_order")))

        # gzip: refused unread after the one call; the outcome is unknown, so needs_attention.
        gz_run, gz = run_agent(h, hostile, {"tool": "cancel_order", "args": {"order_id": "G1"}})
        time.sleep(0.5)  # a re-send would POST tools/call again within ~250 ms
        assert gz["status"] == "needs_attention", gz
        assert server.tool_call_posts.count("cancel_order") == 1 and server.call_count("cancel_order") == 1
        asked = [s["accept-encoding"] for s in server.seen_headers if s.get("accept-encoding")]
        assert asked and set(asked) == {"identity"}, asked

        # Oversized (8 MB against the 1 MiB cap): needs_attention well inside the timeout, one call.
        started = time.monotonic()
        big_run, big = run_agent(h, hostile, {"tool": "big_result", "args": {"size": 8_000_000}})
        elapsed = time.monotonic() - started
        assert big["status"] == "needs_attention", big
        assert elapsed < 30, elapsed
        assert server.tool_call_posts.count("big_result") == 1 and server.call_count("big_result") == 1

        # Remote $ref in the listed schema: never fetched; the call proceeds.
        ref_run, ref = run_agent(h, refd, {"tool": "lookup_order", "args": {"order_id": "R1"}})
        time.sleep(0.5)  # a late schema fetch would still hit the listener
        assert proxy.rewritten >= 1
        assert ref["status"] == "completed", ref
        assert ref_state["connections"] == 0
        assert plain.call_count("lookup_order") == 1
        worker.stop()

    for run_id in (gz_run, big_run):
        assert h.events(run_id)[-1].data["status"] == "needs_attention"
        assert [w[0] for w in h.workflows(run_id)] == ["SUCCESS"], h.workflows(run_id)
        assert str(h.run_row(run_id)["error"]).startswith("SideEffectOutcomeUnknown")
    assert sorted(h.side_effect_states()) == ["completed", "reserved", "reserved"]
    result = tool_results(h, ref_run)
    assert len(result) == 1 and result[0]["status"] == "ok" and result[0]["content"]["order_id"] == "R1"
    big_text = repr(h.sql("SELECT row_to_json(e)::text FROM execution_events e WHERE run_id = :r", r=big_run))
    assert len(big_text) < 100_000, len(big_text)  # nothing of the 8 MB body was stored
    assert_no_secret("worker log", worker.log(), [sentinel])
    report(f"M9 gzip -> needs_attention (1 POST, identity only); 8 MB result -> needs_attention in {elapsed:.1f}s "
           f"(1 POST); remote $ref never fetched (0 connections), run completed")


# --------------------------------------------------------------------------- M10: crash after an MCP call


def test_m10_kill_after_the_mcp_server_answered(harness: Harness, report: Callable[[str], None]) -> None:
    h = harness
    env = secrets_env()
    sentinel = f"sntl{uuid.uuid4().hex}m10"
    with LocalMcpServer(required_auth=f"Bearer {sentinel}") as server:
        h.start_api(extra_env=env)
        r = h.client.post("/api/v1/mcp-servers", json={"name": "orders", "url": server.url,
                                                       "headers": {"Authorization": f"Bearer {sentinel}"}},
                          headers=h.headers)
        assert r.status_code == 201, r.text
        agent_id = h.create_published_agent(mcp_agent_spec(("orders", "cancel_order")))
        first = h.start_worker("worker1-crashing", crash_at="after_tool_call", extra_env=env)
        run_id = str(uuid.uuid4())
        assert h.post_run(agent_id, run_id, json.dumps({"args": {"order_id": "K1"}})).status_code == 201
        assert first.wait_exit() == 137, first.log()[-4000:]
        assert server.call_count("cancel_order") == 1
        requests_before = len(server.seen_headers)
        assert h.side_effect_states() == ["reserved"]

        second = h.start_worker("worker2-recovering", extra_env=env)
        run = h.wait_terminal(run_id)
        time.sleep(0.5)
        assert run["status"] == "needs_attention", (run, h.worker_logs())
        assert server.call_count("cancel_order") == 1  # never called again
        assert len(server.seen_headers) == requests_before  # not even a discovery request
    events = h.events(run_id)
    assert [e.seq for e in events] == list(range(len(events))) and len({e.event_key for e in events}) == len(events)
    assert [e.event_key for e in events].count("end") == 1
    assert [w[0] for w in h.workflows(run_id)] == ["SUCCESS"]
    frames = [f for f in h.sse(run_id, after_seq=-1) if f.event == "run_event"]
    assert [f.data["seq"] for f in frames] == [e.seq for e in events]
    for name, text_ in (("crashed worker log", first.log()), ("recovering worker log", second.log())):
        assert_no_secret(name, text_, [sentinel])
    assert_no_secret("events", repr([e.data for e in events]), [sentinel])
    report("M10 after_tool_call on an MCP side effect: needs_attention once, server called once, "
           "no request after recovery, secret in no worker log")


class FixedReply:
    """Allowlisted HTTP stand-in: a fixed status, or HTTP 200 with a JSON-RPC error (id echoed)."""

    def __init__(self, status: int = 200, *, location: str | None = None, jsonrpc: bool = False) -> None:
        self.requests = 0
        outer = self

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
                        "jsonrpc": "2.0", "id": req.get("id", 1),
                        "error": {"code": -32600, "message": "Invalid Request"},
                    }).encode()
                    code = 200
                else:
                    payload, code = b"", status
                self.send_response(code)
                if location:
                    self.send_header("Location", location)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if payload:
                    self.wfile.write(payload)

            do_GET = do_POST = do_DELETE = _reply  # noqa: N815 - http.server API

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/mcp"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> FixedReply:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()


def test_connection_test_and_agent_run_report_the_same_failure_code(
    harness: Harness, report: Callable[[str], None]
) -> None:
    """POST /mcp-servers/{id}/test and an agent run against the same server report the same failure
    code: 401 -> unauthorized, 302 -> redirect_refused, a JSON-RPC error -> protocol_error.

    A TLS failure is not covered here. httpx raises ConnectError whose chain contains no ssl.SSLError
    (the certificate error is only in the message), and the classifier does not read exception text,
    so both sides report connect_failed rather than tls_failed."""
    h = harness
    env = secrets_env()
    agreed: list[tuple[str, str]] = []
    with FixedReply(401) as unauth, FixedReply(302, location="http://127.0.0.1:9/gone") as redir, FixedReply(jsonrpc=True) as rpc:
        h.start_api(extra_env=env)
        h.start_worker("worker", extra_env=env)
        for expected, server, name in (
            ("unauthorized", unauth, "unauth"),
            ("redirect_refused", redir, "redir"),
            ("protocol_error", rpc, "rpcerr"),
        ):
            created = h.client.post("/api/v1/mcp-servers", json={"name": name, "url": server.url}, headers=h.headers)
            assert created.status_code == 201, created.text
            tested = h.client.post(f"/api/v1/mcp-servers/{created.json()['id']}/test", headers=h.headers)
            assert tested.status_code == 200, tested.text
            assert tested.json()["status"] == "error" and tested.json()["tools"] == [], tested.text
            test_code = tested.json()["error_code"]
            assert "127.0.0.1:9" not in tested.text
            agent_id = h.create_published_agent(mcp_agent_spec((name, "lookup_order")))
            run_id, run = run_agent(h, agent_id, {"tool": "lookup_order", "args": {"order_id": "X1"}})
            assert run["status"] == "failed", (name, run)
            end = h.events(run_id)[-1]
            assert end.type == "run_failed" and end.data["error_code"] == "mcp_tool_unavailable", end.data
            assert str(run["error"]).endswith(f"({expected})"), run["error"]
            assert test_code == expected, (name, test_code, run["error"])
            assert server.requests >= 1
            agreed.append((name, test_code))
    report(f"connection test and worker failure codes agree: {agreed}")


@pytest.fixture(autouse=True)
def _no_otel_in_pytest(monkeypatch: pytest.MonkeyPatch) -> None:
    """The in-process app the harness builds must not export anywhere (only the processes do)."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)

