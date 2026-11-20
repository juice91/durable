"""`http.request` (eap_worker.http_tool): the arguments, the result contract, the error codes, secret
references (resolved inside the call, redacted from the answer) and the outbound guard. No network: a mock
transport answers, a fake resolver stands in for DNS, a dict stands in for the secret store."""

from __future__ import annotations

import asyncio
import functools
import json
from types import SimpleNamespace
from typing import Any

import httpx2
import pytest
from pydantic import ValidationError

from eap_schemas.http_request import http_request_secret_names, http_request_summary
from eap_schemas.tools import TOOL_DECLARATIONS, HttpRequestInput
from eap_secrets.outbound import OutboundPolicy
from eap_worker import http_tool
from eap_worker.http_tool import http_request

PASSWORD = "FAKE-app-password-4d9e1b7c"
CTX = SimpleNamespace(deps=SimpleNamespace(tenant_id="t1"))
SECRETS = {"pwd-main": PASSWORD, "api-key": "FAKE-api-key-92ab33f0"}


def sync(fn):
    """Run an async test body (the suite has no async plugin)."""

    @functools.wraps(fn)
    def run(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return run


class Wire:
    """The mock transport's log and the answer it gives."""

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self.answer: Any = lambda request: httpx2.Response(200, json={"ok": True})

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        answer = self.answer(request)
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def wire(monkeypatch) -> Wire:
    wire = Wire()
    monkeypatch.setattr(
        http_tool, "new_client",
        lambda policy, timeout_s: httpx2.AsyncClient(transport=httpx2.MockTransport(wire.handler)),
    )
    monkeypatch.setattr(http_tool, "secret_lookup", lambda ctx: SECRETS.get)
    return wire


async def call(**kw: Any) -> dict[str, Any]:
    return await http_request(CTX, **{"method": "GET", "url": "https://api.example.com/v1/x", **kw})


# ------------------------------------------------------------------------------- the arguments


def test_the_declaration_is_a_general_tool_with_the_documented_limits():
    declaration = TOOL_DECLARATIONS["http.request"]
    schema = declaration.input_json_schema()
    assert declaration.side_effect and not declaration.external_idempotent
    assert schema["properties"]["method"]["enum"] == ["GET", "POST", "PUT", "PATCH", "DELETE"]
    assert schema["required"] == ["method", "url"]
    assert schema["properties"]["timeout_s"]["default"] == 20
    assert schema["properties"]["max_response_bytes"]["default"] == 65536
    for bad in ({"timeout_s": 0}, {"timeout_s": 31}, {"max_response_bytes": 262145}, {"max_response_bytes": 0},
                {"method": "HEAD"}, {"headers": {"A": 1}}, {"query": {"a": 1}}, {"extra": 1}):
        with pytest.raises(ValidationError):
            HttpRequestInput.model_validate({"method": "GET", "url": "https://a.example/", **bad})
    ok = HttpRequestInput.model_validate({"method": "POST", "url": "https://a.example/", "json": [1, {"a": None}],
                                          "timeout_s": 30, "max_response_bytes": 262144})
    assert ok.json == [1, {"a": None}]


def test_the_registered_tool_matches_its_declaration():
    from eap_runtime import ToolRegistry

    registry = ToolRegistry()
    http_tool.register_http_tool(registry)  # raises when the signature and the declaration differ
    assert "http.request" in registry.tool_ids()
    assert registry.summary_for("http.request", {"method": "POST", "url": "https://a.example/p?q=1"}) == "POST a.example/p"


@sync
async def test_http_is_refused_and_json_xor_body(wire):
    for url in ("http://api.example.com/x", "ftp://api.example.com/x", "//api.example.com/x"):
        assert (await call(url=url))["error"]["code"] == "url_not_https"
    assert (await call(method="POST", json={"a": 1}, body="x"))["error"]["code"] == "body_conflict"
    assert wire.requests == []


# ------------------------------------------------------------------------------- the result


@sync
async def test_a_json_answer(wire):
    wire.answer = lambda r: httpx2.Response(
        200, json={"n": 1}, headers={"x-request-id": "r-1", "ratelimit-remaining": "9", "set-cookie": "a=b", "server": "x"}
    )
    result = await call(query={"q": "a b"}, headers={"X-Mine": "1"})
    assert result["status"] == 200 and result["ok"] is True and result["json"] == {"n": 1}
    assert "text" not in result and isinstance(result["elapsed_ms"], int)
    assert result["headers"] == {"content-type": "application/json", "content-length": "7",
                                 "x-request-id": "r-1", "ratelimit-remaining": "9"}
    (request,) = wire.requests
    assert request.method == "GET" and request.url.params["q"] == "a b" and request.headers["x-mine"] == "1"


@sync
async def test_the_request_body_is_json_or_text(wire):
    await call(method="POST", json={"a": [1, 2]})
    await call(method="PUT", body="plain text", headers={"Content-Type": "text/plain"})
    assert json.loads(wire.requests[0].content) == {"a": [1, 2]}
    assert wire.requests[0].headers["content-type"] == "application/json"
    assert wire.requests[1].content == b"plain text" and wire.requests[1].method == "PUT"


@sync
async def test_a_text_answer_and_its_truncation(wire):
    wire.answer = lambda r: httpx2.Response(200, text="hello " * 100, headers={"content-type": "text/plain"})
    full = await call()
    assert full["text"] == "hello " * 100 and "truncated" not in full
    cut = await call(max_response_bytes=10)
    assert cut["text"] == "hello hell" and cut["truncated"] is True and cut["ok"] is True


@sync
async def test_a_json_answer_over_the_cap_is_response_too_large(wire):
    wire.answer = lambda r: httpx2.Response(200, json={"v": "x" * 500})
    assert (await call(max_response_bytes=100))["error"]["code"] == "response_too_large"
    async def chunks():
        for part in (b'{"v": "', b"x" * 500, b'"}'):
            yield part

    wire.answer = lambda r: httpx2.Response(200, content=chunks(), headers={"content-type": "application/json"})
    assert (await call(max_response_bytes=100))["error"]["code"] == "response_too_large"


@sync
async def test_a_non_2xx_status_is_a_result_not_an_error(wire):
    wire.answer = lambda r: httpx2.Response(401, json={"error": "AuthenticationRequired"})
    result = await call()
    assert result["status"] == 401 and result["ok"] is False and result["json"] == {"error": "AuthenticationRequired"}
    assert "error" not in result or not isinstance(result.get("error"), dict) or "code" not in result["error"]
    wire.answer = lambda r: httpx2.Response(429, text="slow down", headers={"retry-after": "7"})
    limited = await call()
    assert limited["ok"] is False and limited["headers"]["retry-after"] == "7"


@sync
async def test_a_redirect_is_returned_not_followed(wire):
    wire.answer = lambda r: httpx2.Response(302, headers={"location": "https://other.example/y"})
    result = await call()
    assert result["status"] == 302 and result["headers"]["location"] == "https://other.example/y"
    assert len(wire.requests) == 1


# ------------------------------------------------------------------------------- the failures


@sync
async def test_transport_failures_are_error_objects(wire):
    wire.answer = lambda r: httpx2.ConnectTimeout("t")
    assert (await call())["error"]["code"] == "http_timeout"
    wire.answer = lambda r: httpx2.ConnectError("refused to https://api.example.com/x?token=FAKE")
    failed = await call()
    assert failed["error"] == {"code": "http_connect_failed", "message": "the connection could not be made"}


@sync
async def test_the_whole_request_is_bounded_by_timeout_s(monkeypatch):
    class Slow(httpx2.AsyncBaseTransport):
        async def handle_async_request(self, request):
            await asyncio.sleep(5)
            return httpx2.Response(200)

    monkeypatch.setattr(http_tool, "new_client", lambda policy, t: httpx2.AsyncClient(transport=Slow()))
    assert (await call(timeout_s=1))["error"]["code"] == "http_timeout"


@pytest.mark.parametrize("address", ["10.1.2.3", "169.254.169.254", "127.0.0.1", "fd00::1", "100.64.0.9"])
@sync
async def test_a_private_address_is_outbound_blocked(monkeypatch, address):
    async def resolver(host: str, port: int) -> list[str]:
        return [address]

    policy = OutboundPolicy(resolver=resolver)
    monkeypatch.setattr(http_tool, "outbound_policy", lambda: policy)  # the REAL guarded client, a fake DNS
    result = await call(url="https://metadata.example/latest")
    assert result["error"]["code"] == "outbound_blocked"
    literal = await call(url="https://169.254.169.254/latest/meta-data")
    assert literal["error"]["code"] == "outbound_blocked"
    assert (await call(url="https://user:pw@api.example.com/"))["error"]["code"] == "outbound_blocked"


# ------------------------------------------------------------------------------- secrets


@sync
async def test_a_reference_in_a_header_substring_and_a_json_value_is_resolved_and_redacted(wire):
    def echo(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={
            "you_sent": request.headers["authorization"], "body": json.loads(request.content),
            "list": ["x", request.headers["authorization"].split()[1]],
        }, headers={"x-request-id": f"echo-{PASSWORD}"})

    wire.answer = echo
    result = await call(
        method="POST", headers={"Authorization": "Bearer secret:api-key"},
        json={"identifier": "me", "password": "secret:pwd-main", "nested": [{"p": "secret:pwd-main"}]},
    )
    (request,) = wire.requests
    assert request.headers["authorization"] == "Bearer " + SECRETS["api-key"]
    assert json.loads(request.content) == {"identifier": "me", "password": PASSWORD, "nested": [{"p": PASSWORD}]}
    dumped = json.dumps(result)
    assert PASSWORD not in dumped and SECRETS["api-key"] not in dumped
    assert result["status"] == 200 and "[REDACTED]" in dumped
    assert result["json"]["body"]["identifier"] == "me"


@sync
async def test_a_reference_in_the_query_and_the_body_and_a_text_echo(wire):
    wire.answer = lambda r: httpx2.Response(200, text=f"got {r.url.params['k']} and {r.content.decode()}")
    result = await call(method="POST", query={"k": "secret:api-key"}, body="secret:pwd-main")
    assert SECRETS["api-key"] not in result["text"] and PASSWORD not in result["text"]
    assert result["text"].count("[REDACTED]") == 2


@sync
async def test_a_secret_is_redacted_from_a_truncated_text_at_the_cut(wire):
    wire.answer = lambda r: httpx2.Response(200, text="." * 8 + PASSWORD + "." * 50, headers={"content-type": "text/plain"})
    for cap in range(1, 40):
        result = await call(method="POST", body="secret:pwd-main", max_response_bytes=cap)
        text = result["text"]
        assert PASSWORD not in text
        assert not any(PASSWORD[:n] in text for n in range(6, len(PASSWORD)) if text.endswith(PASSWORD[:n])), (cap, text)


@sync
async def test_an_unknown_reference_sends_nothing(wire):
    result = await call(method="POST", json={"p": "secret:nope"})
    assert result == {"error": {"code": "secret_not_found", "message": "secret:nope is not a stored secret"}}
    header = await call(headers={"Authorization": "Bearer secret:nope2"})
    assert header["error"]["code"] == "secret_not_found"
    assert wire.requests == []


@sync
async def test_a_substring_reference_outside_headers_is_not_resolved(wire):
    await call(method="POST", json={"text": "my secret:pwd-main is here"}, body=None)
    assert json.loads(wire.requests[0].content) == {"text": "my secret:pwd-main is here"}


# ------------------------------------------------------------------------------- the pure helpers


def test_secret_names_and_the_card_words():
    args = {"method": "POST", "url": "https://bsky.example/xrpc/c.d?token=secret:zzz",
            "headers": {"Authorization": "Bearer secret:b-key", "X": "plain"},
            "json": {"a": ["secret:a-key", "secret:a-key"], "b": "not secret:inside"}, "query": {"q": "secret:q-key"}}
    assert http_request_secret_names(args) == ["a-key", "b-key", "q-key"]
    assert http_request_summary(args) == "POST bsky.example/xrpc/c.d using secrets a-key, b-key, q-key"
    assert http_request_secret_names({"method": "GET", "url": "https://a.example/"}) == []
    assert http_request_summary({"method": "GET", "url": "https://a.example"}) == "GET a.example/"
    assert "FAKE" not in http_request_summary({**args, "headers": {"A": "Bearer FAKE-literal"}})


# ------------------------------------------------------------------------------- drop_response_paths


@sync
async def test_drop_response_paths_removes_members_before_the_result(wire):
    wire.answer = lambda r: httpx2.Response(200, json={
        "accessJwt": "a", "refreshJwt": "SECRET-R", "email": "x@y", "nested": {"k": 1, "a/b": 2, "t~": 3},
        "items": [10, 11, 12],
    })
    result = await call(drop_response_paths=["/refreshJwt", "/email", "/nested/k", "/nested/a~1b", "/nested/t~0", "/items/1"])
    assert result["json"] == {"accessJwt": "a", "nested": {}, "items": [10, 12]}
    assert "SECRET-R" not in json.dumps(result)


@sync
async def test_drop_response_paths_missing_path_and_non_object_are_no_ops(wire):
    wire.answer = lambda r: httpx2.Response(200, json={"a": 1})
    assert (await call(drop_response_paths=["/nope", "/a/b/c", "/a/0", "/x/y"]))["json"] == {"a": 1}
    wire.answer = lambda r: httpx2.Response(200, json=[1, 2])
    assert (await call(drop_response_paths=["/a", "/5"]))["json"] == [1, 2]
    wire.answer = lambda r: httpx2.Response(200, json="text")
    assert (await call(drop_response_paths=["/a"]))["json"] == "text"
    wire.answer = lambda r: httpx2.Response(200, text="plain", headers={"content-type": "text/plain"})
    assert (await call(drop_response_paths=["/a"]))["text"] == "plain"


def test_drop_response_paths_is_capped_at_16_and_must_be_pointers():
    base = {"method": "GET", "url": "https://a.example/"}
    HttpRequestInput.model_validate({**base, "drop_response_paths": ["/a"] * 16})
    for bad in (["/a"] * 17, ["a"], [""], [1]):
        with pytest.raises(ValidationError):
            HttpRequestInput.model_validate({**base, "drop_response_paths": bad})


# ------------------------------------------------------------------------------- body_from_url

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4
UPLOAD = "https://bsky.example/xrpc/com.atproto.repo.uploadBlob"
IMAGE = "https://cdn.example.com/thumb.png"


def source(**kw: Any) -> dict[str, Any]:
    return {"url": IMAGE, "allowed_hosts": ["cdn.example.com"], **kw}


def upstream(wire: Wire, image=lambda r: httpx2.Response(200, content=PNG, headers={"content-type": "image/png"})):
    """cdn.example.com serves `image`; any other host is the upload endpoint, which echoes the blob."""

    def answer(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "cdn.example.com":
            return image(request)
        return httpx2.Response(200, json={"blob": {"size": len(request.content), "type": request.headers.get("content-type")}})

    wire.answer = answer


@sync
async def test_body_from_url_sends_the_fetched_bytes_with_its_content_type(wire):
    upstream(wire)
    result = await call(method="POST", url=UPLOAD, body_from_url=source())
    assert result["json"] == {"blob": {"size": len(PNG), "type": "image/png"}} and result["status"] == 200
    fetch, send = wire.requests
    assert fetch.method == "GET" and str(fetch.url) == IMAGE and send.content == PNG
    assert set(result) == {"status", "ok", "headers", "json", "elapsed_ms"}  # the result keeps its shape


@sync
async def test_a_caller_content_type_wins_and_secret_refs_still_resolve_in_headers(wire):
    upstream(wire)
    await call(method="POST", url=UPLOAD, headers={"Authorization": "Bearer secret:api-key", "content-type": "image/jpeg"},
               body_from_url=source())
    fetch, send = wire.requests
    assert send.headers["content-type"] == "image/jpeg" and send.headers["authorization"] == f"Bearer {SECRETS['api-key']}"
    assert "authorization" not in fetch.headers  # the credential never goes to the image host
    assert "secret:" not in str(fetch.url)


@pytest.mark.parametrize("answer, why", [
    (lambda r: httpx2.Response(200, content=b"<html>", headers={"content-type": "text/html"}), "not an image"),
    (lambda r: httpx2.Response(200, content=b"x", headers={}), "not an image"),
    (lambda r: httpx2.Response(302, headers={"location": "https://cdn.example.com/other.png"}), "redirect"),
    (lambda r: httpx2.Response(404, headers={"content-type": "image/png"}), "status 404"),
    (lambda r: httpx2.Response(200, content=PNG, headers={"content-type": "image/png", "content-length": "9999999"}), "larger"),
])
@sync
async def test_a_bad_source_answer_is_body_source_rejected_and_nothing_is_sent(wire, answer, why):
    upstream(wire, image=answer)
    result = await call(method="POST", url=UPLOAD, body_from_url=source(max_bytes=1000))
    assert result["error"]["code"] == "body_source_rejected" and why in result["error"]["detail"]
    assert len(wire.requests) == 1 and wire.requests[0].url.host == "cdn.example.com"
    assert IMAGE not in json.dumps(result)


@sync
async def test_an_oversize_body_without_a_content_length_is_cut_and_rejected(wire):
    upstream(wire, image=lambda r: httpx2.Response(200, content=b"A" * 5000, headers={"content-type": "image/png"}))
    result = await call(method="POST", url=UPLOAD, body_from_url=source(max_bytes=1000))
    assert result["error"]["code"] == "body_source_rejected" and "max_bytes" in result["error"]["detail"]
    exact = await call(method="POST", url=UPLOAD, body_from_url=source(max_bytes=5000))
    assert exact["status"] == 200  # exactly max_bytes is allowed


@sync
async def test_the_source_host_must_be_allowed_and_https_and_nothing_is_fetched_otherwise(wire):
    upstream(wire)
    for bad in (source(allowed_hosts=["other.example.com"]), source(url="http://cdn.example.com/a.png"),
                source(url="https://CDN.example.com.evil.example/a.png")):
        result = await call(method="POST", url=UPLOAD, body_from_url=bad)
        assert result["error"]["code"] == "body_source_rejected"
    assert wire.requests == []
    assert (await call(method="POST", url=UPLOAD, body_from_url=source(url="https://CDN.Example.com/a.png")))["status"] == 200


@sync
async def test_body_from_url_excludes_json_and_body(wire):
    upstream(wire)
    for extra in ({"json": {"a": 1}}, {"body": "x"}):
        assert (await call(method="POST", url=UPLOAD, body_from_url=source(), **extra))["error"]["code"] == "body_conflict"
    assert wire.requests == []


@sync
async def test_a_private_source_address_is_rejected_by_the_guard(monkeypatch):
    async def resolver(host: str, port: int) -> list[str]:
        return ["10.1.2.3"]

    monkeypatch.setattr(http_tool, "outbound_policy", lambda: OutboundPolicy(resolver=resolver))
    result = await call(method="POST", url=UPLOAD, body_from_url=source())
    assert result["error"]["code"] == "body_source_rejected" and "outbound policy" in result["error"]["detail"]


def test_body_from_url_limits_and_the_card_names_the_source_host():
    for bad in ({"max_bytes": 5_000_001}, {"max_bytes": 0}, {"allowed_hosts": []}, {"extra": 1}):
        with pytest.raises(ValidationError):
            HttpRequestInput.model_validate({"method": "POST", "url": UPLOAD, "body_from_url": source(**bad)})
    parsed = HttpRequestInput.model_validate({"method": "POST", "url": UPLOAD, "body_from_url": source()})
    assert parsed.body_from_url.max_bytes == 1_000_000
    assert http_request_summary({"method": "POST", "url": UPLOAD, "body_from_url": source()}) == \
        "POST bsky.example/xrpc/com.atproto.repo.uploadBlob with a body from cdn.example.com"


# ------------------------------------------------------------------------------- redirect following


@sync
async def test_default_max_redirects_0_keeps_existing_behavior(wire):
    """Without max_redirects, redirects are returned as-is (not followed)."""
    wire.answer = lambda r: httpx2.Response(301, headers={"location": "https://api.example.com/v1/y"})
    result = await call()
    assert result["status"] == 301 and result["headers"]["location"] == "https://api.example.com/v1/y"
    assert "redirected_from" not in result
    assert len(wire.requests) == 1  # Only the original request


@sync
async def test_same_host_301_followed_once_with_max_redirects(wire, monkeypatch):
    """A same-host 301 redirect followed once with max_redirects=1."""
    async def resolver(host: str, port: int) -> list[str]:
        return ["1.2.3.4"]  # Public IP for testing
    
    monkeypatch.setattr(http_tool, "outbound_policy", lambda: OutboundPolicy(resolver=resolver))
    monkeypatch.setattr(
        http_tool, "new_client",
        lambda policy, timeout_s: httpx2.AsyncClient(transport=httpx2.MockTransport(wire.handler)),
    )
    
    call_count = [0]
    def answer(r: httpx2.Request) -> httpx2.Response:
        call_count[0] += 1
        if call_count[0] == 1:
            return httpx2.Response(301, headers={"location": "/v1/y"})
        else:
            return httpx2.Response(200, json={"ok": True})
    
    wire.answer = answer
    result = await call(max_redirects=1)
    assert result["status"] == 200 and result["ok"] is True and result["json"] == {"ok": True}
    assert result["redirected_from"] == ["https://api.example.com/v1/x", "https://api.example.com/v1/y"]
    assert len(wire.requests) == 2


@sync
async def test_limit_reached_returns_3xx_with_error(wire, monkeypatch):
    """When max_redirects is reached, the last 3xx is returned with redirect_refused error."""
    async def resolver(host: str, port: int) -> list[str]:
        return ["1.2.3.4"]
    
    monkeypatch.setattr(http_tool, "outbound_policy", lambda: OutboundPolicy(resolver=resolver))
    monkeypatch.setattr(
        http_tool, "new_client",
        lambda policy, timeout_s: httpx2.AsyncClient(transport=httpx2.MockTransport(wire.handler)),
    )
    
    call_count = [0]
    def answer(r: httpx2.Request) -> httpx2.Response:
        call_count[0] += 1
        return httpx2.Response(301, headers={"location": "/next"})
    
    wire.answer = answer
    result = await call(max_redirects=1)
    assert result["status"] == 301
    assert result["ok"] is False
    assert result["headers"]["location"] == "/next"
    assert result["error"]["code"] == "redirect_refused"
    assert "maximum redirects exceeded" in result["error"]["message"]
    assert "redirected_from" in result


@sync
async def test_cross_host_hop_refused_without_allowed_hosts(wire, monkeypatch):
    """A cross-host redirect is refused unless the host is in allowed_hosts."""
    async def resolver(host: str, port: int) -> list[str]:
        return ["1.2.3.4"]
    
    monkeypatch.setattr(http_tool, "outbound_policy", lambda: OutboundPolicy(resolver=resolver))
    monkeypatch.setattr(
        http_tool, "new_client",
        lambda policy, timeout_s: httpx2.AsyncClient(transport=httpx2.MockTransport(wire.handler)),
    )
    
    call_count = [0]
    def answer(r: httpx2.Request) -> httpx2.Response:
        call_count[0] += 1
        if call_count[0] == 1:
            return httpx2.Response(301, headers={"location": "https://other.example.com/y"})
        else:
            return httpx2.Response(200, json={"ok": True})
    
    wire.answer = answer
    result = await call(max_redirects=1)
    assert result["error"]["code"] == "redirect_refused"
    assert "redirect host not allowed" in result["error"]["message"]


@sync
async def test_303_after_POST_becomes_GET_without_body(wire, monkeypatch):
    """303 redirects always use GET without a body."""
    async def resolver(host: str, port: int) -> list[str]:
        return ["1.2.3.4"]
    
    monkeypatch.setattr(http_tool, "outbound_policy", lambda: OutboundPolicy(resolver=resolver))
    monkeypatch.setattr(
        http_tool, "new_client",
        lambda policy, timeout_s: httpx2.AsyncClient(transport=httpx2.MockTransport(wire.handler)),
    )
    
    call_count = [0]
    def answer(r: httpx2.Request) -> httpx2.Response:
        call_count[0] += 1
        if call_count[0] == 1:
            return httpx2.Response(303, headers={"location": "/new"})
        else:
            return httpx2.Response(200, json={"ok": True})
    
    wire.answer = answer
    result = await call(method="POST", json={"data": "test"}, max_redirects=1)
    assert result["status"] == 200
    # The second request should be GET without body
    assert wire.requests[1].method == "GET"
    # GET requests can have empty content
    assert wire.requests[1].content == b""


@sync
async def test_307_keeps_POST_and_body(wire, monkeypatch):
    """307 redirects keep the method and body."""
    async def resolver(host: str, port: int) -> list[str]:
        return ["1.2.3.4"]
    
    monkeypatch.setattr(http_tool, "outbound_policy", lambda: OutboundPolicy(resolver=resolver))
    monkeypatch.setattr(
        http_tool, "new_client",
        lambda policy, timeout_s: httpx2.AsyncClient(transport=httpx2.MockTransport(wire.handler)),
    )
    
    call_count = [0]
    def answer(r: httpx2.Request) -> httpx2.Response:
        call_count[0] += 1
        if call_count[0] == 1:
            return httpx2.Response(307, headers={"location": "/new"})
        else:
            return httpx2.Response(200, json={"ok": True})
    
    wire.answer = answer
    result = await call(method="POST", json={"data": "test"}, max_redirects=1)
    assert result["status"] == 200
    # The second request should still be POST with body
    assert wire.requests[1].method == "POST"
    assert wire.requests[1].content is not None


@sync
async def test_redirected_from_absent_when_no_hop(wire):
    """redirected_from is absent when no redirect was followed."""
    wire.answer = lambda r: httpx2.Response(200, json={"ok": True})
    result = await call(max_redirects=1)  # max_redirects > 0 but no redirect
    assert result["status"] == 200
    assert "redirected_from" not in result


def test_max_redirects_validation():
    """max_redirects must be 0-5."""
    for bad in (6, -1, 10):
        with pytest.raises(ValidationError):
            HttpRequestInput.model_validate({"method": "GET", "url": "https://a.example/", "max_redirects": bad})
    
    for ok in (0, 1, 5):
        parsed = HttpRequestInput.model_validate({"method": "GET", "url": "https://a.example/", "max_redirects": ok})
        assert parsed.max_redirects == ok
