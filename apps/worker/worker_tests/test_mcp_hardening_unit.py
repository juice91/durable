"""Task 3.5 second review, unit level (no database): exceptions survive DBOS's pickle round trip,
redaction covers encodings and split secrets, argument checks, JSON-RPC rejection classification."""

from __future__ import annotations

import base64
import json
import pickle
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

import httpx2
import pytest
from mcp.shared.exceptions import MCPError
from pydantic_ai import ModelRetry, ToolFailed

from eap_runtime import SideEffectOutcomeUnknown
from eap_worker.errors import classify
from eap_worker.mcp import (
    REDACTED,
    McpToolUnavailable,
    _definite_rejection,
    _result_json,
    _schema_check,
    redact,
    secret_variants,
)

SECRET = "sntl-Hard3n-9f2c-secret"


@pytest.mark.parametrize(
    "exc",
    [McpToolUnavailable("orders", "cancel_order", "server_disabled"), SideEffectOutcomeUnknown("run:call"),
     ToolFailed("final")],
    ids=["McpToolUnavailable", "SideEffectOutcomeUnknown", "ToolFailed"],
)
def test_step_exceptions_survive_pickle(exc):
    restored = pickle.loads(pickle.dumps(exc))
    assert type(restored) is type(exc)
    assert str(restored) == str(exc)
    assert vars(restored) == vars(exc)
    assert classify(restored) == classify(exc)


def test_restored_mcp_tool_unavailable_classifies_with_its_code():
    failure = classify(pickle.loads(pickle.dumps(McpToolUnavailable("orders", "cancel_order", "timeout"))))
    assert (failure.status, failure.code) == ("failed", "mcp_tool_unavailable")
    assert failure.error == "McpToolUnavailable: MCP tool 'orders/cancel_order' is unavailable (timeout)"


def _scrub(text: str, header: str = f"Bearer {SECRET}") -> str:
    return redact(text, secret_variants([header]))


@pytest.mark.parametrize(
    "leak",
    [
        SECRET,
        SECRET.upper(),
        quote(SECRET, safe="").lower(),
        quote(f"Bearer {SECRET}", safe="").lower(),
        SECRET.encode().hex(),
        SECRET.encode().hex().upper(),
        base64.b64encode(SECRET.encode()).decode(),
        base64.urlsafe_b64encode(json.dumps({"headers": {"authorization": f"Bearer {SECRET}"}}).encode()).decode(),
        base64.b64encode(json.dumps({"auth": SECRET.upper()}).encode()).decode().rstrip("="),
        json.dumps({"auth": SECRET}).encode().hex(),
    ],
    ids=["raw", "upper", "pct-lower", "pct-header", "hex", "hex-upper", "b64", "b64-json", "b64-json-upper",
         "hex-json"],
)
def test_redaction_covers_encodings(leak):
    out = _scrub(f"prefix {leak} suffix")
    assert REDACTED in out
    assert SECRET.lower() not in out.lower()
    assert leak not in out


def test_redaction_leaves_ordinary_text_alone():
    text = "Order 12345 shipped; tracking ABCDEFGHIJKLMNOP; Bearer tokens are documented."
    assert _scrub(text) == text


def test_error_results_keep_no_body():
    result = SimpleNamespace(is_error=True, structured_content=None, content=[SimpleNamespace(type="text", text=SECRET)])
    assert _result_json(result, []) == {"is_error": True, "content": None}


def test_schema_check_raises_a_retry_before_any_call():
    check = _schema_check({"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]})
    check(None, order_id="A1")
    with pytest.raises(ModelRetry, match="order_id"):
        check(None)
    with pytest.raises(ModelRetry):
        check(None, order_id=5)
    _schema_check({"type": "not-a-type"})(None, anything=1)  # an invalid server schema checks nothing


@pytest.mark.parametrize("code,definite", [(-32700, True), (-32600, True), (-32601, True), (-32602, True),
                                           (-32603, False), (-32001, False), (-32000, False), (1234, False)])
def test_only_jsonrpc_rejections_are_definite(code, definite):
    assert _definite_rejection(MCPError(code, "x")) is definite
    try:
        raise RuntimeError("wrapped") from MCPError(code, "x")
    except RuntimeError as exc:
        assert _definite_rejection(exc) is definite
    assert _definite_rejection(TimeoutError()) is False


@pytest.mark.parametrize("outcome,status", [("success", "ok"), ("failed", "error"), ("retried", "error"),
                                            ("denied", "ok"), ("interrupted", "ok")])
def test_tool_result_status_by_outcome(monkeypatch, outcome, status):
    import asyncio

    from pydantic_ai.messages import ToolCallPart, ToolReturnPart

    from eap_worker import workflow

    written: list[dict] = []

    async def fake_append(run_id, tenant_id, key, type_, data):
        written.append(data)

    monkeypatch.setattr(workflow, "append_event_step", fake_append)
    call = ToolCallPart("mcp__orders__cancel_order", {}, tool_call_id="c1")
    ret = ToolReturnPart("mcp__orders__cancel_order", "x", tool_call_id="c1", outcome=outcome)
    asyncio.run(workflow._record_tool_results("r", "t", [call], [ret]))
    assert written[0]["status"] == status


# --- third review: remote $ref, generic retry text, structured split secrets, bounded results ----


@pytest.fixture
def counting_listener():
    """A TCP listener on 127.0.0.1 that counts accepted connections (and answers nothing useful)."""
    import socket
    import threading

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
    yield sock.getsockname()[1], state
    state["stop"] = True
    thread.join(timeout=2)
    sock.close()


def test_remote_ref_in_a_tool_schema_opens_no_connection(counting_listener):
    port, state = counting_listener
    base = f"http://127.0.0.1:{port}"
    schemas = [
        {"$ref": f"{base}/schema.json"},
        {"type": "object", "properties": {"a": {"$ref": f"{base}/a.json"}}},
        {"$id": f"{base}/root.json", "type": "object", "properties": {"a": {"$ref": "other.json#/x"}}},
        {"type": "object", "properties": {"a": {"$dynamicRef": f"{base}/d.json"}}},
    ]
    for schema in schemas:
        check = _schema_check(schema)
        check(None, a="x")  # unresolvable reference: treated as an invalid schema, nothing raised
        check(None, a=1, b=[1, 2])
    import time

    time.sleep(0.3)
    assert state["connections"] == 0


def test_retry_text_names_path_and_keyword_only():
    check = _schema_check({
        "type": "object",
        "properties": {"mode": {"const": "VALUE-FROM-SCHEMA-1"}, "kind": {"enum": ["VALUE-FROM-SCHEMA-2"]},
                       "code": {"type": "string", "pattern": "^VALUE-FROM-SCHEMA-3$"}},
        "required": ["mode"],
    })
    with pytest.raises(ModelRetry) as info:
        check(None, kind="x", code="y")
    message = str(info.value.message)
    assert "VALUE-FROM-SCHEMA" not in message
    assert "is invalid: required" in message
    assert "argument 'kind' is invalid: enum" in message
    assert "argument 'code' is invalid: pattern" in message


# --- split secrets (masked in place) and the cut ------------------------------------------------

LONG_SECRET = ("sntl-" + "".join(f"{i:02d}Zq" for i in range(24)))[:96] + "-end"  # 100 characters
assert len(LONG_SECRET) == 100


def _stored(structured: Any = None, blocks: list[str] | None = None, secret: str = SECRET) -> dict:
    content = [SimpleNamespace(type="text", text=t) for t in blocks or []]
    result = SimpleNamespace(is_error=False, structured_content=structured, content=content)
    return _result_json(result, secret_variants([secret]))


def _assert_no_rebuild(content: Any, secret: str = SECRET) -> None:
    """No contiguous run of stored strings contains the secret, joined in any of three orders: keys
    and values interleaved, values only, keys only."""
    from eap_worker.mcp import _leaves_with_kind

    leaves = _leaves_with_kind(content)
    orders = ([s for s, _ in leaves], [s for s, is_key in leaves if not is_key], [s for s, is_key in leaves if is_key])
    for seq in orders:
        for i in range(len(seq)):
            for j in range(i + 1, len(seq) + 1):
                assert secret.lower() not in "".join(seq[i:j]).lower(), seq[i:j]


@pytest.mark.parametrize(
    "structured",
    [
        {"a": f"key is {SECRET[:9]}", "b": f"{SECRET[9:]} ok"},
        {"items": [f"key is {SECRET[:5]}", SECRET[5:14], f"{SECRET[14:]} ok"]},
        [{"x": f"key is {SECRET[:9]}"}, {"y": f"{SECRET[9:]} ok"}],
        {f"key is {SECRET[:9]}": f"{SECRET[9:]} ok"},
        {"k": {f"key is {SECRET[:4]}": [SECRET[4:12], {SECRET[12:]: "ok"}]}},
        {SECRET[:8]: "xxxx", SECRET[8:16]: "yyyy", SECRET[16:]: "zzzz"},
        [{SECRET[:8]: 1}, {SECRET[8:16]: 2}, {SECRET[16:]: 3}],
        [SECRET[:8], {SECRET[8:16]: "q"}, SECRET[16:]],
        [SECRET[:8].upper(), {SECRET[8:16]: "q"}, SECRET[16:]],
    ],
    ids=["fields", "list", "nested", "key-value", "key-value-nested", "keys-with-values-between",
         "one-key-per-object", "breaker", "breaker-upper"],
)
def test_secret_split_across_structured_content_is_masked(structured):
    stored = _stored(structured)
    assert REDACTED in json.dumps(stored)
    _assert_no_rebuild(stored["content"])


def test_secret_split_across_text_blocks_is_masked():
    stored = _stored(blocks=[f"key is {SECRET[:9]}", f"{SECRET[9:]} ok"])
    assert stored["content"] == [f"key is {REDACTED}", f"{REDACTED} ok"]
    _assert_no_rebuild(stored["content"])


def test_keys_holding_a_secret_are_renamed_uniquely():
    stored = _stored({f"x{SECRET}": 1, f"y{SECRET}": 2, "plain": SECRET})
    assert stored["content"] == {f"{REDACTED}-1": 1, f"{REDACTED}-2": 2, "plain": REDACTED}


def test_structured_content_without_split_secret_keeps_its_shape():
    stored = _stored({"a": "x", "b": SECRET, "n": 3})
    assert stored["content"] == {"a": "x", "b": REDACTED, "n": 3}


def _no_prefix_survives(text: str, secret: str) -> None:
    low = text.lower()
    for i in range(6, len(secret) + 1):
        assert secret[:i].lower() not in low, (i, secret[:i])


@pytest.mark.parametrize("secret", [SECRET, LONG_SECRET], ids=["short", "long"])
@pytest.mark.parametrize("back", [1, 2, 6, 12, 23, 50, 92, 99, 104, 230])
def test_secret_straddling_the_actual_cut_leaves_no_prefix(secret, back):
    """The secret starts `back` characters before the real cut index of the encoded result."""
    from eap_worker.mcp import MAX_RESULT_CHARS

    head = '{"t": "'  # json.dumps({"t": ...}) puts the text right after these 7 characters
    filler = "y" * (MAX_RESULT_CHARS - len(head) - back)
    stored = _stored({"t": filler + secret + "z" * MAX_RESULT_CHARS}, secret=secret)
    text = stored["content"]["text"]
    assert stored["content"]["truncated"] is True and len(text) <= MAX_RESULT_CHARS
    _no_prefix_survives(text, secret)


@pytest.mark.parametrize("back", [3, 20, 60, 140, 200])
def test_base64_blob_straddling_the_cut_leaves_no_decodable_prefix(back):
    from eap_worker.mcp import MAX_RESULT_CHARS, _decodings

    blob = base64.b64encode(json.dumps({"auth": f"Bearer {LONG_SECRET}"}).encode()).decode()
    head = '{"t": "'
    filler = "y " * ((MAX_RESULT_CHARS - len(head) - back) // 2)
    stored = _stored({"t": filler + blob + " " + "z" * MAX_RESULT_CHARS}, secret=LONG_SECRET)
    text = stored["content"]["text"]
    _no_prefix_survives(text, LONG_SECRET)
    import re

    tail = re.search(r"[A-Za-z0-9+/_=-]+$", text)
    for decoded in _decodings(tail.group(0)) if tail else ():
        _no_prefix_survives(decoded, LONG_SECRET)


def test_split_secret_straddling_the_cut_leaves_no_prefix():
    from eap_worker.mcp import MAX_RESULT_CHARS

    for back in (5, 15, 40):
        filler = "y" * (MAX_RESULT_CHARS - back)
        stored = _stored({"a": filler + LONG_SECRET[:30], "b": LONG_SECRET[30:] + "z" * MAX_RESULT_CHARS},
                         secret=LONG_SECRET)
        _no_prefix_survives(stored["content"]["text"], LONG_SECRET)


# --- encoded responses are refused before any body byte is read ----------------------------------


class _CountingStream(httpx2.AsyncByteStream):
    def __init__(self) -> None:
        self.reads = 0

    async def __aiter__(self):
        self.reads += 1
        yield b"{}"

    async def aclose(self) -> None:
        pass


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "br", "zstd", "GZIP", "gzip, identity"])
def test_encoded_response_is_refused_unread(encoding):
    import asyncio

    from eap_worker.mcp import UnsupportedEncoding, _cap_response

    stream = _CountingStream()
    response = httpx2.Response(200, headers={"content-encoding": encoding}, stream=stream)
    with pytest.raises(UnsupportedEncoding):
        asyncio.run(_cap_response(response))
    assert stream.reads == 0


@pytest.mark.parametrize("headers", [{}, {"content-encoding": "identity"}])
def test_identity_response_is_capped_not_refused(headers):
    import asyncio

    from eap_worker.mcp import _cap_response, _CappedStream

    response = httpx2.Response(200, headers=headers, stream=_CountingStream())
    asyncio.run(_cap_response(response))
    assert isinstance(response.stream, _CappedStream)


def test_whole_string_fragments_do_not_mangle_ordinary_values():
    stored = _stored({SECRET[:8]: "xxxx", SECRET[8:16]: "yyyy", SECRET[16:]: "zzzz", "note": "true"})
    assert list(stored["content"].values()) == ["xxxx", "yyyy", "zzzz", "true"]
    _assert_no_rebuild(stored["content"])


@pytest.mark.parametrize("case", ["upper", "mixed", "lower"])
def test_hex_pieces_split_across_values_are_masked_in_any_case(case):
    hexed = SECRET.encode().hex()
    hexed = {"upper": hexed.upper(), "lower": hexed,
             "mixed": "".join(c.upper() if i % 2 else c for i, c in enumerate(hexed))}[case]
    chunks = [hexed[i:i + 15] for i in range(0, len(hexed), 15)]
    structured: dict[str, str] = {}
    for n, chunk in enumerate(chunks):
        structured[f"c{n}"] = chunk
        structured[f"n{n}"] = "note"
    stored = _stored(structured)["content"]
    assert [stored[f"n{n}"] for n in range(len(chunks))] == ["note"] * len(chunks)  # ordinary values kept
    values = [v for k, v in stored.items() if k.startswith("c")]
    joined = "".join(values)
    try:
        decoded = bytes.fromhex(joined).decode("utf-8", "ignore")
    except ValueError:
        decoded = ""
    assert SECRET.lower() not in decoded.lower()
    assert hexed.lower() not in joined.lower()
    _assert_no_rebuild(stored)
