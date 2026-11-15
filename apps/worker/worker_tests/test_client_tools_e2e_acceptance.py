"""AGUI.7 end to end (tester): the real POST /api/v1/ag-ui/agents/{id} (AGUI.6) in front of the
real in-process worker and the scripted `test:frontend_tool` model. A frontend tool round trip:
the call is streamed, the client returns a result in the next run, the run resumes and answers
`tool said: <result>`. Also the API-side refusals (D10): shadowing, unknown results, size cap."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "api"))

from api_tests.conftest import get_auth_header  # noqa: E402
from api_tests.run_helpers import cleanup_tenant as api_cleanup  # noqa: E402
from api_tests.run_helpers import hard_timeout, iter_frames  # noqa: E402
from eap_api.app import create_app  # noqa: E402
from eap_api.launcher import DBOSRunLauncher, LauncherSettings, get_run_launcher  # noqa: E402
from eap_api.routes.ag_ui import AgUiRouteSettings, get_agui_settings  # noqa: E402
from eap_api.sse import StreamSettings, get_delta_source, get_stream_settings, no_deltas  # noqa: E402
from eap_schemas.client_tools import MAX_CLIENT_TOOL_RESULT_BYTES  # noqa: E402
from worker_tests.conftest import APP_DB_URL, APP_VERSION, event_rows  # noqa: E402
from worker_tests.test_client_tools import NOT_EXECUTED_MESSAGE, WEATHER  # noqa: E402


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("EAP_AUTH_MODE", "dev")
    monkeypatch.setenv("EAP_ALLOWED_MODELS", "test:*")
    with hard_timeout(120):
        yield


@pytest.fixture
def api(worker, client, worker_settings, tenant):
    app = create_app(database_url=APP_DB_URL)
    settings = LauncherSettings(worker_settings.dbos_system_database_url, worker_settings.dbos_system_schema,
                                APP_VERSION)
    app.dependency_overrides[get_run_launcher] = lambda: DBOSRunLauncher(settings, client=client)
    app.dependency_overrides[get_stream_settings] = lambda: StreamSettings(poll_interval=0.05, heartbeat_interval=0.5)
    app.dependency_overrides[get_delta_source] = lambda: no_deltas
    app.dependency_overrides[get_agui_settings] = lambda: AgUiRouteSettings(previous_turn_wait_s=30.0, key_recheck_s=60.0)
    with TestClient(app) as test_client:
        yield test_client, get_auth_header(tenant, "alice")
    api_cleanup(app.state.database_engine, tenant)
    app.state.database_engine.dispose()


def make_agent(api, *, tools: tuple[str, ...] = (), **extra: Any) -> str:
    client, headers = api
    spec = {"name": "Frontend", "instructions": "Use the browser.", "model": {"provider": "test", "name": "frontend_tool"},
            "tools": list(tools), **extra}
    r = client.post("/api/v1/agents", json={"spec": spec}, headers=headers)
    assert r.status_code == 201, r.text
    agent_id = r.json()["id"]
    assert client.post(f"/api/v1/agents/{agent_id}/versions", headers=headers).status_code == 201
    return agent_id


def run_body(thread: str, run: str, *messages: dict[str, Any], tools: list[dict] | None = None) -> dict[str, Any]:
    return {"threadId": thread, "runId": run, "messages": list(messages), "tools": tools or [], "context": [],
            "state": None, "forwardedProps": {}}


def post(api, agent_id: str, payload: dict[str, Any]):
    client, headers = api
    with client.stream("POST", f"/api/v1/ag-ui/agents/{agent_id}", json=payload, headers=headers) as response:
        if response.status_code != 200:
            response.read()
            return response.status_code, response.json(), []
        return 200, None, [f.data for f in iter_frames(response, deadline_s=60) if f.data is not None]


def text_of(frames) -> str:
    return "".join(f.get("delta", "") for f in frames if f["type"] == "TEXT_MESSAGE_CONTENT")


USER = {"id": "u1", "role": "user", "content": "weather?"}


def tool_message(call_id: str, content: str, mid: str = "tm1") -> dict[str, Any]:
    return {"id": mid, "role": "tool", "toolCallId": call_id, "content": content}


def assistant_call(call_id: str) -> dict[str, Any]:
    return {"id": "a1", "role": "assistant", "toolCalls": [
        {"id": call_id, "type": "function", "function": {"name": "get_weather", "arguments": '{"city": "x"}'}}]}


def test_frontend_tool_round_trip_through_the_real_endpoint(api, worker, tenant):
    agent_id = make_agent(api)
    status, _, frames = post(api, agent_id, run_body("t1", "r1", USER, tools=[WEATHER]))
    assert status == 200
    kinds = [f["type"] for f in frames]
    assert kinds[0] == "RUN_STARTED" and kinds[-1] == "RUN_FINISHED" and "RUN_ERROR" not in kinds
    starts = [f for f in frames if f["type"] == "TOOL_CALL_START"]
    assert [s["toolCallName"] for s in starts] == ["get_weather"]
    call_id = starts[0]["toolCallId"]
    assert "TOOL_CALL_ARGS" in kinds and "TOOL_CALL_END" in kinds and "TOOL_CALL_RESULT" not in kinds
    assert text_of(frames) == ""  # no answer yet: the turn is pending
    # The client runs the tool and returns the result in the next run.
    status, _, frames = post(api, agent_id, run_body("t1", "r2", USER, assistant_call(call_id),
                                                     tool_message(call_id, "sunny"), tools=[WEATHER]))
    assert status == 200
    kinds = [f["type"] for f in frames]
    assert kinds[0] == "RUN_STARTED" and kinds[-1] == "RUN_FINISHED" and "RUN_ERROR" not in kinds
    assert "tool said: sunny" in text_of(frames)
    # The client's own result is not echoed back to it (translate.py drops source=client).
    assert "TOOL_CALL_RESULT" not in kinds and "TOOL_CALL_START" not in kinds
    # One run row per turn; the second carries no new user message.
    with worker.engine.connect() as conn:
        from sqlalchemy import text

        rows = conn.execute(text("SELECT turn, input, status FROM runs WHERE tenant_id = :t ORDER BY turn"),
                            {"t": tenant}).all()
    assert [(r[0], r[2]) for r in rows] == [(1, "completed"), (2, "completed")] and rows[1][1] == ""


def test_an_unanswered_call_is_closed_with_the_not_executed_error(api):
    agent_id = make_agent(api)
    status, _, frames = post(api, agent_id, run_body("t2", "r1", USER, tools=[WEATHER]))
    assert status == 200 and [f for f in frames if f["type"] == "TOOL_CALL_START"]
    status, _, frames = post(api, agent_id, run_body("t2", "r2", USER, {"id": "u2", "role": "user", "content": "never mind"}))
    assert status == 200 and "RUN_ERROR" not in [f["type"] for f in frames]
    assert f"tool said: error: {NOT_EXECUTED_MESSAGE}" in text_of(frames)


def test_reattaching_the_pending_run_replays_the_same_call_once(api):
    agent_id = make_agent(api)
    _, _, first = post(api, agent_id, run_body("t3", "r1", USER, tools=[WEATHER]))
    status, _, again = post(api, agent_id, run_body("t3", "r1", USER, tools=[WEATHER]))
    assert status == 200
    ids = lambda fs: [f["toolCallId"] for f in fs if f["type"] == "TOOL_CALL_START"]  # noqa: E731
    assert ids(first) == ids(again) and len(ids(again)) == 1


@pytest.mark.parametrize("name", ["retrieve", "update_working_memory", "final_result"])
def test_reserved_names_are_refused_even_when_the_spec_does_not_use_them(api, name):
    agent_id = make_agent(api)
    decl = {"name": name, "description": "x", "parameters": {"type": "object", "properties": {}}}
    status, detail, _ = post(api, agent_id, run_body(f"t-{name}", "r1", USER, tools=[decl]))
    assert status == 422 and detail["detail"][0]["type"] == "client_tool_shadows_server_tool", detail


def test_a_server_tool_name_is_refused_and_nothing_is_recorded(api, worker, tenant):
    agent_id = make_agent(api, tools=("tickets.create",))
    decl = {"name": "tickets__create", "description": "x", "parameters": {"type": "object", "properties": {}}}
    status, detail, _ = post(api, agent_id, run_body("t4", "r1", USER, tools=[decl]))
    assert status == 422 and detail["detail"][0]["type"] == "client_tool_shadows_server_tool", detail
    with worker.engine.connect() as conn:
        from sqlalchemy import text

        assert conn.execute(text("SELECT count(*) FROM runs WHERE tenant_id = :t"), {"t": tenant}).scalar_one() == 0


def test_results_must_answer_pending_calls_and_respect_the_size_cap(api):
    agent_id = make_agent(api)
    status, _, frames = post(api, agent_id, run_body("t5", "r1", USER, tools=[WEATHER]))
    call_id = next(f["toolCallId"] for f in frames if f["type"] == "TOOL_CALL_START")
    status, detail, _ = post(api, agent_id, run_body("t5", "r2", USER, assistant_call(call_id),
                                                     tool_message("not-a-pending-id", "x")))
    assert status == 422 and detail["detail"][0]["type"] == "client_tool_result_not_pending", detail
    big = "x" * (MAX_CLIENT_TOOL_RESULT_BYTES + 1)
    status, detail, _ = post(api, agent_id, run_body("t5", "r3", USER, assistant_call(call_id),
                                                     tool_message(call_id, big)))
    assert status in (413, 422), (status, str(detail)[:200])
    # The pending call is still answerable afterwards.
    status, _, frames = post(api, agent_id, run_body("t5", "r4", USER, assistant_call(call_id),
                                                     tool_message(call_id, "ok")))
    assert status == 200 and "tool said: ok" in text_of(frames)
