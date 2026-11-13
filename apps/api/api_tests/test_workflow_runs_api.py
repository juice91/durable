"""POST /workflows/{id}/runs and the approve/reject routes: focused implementer tests of the route
contract and the hand-off (a fake DBOSClient inside the real DBOSRunLauncher; no worker)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from eap_api.app import create_app
from eap_api.launcher import DBOSRunLauncher, LauncherSettings, get_run_launcher

from .conftest import get_auth_header
from .run_helpers import PG_URL, cleanup_tenant, hard_timeout, requires_pg
from .test_runs import FakeDBOSClient

pytestmark = requires_pg


class FakeClient(FakeDBOSClient):
    """Records enqueues (FakeDBOSClient) and DBOS sends."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[tuple[str, Any, str]] = []
        self.fail_send = False

    def send(self, destination_id: str, message: Any, topic: str | None = None, idempotency_key: str | None = None) -> None:
        if self.fail_send:
            raise ConnectionError("system database unreachable")
        self.sent.append((destination_id, message, topic))


@pytest.fixture(autouse=True)
def _no_hang():
    with hard_timeout(60):
        yield


@pytest.fixture
def fake() -> FakeClient:
    return FakeClient()


@pytest.fixture
def api(fake):
    app = create_app(database_url=PG_URL)
    app.dependency_overrides[get_run_launcher] = lambda: DBOSRunLauncher(
        LauncherSettings("postgresql://unused/x", "dbos", "m1-test"), client=fake
    )
    tenants: list[str] = []

    def tenant_of() -> tuple[str, dict[str, str]]:
        t = f"t{uuid.uuid4().hex[:10]}"
        tenants.append(t)
        return t, get_auth_header(t, "alice")

    with TestClient(app) as client:
        yield client, app, tenant_of
    engine = app.state.database_engine
    for t in tenants:
        cleanup_tenant(engine, t)
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM workflow_versions WHERE tenant_id = :t"), {"t": t})
            conn.execute(text("DELETE FROM workflows WHERE tenant_id = :t"), {"t": t})
    engine.dispose()


AGENT_SPEC = {"name": "Helper", "instructions": "Help.", "model": {"provider": "test", "name": "echo"}}


def publish_demo(client: TestClient, headers: dict[str, str]) -> str:
    """Create + publish an agent and a workflow (agent, tool, branch, approval) through the API."""
    r = client.post("/api/v1/agents", json={"spec": AGENT_SPEC}, headers=headers)
    agent_id = r.json()["id"]
    assert client.post(f"/api/v1/agents/{agent_id}/versions", headers=headers).status_code == 201
    spec = {
        "name": "Triage",
        "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"],
                         "additionalProperties": False},
        "start": "ask",
        "steps": [
            {"id": "ask", "type": "agent", "agent_id": agent_id, "input": {"p": {"$ref": "input.q"}}, "next": "now"},
            {"id": "now", "type": "tool", "tool_id": "clock.now", "input": {}, "next": "route"},
            {"id": "route", "type": "branch", "default": "approve",
             "cases": [{"when": {"op": "eq", "left": {"$ref": "input.q"}, "right": "skip"}, "next": "done"}]},
            {"id": "approve", "type": "approval", "prompt": "OK?", "timeout_hours": 1, "next": "done"},
            {"id": "done", "type": "tool", "tool_id": "clock.now", "input": {}},
        ],
    }
    r = client.post("/api/v1/workflows", json={"spec": spec}, headers=headers)
    assert r.status_code == 201, r.text
    workflow_id = r.json()["id"]
    assert client.post(f"/api/v1/workflows/{workflow_id}/versions", headers=headers).status_code == 201
    return workflow_id


def test_workflow_run_is_recorded_and_enqueued(api, fake):
    client, app, tenant_of = api
    tenant, headers = tenant_of()
    workflow_id = publish_demo(client, headers)
    run_id = str(uuid.uuid4())
    r = client.post(f"/api/v1/workflows/{workflow_id}/runs", json={"input": {"q": "hi"}, "run_id": run_id}, headers=headers)
    assert r.status_code == 202, r.text
    assert r.json() | {"trace_id": None} == {
        "run_id": run_id, "kind": "workflow", "workflow_id": workflow_id, "version": 1,
        "status": "queued", "display_status": "queued", "trace_id": None,
    }
    (options, args), = fake.calls
    assert options["workflow_name"] == "eap.workflow_run" and options["workflow_id"] == run_id
    assert options["queue_name"] == "eap-agent-runs"
    with app.state.database_engine.connect() as conn:
        version_id = conn.execute(
            text("SELECT id FROM workflow_versions WHERE workflow_id = :w"), {"w": workflow_id}
        ).scalar_one()
    assert args == (run_id, tenant, version_id, '{"q":"hi"}')

    run = client.get(f"/api/v1/runs/{run_id}", headers=headers).json()
    assert run["kind"] == "workflow" and run["workflow_id"] == workflow_id and run["workflow_version"] == 1
    assert run["agent_id"] is None and run["parent_run_id"] is None
    listed = client.get(f"/api/v1/runs?kind=workflow&workflow_id={workflow_id}", headers=headers).json()["items"]
    assert [i["run_id"] for i in listed] == [run_id]

    # Idempotent retry; a different input with the same run_id is a 409.
    body = {"input": {"q": "hi"}, "run_id": run_id, "version": 1}
    assert client.post(f"/api/v1/workflows/{workflow_id}/runs", json=body, headers=headers).status_code == 202
    body["input"] = {"q": "other"}
    assert client.post(f"/api/v1/workflows/{workflow_id}/runs", json=body, headers=headers).status_code == 409
    # Another tenant sees nothing.
    _, other = tenant_of()
    assert client.post(f"/api/v1/workflows/{workflow_id}/runs", json={"input": {"q": "x"}}, headers=other).status_code == 404
    assert client.get(f"/api/v1/runs/{run_id}", headers=other).status_code == 404


def test_refusals_and_compact_422s(api, fake):
    client, _app, tenant_of = api
    _, headers = tenant_of()
    workflow_id = publish_demo(client, headers)
    url = f"/api/v1/workflows/{workflow_id}/runs"
    assert client.post(f"/api/v1/workflows/{uuid.uuid4()}/runs", json={"input": {}}, headers=headers).status_code == 404
    assert client.post(url, json={"input": {"q": "x"}, "version": 9}, headers=headers).status_code == 404
    secret = "sk-very-secret-value"
    for body in ({"input": {"q": 5, "x": secret}}, {"input": {}}, {"input": secret}, {"input": {"q": "a"}, "extra": secret}):
        r = client.post(url, json=body, headers=headers)
        assert r.status_code == 422, r.text
        assert secret not in r.text and isinstance(r.json()["detail"], list)
    r = client.post(url, json={"input": {"q": 5}}, headers=headers)
    assert r.json()["detail"][0] == {"loc": ["body", "input", "q"], "type": "json_schema.type",
                                     "msg": "does not satisfy the input schema's 'type' keyword"}
    assert client.post(url, json={"input": {"q": "x" * 40000}}, headers=headers).status_code == 422
    assert not fake.calls

    # No published version -> 409.
    spec = {"name": "Draft", "input_schema": {"type": "object"}, "start": "t",
            "steps": [{"id": "t", "type": "tool", "tool_id": "clock.now"}]}
    draft_id = client.post("/api/v1/workflows", json={"spec": spec}, headers=headers).json()["id"]
    assert client.post(f"/api/v1/workflows/{draft_id}/runs", json={"input": {}}, headers=headers).status_code == 409

    # A remote $ref in the input schema is never fetched: 422, not a network call.
    spec["input_schema"] = {"type": "object", "properties": {"a": {"$ref": "https://example.invalid/s.json"}}}
    remote_id = client.post("/api/v1/workflows", json={"spec": spec}, headers=headers).json()["id"]
    assert client.post(f"/api/v1/workflows/{remote_id}/versions", headers=headers).status_code == 201
    r = client.post(f"/api/v1/workflows/{remote_id}/runs", json={"input": {"a": 1}}, headers=headers)
    assert r.status_code == 422 and r.json()["detail"][0]["type"] == "json_schema.invalid_schema"


def test_failed_enqueue_is_503_and_retry_requeues(api, fake):
    client, _app, tenant_of = api
    _, headers = tenant_of()
    workflow_id = publish_demo(client, headers)
    fake.fail_times = 1
    body = {"input": {"q": "hi"}, "run_id": str(uuid.uuid4())}
    r = client.post(f"/api/v1/workflows/{workflow_id}/runs", json=body, headers=headers)
    assert r.status_code == 503
    run = client.get(f"/api/v1/runs/{body['run_id']}", headers=headers).json()
    assert run["status"] == "failed" and "enqueue_failed" in run["error"]
    r = client.post(f"/api/v1/workflows/{workflow_id}/runs", json=body, headers=headers)
    assert r.status_code == 202 and r.json()["status"] == "queued"


def test_approve_and_reject_only_while_waiting(api, fake):
    client, app, tenant_of = api
    tenant, headers = tenant_of()
    workflow_id = publish_demo(client, headers)
    run_id = str(uuid.uuid4())
    client.post(f"/api/v1/workflows/{workflow_id}/runs", json={"input": {"q": "hi"}, "run_id": run_id}, headers=headers)
    url = f"/api/v1/workflows/runs/{run_id}/steps/approve"
    assert client.post(f"{url}/approve", headers=headers).status_code == 409  # not waiting yet

    _store, events = app.state.run_stores
    events.append(tenant, uuid.UUID(run_id), "start", "run_started", {})
    events.append(tenant, uuid.UUID(run_id), "step:approve:waiting", "step_waiting",
                  {"step_id": "approve", "workflow_id": run_id})
    _, other = tenant_of()
    assert client.post(f"{url}/approve", headers=other).status_code == 404
    assert client.post(f"{url}/approve", json={"comment": "x" * 2001}, headers=headers).status_code == 422

    r = client.post(f"{url}/approve", json={"comment": "looks fine"}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json() == {"run_id": run_id, "step_id": "approve", "decision": "approved", "decided_by": "alice",
                        "comment": "looks fine", "notified": True}
    assert fake.sent == [(run_id, {"decision": "approved"}, "approval:approve")]
    decision = [e for e in client.get(f"/api/v1/runs/{run_id}/events", headers=headers).json()
                if e["type"] == "step_approved"]
    assert decision and decision[0]["data"]["decided_by"] == "alice"
    assert client.post(f"{url}/approve", headers=headers).status_code == 409
    assert client.post(f"{url}/reject", headers=headers).status_code == 409

    # A lost wake-up still records the decision (the worker polls for it).
    events.append(tenant, uuid.UUID(run_id), "step:second:waiting", "step_waiting", {"step_id": "second", "workflow_id": run_id})
    fake.fail_send = True
    r = client.post(f"/api/v1/workflows/runs/{run_id}/steps/second/reject", headers=headers)
    assert r.status_code == 200 and r.json()["notified"] is False and r.json()["decision"] == "rejected"

    # Not a workflow run, or unknown: 404.
    assert client.post(f"/api/v1/workflows/runs/{uuid.uuid4()}/steps/approve/approve", headers=headers).status_code == 404


@pytest.mark.parametrize("decision", ["approve", "reject"])
@pytest.mark.parametrize(
    "raw_step_id",
    ["%00", "a%00b", "%ED%A0%80", "a%ED%A0%80", "a" * 65, "Approve", "APPROVE", "approve%0A", "1step", "a-b"],
)
def test_malformed_step_id_is_a_compact_422_never_500(api, fake, decision, raw_step_id):
    """step_id must match the spec's step id pattern before anything queries with it."""
    client, app, tenant_of = api
    tenant, headers = tenant_of()
    workflow_id = publish_demo(client, headers)
    run_id = str(uuid.uuid4())
    client.post(f"/api/v1/workflows/{workflow_id}/runs", json={"input": {"q": "hi"}, "run_id": run_id}, headers=headers)
    _store, events = app.state.run_stores
    events.append(tenant, uuid.UUID(run_id), "step:approve:waiting", "step_waiting", {"step_id": "approve", "workflow_id": run_id})
    r = client.post(f"/api/v1/workflows/runs/{run_id}/steps/{raw_step_id}/{decision}", headers=headers)
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, list) and detail[0]["loc"] == ["path", "step_id"]
    assert set(detail[0]) == {"loc", "msg", "type"}
    assert not fake.sent
    # The waiting step is untouched and still decidable.
    assert client.post(f"/api/v1/workflows/runs/{run_id}/steps/approve/{decision}", headers=headers).status_code == 200


@pytest.mark.parametrize(
    "raw_input",
    ['{"q": "x", "n": NaN}', '{"q": "x", "n": Infinity}', '{"q": "x", "n": -Infinity}',
     '{"q": "x", "deep": {"a": [1, {"b": NaN}]}}', '{"q": "x", "list": [0.5, -Infinity]}'],
)
def test_non_finite_input_is_a_compact_422_and_nothing_is_recorded(api, fake, raw_input):
    client, app, tenant_of = api
    tenant, headers = tenant_of()
    workflow_id = publish_demo(client, headers)
    run_id = str(uuid.uuid4())
    body = '{"input": ' + raw_input + ', "run_id": "' + run_id + '"}'
    r = client.post(f"/api/v1/workflows/{workflow_id}/runs", content=body,
                    headers={**headers, "Content-Type": "application/json"})
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail[0]["type"] == "finite_number" and detail[0]["loc"][:2] == ["body", "input"]
    echoed = r.text.replace("(NaN and Infinity are not JSON)", "")
    assert "NaN" not in echoed and "Infinity" not in echoed  # the value itself is never echoed
    assert not fake.calls
    with app.state.database_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM runs WHERE id = :r"), {"r": run_id}).scalar_one() == 0


def test_decision_wakes_the_branch_workflow_that_waits(api, fake):
    client, app, tenant_of = api
    tenant, headers = tenant_of()
    workflow_id = publish_demo(client, headers)
    run_id = str(uuid.uuid4())
    client.post(f"/api/v1/workflows/{workflow_id}/runs", json={"input": {"q": "hi"}, "run_id": run_id}, headers=headers)
    _store, events = app.state.run_stores
    events.append(tenant, uuid.UUID(run_id), "step:a:waiting", "step_waiting",
                  {"step_id": "a", "workflow_id": f"{run_id}:fan:a"})
    r = client.post(f"/api/v1/workflows/runs/{run_id}/steps/a/approve", headers=headers)
    assert r.status_code == 200 and r.json()["notified"] is True
    assert fake.sent == [(f"{run_id}:fan:a", {"decision": "approved"}, "approval:a")]
