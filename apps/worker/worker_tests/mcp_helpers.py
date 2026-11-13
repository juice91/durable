"""Helpers for MCP tool tests (task 3.5): a local MCP server, server registration, specs.

- `LocalMcpServer`: FastMCP over Streamable HTTP in a uvicorn thread on 127.0.0.1 (use with
  EAP_MCP_DEV_ALLOW_HOSTS=127.0.0.1, see the `mcp_env` fixture). Two tools, `lookup_order` and
  `cancel_order`. Counts executed tool calls (`call_count`, `calls`) and records every HTTP
  request's headers (`seen_headers`); with `required_auth` it answers 401 unless the Authorization
  header matches. It runs in the pytest process, so a worker subprocess can call it too.
  Opt-in: `echo_headers=True` adds `echo_headers` / `fail_echo_headers` (return or fail quoting the
  request headers, for redaction tests); `failing={"cancel_order"}` makes those tools record the
  call and then answer with a tool error (isError); `reject={"cancel_order": -32602}` answers that
  tool's tools/call with a JSON-RPC error (recorded, counted in `call_count`). More options (input_required,
  json_response, failing_message, big_results, tool_call_posts) are documented on `__init__`.
- `register_mcp_server`: inserts mcp_servers / mcp_server_secrets rows the way the API does
  (header values sealed with the platform key from the environment, bound to tenant/server/header).
- `mcp_spec`: an agent spec dict for `test:mcp` listing the given (server, tool) pairs.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Iterable
from typing import Any

import uvicorn
from sqlalchemy import text
from sqlalchemy.engine import Engine

from eap_secrets import load_keyring, mcp_header_context


class LocalMcpServer:
    TOOLS = ("lookup_order", "cancel_order")

    def __init__(
        self,
        required_auth: str | None = None,
        *,
        echo_headers: bool = False,
        failing: Iterable[str] = (),
        reject: dict[str, int] | None = None,
        input_required: Iterable[str] = (),
        json_response: bool = False,
        failing_message: str | None = None,
        big_results: bool = False,
        gzip_results: dict[str, int] | None = None,
    ) -> None:
        """`echo_headers`: also expose `echo_headers` (returns the request's headers, Authorization
        included) and `fail_echo_headers` (a tool error quoting them), to test secret redaction.
        `failing`: tool names that record the call, then answer with a tool error (isError).
        `reject`: tool name -> JSON-RPC error code; a tools/call for it is recorded (it counts in
        `call_count`) and answered with that JSON-RPC error instead of reaching the tool.
        `input_required`: tool names whose tools/call is recorded and answered with an
        InputRequiredResult carrying only a request_state (a client that re-sends would re-execute).
        `json_response`: answer with application/json bodies instead of SSE.
        `failing_message`: the error text of `failing` tools (default: quotes the request headers).
        `big_results`: also expose `big_result(size)`, returning a string of `size` characters.
        `gzip_results`: tool name -> plaintext size; its tools/call is recorded and answered with a
        gzip-encoded application/json result whose text is that many bytes (built by a streaming
        compressor, so the plaintext never exists in memory), whatever Accept-Encoding said.
        Every tools/call POST seen on the wire is appended to `tool_call_posts` (tool names).
        The server's own FastMCP telemetry is suppressed: it stands in for a remote server, so its
        spans must not mix with the worker's in a test's span exporter."""
        from fastmcp import FastMCP
        from fastmcp.exceptions import ToolError
        from fastmcp.server.dependencies import get_http_headers

        failing = frozenset(failing)
        self.tool_call_posts: list[str] = []
        self._intercept = {"reject": dict(reject or {}), "input_required": frozenset(input_required),
                           "gzip": dict(gzip_results or {})}

        mcp = FastMCP("worker-test")
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.seen_headers: list[dict[str, str]] = []
        self._lock = threading.Lock()

        @mcp.tool
        def lookup_order(order_id: str) -> dict[str, str]:
            """Look up an order's status."""
            self._record("lookup_order", {"order_id": order_id})
            if "lookup_order" in failing:
                raise ToolError(failing_message or f"lookup failed; request headers were {get_http_headers(include_all=True)}")
            return {"order_id": order_id, "status": "shipped"}

        @mcp.tool
        def cancel_order(order_id: str) -> str:
            """Cancel an order (an external side effect)."""
            self._record("cancel_order", {"order_id": order_id})
            if "cancel_order" in failing:
                raise ToolError(failing_message or f"cancel failed; request headers were {get_http_headers(include_all=True)}")
            return f"cancelled {order_id}"

        if echo_headers:

            @mcp.tool
            def echo_headers() -> dict[str, str]:
                """Return the HTTP request headers this call arrived with."""
                self._record("echo_headers", {})
                return get_http_headers(include_all=True)

            @mcp.tool
            def fail_echo_headers() -> str:
                """Fail with an error message that quotes the request headers."""
                self._record("fail_echo_headers", {})
                raise ToolError(f"denied; request headers were {get_http_headers(include_all=True)}")

        if big_results:

            @mcp.tool
            def big_result(size: int) -> str:
                """Return a string of `size` characters."""
                self._record("big_result", {"size": size})
                return "x" * size

        inner = mcp.http_app(path="/mcp", json_response=json_response)
        seen = self.seen_headers

        async def app(scope, receive, send):
            if scope["type"] == "http":
                headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
                seen.append(headers)
                if required_auth is not None and headers.get("authorization") != required_auth:
                    await send({"type": "http.response.start", "status": 401, "headers": [(b"content-length", b"0")]})
                    await send({"type": "http.response.body", "body": b""})
                    return
                if scope["method"] == "POST":
                    await self._handle_post(scope, receive, send, inner)
                    return
            await inner(scope, receive, send)

        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        from fastmcp.telemetry import suppress_fastmcp_telemetry

        with suppress_fastmcp_telemetry():
            self.server.run()

    async def _respond_json(self, send, payload: dict[str, Any]) -> None:
        out = json.dumps(payload).encode()
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(out)).encode())]})
        await send({"type": "http.response.body", "body": out})

    async def _handle_post(self, scope, receive, send, inner) -> None:
        body, more = b"", True
        while more:
            message = await receive()
            body += message.get("body", b"")
            more = message.get("more_body", False)
        try:
            payload = json.loads(body)
        except ValueError:
            payload = None
        params = payload.get("params") if isinstance(payload, dict) else None
        name = params.get("name") if isinstance(params, dict) else None
        is_call = isinstance(payload, dict) and payload.get("method") == "tools/call"
        if is_call:
            with self._lock:
                self.tool_call_posts.append(name)
        reject, input_required = self._intercept["reject"], self._intercept["input_required"]
        if is_call and name in reject:
            self._record(name, params.get("arguments") or {})
            await self._respond_json(send, {"jsonrpc": "2.0", "id": payload.get("id"),
                                            "error": {"code": reject[name], "message": "rejected by test server"}})
            return
        if is_call and name in self._intercept["gzip"]:
            self._record(name, params.get("arguments") or {})
            out = _gzip_call_result(payload.get("id"), self._intercept["gzip"][name])
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"application/json"), (b"content-encoding", b"gzip"),
                                    (b"content-length", str(len(out)).encode())]})
            await send({"type": "http.response.body", "body": out})
            return
        if is_call and name in input_required:
            self._record(name, params.get("arguments") or {})
            await self._respond_json(send, {"jsonrpc": "2.0", "id": payload.get("id"),
                                            "result": {"resultType": "input_required", "requestState": "s1"}})
            return
        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await inner(scope, replay, send)

    def _record(self, tool: str, args: dict[str, Any]) -> None:
        with self._lock:
            self.calls.append((tool, args))

    def call_count(self, tool: str | None = None) -> int:
        """Executed tools/call requests, for one tool or all."""
        with self._lock:
            return sum(1 for name, _ in self.calls if tool is None or name == tool)

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


def _gzip_call_result(request_id: Any, size: int) -> bytes:
    """A gzip-compressed JSON-RPC tools/call result with a `size`-byte text, streamed through the
    compressor 64 KiB at a time (the plaintext is never materialised)."""
    import zlib

    template = json.dumps({"jsonrpc": "2.0", "id": request_id,
                           "result": {"content": [{"type": "text", "text": "@@TEXT@@"}]}}).encode()
    head, tail = template.split(b"@@TEXT@@")
    comp = zlib.compressobj(9, zlib.DEFLATED, 31)
    out = [comp.compress(head)]
    chunk = b"x" * 65536
    for _ in range(size // len(chunk)):
        out.append(comp.compress(chunk))
    out.append(comp.compress(b"x" * (size % len(chunk))))
    out.append(comp.compress(tail))
    out.append(comp.flush())
    return b"".join(out)


def register_mcp_server(
    engine: Engine,
    tenant_id: str,
    name: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    enabled: bool = True,
) -> str:
    """Insert a registered server (and sealed header secrets) directly; returns its id."""
    server_id = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO mcp_servers (id, tenant_id, name, url, transport, enabled, created_at, updated_at) "
                "VALUES (:id, :t, :n, :u, 'streamable_http', :e, now(), now())"
            ),
            {"id": server_id, "t": tenant_id, "n": name, "u": url, "e": enabled},
        )
        if headers:
            ring = load_keyring()
            for header, value in headers.items():
                sealed = ring.encrypt(value, context=mcp_header_context(tenant_id, server_id, header))
                conn.execute(
                    text(
                        "INSERT INTO mcp_server_secrets (tenant_id, server_id, header_name, ciphertext, key_id) "
                        "VALUES (:t, :s, :h, :c, :k)"
                    ),
                    {"t": tenant_id, "s": server_id, "h": header, "c": sealed.ciphertext, "k": sealed.key_id},
                )
    return server_id


def mcp_spec(*pairs: tuple[str, str], tools: tuple[str, ...] = (), model: str = "mcp") -> dict[str, Any]:
    """A spec dict for scripted model `test:<model>` with the given MCP (server, tool) pairs."""
    return {
        "spec_version": 1,
        "name": "MCP agent",
        "instructions": "Use the MCP tool.",
        "model": {"provider": "test", "name": model},
        "tools": list(tools),
        "model_settings": {},
        "mcp_tools": [{"server": s, "tool": t} for s, t in pairs],
    }
