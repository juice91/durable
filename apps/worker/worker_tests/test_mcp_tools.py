"""MCP tools end-to-end (milestone 3, task 3.5)."""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from sqlalchemy import text

from worker_tests.conftest import APP_VERSION, enqueue_run, event_rows, wait_terminal
from worker_tests.mcp_helpers import LocalMcpServer, register_mcp_server, mcp_spec


def _insert_agent_and_version(conn, agent_id, tenant, spec):
    """Helper to insert an agent and version with proper JSONB casting."""
    import json
    from uuid import uuid4

    conn.execute(
        text(
            "INSERT INTO agents (id, tenant_id, name, draft_spec, archived, created_at, updated_at) "
            "VALUES (:id, :t, :n, cast(:s as jsonb), false, now(), now())"
        ),
        {"id": str(agent_id), "t": tenant, "n": f"agent_{agent_id}", "s": json.dumps(spec)},
    )
    conn.execute(
        text(
            "INSERT INTO agent_versions (id, agent_id, tenant_id, version, spec, spec_hash, created_at, created_by) "
            "VALUES (:id, :a, :t, 1, cast(:s as jsonb), :h, now(), 'test')"
        ),
        {"id": str(uuid4()), "a": str(agent_id), "t": tenant, "s": json.dumps(spec), "h": "dummy_hash"},
    )


def test_mcp_tool_lookup_order_only(worker, client, tenant, mcp_env, local_mcp_server):
    """A spec listing only lookup_order: the run succeeds, tool result in events, model sees only that tool."""
    # Register the MCP server
    server_id = register_mcp_server(worker.engine, tenant, "orders", local_mcp_server.url)
    # Create an agent with the spec
    from eap_secrets import generate_key

    key = generate_key()
    import os
    from eap_secrets import KEY_ENV, OLD_KEYS_ENV

    os.environ[KEY_ENV] = key
    os.environ[OLD_KEYS_ENV] = ""
    agent_id = uuid.uuid4()
    spec = mcp_spec(("orders", "lookup_order"))
    spec["spec_version"] = 1
    spec["name"] = "MCP test"
    with worker.engine.begin() as conn:
        _insert_agent_and_version(conn, agent_id, tenant, spec)
    # Enqueue a run with proper tool call JSON - use the full mcp__orders__lookup_order name
    import json
    tool_input = json.dumps({"tool": "mcp__orders__lookup_order", "args": {"order_id": "123"}})
    run_id = enqueue_run(client, worker.run_store, tenant_id=tenant, spec=spec, input_text=tool_input,
                         app_version=APP_VERSION, agent_id=agent_id, version=1)
    # Wait for completion
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "completed", run.get("error")
    events = event_rows(worker.engine, run_id)
    tool_result_events = [e for e in events if e[2] == "tool_result"]
    assert len(tool_result_events) == 1
    result = tool_result_events[0][3]
    assert result["tool_name"] == "mcp__orders__lookup_order"
    assert result["status"] == "ok"
    content = result["content"]
    # The content is a dict (the tool result structure)
    assert isinstance(content, dict) and content.get("order_id") == "123"
    # The model should have been offered only mcp__orders__lookup_order, not cancel_order
    model_events = [e for e in events if e[2] == "model_message"]
    assert len(model_events) >= 1
    model_text = model_events[-1][3].get("text", "")
    assert "mcp__orders__lookup_order" in model_text
    assert "mcp__orders__cancel_order" not in model_text
    # LocalMcpServer saw the call
    assert local_mcp_server.call_count("lookup_order") == 1
    assert local_mcp_server.call_count("cancel_order") == 0


def test_mcp_tool_with_header_secret(worker, client, tenant, mcp_env):
    """Header secret reaches the MCP server but never appears in logs/events."""
    from uuid import uuid4

    sentinel = f"Bearer SENTINEL-{uuid4().hex}"
    local_mcp_server = LocalMcpServer(required_auth=sentinel)
    with local_mcp_server:
        # Register with header
        server_id = register_mcp_server(
            worker.engine, tenant, "auth_orders", local_mcp_server.url, headers={"Authorization": sentinel}
        )
        agent_id = uuid.uuid4()
        spec = mcp_spec(("auth_orders", "lookup_order"))
        spec["spec_version"] = 1
        spec["name"] = "Auth MCP test"
        with worker.engine.begin() as conn:
            _insert_agent_and_version(conn, agent_id, tenant, spec)

        run_id = enqueue_run(
            client, worker.run_store, tenant_id=tenant, spec=spec,
            input_text=json.dumps({"tool": "lookup_order", "args": {"order_id": "456"}}),
            app_version=APP_VERSION, agent_id=agent_id, version=1,
        )
        run = wait_terminal(worker.run_store, tenant, run_id)
        assert run["status"] == "completed", run.get("error")

        # Server saw the sentinel
        assert any(sentinel in h.values() for h in local_mcp_server.seen_headers), local_mcp_server.seen_headers

        events = event_rows(worker.engine, run_id)
        all_event_data = " ".join(repr(e[3]) for e in events)
        assert sentinel not in all_event_data

        # Sentinel absent from run error/result
        run_row = worker.run_store.get_run(tenant, run_id)
        assert run_row["error"] is None or sentinel not in run_row["error"]
        assert run_row["output"] is None or sentinel not in run_row["output"]


def test_mcp_server_deleted_after_publish_fails_run(worker, client, tenant, mcp_env, local_mcp_server):
    """A server deleted after publish: run fails with mcp_tool_unavailable / server_not_found."""
    server_id = register_mcp_server(worker.engine, tenant, "orders", local_mcp_server.url)
    agent_id = uuid.uuid4()
    spec = mcp_spec(("orders", "lookup_order"))
    spec["spec_version"] = 1
    spec["name"] = "Deleted server test"
    with worker.engine.begin() as conn:
        _insert_agent_and_version(conn, agent_id, tenant, spec)

    # Delete the server
    with worker.engine.begin() as conn:
        conn.execute(text("DELETE FROM mcp_servers WHERE id = :id"), {"id": server_id})

    run_id = enqueue_run(client, worker.run_store, tenant_id=tenant, spec=spec, input_text="Look up order 789",
                         app_version=APP_VERSION, agent_id=agent_id, version=1)
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "failed", run
    assert run["error"] == "McpToolUnavailable: MCP tool 'orders/lookup_order' is unavailable (server_not_found)"
    assert local_mcp_server.call_count() == 0


def test_mcp_server_disabled_after_publish_fails_run(worker, client, tenant, mcp_env, local_mcp_server):
    """A server disabled after publish: run fails with mcp_tool_unavailable / server_disabled."""
    server_id = register_mcp_server(worker.engine, tenant, "orders", local_mcp_server.url)
    agent_id = uuid.uuid4()
    spec = mcp_spec(("orders", "lookup_order"))
    spec["spec_version"] = 1
    spec["name"] = "Disabled server test"
    with worker.engine.begin() as conn:
        _insert_agent_and_version(conn, agent_id, tenant, spec)

    # Disable the server
    with worker.engine.begin() as conn:
        conn.execute(text("UPDATE mcp_servers SET enabled = false WHERE id = :id"), {"id": server_id})

    run_id = enqueue_run(client, worker.run_store, tenant_id=tenant, spec=spec, input_text="Look up order 999",
                         app_version=APP_VERSION, agent_id=agent_id, version=1)
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "failed", run
    assert run["error"] == "McpToolUnavailable: MCP tool 'orders/lookup_order' is unavailable (server_disabled)"
    assert local_mcp_server.call_count() == 0


def test_mcp_tool_missing_on_server_fails_run(worker, client, tenant, mcp_env, local_mcp_server):
    """A tool listed but missing on the server: tool_not_found."""
    # Register server with only lookup_order and cancel_order
    server_id = register_mcp_server(worker.engine, tenant, "orders", local_mcp_server.url)
    agent_id = uuid.uuid4()
    # Ask for a tool that doesn't exist
    spec = mcp_spec(("orders", "nonexistent_tool"))
    spec["spec_version"] = 1
    spec["name"] = "Missing tool test"
    with worker.engine.begin() as conn:
        _insert_agent_and_version(conn, agent_id, tenant, spec)

    run_id = enqueue_run(client, worker.run_store, tenant_id=tenant, spec=spec, input_text="Try to call nonexistent",
                         app_version=APP_VERSION, agent_id=agent_id, version=1)
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "failed", run
    assert run["error"] == "McpToolUnavailable: MCP tool 'orders/nonexistent_tool' is unavailable (tool_not_found)"
    assert local_mcp_server.call_count() == 0


def test_blocked_url_policy_makes_no_server_calls(worker, client, tenant, monkeypatch):
    """No dev allow host: the http://127.0.0.1 server is refused before any request."""
    from eap_secrets.outbound import DEV_ALLOW_HOSTS_ENV

    monkeypatch.setenv(DEV_ALLOW_HOSTS_ENV, "")
    with LocalMcpServer() as server:
        register_mcp_server(worker.engine, tenant, "orders", server.url)
        spec = mcp_spec(("orders", "lookup_order"))
        run_id = enqueue_run(
            client, worker.run_store, tenant_id=tenant, spec=spec,
            input_text=json.dumps({"tool": "lookup_order", "args": {"order_id": "1"}}),
            app_version=APP_VERSION,
        )
        run = wait_terminal(worker.run_store, tenant, run_id)
        assert run["status"] == "failed", run
        assert run["error"] == (
            "McpToolUnavailable: MCP tool 'orders/lookup_order' is unavailable (url_scheme_not_allowed)"
        )
        assert server.call_count() == 0
        assert server.seen_headers == []


def _side_effect_states(engine, tenant: str) -> list[str]:
    with engine.connect() as conn:
        return list(
            conn.execute(text("SELECT state FROM side_effects WHERE tenant_id = :t"), {"t": tenant}).scalars()
        )


@contextmanager
def _counting_listener() -> Iterator[tuple[int, dict[str, int]]]:
    """A 127.0.0.1 listener that only counts accepted connections. It answers nothing."""
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


def _rewrite_listed_schema(payload: object, ref: str) -> bool:
    """Point lookup_order's listed schema at `ref` and require an argument the model will not send.

    A client that drops the remote $ref and then validates would reject the call. A client that
    keeps the ref and skips the check calls the tool."""
    if not isinstance(payload, dict):
        return False
    result = payload.get("result")
    tools = result.get("tools") if isinstance(result, dict) else None
    if not isinstance(tools, list):
        return False
    changed = False
    for tool in tools:
        if isinstance(tool, dict) and tool.get("name") == "lookup_order":
            tool["inputSchema"] = {
                "type": "object",
                "properties": {"order_id": {"$ref": ref}},
                "required": ["order_id", "must_be_present"],
            }
            changed = True
    return changed


def _rewrite_response(content_type: str, body: bytes, ref: str) -> tuple[bytes, bool]:
    if "text/event-stream" in content_type:
        changed = False
        lines: list[str] = []
        for line in body.decode().splitlines(keepends=True):
            raw = line[5:].strip() if line.startswith("data:") else None
            if raw is None:
                lines.append(line)
                continue
            try:
                payload = json.loads(raw)
            except ValueError:
                lines.append(line)
                continue
            if _rewrite_listed_schema(payload, ref):
                changed = True
                ending = "\n" if line.endswith("\n") else ""
                lines.append("data: " + json.dumps(payload) + ending)
            else:
                lines.append(line)
        return "".join(lines).encode(), changed
    try:
        payload = json.loads(body) if body else None
    except ValueError:
        return body, False
    changed = _rewrite_listed_schema(payload, ref)
    return (json.dumps(payload).encode() if changed else body), changed


class _ToolsListProxy:
    """Forwards to a local MCP server and rewrites tools/list so lookup_order advertises `ref`.

    FastMCP strips a non-local $ref before it lists a tool, so the schema the worker validates has
    to be introduced on the wire."""

    def __init__(self, upstream: str, ref: str) -> None:
        import uvicorn

        self.upstream = upstream
        self.ref = ref
        self.rewritten: list[bytes] = []

        async def app(scope, receive, send) -> None:
            # A nested function, not a method: uvicorn treats a bound method as an ASGI2 app.
            import httpx2

            if scope["type"] != "http":
                return
            body, more = b"", True
            while more:
                message = await receive()
                body += message.get("body", b"")
                more = message.get("more_body", False)
            headers = {
                k.decode(): v.decode()
                for k, v in scope["headers"]
                if k.decode().lower() not in {"host", "content-length"}
            }
            async with httpx2.AsyncClient(timeout=30.0) as client:
                response = await client.request(scope["method"], self.upstream, content=body, headers=headers)
            out, changed = _rewrite_response(response.headers.get("content-type", ""), response.content, self.ref)
            if changed:
                self.rewritten.append(out)
            hop = {"content-length", "transfer-encoding", "connection", "content-encoding"}
            resp_headers = [(k.encode(), v.encode()) for k, v in response.headers.multi_items() if k.lower() not in hop]
            resp_headers.append((b"content-length", str(len(out)).encode()))
            await send({"type": "http.response.start", "status": response.status_code, "headers": resp_headers})
            await send({"type": "http.response.body", "body": out})

        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> _ToolsListProxy:
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("tools/list proxy did not start")
            time.sleep(0.02)
        port = self.server.servers[0].sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}/mcp"
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


def test_remote_ref_schema_makes_no_outbound_connection_and_the_run_proceeds(worker, client, tenant, mcp_env):
    """A tool schema with a remote $ref is not fetched. Validation is skipped and the call runs.

    127.0.0.1 is on the dev allow list, so a fetch would be allowed to connect. The listed schema
    also requires must_be_present, which the model does not send: a check that ran would not call
    the tool."""
    with _counting_listener() as (port, state):
        ref = f"http://127.0.0.1:{port}/schema.json"
        with LocalMcpServer(json_response=True) as server, _ToolsListProxy(server.url, ref) as proxy:
            register_mcp_server(worker.engine, tenant, "orders", proxy.url)
            run_id = enqueue_run(
                client, worker.run_store, tenant_id=tenant, spec=mcp_spec(("orders", "lookup_order")),
                input_text=json.dumps({"tool": "lookup_order", "args": {"order_id": "R1"}}),
                app_version=APP_VERSION,
            )
            run = wait_terminal(worker.run_store, tenant, run_id)
            time.sleep(0.5)  # a late schema fetch would still hit the listener
            assert any(ref.encode() in body for body in proxy.rewritten), proxy.rewritten
            assert run["status"] == "completed", run.get("error")
            assert server.call_count("lookup_order") == 1
            result = [e[3] for e in event_rows(worker.engine, run_id) if e[2] == "tool_result"]
            assert len(result) == 1 and result[0]["status"] == "ok"
            assert result[0]["content"]["order_id"] == "R1"
        assert state["connections"] == 0


def test_input_required_answer_needs_attention_with_one_post(worker, client, tenant, mcp_env, caplog):
    """An InputRequired answer is not re-sent: one tools/call, then needs_attention (input_required)."""
    caplog.set_level(logging.WARNING, logger="eap_worker.mcp")
    with LocalMcpServer(input_required={"cancel_order"}) as server:
        register_mcp_server(worker.engine, tenant, "orders", server.url)
        run_id = enqueue_run(
            client, worker.run_store, tenant_id=tenant, spec=mcp_spec(("orders", "cancel_order")),
            input_text=json.dumps({"args": {"order_id": "I1"}}), app_version=APP_VERSION,
        )
        run = wait_terminal(worker.run_store, tenant, run_id)
        time.sleep(0.5)  # a re-send loop would POST tools/call again within ~50-250 ms
        assert server.tool_call_posts == ["cancel_order"]
    assert run["status"] == "needs_attention", run.get("error")
    assert run["error"].startswith("SideEffectOutcomeUnknown:")
    assert _side_effect_states(worker.engine, tenant) == ["reserved"]
    assert "error_code=input_required" in caplog.text


def test_oversized_result_needs_attention(worker, client, tenant, mcp_env, caplog):
    """A result over the 1 MiB response cap ends needs_attention (response_too_large) inside the timeout."""
    caplog.set_level(logging.WARNING, logger="eap_worker.mcp")
    with LocalMcpServer(big_results=True, json_response=True) as server:
        register_mcp_server(worker.engine, tenant, "orders", server.url)
        started = time.monotonic()
        run_id = enqueue_run(
            client, worker.run_store, tenant_id=tenant, spec=mcp_spec(("orders", "big_result")),
            input_text=json.dumps({"args": {"size": 8_000_000}}), app_version=APP_VERSION,
        )
        run = wait_terminal(worker.run_store, tenant, run_id, timeout=60)
        assert time.monotonic() - started < 30
        assert server.call_count("big_result") == 1
        assert server.tool_call_posts == ["big_result"]
    assert run["status"] == "needs_attention", run.get("error")
    assert run["error"].startswith("SideEffectOutcomeUnknown:")
    assert _side_effect_states(worker.engine, tenant) == ["reserved"]
    assert "error_code=response_too_large" in caplog.text


def test_gzip_tool_result_needs_attention_with_one_post(worker, client, tenant, mcp_env, caplog):
    """A gzip body is refused unread: one tools/call, then needs_attention (unsupported_encoding)."""
    caplog.set_level(logging.WARNING, logger="eap_worker.mcp")
    with LocalMcpServer(gzip_results={"cancel_order": 4096}) as server:
        register_mcp_server(worker.engine, tenant, "orders", server.url)
        run_id = enqueue_run(
            client, worker.run_store, tenant_id=tenant, spec=mcp_spec(("orders", "cancel_order")),
            input_text=json.dumps({"args": {"order_id": "G1"}}), app_version=APP_VERSION,
        )
        run = wait_terminal(worker.run_store, tenant, run_id)
        time.sleep(0.5)  # a re-send loop would POST tools/call again within ~50-250 ms
        assert server.tool_call_posts == ["cancel_order"]
        assert server.call_count("cancel_order") == 1
        encoded = [h for h in server.seen_headers if h.get("accept-encoding")]
        assert encoded and all(h["accept-encoding"] == "identity" for h in encoded)
    assert run["status"] == "needs_attention", run.get("error")
    assert run["error"].startswith("SideEffectOutcomeUnknown:")
    assert _side_effect_states(worker.engine, tenant) == ["reserved"]
    assert "error_code=unsupported_encoding" in caplog.text


def test_split_secret_is_masked_in_place_in_the_stored_result(worker, client, tenant, mcp_env, monkeypatch):
    """A secret split across a result key and its value is masked in place, and a key that is the
    whole secret becomes [REDACTED] (suffixed only when that name is already used). The stored
    tool_result and side effect cannot rebuild it."""
    from eap_worker.mcp import REDACTED

    secret = "sntl-Hard3n-9f2c-secret"
    pieces = (secret[:9], secret[9:])

    def split_headers(include_all: bool = False) -> dict[str, str]:
        return {f"key is {pieces[0]}": f"{pieces[1]} ok", secret: "kept"}

    monkeypatch.setattr("fastmcp.server.dependencies.get_http_headers", split_headers)
    with LocalMcpServer(echo_headers=True) as server:
        register_mcp_server(
            worker.engine, tenant, "orders", server.url, headers={"Authorization": secret},
        )
        run_id = enqueue_run(
            client, worker.run_store, tenant_id=tenant, spec=mcp_spec(("orders", "echo_headers")),
            input_text=json.dumps({"tool": "echo_headers", "args": {}}), app_version=APP_VERSION,
        )
        run = wait_terminal(worker.run_store, tenant, run_id)
        assert run["status"] == "completed", run.get("error")
        assert server.call_count("echo_headers") == 1
    expected = {f"key is {REDACTED}": f"{REDACTED} ok", REDACTED: "kept"}
    result = [e[3] for e in event_rows(worker.engine, run_id) if e[2] == "tool_result"]
    assert len(result) == 1 and result[0]["status"] == "ok"
    assert result[0]["content"] == expected
    with worker.engine.connect() as conn:
        stored = conn.execute(
            text("SELECT result FROM side_effects WHERE tenant_id = :t"), {"t": tenant},
        ).scalar()
        event_text = conn.execute(
            text("SELECT data::text FROM execution_events WHERE run_id = :r AND type = 'tool_result'"),
            {"r": str(run_id)},
        ).scalar()
    assert stored == {"is_error": False, "content": expected}
    durable = json.dumps(stored) + event_text
    low = durable.lower()
    for i in range(6, len(secret) + 1):
        assert secret[:i].lower() not in low
    assert secret[9:].lower() not in low


def test_secret_split_across_three_keys_cannot_be_rebuilt(worker, client, tenant, mcp_env, monkeypatch):
    """A secret split across three dict keys, with ordinary values between them, is masked on the
    keys only. Joining the stored keys does not rebuild it, and xxxx and yyyy stay."""
    from eap_worker.mcp import REDACTED

    secret = "sntl-Hard3n-9f2c-secret"
    pieces = (secret[:8], secret[8:16], secret[16:])

    def split_headers(include_all: bool = False) -> dict[str, str]:
        return {pieces[0]: "xxxx", pieces[1]: "yyyy", pieces[2]: "zzzz"}

    monkeypatch.setattr("fastmcp.server.dependencies.get_http_headers", split_headers)
    with LocalMcpServer(echo_headers=True) as server:
        register_mcp_server(
            worker.engine, tenant, "orders", server.url, headers={"Authorization": secret},
        )
        run_id = enqueue_run(
            client, worker.run_store, tenant_id=tenant, spec=mcp_spec(("orders", "echo_headers")),
            input_text=json.dumps({"tool": "echo_headers", "args": {}}), app_version=APP_VERSION,
        )
        run = wait_terminal(worker.run_store, tenant, run_id)
        assert run["status"] == "completed", run.get("error")
        assert server.call_count("echo_headers") == 1
    expected = {REDACTED: "xxxx", f"{REDACTED}-2": "yyyy", f"{REDACTED}-4": "zzzz"}
    result = [e[3] for e in event_rows(worker.engine, run_id) if e[2] == "tool_result"]
    assert len(result) == 1 and result[0]["status"] == "ok"
    assert result[0]["content"] == expected
    assert list(result[0]["content"].values())[:2] == ["xxxx", "yyyy"]
    with worker.engine.connect() as conn:
        stored = conn.execute(
            text("SELECT result FROM side_effects WHERE tenant_id = :t"), {"t": tenant},
        ).scalar()
        event_text = conn.execute(
            text("SELECT data::text FROM execution_events WHERE run_id = :r AND type = 'tool_result'"),
            {"r": str(run_id)},
        ).scalar()
    assert stored == {"is_error": False, "content": expected}
    keys = list(stored["content"])
    for i in range(len(keys)):
        for j in range(i + 1, len(keys) + 1):
            assert secret.lower() not in "".join(keys[i:j]).lower()
    durable = (json.dumps(stored) + event_text).lower()
    for piece in pieces:
        assert piece.lower() not in durable


def _hex_joined(values: list[str]) -> str:
    """What the joined values hex-decode to, or empty when they are not hex."""
    try:
        return bytes.fromhex("".join(values)).decode("utf-8", "ignore")
    except ValueError:
        return ""


def test_uppercase_hex_pieces_cannot_be_decoded_when_joined(worker, client, tenant, mcp_env, monkeypatch):
    """Uppercase hex pieces of the header secret, split across values with 'note' between them, are
    masked. Joining the stored values does not hex-decode to the secret, and 'note' is kept."""
    from eap_worker.mcp import REDACTED

    secret = "sntl-Hard3n-9f2c-secret"
    hexed = secret.encode().hex().upper()
    chunks = [hexed[i:i + 15] for i in range(0, len(hexed), 15)]

    def split_headers(include_all: bool = False) -> dict[str, str]:
        out: dict[str, str] = {}
        for n, chunk in enumerate(chunks):
            out[f"c{n}"] = chunk
            out[f"n{n}"] = "note"
        return out

    monkeypatch.setattr("fastmcp.server.dependencies.get_http_headers", split_headers)
    with LocalMcpServer(echo_headers=True) as server:
        register_mcp_server(
            worker.engine, tenant, "orders", server.url, headers={"Authorization": secret},
        )
        run_id = enqueue_run(
            client, worker.run_store, tenant_id=tenant, spec=mcp_spec(("orders", "echo_headers")),
            input_text=json.dumps({"tool": "echo_headers", "args": {}}), app_version=APP_VERSION,
        )
        run = wait_terminal(worker.run_store, tenant, run_id)
        assert run["status"] == "completed", run.get("error")
        assert server.call_count("echo_headers") == 1
    expected = {
        "c0": REDACTED, "n0": "note",
        "c1": REDACTED, "n1": "note",
        "c2": REDACTED, "n2": "note",
        "c3": chunks[-1], "n3": "note",
    }
    result = [e[3] for e in event_rows(worker.engine, run_id) if e[2] == "tool_result"]
    assert len(result) == 1 and result[0]["status"] == "ok"
    assert result[0]["content"] == expected
    with worker.engine.connect() as conn:
        stored = conn.execute(
            text("SELECT result FROM side_effects WHERE tenant_id = :t"), {"t": tenant},
        ).scalar()
        event_text = conn.execute(
            text("SELECT data::text FROM execution_events WHERE run_id = :r AND type = 'tool_result'"),
            {"r": str(run_id)},
        ).scalar()
    assert stored == {"is_error": False, "content": expected}
    for content in (result[0]["content"], stored["content"]):
        assert [content[f"n{n}"] for n in range(len(chunks))] == ["note"] * len(chunks)
        pieces = [content[f"c{n}"] for n in range(len(chunks))]
        assert secret.lower() not in _hex_joined(pieces).lower()
        assert hexed.lower() not in "".join(pieces).lower()
    durable = json.dumps(stored) + event_text
    assert hexed.lower() not in durable.lower()
