"""`http.request` in runs: the gating rule of an agent run, and a workflow tool step (real worker, mock transport).

- An agent run's call whose arguments name a secret waits for an approval (the spec lists the tool and no
  `approvals`); the card's summary shows method, host, path and the secret NAMES; the value is in no record.
  A call without a reference does not wait.
- A workflow tool step with a reference runs without any approval.
All secret values are obviously fake; nothing leaves the process (a mock transport answers)."""

from __future__ import annotations

import json
from typing import Any

import httpx2
import pytest

from eap_secrets import load_keyring
from eap_worker import http_tool
from worker_tests.conftest import APP_VERSION, enqueue_run, event_rows, ticket_spec, wait_terminal
from worker_tests.test_secrets_request import everything_recorded
from worker_tests.test_tool_approval import decide, events, start, wait_waiting
from worker_tests.workflow_run_helpers import cleanup_definitions, enqueue_workflow_run, publish_workflow, wait_event

FAKE = "FAKE-bluesky-credential-31c9"
URL = "https://pds.example.com/xrpc/com.example.login"
SENT: list[httpx2.Request] = []


@pytest.fixture(autouse=True)
def _env(mcp_env):
    return mcp_env


@pytest.fixture(autouse=True)
def mock_wire(monkeypatch, worker):
    SENT.clear()

    def handler(request: httpx2.Request) -> httpx2.Response:
        SENT.append(request)
        body = json.loads(request.content) if request.content else {}
        return httpx2.Response(200, json={"echo": body, "auth": request.headers.get("authorization")})

    monkeypatch.setattr(
        http_tool, "new_client",
        lambda policy, timeout_s: httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )


@pytest.fixture
def store(worker, tenant):
    from eap_run_store.secrets import SecretStore

    s = SecretStore(worker.engine, load_keyring())
    s.put(tenant, "pwd-test", FAKE, "alice")
    yield s
    s.delete(tenant, "pwd-test") if hasattr(s, "delete") else None


def spec() -> dict[str, Any]:
    return ticket_spec(tools=("http.request",), model="approval")  # no `approvals` at all


def prompt(**args: Any) -> str:
    return json.dumps({"tool": "http__request", "args": {"method": "POST", "url": URL, **args}})


def test_a_call_naming_a_secret_waits_for_approval_and_shows_names_only(worker, client, tenant, store):
    run_id = start(worker, client, tenant, spec(), prompt(json={"password": "secret:pwd-test"},
                                                          headers={"Authorization": "Bearer secret:pwd-test"}))
    (waiting,) = wait_waiting(worker, run_id)
    assert waiting["tool_name"] == "http__request"
    assert waiting["summary"] == "POST pds.example.com/xrpc/com.example.login using secret pwd-test"
    assert FAKE not in json.dumps(waiting)
    assert SENT == [] and not [e for e in event_rows(worker.engine, run_id) if e[2] == "tool_result"]
    decide(worker, client, tenant, run_id, waiting["tool_call_id"])
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "completed", run["error"]
    (request,) = SENT
    assert json.loads(request.content) == {"password": FAKE} and request.headers["authorization"] == f"Bearer {FAKE}"
    result = events(worker, run_id)[f"tool:{waiting['tool_call_id']}:result"][1]
    assert result["status"] == "ok" and result["content"]["status"] == 200
    assert FAKE not in everything_recorded(worker, tenant, run_id)


def test_a_denied_call_sends_nothing(worker, client, tenant, store):
    run_id = start(worker, client, tenant, spec(), prompt(json={"password": "secret:pwd-test"}))
    (waiting,) = wait_waiting(worker, run_id)
    decide(worker, client, tenant, run_id, waiting["tool_call_id"], approve=False)
    assert wait_terminal(worker.run_store, tenant, run_id)["status"] == "completed"
    assert SENT == []


def test_a_call_without_a_reference_runs_without_approval(worker, client, tenant):
    run_id = start(worker, client, tenant, spec(), prompt(json={"text": "my secret:pwd-test is not a ref"}))
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "completed", run["error"]
    assert not [e for e in event_rows(worker.engine, run_id) if e[2] == "tool_approval_waiting"]
    assert len(SENT) == 1


def test_an_unknown_reference_waits_and_then_sends_nothing(worker, client, tenant):
    run_id = start(worker, client, tenant, spec(), prompt(json={"password": "secret:nope"}))
    (waiting,) = wait_waiting(worker, run_id)
    decide(worker, client, tenant, run_id, waiting["tool_call_id"])
    wait_terminal(worker.run_store, tenant, run_id)
    content = events(worker, run_id)[f"tool:{waiting['tool_call_id']}:result"][1]["content"]
    assert content["error"]["code"] == "secret_not_found" and SENT == []


def test_a_workflow_tool_step_runs_without_approval(worker, client, tenant, store):
    workflow = {
        "name": "Login", "input_schema": {"type": "object"}, "start": "login",
        "steps": [{
            "id": "login", "type": "tool", "tool_id": "http.request",
            "input": {"method": "POST", "url": URL, "headers": {"Authorization": "Bearer secret:pwd-test"},
                      "json": {"password": "secret:pwd-test"}},
        }],
    }
    workflow_id, version_id = publish_workflow(worker.engine, tenant, workflow)
    try:
        run_id = enqueue_workflow_run(client, worker.run_store, tenant_id=tenant, workflow_id=workflow_id,
                                      version_id=version_id, input_value={}, app_version=APP_VERSION)
        run = wait_terminal(worker.run_store, tenant, run_id)
        assert run["status"] == "completed", run["error"]
        kinds = {e[2] for e in event_rows(worker.engine, run_id)}
        assert not kinds & {"tool_approval_waiting", "step_waiting"}
        (request,) = SENT
        assert json.loads(request.content) == {"password": FAKE}
        completed = wait_event(worker.engine, run_id, "step:login:completed")["data"]["output"]
        assert completed["status"] == 200 and completed["json"]["auth"] == "Bearer [REDACTED]"
        assert completed["json"]["echo"] == {"password": "[REDACTED]"}
        recorded = everything_recorded(worker, tenant, run_id)
        assert FAKE not in recorded and "secret:pwd-test" in recorded  # the spec's reference is recorded, never the value
    finally:
        cleanup_definitions(worker.engine, tenant)
