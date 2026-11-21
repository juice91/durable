"""Acceptance tests for API input hardening, beyond the implementer's suite.

Covers every guarded write on the agents, runs, conversations, MCP, tools and workflows
routers (NUL and a lone surrogate in a nested field and in a key), the body cap at the
boundary and on a stream with no Content-Length, UTF-16 with a BOM, compact query and
path 422s, NUL in query and path parameters on every guarded router (401 before 422),
MCP header values never coming back in a 422, the agent-run and turn enqueue-failure
flows including an enqueue DBOS accepted and the client call then raised, and the CORS
deny list.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import anyio
import pytest
from sqlalchemy import create_engine, text

from eap_api.routes.mcp_servers import MAX_HEADER_VALUE, get_outbound_policy
from eap_run_store import ENQUEUE_FAILED_ERROR, TERMINAL_RUN_STATUSES
from eap_secrets.outbound import OutboundPolicy

from .run_helpers import PG_URL, hard_timeout
from .test_conversations import new_conversation, turn
from .test_runs import (  # noqa: F401 - fixtures
    SPEC,
    FakeDBOSClient,
    _no_hang,
    deltas,
    fake_dbos,
    make_agent,
    pg,
    start,
    stream_settings,
    tenant_of,
)
from .test_workflows_smoke import _spec as workflow_spec

# Same default as the API end-to-end test. Never print it: the URL carries a password.
_DBOS_URL = os.environ.get("EAP_TEST_DBOS_URL", "postgresql://eap_dbos:eap_dbos_password@localhost:54329/eap_dbos")
_REPO_ROOT = Path(__file__).resolve().parents[3]
_APP_VERSION = "m1-test"  # LauncherSettings app_version the `pg` fixture enqueues with

AGENT_CAP = 262144
WORKFLOW_CAP = 131072
AGENT_DEPTH = "request body is nested more than 32 levels deep"
WORKFLOW_DEPTH = "workflow spec is nested more than 64 levels deep"
NOT_UTF8 = "request body must be UTF-8 encoded JSON (no NUL bytes)"
ENQUEUE_503 = "Run recorded but could not be started; retry the same request (same run_id)"
LATER_TURN = "This turn could not be started and a later turn has started since; start a new turn"
DIFFERENT_RUN = "run_id is already used by a different run request"

NUL = "a NUL character (\\u0000), which cannot be stored"
SUR_HIGH = "an unpaired surrogate (\\ud800), which cannot be stored"
SUR_LOW = "an unpaired surrogate (\\udfff), which cannot be stored"

# Document order: nested values first, then each bad key. Both surrogate ends of the range.
POISON: dict[str, Any] = {
    "nest": {"level": {"nul": "a\x00b", "high": "c\ud800d", "low": "e\udfffd"}},
    "n\x00k": 1,
    "h\ud800k": 2,
    "l\udfffk": 3,
}
POISON_ERRORS = [
    {"loc": ["body", "nest", "level", "nul"], "msg": f"string contains {NUL}", "type": "string_unstorable"},
    {"loc": ["body", "nest", "level", "high"], "msg": f"string contains {SUR_HIGH}", "type": "string_unstorable"},
    {"loc": ["body", "nest", "level", "low"], "msg": f"string contains {SUR_LOW}", "type": "string_unstorable"},
    {"loc": ["body", "n?k"], "msg": f"key contains {NUL}", "type": "string_unstorable"},
    {"loc": ["body", "h?k"], "msg": f"key contains {SUR_HIGH}", "type": "string_unstorable"},
    {"loc": ["body", "l?k"], "msg": f"key contains {SUR_LOW}", "type": "string_unstorable"},
]


def _send(client, method: str, url: str, payload: Any, headers: dict[str, str]):
    """JSON with \\u escapes, the way JSON.stringify sends a NUL or a lone surrogate."""
    return client.request(
        method, url, content=json.dumps(payload).encode(),
        headers={**headers, "content-type": "application/json"},
    )


def _pad(payload: bytes, size: int) -> bytes:
    assert len(payload) <= size, len(payload)
    return payload + b" " * (size - len(payload))


def _nested(levels: int) -> bytes:
    """JSON object whose bracket depth is exactly `levels` (the outer object counts as one)."""
    arrays = levels - 1
    return b'{"a":' + b"[" * arrays + b"1" + b"]" * arrays + b"}"


def _assert_compact(detail: Any) -> None:
    assert isinstance(detail, list) and detail, detail
    assert len(detail) <= 20
    for item in detail:
        assert set(item) == {"loc", "msg", "type"}, item


def _run_row(app, run_id: str) -> tuple[str, str | None]:
    with app.state.database_engine.connect() as conn:
        row = conn.execute(text("SELECT status, error FROM runs WHERE id = :r"), {"r": run_id}).one()
    return row[0], row[1]


def _asgi(app, method: str, path: str, chunks: list[bytes], headers: list[tuple[bytes, bytes]]):
    """One HTTP request straight to the ASGI app. `chunks` are what the receive channel yields,
    and nothing past the guard's early return is pulled (a later chunk stays unread)."""
    seen: list[str] = []
    sent: list[dict[str, Any]] = []

    async def receive():
        index = len(seen)
        if index < len(chunks):
            seen.append("body")
            return {"type": "http.request", "body": chunks[index], "more_body": index + 1 < len(chunks)}
        seen.append("extra")
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "root_path": "",
        "state": {},
    }

    async def run():
        with anyio.fail_after(15):
            await app(scope, receive, send)

    anyio.run(run)
    start = next(message for message in sent if message["type"] == "http.response.start")
    body = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
    return start["status"], bytes(body), seen


def _headers(auth: str | None = None, content_length: int | None = None) -> list[tuple[bytes, bytes]]:
    headers = [(b"host", b"testserver"), (b"content-type", b"application/json")]
    if auth is not None:
        headers.append((b"authorization", auth.encode()))
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode()))
    return headers


def _json_body(status: int, body: bytes) -> Any:
    assert body, status
    return json.loads(body)


def _allow_mcp(app) -> None:
    async def resolve(host: str, port: int) -> list[str]:
        return ["93.184.215.14"]

    app.dependency_overrides[get_outbound_policy] = lambda: OutboundPolicy(resolver=resolve)


def _create_mcp(client, headers: dict[str, str]) -> str:
    resp = client.post(
        "/api/v1/mcp-servers",
        json={"name": f"srv-{uuid.uuid4().hex[:8]}", "url": "https://mcp.example.com/mcp"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# --------------------------------------------------------------------------- every write route


def test_every_guarded_write_rejects_nul_and_surrogates_in_nested_fields_and_keys(pg, tenant_of):
    """POST, PATCH (there is no PUT) on every guarded router. The bad strings are \\u escapes
    inside a nested object and inside object keys. Nothing is stored."""
    client, app = pg
    _allow_mcp(app)
    _, headers = tenant_of()
    agent_id = make_agent(client, headers)
    conversation_id = new_conversation(client, headers, agent_id, title="kept")
    server_id = _create_mcp(client, headers)
    created = client.post("/api/v1/workflows", json={"spec": workflow_spec(agent_id)}, headers=headers)
    assert created.status_code == 201, created.text
    workflow_id = created.json()["id"]
    missing = str(uuid.uuid4())

    before = {
        "agents": client.get("/api/v1/agents", headers=headers).json(),
        "conversations": client.get("/api/v1/conversations", headers=headers).json(),
        "mcp": client.get("/api/v1/mcp-servers", headers=headers).json(),
        "workflows": client.get("/api/v1/workflows", headers=headers).json(),
        "runs": client.get("/api/v1/runs", headers=headers).json()["items"],
    }
    targets = [
        ("POST", "/api/v1/agents"),
        ("PATCH", f"/api/v1/agents/{agent_id}"),
        ("POST", f"/api/v1/agents/{agent_id}/versions"),
        ("POST", f"/api/v1/agents/{agent_id}/runs"),
        ("POST", f"/api/v1/agents/{agent_id}/conversations"),
        ("POST", f"/api/v1/conversations/{conversation_id}/runs"),
        ("POST", "/api/v1/mcp-servers"),
        ("PATCH", f"/api/v1/mcp-servers/{server_id}"),
        ("POST", f"/api/v1/mcp-servers/{server_id}/test"),
        ("POST", "/api/v1/tools/tickets.create/runs"),
        ("POST", f"/api/v1/workflows/{workflow_id}/versions"),
        ("POST", f"/api/v1/workflows/{workflow_id}/runs"),
        ("POST", f"/api/v1/workflows/runs/{missing}/steps/sign-off/approve"),
        ("POST", f"/api/v1/workflows/runs/{missing}/steps/sign-off/reject"),
    ]
    for method, url in targets:
        resp = _send(client, method, url, POISON, headers)
        assert resp.status_code == 422, (method, url, resp.status_code, resp.text[:400])
        assert resp.json()["detail"] == POISON_ERRORS, (method, url, resp.json()["detail"])

    assert client.get("/api/v1/agents", headers=headers).json() == before["agents"]
    assert client.get("/api/v1/conversations", headers=headers).json() == before["conversations"]
    assert client.get("/api/v1/mcp-servers", headers=headers).json() == before["mcp"]
    assert client.get("/api/v1/workflows", headers=headers).json() == before["workflows"]
    assert client.get("/api/v1/runs", headers=headers).json()["items"] == before["runs"]
    assert client.get(f"/api/v1/agents/{agent_id}", headers=headers).json()["name"] == SPEC["name"]
    assert client.get(f"/api/v1/conversations/{conversation_id}", headers=headers).json()["title"] == "kept"
    assert client.get(f"/api/v1/workflows/{workflow_id}", headers=headers).json()["draft_spec"]["name"] == "Smoke"


def test_workflow_drafts_name_their_own_nul_and_surrogate_paths(pg, tenant_of):
    """Create and patch skip the shared walk (@schema_checks_storable). WorkflowSpec rejects the
    string itself and names the step path; a bad key is shown with '?' ."""
    client, _app = pg
    _, headers = tenant_of()
    agent_id = str(uuid.uuid4())
    created = client.post("/api/v1/workflows", json={"spec": workflow_spec(agent_id)}, headers=headers)
    assert created.status_code == 201, created.text
    workflow_id = created.json()["id"]
    cases = [
        ("a\x00b", "a NUL character (\\u0000) at steps.0.input.nest.level", False),
        ("c\ud800d", "an unpaired surrogate (\\ud800) at steps.0.input.nest.level", False),
        ("e\udfffd", "an unpaired surrogate (\\udfff) at steps.0.input.nest.level", False),
        ("n\x00k", "a NUL character (\\u0000) in the key at steps.0.input.n?k", True),
        ("h\ud800k", "an unpaired surrogate (\\ud800) in the key at steps.0.input.h?k", True),
        ("l\udfffk", "an unpaired surrogate (\\udfff) in the key at steps.0.input.l?k", True),
    ]
    listed = len(client.get("/api/v1/workflows", headers=headers).json())
    for bad, needle, is_key in cases:
        spec = workflow_spec(agent_id)
        if is_key:
            spec["steps"][0]["input"] = {bad: "v"}
        else:
            spec["steps"][0]["input"] = {"nest": {"level": bad}}
        for method, url in (("POST", "/api/v1/workflows"), ("PATCH", f"/api/v1/workflows/{workflow_id}")):
            resp = _send(client, method, url, {"spec": spec}, headers)
            assert resp.status_code == 422, (method, bad, resp.status_code, resp.text[:500])
            detail = resp.json()["detail"]
            _assert_compact(detail)
            assert detail[0]["loc"] == ["body", "spec"]
            # The parsed message, not the raw JSON: the body escapes the backslash in `\u0000`.
            assert f"workflow spec contains {needle}" in detail[0]["msg"], detail[0]["msg"]
            assert detail[0]["type"] != "string_unstorable"
    assert len(client.get("/api/v1/workflows", headers=headers).json()) == listed
    assert client.get(f"/api/v1/workflows/{workflow_id}", headers=headers).json()["draft_spec"]["name"] == "Smoke"

    # An extra top-level key is the shared compact 422, still with no echo of the value.
    extra = {"spec": workflow_spec(agent_id), "n\x00k": {"level": "hidden-surrogate-\ud800"}}
    resp = _send(client, "POST", "/api/v1/workflows", extra, headers)
    assert resp.status_code == 422, resp.text[:400]
    _assert_compact(resp.json()["detail"])
    assert "hidden-surrogate" not in resp.text


def test_twenty_one_unstorable_strings_report_the_first_twenty(pg, tenant_of):
    client, _app = pg
    _, headers = tenant_of()
    resp = _send(client, "POST", "/api/v1/agents", {"items": [f"{i}\x00" for i in range(25)]}, headers)
    detail = resp.json()["detail"]
    _assert_compact(detail)
    assert len(detail) == 20
    assert [item["loc"] for item in detail] == [["body", "items", i] for i in range(20)]
    assert all(item["type"] == "string_unstorable" for item in detail)


# --------------------------------------------------------------------------- caps, streaming, encoding, depth, auth order


def test_body_cap_boundary_content_length_and_stream(pg, tenant_of):
    """Exactly the cap is accepted; one byte over is 413. Content-Length is decided before the
    stream is read. A chunked body with no Content-Length stops at the cap and does not read on."""
    client, app = pg
    _, headers = tenant_of()
    auth = headers["Authorization"]
    agent_exact = _pad(json.dumps({"spec": SPEC}).encode(), AGENT_CAP)
    workflow_exact = _pad(json.dumps({"spec": workflow_spec(str(uuid.uuid4()))}).encode(), WORKFLOW_CAP)
    json_headers = {**headers, "content-type": "application/json"}

    for url, exact, cap in (
        ("/api/v1/agents", agent_exact, AGENT_CAP),
        ("/api/v1/workflows", workflow_exact, WORKFLOW_CAP),
    ):
        assert len(exact) == cap
        ok = client.post(url, content=exact, headers=json_headers)
        assert ok.status_code == 201, (url, ok.status_code, ok.text[:300])
        over = client.post(url, content=exact + b" ", headers=json_headers)
        assert over.status_code == 413, (url, over.status_code, over.text[:300])
        assert over.json() == {"detail": f"request body too large (max {cap} bytes)"}
        assert len(over.content) < 200

        # No Authorization: one over is 413, exactly the cap falls through to 401.
        anon_over = client.post(url, content=exact + b" ", headers={"content-type": "application/json"})
        assert anon_over.status_code == 413 and anon_over.json()["detail"].endswith(f"(max {cap} bytes)")
        anon_exact = client.post(url, content=exact, headers={"content-type": "application/json"})
        assert anon_exact.status_code == 401, (url, anon_exact.status_code, anon_exact.text[:200])

        def _chunks(payload: bytes = exact + b" "):
            mid = len(payload) // 2
            yield payload[:mid]
            yield payload[mid:]

        streamed = client.post(url, content=_chunks(), headers={"content-type": "application/json"})
        assert streamed.status_code == 413, (url, streamed.status_code, streamed.text[:200])
        assert streamed.json()["detail"] == f"request body too large (max {cap} bytes)"

    # The iterator request carries no Content-Length (chunked). Prove it on the request the client
    # actually builds, then prove the app stops reading once the cap is crossed.
    bare = {"content-type": "application/json"}

    def _over_chunks():
        yield b" " * (AGENT_CAP // 2)
        yield b" " * (AGENT_CAP // 2 + 8)
        yield b"SHOULD-NOT-BE-READ"

    request = client.build_request("POST", "/api/v1/agents", content=_over_chunks(), headers=bare)
    assert "content-length" not in {name.lower() for name in request.headers}
    assert request.headers["transfer-encoding"] == "chunked"
    sent = client.send(request)
    assert sent.status_code == 413
    assert sent.json()["detail"] == f"request body too large (max {AGENT_CAP} bytes)"

    # Declared length over the cap, body a few bytes: 413 and the stream is never read.
    status, body, seen = _asgi(
        app, "POST", "/api/v1/agents", [b"{}"], _headers(content_length=AGENT_CAP + 1)
    )
    assert (status, json.loads(body), seen) == (
        413, {"detail": f"request body too large (max {AGENT_CAP} bytes)"}, [],
    )
    status, body, seen = _asgi(
        app, "POST", "/api/v1/workflows", [b"{}"], _headers(content_length=WORKFLOW_CAP + 1)
    )
    assert status == 413 and json.loads(body)["detail"] == f"request body too large (max {WORKFLOW_CAP} bytes)"
    assert seen == []

    # No Content-Length. The second chunk crosses the cap; the third is never pulled.
    half = AGENT_CAP // 2
    status, body, seen = _asgi(
        app, "POST", "/api/v1/agents",
        [b" " * half, b" " * (AGENT_CAP - half + 1), b"SHOULD-NOT-BE-READ"],
        _headers(),
    )
    assert status == 413 and json.loads(body)["detail"].endswith(f"(max {AGENT_CAP} bytes)")
    assert seen == ["body", "body"]

    # Streamed body of exactly the cap (split in two) is accepted; one more byte is 413.
    mid = len(agent_exact) // 2
    status, body, seen = _asgi(
        app, "POST", "/api/v1/agents", [agent_exact[:mid], agent_exact[mid:]], _headers(auth=auth)
    )
    assert status == 201, (status, body[:300], seen)
    status, body, _seen = _asgi(
        app, "POST", "/api/v1/agents", [agent_exact, b" "], _headers(auth=auth)
    )
    assert status == 413 and json.loads(body)["detail"] == f"request body too large (max {AGENT_CAP} bytes)"

    mid = len(workflow_exact) // 2
    status, body, _seen = _asgi(
        app, "POST", "/api/v1/workflows", [workflow_exact[:mid], workflow_exact[mid:]], _headers(auth=auth)
    )
    assert status == 201, (status, body[:300])
    status, body, seen = _asgi(
        app, "POST", "/api/v1/workflows",
        [b" " * WORKFLOW_CAP, b" "],
        _headers(),
    )
    assert status == 413 and seen == ["body", "body"]


def test_utf16_bom_raw_nul_and_depth_are_rejected_before_auth(pg, tenant_of):
    client, _app = pg
    _, headers = tenant_of()
    json_headers = {"content-type": "application/json"}
    authed = {**headers, "content-type": "application/json"}
    text = json.dumps({"spec": SPEC})

    for label, payload in (
        ("utf-16", text.encode("utf-16")),
        ("utf-16-le BOM", b"\xff\xfe" + text.encode("utf-16-le")),
        ("utf-16-be BOM", b"\xfe\xff" + text.encode("utf-16-be")),
        ("raw NUL", b'{"spec":"a\x00b"}'),
        ("invalid utf-8", b'{"spec":"\xff"}'),
    ):
        resp = client.post("/api/v1/agents", content=payload, headers=json_headers)
        assert (label, resp.status_code, resp.json()) == (label, 415, {"detail": NOT_UTF8})

    # A UTF-8 BOM is allowed; the same bytes without auth are a 401, not a 415.
    bom = b"\xef\xbb\xbf" + text.encode()
    assert client.post("/api/v1/agents", content=bom, headers=authed).status_code == 201
    assert client.post("/api/v1/agents", content=bom, headers=json_headers).status_code == 401

    # Escaped NUL is well-formed UTF-8, so auth still wins.
    escaped = json.dumps({"nest": {"level": "a\x00b"}}).encode()
    assert b"\\u0000" in escaped
    assert client.post("/api/v1/agents", content=escaped, headers=json_headers).status_code == 401

    # Deeper than 32 is the agents' string 422, before auth. Exactly 32 reaches auth.
    deep = client.post("/api/v1/agents", content=_nested(33), headers=json_headers)
    assert (deep.status_code, deep.json()) == (422, {"detail": AGENT_DEPTH})
    exact = client.post("/api/v1/agents", content=_nested(32), headers=json_headers)
    assert exact.status_code == 401, exact.text[:200]
    # With auth, exactly 32 is an ordinary compact validation 422, not the depth string.
    validated = client.post("/api/v1/agents", content=_nested(32), headers=authed)
    assert validated.status_code == 422 and isinstance(validated.json()["detail"], list)
    assert validated.json()["detail"] != AGENT_DEPTH

    # Workflows keep their own limit (64, plus the {"spec": } level) and their own sentence.
    workflow_deep = client.post("/api/v1/workflows", content=_nested(66), headers=json_headers)
    assert (workflow_deep.status_code, workflow_deep.json()) == (422, {"detail": WORKFLOW_DEPTH})
    workflow_ok = client.post("/api/v1/workflows", content=_nested(65), headers=json_headers)
    assert workflow_ok.status_code == 401, workflow_ok.text[:200]
    # A body the agents' guard would already have rejected is still under the workflow limit.
    agents_depth = client.post("/api/v1/workflows", content=_nested(33), headers=json_headers)
    assert agents_depth.status_code == 401
    assert agents_depth.json().get("detail") != AGENT_DEPTH


def test_get_and_delete_ignore_oversized_and_non_utf8_bodies(pg, tenant_of):
    client, app = pg
    _allow_mcp(app)
    _, headers = tenant_of()
    server_id = _create_mcp(client, headers)
    over = b" " * (AGENT_CAP + 50)
    raw_nul = b'{"a":"\x00"}'
    utf16 = b"\xff\xfe" + '{"a":1}'.encode("utf-16-le")
    for payload in (over, raw_nul, utf16):
        listed = client.request("GET", "/api/v1/runs", content=payload, headers=headers)
        assert listed.status_code == 200, (len(payload), listed.status_code, listed.text[:200])
        deleted = client.request("DELETE", f"/api/v1/mcp-servers/{server_id}", content=payload, headers=headers)
        # The first payload deletes it; a repeat is 404. Neither is a body-guard status.
        assert deleted.status_code in (204, 404), (deleted.status_code, deleted.text[:200])
        assert deleted.status_code not in (413, 415)
    assert client.get(f"/api/v1/mcp-servers/{server_id}", headers=headers).status_code == 404


# --------------------------------------------------------------------------- compact 422s


def test_query_and_path_422s_are_compact_and_do_not_echo_input(pg, tenant_of):
    client, _app = pg
    _, headers = tenant_of()
    secret = "query-secret-" + "Z" * 80

    resp = client.get("/api/v1/runs", params={"limit": 0}, headers=headers)
    assert resp.status_code == 422
    _assert_compact(resp.json()["detail"])
    assert ["query", "limit"] in [item["loc"] for item in resp.json()["detail"]]

    both = client.get("/api/v1/runs", params={"limit": 0, "tool_id": secret}, headers=headers)
    assert both.status_code == 422
    _assert_compact(both.json()["detail"])
    locs = [item["loc"] for item in both.json()["detail"]]
    assert ["query", "limit"] in locs and ["query", "tool_id"] in locs
    assert secret not in both.text and len(both.content) < 2000

    path = client.get("/api/v1/runs/not-a-uuid", headers=headers)
    assert path.status_code == 422
    _assert_compact(path.json()["detail"])
    assert ["path", "run_id"] in [item["loc"] for item in path.json()["detail"]]
    assert "not-a-uuid" not in path.text

    # Twenty-five extra fields: the validation list is capped at 20 and never carries the value.
    echoed = "echo-me-not-" + "Q" * 120
    spec = {**SPEC, **{f"extra{i:02d}": echoed for i in range(25)}}
    resp = _send(client, "POST", "/api/v1/agents", {"spec": spec}, headers)
    detail = resp.json()["detail"]
    _assert_compact(detail)
    assert len(detail) == 20
    assert echoed not in resp.text

    # Route-raised 422s stay a string, including a cursor this API did not issue.
    cursor = client.get("/api/v1/runs", params={"before": "%%%not-a-cursor%%%"}, headers=headers)
    assert cursor.status_code == 422
    assert cursor.json() == {
        "detail": "before is not a valid cursor; pass the previous page's next_before verbatim"
    }
    assert "%%%not-a-cursor%%%" not in cursor.text
    mcp = client.post("/api/v1/mcp-servers", json=[1], headers=headers)
    assert mcp.status_code == 422 and mcp.json() == {"detail": "body must be a JSON object"}


def test_mcp_header_values_are_never_echoed_in_any_422(pg, tenant_of):
    client, app = pg
    _allow_mcp(app)
    _, headers = tenant_of()
    server_id = _create_mcp(client, headers)
    secret = "mcpHdrSecret_" + uuid.uuid4().hex + "_END"

    def bodies() -> list[tuple[str, dict[str, Any]]]:
        return [
            ("unstorable value", {
                "name": "srv-a", "url": "https://mcp.example.com/mcp",
                "headers": {"X-Api-Key": f"{secret}\x00", "X-Other": f"{secret}\ud800"},
            }),
            ("unstorable key", {
                "name": "srv-b", "url": "https://mcp.example.com/mcp",
                "headers": {f"X-{secret}": "plain", "Y-\ud800": secret},
            }),
            ("forbidden character", {
                "name": "srv-c", "url": "https://mcp.example.com/mcp",
                "headers": {"X-Api-Key": f"{secret}\nmore"},
            }),
            ("too long", {
                "name": "srv-d", "url": "https://mcp.example.com/mcp",
                "headers": {"X-Api-Key": secret + "a" * MAX_HEADER_VALUE},
            }),
            ("sibling field", {
                "name": "bad name", "url": "https://mcp.example.com/mcp",
                "headers": {"X-Api-Key": secret},
            }),
            ("nested elsewhere", {
                "name": "srv-e", "url": "https://mcp.example.com/mcp",
                "headers": {"X-Api-Key": secret},
                "note": {"level": "a\x00b"},
            }),
        ]

    for label, body in bodies():
        for method, url in (("POST", "/api/v1/mcp-servers"), ("PATCH", f"/api/v1/mcp-servers/{server_id}")):
            resp = _send(client, method, url, body, headers)
            assert resp.status_code == 422, (label, method, resp.status_code, resp.text[:400])
            assert secret not in resp.text, (label, method, resp.text[:400])
            assert len(resp.content) < 4000, (label, len(resp.content))
            detail = resp.json()["detail"]
            if isinstance(detail, list):
                _assert_compact(detail)
                assert all("input" not in item and "ctx" not in item for item in detail)
    # The server we created has no stored header names from these rejected writes.
    assert client.get(f"/api/v1/mcp-servers/{server_id}", headers=headers).json()["header_names"] == []


# --------------------------------------------------------------------------- enqueue failure, end to end


@pytest.mark.parametrize("fake_dbos", [FakeDBOSClient(fail_times=1)])
def test_agent_run_enqueue_failure_retries_only_the_same_input(pg, tenant_of, fake_dbos):
    client, app = pg
    _, headers = tenant_of()
    agent_id = make_agent(client, headers)
    run_id = str(uuid.uuid4())

    failed = start(client, headers, agent_id, run_id)
    assert failed.status_code == 503
    assert failed.json() == {"detail": ENQUEUE_503}
    assert "unreachable" not in failed.text and "system database" not in failed.text
    assert _run_row(app, run_id) == ("failed", ENQUEUE_FAILED_ERROR)
    got = client.get(f"/api/v1/runs/{run_id}", headers=headers).json()
    assert (got["status"], got["display_status"], got["error"]) == ("failed", "failed", ENQUEUE_FAILED_ERROR)

    conflict = start(client, headers, agent_id, run_id, input="a different input")
    assert conflict.status_code == 409
    assert conflict.json() == {"detail": DIFFERENT_RUN}
    assert _run_row(app, run_id) == ("failed", ENQUEUE_FAILED_ERROR)

    retry = start(client, headers, agent_id, run_id)
    assert retry.status_code == 200, retry.text
    assert (retry.json()["status"], retry.json()["display_status"]) == ("queued", "queued")
    assert retry.json().get("error") is None
    assert _run_row(app, run_id) == ("queued", None)
    healed = client.get(f"/api/v1/runs/{run_id}", headers=headers).json()
    assert (healed["status"], healed["error"]) == ("queued", None)
    assert [call[0]["workflow_id"] for call in fake_dbos.calls] == [run_id]


@pytest.mark.parametrize("fake_dbos", [FakeDBOSClient(fail_times=2)])
def test_failed_turn_frees_the_conversation_and_its_retry_cannot_overtake(pg, tenant_of, fake_dbos):
    client, app = pg
    _, headers = tenant_of()
    agent_id = make_agent(client, headers)
    conv_a = new_conversation(client, headers, agent_id, title="A")
    conv_b = new_conversation(client, headers, agent_id, title="B")
    first = str(uuid.uuid4())
    later = str(uuid.uuid4())
    only = str(uuid.uuid4())

    failed = turn(client, headers, conv_a, first, input="same")
    assert failed.status_code == 503 and failed.json() == {"detail": ENQUEUE_503}
    assert _run_row(app, first) == ("failed", ENQUEUE_FAILED_ERROR)

    failed_b = turn(client, headers, conv_b, only, input="same")
    assert failed_b.status_code == 503
    assert _run_row(app, only) == ("failed", ENQUEUE_FAILED_ERROR)

    # The failed turn is terminal, so the conversation accepts the next turn.
    nxt = turn(client, headers, conv_a, later, input="next")
    assert nxt.status_code == 201, nxt.text
    assert nxt.json()["turn"] == 2 and nxt.json()["status"] == "queued"

    again = turn(client, headers, conv_a, first, input="same")
    assert again.status_code == 409
    assert again.json() == {"detail": LATER_TURN}
    assert _run_row(app, first) == ("failed", ENQUEUE_FAILED_ERROR)

    # No later turn on B: the same run_id and input is re-queued.
    healed = turn(client, headers, conv_b, only, input="same")
    assert healed.status_code == 200, healed.text
    assert healed.json()["status"] == "queued" and healed.json()["turn"] == 1
    assert healed.json().get("error") is None
    assert _run_row(app, only) == ("queued", None)
    got = client.get(f"/api/v1/runs/{only}", headers=headers).json()
    assert (got["status"], got["error"]) == ("queued", None)
    assert [call[0]["workflow_id"] for call in fake_dbos.calls] == [later, only]


class RecordingFailDBOS(FakeDBOSClient):
    """DBOS accepted the enqueue, then the client call raised. The API still answers 503."""

    def enqueue(self, options: dict[str, Any], *args: Any) -> None:
        self.calls.append((dict(options), args))
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ConnectionError("system database unreachable")


def _redact(message: str) -> str:
    return re.sub(r"postgres(?:ql)?(?:\+\w+)?://\S+", "postgresql://redacted", message)


def _drop_dbos_schema(schema: str) -> None:
    engine = create_engine(_DBOS_URL.replace("postgresql://", "postgresql+psycopg://", 1))
    try:
        with engine.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    finally:
        engine.dispose()


def _stop_process(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        proc.wait(timeout=10)


def _start_worker(schema: str) -> tuple[subprocess.Popen[str], list[str]]:
    """In-process-queue worker on a private DBOS schema. The caller stops it."""
    env = {
        "EAP_DATABASE_URL": PG_URL or "",
        "EAP_DBOS_URL": _DBOS_URL,
        "EAP_DBOS_SYSTEM_SCHEMA": schema,
        "EAP_APP_VERSION": _APP_VERSION,
        "EAP_EXECUTOR_ID": f"accept-{schema[-8:]}",
        "EAP_QUEUE_POLL_SECONDS": "0.2",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "eap_worker"],
        cwd=_REPO_ROOT,
        env={**os.environ, **env},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    lines: list[str] = []

    def drain() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.append(line)

    threading.Thread(target=drain, daemon=True).start()
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline and proc.poll() is None:
        if any(line.strip() == "EAP_WORKER_READY" for line in lines):
            return proc, lines
        time.sleep(0.05)
    _stop_process(proc)
    raise AssertionError("worker did not become ready:\n" + _redact("".join(lines[-40:])))


def _turns(app, conversation_id: str) -> list[tuple[str, int, str]]:
    with app.state.database_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT id, turn, status FROM runs WHERE conversation_id = :c ORDER BY turn"),
            {"c": conversation_id},
        ).all()
    return [(str(row.id), int(row.turn), row.status) for row in rows]


def _events(app, run_id: str) -> list[tuple[str, str]]:
    with app.state.database_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT event_key, type FROM execution_events WHERE run_id = :r ORDER BY seq"),
            {"r": run_id},
        ).all()
    return [(row.event_key, row.type) for row in rows]


def _step_names(schema: str, run_id: str) -> list[str]:
    import psycopg

    with psycopg.connect(_DBOS_URL, connect_timeout=5) as conn:
        return [
            row[0]
            for row in conn.execute(
                f'SELECT function_name FROM "{schema}".operation_outputs '
                "WHERE workflow_uuid = %s ORDER BY function_id",
                (run_id,),
            )
        ]


def _watch_turns(app, conversation_id: str, stop: threading.Event, seen: list[list[tuple[str, int, str]]]) -> None:
    while not stop.wait(0.01):
        seen.append(_turns(app, conversation_id))


@pytest.mark.parametrize("fake_dbos", [RecordingFailDBOS(fail_times=1)])
def test_accepted_enqueue_that_raised_is_superseded_by_the_next_turn(pg, tenant_of, fake_dbos):
    """Turn 1's enqueue is recorded and then raises (503). Turn 2 starts (201). When turn 1's
    workflow actually runs, it stays failed: no event, no model call. Turn 2 then completes.
    The conversation never has two active turns."""
    with hard_timeout(140):
        client, app = pg
        tenant, headers = tenant_of()
        agent_id = make_agent(client, headers)
        conversation_id = new_conversation(client, headers, agent_id, title="race")
        first = str(uuid.uuid4())
        later = str(uuid.uuid4())

        failed = turn(client, headers, conversation_id, first, input="first")
        assert failed.status_code == 503 and failed.json() == {"detail": ENQUEUE_503}
        assert _run_row(app, first) == ("failed", ENQUEUE_FAILED_ERROR)
        assert len(fake_dbos.calls) == 1
        recorded_options, recorded_args = fake_dbos.calls[0]
        assert recorded_options["workflow_name"] == "eap.agent_run"
        assert recorded_options["workflow_id"] == first
        assert recorded_options["app_version"] == _APP_VERSION
        assert recorded_args[0] == first and recorded_args[1] == tenant and recorded_args[5] == "first"
        assert _turns(app, conversation_id) == [(first, 1, "failed")]

        nxt = turn(client, headers, conversation_id, later, input="second")
        assert nxt.status_code == 201, nxt.text
        assert nxt.json()["turn"] == 2 and nxt.json()["status"] == "queued"
        assert [call[0]["workflow_id"] for call in fake_dbos.calls] == [first, later]
        assert _active(app, conversation_id) == [later]

        schema = f"eap_accept_{uuid.uuid4().hex[:10]}"
        from eap_db.testing import create_commented_schema

        create_commented_schema(
            os.environ.get("EAP_TEST_DBOS_URL", "postgresql://eap_dbos:eap_dbos_password@localhost:54329/eap_dbos")
            .replace("postgresql://", "postgresql+psycopg://", 1),
            schema,
        )
        worker: subprocess.Popen[str] | None = None
        dbos_client: Any = None
        stop = threading.Event()
        seen: list[list[tuple[str, int, str]]] = []
        watcher = threading.Thread(target=_watch_turns, args=(app, conversation_id, stop, seen), daemon=True)
        watcher_started = False
        try:
            worker, _worker_log = _start_worker(schema)
            from dbos import DBOSClient

            dbos_client = DBOSClient(system_database_url=_DBOS_URL, dbos_system_schema=schema)
            watcher.start()
            watcher_started = True
            dbos_client.enqueue(recorded_options, *recorded_args)
            result = dbos_client.retrieve_workflow(first).get_result()
            assert result == {
                "status": "failed",
                "error": ENQUEUE_FAILED_ERROR,
                "error_code": "superseded",
            }
            assert _run_row(app, first) == ("failed", ENQUEUE_FAILED_ERROR)
            assert _events(app, first) == []
            assert _step_names(schema, first) == ["eap.start_run"]
            assert _active(app, conversation_id) == [later]

            second_options, second_args = fake_dbos.calls[1]
            dbos_client.enqueue(second_options, *second_args)
            second = dbos_client.retrieve_workflow(later).get_result()
            assert second["status"] == "completed", second
            assert _run_row(app, later) == ("completed", None)
            assert _run_row(app, first) == ("failed", ENQUEUE_FAILED_ERROR)
            assert _events(app, first) == []
            assert any(kind == "model_message" for _key, kind in _events(app, later))
            assert _active(app, conversation_id) == []
            # Every sample while the workflows were in flight: turn 1 stayed failed, so at most
            # turn 2 was active.
            assert seen, "the watcher never observed the conversation"
            for sample in seen:
                active = [run_id for run_id, _turn, status in sample if status not in TERMINAL_RUN_STATUSES]
                assert active in ([later], []), sample
                assert sample[0] == (first, 1, "failed")
        finally:
            stop.set()
            if watcher_started:
                watcher.join(timeout=2)
            if dbos_client is not None:
                dbos_client.destroy()
            if worker is not None:
                _stop_process(worker)
            _drop_dbos_schema(schema)


def _active(app, conversation_id: str) -> list[str]:
    return [run_id for run_id, _turn, status in _turns(app, conversation_id) if status not in TERMINAL_RUN_STATUSES]


def _assert_unstorable_param(resp, loc: list[str]) -> None:
    assert resp.status_code == 422, (loc, resp.status_code, resp.text[:300])
    detail = resp.json()["detail"]
    _assert_compact(detail)
    assert len(detail) == 1, detail
    assert detail[0]["loc"] == loc
    assert detail[0]["type"] == "string_unstorable"
    assert "NUL character" in detail[0]["msg"]
    assert "\x00" not in resp.text


def test_nul_in_query_and_path_on_every_guarded_router_is_422_after_auth(pg, tenant_of):
    client, _app = pg
    _, headers = tenant_of()
    bad_id = "00000000-0000-0000-0000-00000000000%00"
    run_id = str(uuid.uuid4())
    cases = [
        ("GET", "/api/v1/runs?status=%00", ["query", "status"], None),
        ("GET", "/api/v1/runs?tool_id=%00", ["query", "tool_id"], None),
        ("GET", "/api/v1/runs?st%00atus=queued", ["query", "st?atus"], None),
        ("GET", "/api/v1/agents?na%00me=x", ["query", "na?me"], None),
        ("GET", f"/api/v1/agents/{bad_id}", ["path", "agent_id"], None),
        ("PATCH", f"/api/v1/agents/{bad_id}", ["path", "agent_id"], b"{}"),
        ("GET", f"/api/v1/runs/{bad_id}", ["path", "run_id"], None),
        ("GET", f"/api/v1/runs/{bad_id}/events", ["path", "run_id"], None),
        ("GET", "/api/v1/conversations?agent_id=%00", ["query", "agent_id"], None),
        ("GET", f"/api/v1/conversations/{bad_id}", ["path", "conversation_id"], None),
        ("GET", "/api/v1/mcp-servers?q=%00", ["query", "q"], None),
        ("GET", f"/api/v1/mcp-servers/{bad_id}", ["path", "server_id"], None),
        ("DELETE", f"/api/v1/mcp-servers/{bad_id}", ["path", "server_id"], None),
        ("GET", "/api/v1/tools?q=%00", ["query", "q"], None),
        ("GET", "/api/v1/tools/clock%00now", ["path", "tool_id"], None),
        ("GET", "/api/v1/workflows?q=%00", ["query", "q"], None),
        ("GET", f"/api/v1/workflows/{bad_id}", ["path", "workflow_id"], None),
        ("POST", f"/api/v1/workflows/runs/{bad_id}/steps/approve/approve", ["path", "run_id"], b"{}"),
        ("POST", f"/api/v1/workflows/runs/{run_id}/steps/approve/approve?note=%00", ["query", "note"], b"{}"),
    ]
    for method, url, loc, body in cases:
        authed = client.request(method, url, headers=headers, content=body)
        _assert_unstorable_param(authed, loc)
        anon = client.request(method, url, content=body)
        assert anon.status_code == 401, (method, url, anon.status_code, anon.text[:200])


# --------------------------------------------------------------------------- CORS


def test_cors_preflight_allows_only_the_studio_dev_origins(pg):
    client, _app = pg
    allowed = ["http://localhost:3000", "http://127.0.0.1:3000"]
    denied = [
        "https://evil.example",
        "http://localhost:3001",
        "http://127.0.0.1:3001",
        "https://localhost:3000",
        "http://localhost:3000.evil.example",
        "null",
    ]
    for origin in allowed:
        preflight = client.options("/api/v1/agents", headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,authorization",
        })
        assert preflight.status_code == 200, (origin, preflight.status_code, preflight.headers)
        assert preflight.headers.get("access-control-allow-origin") == origin
        live = client.get("/healthz", headers={"Origin": origin})
        assert live.status_code == 200
        assert live.headers.get("access-control-allow-origin") == origin
    for origin in denied:
        preflight = client.options("/api/v1/agents", headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,authorization",
        })
        assert preflight.status_code != 200, origin
        assert preflight.headers.get("access-control-allow-origin") != origin
        assert preflight.headers.get("access-control-allow-origin") in (None, "")
        live = client.get("/healthz", headers={"Origin": origin})
        assert live.status_code == 200
        assert live.headers.get("access-control-allow-origin") in (None, "")
