"""API input hardening (milestone 3, cross-cutting): the shared JSON write-route guards of
eap_api.request_guard on every write route, agent-run and turn enqueue-failure semantics, and CORS.

Guards: body cap -> 413; not strict UTF-8 or a NUL byte -> 415; nesting too deep -> 422 (string);
U+0000 or a lone surrogate in any string or key -> 422 naming the field path (compact list), never
a 500; every validation 422 is a compact list of {loc, msg, type} without the rejected input.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from eap_api.request_guard import DEFAULT_MAX_BODY_BYTES, NOT_UTF8_DETAIL, unstorable_strings
from eap_api.routes.mcp_servers import get_outbound_policy
from eap_run_store import ENQUEUE_FAILED_ERROR, RunStore
from eap_secrets import KEY_ENV, generate_key
from eap_secrets.outbound import OutboundPolicy

from .run_helpers import requires_pg
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

BAD = [("\x00", "a NUL character (\\u0000)"), ("\ud800", "an unpaired surrogate (\\ud800)"),
       ("\udfff", "an unpaired surrogate (\\udfff)")]


def send(client: TestClient, method: str, url: str, payload: Any, headers: dict[str, str]):
    """JSON with \\u escapes, exactly as a browser's JSON.stringify sends NUL and lone surrogates."""
    body = json.dumps(payload).encode()
    return client.request(method, url, content=body, headers={**headers, "Content-Type": "application/json"})


def assert_compact_422(resp, loc: list[Any], reason: str | None = None) -> None:
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert isinstance(detail, list) and detail, detail
    for item in detail:
        assert set(item) == {"loc", "msg", "type"}, item
    assert loc in [item["loc"] for item in detail], detail
    if reason is not None:
        assert any(reason in item["msg"] for item in detail), detail


# --------------------------------------------------------------------------- the pure walk


def test_unstorable_strings_names_values_and_keys_in_document_order():
    data = {"a": ["ok", "x\x00"], "b\ud800": {"c": "y\udfff"}, "d": 1}
    assert unstorable_strings(data) == [
        {"loc": ["body", "a", 1], "msg": "string contains a NUL character (\\u0000), which cannot be stored",
         "type": "string_unstorable"},
        {"loc": ["body", "b?"], "msg": "key contains an unpaired surrogate (\\ud800), which cannot be stored",
         "type": "string_unstorable"},
        {"loc": ["body", "b?", "c"], "msg": "string contains an unpaired surrogate (\\udfff), which cannot be stored",
         "type": "string_unstorable"},
    ]
    assert unstorable_strings({"emoji": "\U0001f600", "n": None}) == []


def test_unstorable_strings_is_bounded():
    assert len(unstorable_strings(["\x00"] * 100)) == 20


# --------------------------------------------------------------------------- agents


@requires_pg
class TestAgents:
    @pytest.mark.parametrize("field", ["name", "instructions"])
    @pytest.mark.parametrize("bad,reason", BAD)
    def test_unstorable_text_is_422_on_create_and_patch(self, pg, tenant_of, field, bad, reason):
        client, _ = pg
        _, h = tenant_of()
        agent_id = make_agent(client, h, publish=False)
        spec = {**SPEC, field: f"x{bad}y"}
        assert_compact_422(send(client, "POST", "/api/v1/agents", {"spec": spec}, h), ["body", "spec", field], reason)
        assert_compact_422(
            send(client, "PATCH", f"/api/v1/agents/{agent_id}", {"spec": spec}, h), ["body", "spec", field], reason
        )
        assert client.get(f"/api/v1/agents/{agent_id}", headers=h).json()["name"] == SPEC["name"]

    def test_unstorable_key_is_422(self, pg, tenant_of):
        client, _ = pg
        _, h = tenant_of()
        resp = send(client, "POST", "/api/v1/agents", {"spec": {**SPEC, "na\x00me": "x"}}, h)
        assert_compact_422(resp, ["body", "spec", "na?me"], "key contains a NUL character")

    def test_oversized_body_is_413_without_echo(self, pg, tenant_of):
        client, _ = pg
        _, h = tenant_of()
        spec = {**SPEC, "instructions": "a" * (3 * 1024 * 1024)}
        resp = send(client, "POST", "/api/v1/agents", {"spec": spec}, h)
        assert resp.status_code == 413
        assert resp.json() == {"detail": f"request body too large (max {DEFAULT_MAX_BODY_BYTES} bytes)"}
        agent_id = make_agent(client, h, publish=False)
        assert send(client, "PATCH", f"/api/v1/agents/{agent_id}", {"spec": spec}, h).status_code == 413

    def test_body_just_under_the_cap_is_accepted(self, pg, tenant_of):
        client, _ = pg
        _, h = tenant_of()
        body = json.dumps({"spec": SPEC}).encode()
        hj = {**h, "Content-Type": "application/json"}
        exact = body + b" " * (DEFAULT_MAX_BODY_BYTES - len(body))  # JSON whitespace counts too
        assert client.post("/api/v1/agents", content=exact, headers=hj).status_code == 201
        assert client.post("/api/v1/agents", content=exact + b" ", headers=hj).status_code == 413

    def test_validation_422_is_compact_and_never_echoes_input(self, pg, tenant_of):
        client, _ = pg
        _, h = tenant_of()
        secretish = "s3cr3t-" + "z" * 50_000
        resp = send(client, "POST", "/api/v1/agents", {"spec": {**SPEC, "model": secretish}}, h)
        assert_compact_422(resp, ["body", "spec", "model"])
        assert "s3cr3t" not in resp.text and len(resp.content) < 2000

    def test_not_utf8_is_415_and_deep_nesting_is_422(self, pg, tenant_of):
        client, _ = pg
        _, h = tenant_of()
        hj = {**h, "Content-Type": "application/json"}
        utf16 = json.dumps({"spec": SPEC}).encode("utf-16")
        resp = client.post("/api/v1/agents", content=utf16, headers=hj)
        assert (resp.status_code, resp.json()) == (415, {"detail": NOT_UTF8_DETAIL})
        deep = b'{"spec":' + b"[" * 40 + b"]" * 40 + b"}"
        resp = client.post("/api/v1/agents", content=deep, headers=hj)
        assert (resp.status_code, resp.json()) == (422, {"detail": "request body is nested more than 32 levels deep"})

    def test_auth_answers_before_the_unstorable_check(self, pg):
        client, _ = pg
        resp = send(client, "POST", "/api/v1/agents", {"spec": {**SPEC, "name": "x\x00"}}, {})
        assert resp.status_code == 401

    def test_route_raised_422_strings_stay_strings(self, pg, tenant_of, monkeypatch):
        client, _ = pg
        _, h = tenant_of()
        monkeypatch.setenv("EAP_ALLOWED_MODELS", "openai:gpt-4")
        agent_id = make_agent(client, h, publish=False)
        resp = client.post(f"/api/v1/agents/{agent_id}/versions", headers=h)
        assert resp.status_code == 422 and isinstance(resp.json()["detail"], str)


# --------------------------------------------------------------------------- runs and turns


@requires_pg
class TestRunsAndConversations:
    @pytest.mark.parametrize("bad,reason", BAD)
    def test_run_input(self, pg, tenant_of, fake_dbos, bad, reason):
        client, app = pg
        _, h = tenant_of()
        agent_id = make_agent(client, h)
        run_id = str(uuid.uuid4())
        payload = {"run_id": run_id, "input": f"hi{bad}", "version": None}
        assert_compact_422(send(client, "POST", f"/api/v1/agents/{agent_id}/runs", payload, h), ["body", "input"], reason)
        assert fake_dbos.calls == []
        assert client.get(f"/api/v1/runs/{run_id}", headers=h).status_code == 404

    @pytest.mark.parametrize("bad,reason", BAD)
    def test_conversation_title_and_turn_input(self, pg, tenant_of, fake_dbos, bad, reason):
        client, _ = pg
        _, h = tenant_of()
        agent_id = make_agent(client, h)
        resp = send(client, "POST", f"/api/v1/agents/{agent_id}/conversations", {"title": f"t{bad}"}, h)
        assert_compact_422(resp, ["body", "title"], reason)
        conv_id = new_conversation(client, h, agent_id)
        payload = {"run_id": str(uuid.uuid4()), "input": f"hi{bad}"}
        assert_compact_422(send(client, "POST", f"/api/v1/conversations/{conv_id}/runs", payload, h),
                           ["body", "input"], reason)
        assert fake_dbos.calls == []

    def test_run_body_cap(self, pg, tenant_of):
        client, _ = pg
        _, h = tenant_of()
        agent_id = make_agent(client, h)
        resp = start(client, h, agent_id, input="a" * (DEFAULT_MAX_BODY_BYTES + 1))
        assert resp.status_code == 413

    def test_query_validation_422_is_compact(self, pg, tenant_of):
        client, _ = pg
        _, h = tenant_of()
        resp = client.get("/api/v1/runs?limit=0", headers=h)
        assert_compact_422(resp, ["query", "limit"])


# --------------------------------------------------------------------------- enqueue failure


def _run_row(app, run_id: str) -> tuple[str, str | None]:
    with app.state.database_engine.connect() as conn:
        row = conn.execute(text("SELECT status, error FROM runs WHERE id = :r"), {"r": run_id}).one()
    return row[0], row[1]


@requires_pg
class TestEnqueueFailure:
    @pytest.mark.parametrize("fake_dbos", [FakeDBOSClient(fail_times=1)])
    def test_agent_run_is_failed_then_requeued_by_the_retry(self, pg, tenant_of, fake_dbos):
        client, app = pg
        _, h = tenant_of()
        agent_id = make_agent(client, h)
        run_id = str(uuid.uuid4())
        resp = start(client, h, agent_id, run_id)
        assert resp.status_code == 503
        assert resp.json() == {"detail": "Run recorded but could not be started; retry the same request (same run_id)"}
        assert _run_row(app, run_id) == ("failed", ENQUEUE_FAILED_ERROR)
        got = client.get(f"/api/v1/runs/{run_id}", headers=h).json()
        assert (got["status"], got["display_status"], got["error"]) == ("failed", "failed", ENQUEUE_FAILED_ERROR)

        retry = start(client, h, agent_id, run_id)
        assert retry.status_code == 200, retry.text
        assert (retry.json()["status"], retry.json()["display_status"]) == ("queued", "queued")
        assert _run_row(app, run_id) == ("queued", None)
        assert [c[0]["workflow_id"] for c in fake_dbos.calls] == [run_id]

    @pytest.mark.parametrize("fake_dbos", [FakeDBOSClient(fail_times=1)])
    def test_retry_with_different_input_is_409_and_keeps_the_failure(self, pg, tenant_of, fake_dbos):
        client, app = pg
        _, h = tenant_of()
        agent_id = make_agent(client, h)
        run_id = str(uuid.uuid4())
        assert start(client, h, agent_id, run_id).status_code == 503
        assert start(client, h, agent_id, run_id, input="other").status_code == 409
        assert _run_row(app, run_id) == ("failed", ENQUEUE_FAILED_ERROR)

    @pytest.mark.parametrize("fake_dbos", [FakeDBOSClient(fail_times=1)])
    def test_failed_turn_frees_the_conversation_and_cannot_overtake_a_later_turn(self, pg, tenant_of, fake_dbos):
        client, app = pg
        _, h = tenant_of()
        conv_id = new_conversation(client, h, make_agent(client, h))
        first = str(uuid.uuid4())
        assert turn(client, h, conv_id, first).status_code == 503
        assert _run_row(app, first) == ("failed", ENQUEUE_FAILED_ERROR)
        # The failed turn is terminal: the conversation is not stuck behind it.
        second = turn(client, h, conv_id, input="next")
        assert second.status_code == 201 and second.json()["turn"] == 2
        # Retrying the first turn now would run two turns at once: refused, and it stays failed.
        retry = turn(client, h, conv_id, first)
        assert retry.status_code == 409
        assert retry.json() == {
            "detail": "This turn could not be started and a later turn has started since; start a new turn"
        }
        assert _run_row(app, first) == ("failed", ENQUEUE_FAILED_ERROR)
        assert [c[0]["workflow_id"] for c in fake_dbos.calls] == [second.json()["run_id"]]

    @pytest.mark.parametrize("fake_dbos", [FakeDBOSClient(fail_times=1)])
    def test_failed_latest_turn_is_requeued_by_its_retry(self, pg, tenant_of, fake_dbos):
        client, app = pg
        _, h = tenant_of()
        conv_id = new_conversation(client, h, make_agent(client, h))
        run_id = str(uuid.uuid4())
        assert turn(client, h, conv_id, run_id).status_code == 503
        retry = turn(client, h, conv_id, run_id)
        assert retry.status_code == 200 and retry.json()["status"] == "queued" and retry.json()["turn"] == 1
        assert _run_row(app, run_id) == ("queued", None)

    def test_orphan_left_queued_by_a_crash_is_not_owned_by_the_retry(self, pg, tenant_of):
        """A crash between insert and enqueue leaves the run queued (nothing could mark it). The
        retry's enqueue failing does not mark it failed: it may be another request's live hand-off."""
        client, app = pg
        tenant, h = tenant_of()
        agent_id = make_agent(client, h)
        run_id = uuid.uuid4()
        RunStore(app.state.database_engine).create_run(tenant, uuid.UUID(agent_id), 1, "printer is on fire", run_id)
        failing = FakeDBOSClient(fail_times=1)
        from eap_api.launcher import DBOSRunLauncher, LauncherSettings, get_run_launcher

        app.dependency_overrides[get_run_launcher] = lambda: DBOSRunLauncher(
            LauncherSettings("postgresql://unused/x", "dbos", "m1-test"), client=failing
        )
        assert start(client, h, agent_id, str(run_id)).status_code == 503
        assert _run_row(app, str(run_id)) == ("queued", None)
        assert start(client, h, agent_id, str(run_id)).status_code == 200


# --------------------------------------------------------------------------- MCP servers


@pytest.fixture
def mcp_client(pg, monkeypatch):
    monkeypatch.setenv(KEY_ENV, generate_key())

    async def resolve(host: str, port: int) -> list[str]:
        return ["93.184.215.14"]

    pg[1].dependency_overrides[get_outbound_policy] = lambda: OutboundPolicy(resolver=resolve)
    return pg[0]


@requires_pg
class TestMcpServers:
    BASE = "/api/v1/mcp-servers"

    def _create(self, client, h) -> str:
        resp = client.post(self.BASE, json={"name": f"srv-{uuid.uuid4().hex[:6]}", "url": "https://mcp.example.com/mcp"},
                           headers=h)
        assert resp.status_code == 201, resp.text
        return resp.json()["id"]

    @pytest.mark.parametrize("bad,reason", BAD)
    def test_url_on_create_and_patch(self, mcp_client, tenant_of, bad, reason):
        _, h = tenant_of()
        url = f"https://mcp.example.com/{bad}"
        resp = send(mcp_client, "POST", self.BASE, {"name": "srv-a", "url": url}, h)
        assert_compact_422(resp, ["body", "url"], reason)
        server_id = self._create(mcp_client, h)
        assert_compact_422(send(mcp_client, "PATCH", f"{self.BASE}/{server_id}", {"url": url}, h), ["body", "url"], reason)

    @pytest.mark.parametrize("bad,reason", BAD)
    def test_header_values_and_names(self, mcp_client, tenant_of, bad, reason):
        _, h = tenant_of()
        secret = f"Bearer sntl-{uuid.uuid4().hex}"
        body = {"name": "srv-b", "url": "https://mcp.example.com/mcp", "headers": {"X-Api-Key": f"{secret}{bad}"}}
        resp = send(mcp_client, "POST", self.BASE, body, h)
        assert_compact_422(resp, ["body", "headers", "X-Api-Key"], reason)
        assert secret not in resp.text
        server_id = self._create(mcp_client, h)
        resp = send(mcp_client, "PATCH", f"{self.BASE}/{server_id}", {"headers": {"X-Api-Key": f"{secret}{bad}"}}, h)
        assert_compact_422(resp, ["body", "headers", "X-Api-Key"], reason)
        assert secret not in resp.text
        resp = send(mcp_client, "PATCH", f"{self.BASE}/{server_id}", {"headers": {f"X-K{bad}": "v"}}, h)
        assert_compact_422(resp, ["body", "headers", "X-K?"], reason)

    def test_name_and_body_cap(self, mcp_client, tenant_of):
        _, h = tenant_of()
        resp = send(mcp_client, "POST", self.BASE, {"name": "srv\x00", "url": "https://mcp.example.com/mcp"}, h)
        assert_compact_422(resp, ["body", "name"])
        big = {"name": "srv-c", "url": "https://mcp.example.com/mcp", "headers": {"X-A": "a" * DEFAULT_MAX_BODY_BYTES}}
        assert send(mcp_client, "POST", self.BASE, big, h).status_code == 413

    def test_route_raised_422_strings_stay_strings(self, mcp_client, tenant_of):
        _, h = tenant_of()
        resp = mcp_client.post(self.BASE, json=[1], headers=h)
        assert resp.status_code == 422 and resp.json() == {"detail": "body must be a JSON object"}


# --------------------------------------------------------------------------- tool runs


@requires_pg
class TestToolRuns:
    @pytest.mark.parametrize("bad,reason", BAD)
    def test_tool_input(self, pg, tenant_of, fake_dbos, bad, reason):
        client, _ = pg
        _, h = tenant_of()
        resp = send(client, "POST", "/api/v1/tools/tickets.create/runs", {"input": {"title": f"x{bad}", "body": "b"}}, h)
        assert_compact_422(resp, ["body", "input", "title"], reason)
        resp = send(client, "POST", "/api/v1/tools/tickets.create/runs", {"input": {"title": "x", f"k{bad}": "b"}}, h)
        assert_compact_422(resp, ["body", "input", "k?"], reason)
        assert fake_dbos.calls == []


# --------------------------------------------------------------------------- query and path parameters


@requires_pg
class TestQueryAndPathParameters:
    @pytest.mark.parametrize("query,loc", [
        ("status=%00", ["query", "status"]),
        ("tool_id=%00", ["query", "tool_id"]),
        ("status=run%00ning&tool_id=x", ["query", "status"]),
        ("st%00atus=x", ["query", "st?atus"]),
    ])
    def test_nul_in_query_is_422_not_500(self, pg, tenant_of, query, loc):
        client, _ = pg
        _, h = tenant_of()
        resp = client.get(f"/api/v1/runs?{query}", headers=h)
        assert_compact_422(resp, loc, "NUL character")

    def test_nul_in_path_is_422(self, pg, tenant_of):
        client, _ = pg
        _, h = tenant_of()
        assert_compact_422(client.get("/api/v1/tools/clock%00now", headers=h), ["path", "tool_id"], "NUL character")

    def test_auth_answers_first_and_clean_queries_still_work(self, pg, tenant_of):
        client, _ = pg
        _, h = tenant_of()
        assert client.get("/api/v1/runs?status=%00").status_code == 401
        assert client.get("/api/v1/runs?status=queued&kind=tool", headers=h).status_code == 200


# --------------------------------------------------------------------------- CORS


@requires_pg
@pytest.mark.parametrize("origin,allowed", [
    ("http://localhost:3000", True), ("http://127.0.0.1:3000", True),
    ("http://127.0.0.1:3001", False), ("https://evil.example", False),
])
def test_cors_origins(pg, origin, allowed):
    client, _ = pg
    resp = client.options("/api/v1/agents", headers={
        "Origin": origin, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type",
    })
    assert (resp.headers.get("access-control-allow-origin") == origin) is allowed
    assert (resp.status_code == 200) is allowed
