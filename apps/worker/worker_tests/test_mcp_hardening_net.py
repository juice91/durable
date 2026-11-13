"""Task 3.5 third review, end to end: a tool's error body never reaches exported spans; an
InputRequiredResult is never answered by re-sending tools/call; response bodies are bounded."""

from __future__ import annotations

import json
import time

import pytest
from sqlalchemy import text

from eap_worker.mcp import MAX_RESULT_CHARS, TOOL_ERROR_MESSAGE
from worker_tests.conftest import enqueue_run, event_rows, wait_terminal
from worker_tests.mcp_helpers import LocalMcpServer, mcp_spec, register_mcp_server
from worker_tests.test_telemetry import telemetry  # noqa: F401 - fixture

APP_VERSION_INPROC = "m1-test"  # the session worker's app version (conftest.APP_VERSION)


def _start(worker, client, tenant, server, tool: str, args: dict) -> object:
    register_mcp_server(worker.engine, tenant, "srv", server.url)
    return enqueue_run(client, worker.run_store, tenant_id=tenant, spec=mcp_spec(("srv", tool)),
                       input_text=json.dumps({"args": args}), app_version=APP_VERSION_INPROC)


def _states(engine, tenant: str) -> list[str]:
    with engine.connect() as conn:
        return list(conn.execute(text("SELECT state FROM side_effects WHERE tenant_id = :t"), {"t": tenant}).scalars())


@pytest.mark.parametrize("body", ["Abcd1234token", "Token: invalid"])
def test_tool_error_body_never_reaches_exported_spans(worker, client, tenant, mcp_env, telemetry, body):  # noqa: F811
    _provider, finished = telemetry
    with LocalMcpServer(failing={"cancel_order"}, failing_message=body) as server:
        run_id = _start(worker, client, tenant, server, "cancel_order", {"order_id": "S1"})
        run = wait_terminal(worker.run_store, tenant, run_id)
        assert run["status"] == "completed", run["error"]
        assert server.call_count("cancel_order") == 1
    result = [e[3] for e in event_rows(worker.engine, run_id) if e[2] == "tool_result"][0]
    assert (result["status"], result["content"]) == ("error", TOOL_ERROR_MESSAGE)
    spans, logs = finished()
    assert spans, "telemetry captured nothing"
    assert not [s for s in spans if s.name.startswith("tools/call")]  # FastMCP's client spans are off
    for s in spans:
        exported = repr((s.status.description, dict(s.attributes or {}),
                         [(e.name, dict(e.attributes or {})) for e in s.events]))
        assert body not in exported, s.name
    assert body not in repr([(r.body, dict(r.attributes or {})) for r in logs])


def test_input_required_is_never_answered_by_resending(worker, client, tenant, mcp_env):
    with LocalMcpServer(input_required={"cancel_order"}) as server:
        run_id = _start(worker, client, tenant, server, "cancel_order", {"order_id": "I1"})
        run = wait_terminal(worker.run_store, tenant, run_id)
        time.sleep(0.5)  # a re-send loop would POST again within ~50-250 ms
        assert server.tool_call_posts == ["cancel_order"]
    assert run["status"] == "needs_attention", run["error"]
    assert run["error"].startswith("SideEffectOutcomeUnknown:")
    assert _states(worker.engine, tenant) == ["reserved"]


@pytest.mark.parametrize("json_response", [True, False], ids=["json", "sse"])
def test_oversized_response_body_is_an_unknown_outcome(worker, client, tenant, mcp_env, json_response):
    with LocalMcpServer(big_results=True, json_response=json_response) as server:
        started = time.monotonic()
        run_id = _start(worker, client, tenant, server, "big_result", {"size": 8_000_000})
        run = wait_terminal(worker.run_store, tenant, run_id, timeout=60)
        assert time.monotonic() - started < 30
        assert server.call_count("big_result") == 1
        assert server.tool_call_posts == ["big_result"]
    assert run["status"] == "needs_attention", run["error"]
    assert _states(worker.engine, tenant) == ["reserved"]


@pytest.mark.parametrize("json_response", [True, False], ids=["json", "sse"])
def test_large_result_under_the_cap_is_stored_truncated(worker, client, tenant, mcp_env, json_response):
    with LocalMcpServer(big_results=True, json_response=json_response) as server:
        run_id = _start(worker, client, tenant, server, "big_result", {"size": 400_000})
        run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "completed", run["error"]
    result = [e[3] for e in event_rows(worker.engine, run_id) if e[2] == "tool_result"][0]
    assert result["status"] == "ok"
    assert result["content"]["truncated"] is True
    assert len(result["content"]["text"]) <= MAX_RESULT_CHARS


def test_compressed_body_is_refused_without_decompressing(worker, client, tenant, mcp_env):
    """The server ignores Accept-Encoding: identity and sends 64 MiB of plaintext gzipped to ~64 KB.
    The worker refuses the encoded body unread: the run needs attention (the call was sent), and the
    process never allocates anything near the plaintext size."""
    import tracemalloc

    plaintext = 64 * 1024 * 1024
    with LocalMcpServer(gzip_results={"cancel_order": plaintext}) as server:
        tracemalloc.start()
        try:
            tracemalloc.reset_peak()
            run_id = _start(worker, client, tenant, server, "cancel_order", {"order_id": "G1"})
            run = wait_terminal(worker.run_store, tenant, run_id, timeout=90)
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert server.tool_call_posts == ["cancel_order"]
        call_headers = [h for h in server.seen_headers if h.get("accept-encoding")]
        assert call_headers and all(h["accept-encoding"] == "identity" for h in call_headers)
    assert run["status"] == "needs_attention", run["error"]
    assert _states(worker.engine, tenant) == ["reserved"]
    assert peak < 16 * 1024 * 1024, f"peak traced allocation {peak} bytes"
