"""Acceptance for POST /workflows/{id}/runs and the approve/reject routes.

No worker: a fake DBOS client records the enqueue and the approval wake-up. Skips are not used;
these tests need the same Postgres the rest of the API tests use.
"""

from __future__ import annotations

import os
import socket
import threading
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from eap_api.app import create_app
from eap_api.launcher import DBOSRunLauncher, LauncherSettings, get_run_launcher
from eap_api.request_guard import NOT_UTF8_DETAIL, too_large_detail
from eap_api.routes.workflow_runs import (
    MAX_RUN_BODY_BYTES,
    MAX_WORKFLOW_INPUT_BYTES,
    NOT_WAITING_DETAIL,
)
from eap_run_store import ENQUEUE_FAILED_ERROR

from .conftest import get_auth_header
from .run_helpers import cleanup_tenant, hard_timeout
from .test_workflow_runs_api import FakeClient, publish_demo


PG_URL = os.environ.get("EAP_TEST_DATABASE_URL") or os.environ.get(
    "EAP_DATABASE_URL", "postgresql+psycopg://eap_app:eap_app_password@localhost:54329/eap_app"
)


def _api(fake: FakeClient):
    app = create_app(database_url=PG_URL)
    app.dependency_overrides[get_run_launcher] = lambda: DBOSRunLauncher(
        LauncherSettings("postgresql://unused/x", "dbos", "m1-test"), client=fake
    )
    return app


def _tenant_cleanup(app, tenants: list[str]) -> None:
    engine = app.state.database_engine
    for tenant in tenants:
        cleanup_tenant(engine, tenant)
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM workflow_versions WHERE tenant_id = :t"), {"t": tenant})
            conn.execute(text("DELETE FROM workflows WHERE tenant_id = :t"), {"t": tenant})
    engine.dispose()


@pytest.fixture(autouse=True)
def _no_hang():
    with hard_timeout(60):
        yield


@pytest.fixture
def fake() -> FakeClient:
    return FakeClient()


@pytest.fixture
def api(fake):
    app = _api(fake)
    tenants: list[str] = []

    def tenant_of() -> tuple[str, dict[str, str]]:
        tenant = f"t{uuid.uuid4().hex[:10]}"
        tenants.append(tenant)
        return tenant, get_auth_header(tenant, "alice")

    with TestClient(app) as client:
        yield client, app, tenant_of
    _tenant_cleanup(app, tenants)


def _runs(client, workflow_id: str) -> str:
    return f"/api/v1/workflows/{workflow_id}/runs"


def test_start_202_shape_idempotent_retry_and_conflicts(api, fake):
    client, _app, tenant_of = api
    tenant, headers = tenant_of()
    workflow_id = publish_demo(client, headers)
    run_id = str(uuid.uuid4())
    body = {"input": {"q": "hi"}, "run_id": run_id, "version": 1}
    first = client.post(_runs(client, workflow_id), json=body, headers=headers)
    assert first.status_code == 202, first.text
    assert first.json() == {
        "run_id": run_id,
        "kind": "workflow",
        "workflow_id": workflow_id,
        "version": 1,
        "status": "queued",
        "display_status": "queued",
        "trace_id": None,
    }
    assert client.post(_runs(client, workflow_id), json=body, headers=headers).status_code == 202
    changed = {**body, "input": {"q": "other"}}
    conflict = client.post(_runs(client, workflow_id), json=changed, headers=headers)
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "run_id is already used by a different run request"

    # Same run_id on a tool run, then a workflow run: still a different request.
    tool_id = str(uuid.uuid4())
    tool = client.post(
        "/api/v1/tools/clock.now/runs", json={"input": {}, "run_id": tool_id}, headers=headers
    )
    assert tool.status_code == 202, tool.text
    reused = client.post(
        _runs(client, workflow_id), json={"input": {"q": "hi"}, "run_id": tool_id}, headers=headers
    )
    assert reused.status_code == 409
    assert reused.json()["detail"] == "run_id is already used by a different run request"

    # Another tenant's run_id is 409, and that tenant cannot see this workflow.
    _other_tenant, other = tenant_of()
    stolen = client.post(
        _runs(client, workflow_id), json={"input": {"q": "hi"}, "run_id": run_id}, headers=other
    )
    assert stolen.status_code == 404
    assert stolen.json()["detail"] == "Workflow not found"
    other_run = client.post(
        _runs(client, workflow_id), json={"input": {"q": "hi"}, "run_id": str(uuid.uuid4())}, headers=headers
    ).json()["run_id"]
    # other_run belongs to `tenant`. A second tenant posting that id hits the workflow 404 first
    # because the workflow is not theirs. Cross-tenant run_id conflict is checked once the
    # workflow is visible, so publish a workflow for the other tenant and reuse `other_run`.
    other_workflow = publish_demo(client, other)
    cross = client.post(
        _runs(client, other_workflow),
        json={"input": {"q": "hi"}, "run_id": other_run},
        headers=other,
    )
    assert cross.status_code == 409, cross.text
    assert cross.json()["detail"] == "run_id is already in use"
    assert tenant  # the owner of other_run


def test_start_404_and_409(api, fake):
    client, app, tenant_of = api
    _tenant, headers = tenant_of()
    workflow_id = publish_demo(client, headers)
    missing = client.post(f"/api/v1/workflows/{uuid.uuid4()}/runs", json={"input": {}}, headers=headers)
    assert missing.status_code == 404 and missing.json()["detail"] == "Workflow not found"
    unknown_version = client.post(
        _runs(client, workflow_id), json={"input": {"q": "x"}, "version": 9}, headers=headers
    )
    assert unknown_version.status_code == 404 and unknown_version.json()["detail"] == "Version not found"
    _other, other = tenant_of()
    foreign = client.post(_runs(client, workflow_id), json={"input": {"q": "x"}}, headers=other)
    assert foreign.status_code == 404 and foreign.json()["detail"] == "Workflow not found"

    with app.state.database_engine.begin() as conn:
        conn.execute(text("UPDATE workflows SET archived = true WHERE id = :id"), {"id": workflow_id})
    archived = client.post(_runs(client, workflow_id), json={"input": {"q": "x"}}, headers=headers)
    assert archived.status_code == 404 and archived.json()["detail"] == "Workflow not found"

    draft = {
        "name": "Draft",
        "input_schema": {"type": "object"},
        "start": "t",
        "steps": [{"id": "t", "type": "tool", "tool_id": "clock.now"}],
    }
    draft_id = client.post("/api/v1/workflows", json={"spec": draft}, headers=headers).json()["id"]
    unpublished = client.post(_runs(client, draft_id), json={"input": {}}, headers=headers)
    assert unpublished.status_code == 409
    assert unpublished.json()["detail"] == "Workflow has no published version"
    assert fake.calls == []


def test_start_422_schema_no_echo_remote_ref_and_missing_pin(api, fake):
    client, app, tenant_of = api
    _tenant, headers = tenant_of()
    workflow_id = publish_demo(client, headers)
    url = _runs(client, workflow_id)
    secret = "sk-acceptance-secret-value"
    for body in (
        {"input": {"q": 5, "x": secret}},
        {"input": {}},
        {"input": secret},
        {"input": {"q": "a"}, "extra": secret},
    ):
        response = client.post(url, json=body, headers=headers)
        assert response.status_code == 422, response.text
        assert secret not in response.text
        assert isinstance(response.json()["detail"], list)
        assert len(response.content) < 2_000
        for err in response.json()["detail"]:
            assert set(err) <= {"loc", "msg", "type"}
    typed = client.post(url, json={"input": {"q": 5}}, headers=headers)
    assert typed.json()["detail"][0] == {
        "loc": ["body", "input", "q"],
        "type": "json_schema.type",
        "msg": "does not satisfy the input schema's 'type' keyword",
    }
    huge = "x" * (MAX_WORKFLOW_INPUT_BYTES + 8)
    over = client.post(url, json={"input": {"q": huge}}, headers=headers)
    assert over.status_code == 422
    assert over.json()["detail"] == f"input must be at most {MAX_WORKFLOW_INPUT_BYTES // 1024} KiB as JSON"
    assert huge[:80] not in over.text
    assert len(over.content) < 500

    # A remote $ref is rejected locally. Nothing connects to the server named in the schema.
    hits = {"n": 0}
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(5)
    server.settimeout(0.2)
    port = server.getsockname()[1]
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            try:
                conn, _addr = server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            hits["n"] += 1
            conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        remote = {
            "name": "Remote",
            "input_schema": {"type": "object", "properties": {"a": {"$ref": f"http://127.0.0.1:{port}/schema.json"}}},
            "start": "t",
            "steps": [{"id": "t", "type": "tool", "tool_id": "clock.now"}],
        }
        remote_id = client.post("/api/v1/workflows", json={"spec": remote}, headers=headers).json()["id"]
        assert client.post(f"/api/v1/workflows/{remote_id}/versions", headers=headers).status_code == 201
        refused = client.post(
            _runs(client, remote_id), json={"input": {"a": secret}}, headers=headers
        )
        assert refused.status_code == 422, refused.text
        assert refused.json()["detail"] == [{
            "loc": ["body", "input"],
            "msg": "the workflow's input schema could not be applied",
            "type": "json_schema.invalid_schema",
        }]
        assert secret not in refused.text
        stop.wait(0.3)
    finally:
        stop.set()
        server.close()
        thread.join(timeout=2)
    assert hits["n"] == 0

    # Pinned agent version that is not published: the draft cannot be published, and a version
    # whose agent version disappears later is refused at run start with a compact detail.
    created = client.post(
        "/api/v1/agents",
        json={"spec": {"name": "Helper", "instructions": "Help.", "model": {"provider": "test", "name": "echo"}}},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    agent_id = created.json()["id"]
    assert client.post(f"/api/v1/agents/{agent_id}/versions", headers=headers).status_code == 201
    pinned = {
        "name": "Pinned",
        "input_schema": {"type": "object"},
        "start": "a",
        "steps": [{"id": "a", "type": "agent", "agent_id": agent_id, "version": 2, "input": {}}],
    }
    pinned_id = client.post("/api/v1/workflows", json={"spec": pinned}, headers=headers).json()["id"]
    unpublished_pin = client.post(f"/api/v1/workflows/{pinned_id}/versions", headers=headers)
    assert unpublished_pin.status_code == 422, unpublished_pin.text
    assert unpublished_pin.json()["detail"] == (
        f"step 'a': agent '{agent_id}' has no published version 2"
    )
    live = {
        "name": "Live",
        "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
        "start": "a",
        "steps": [{"id": "a", "type": "agent", "agent_id": agent_id, "version": 1, "input": {"p": "ok"}}],
    }
    live_id = client.post("/api/v1/workflows", json={"spec": live}, headers=headers).json()["id"]
    assert client.post(f"/api/v1/workflows/{live_id}/versions", headers=headers).status_code == 201
    with app.state.database_engine.begin() as conn:
        conn.execute(text("DELETE FROM agent_versions WHERE agent_id = :a AND version = 1"), {"a": agent_id})
    gone = client.post(_runs(client, live_id), json={"input": {"q": secret}}, headers=headers)
    assert gone.status_code == 422, gone.text
    assert gone.json()["detail"] == f"step 'a': agent '{agent_id}' has no published version 1"
    assert secret not in gone.text
    assert not any(call[0].get("workflow_name") == "eap.workflow_run" and secret in str(call) for call in fake.calls)


def test_start_413_and_415_create_no_run(api, fake):
    client, _app, tenant_of = api
    _tenant, headers = tenant_of()
    workflow_id = publish_demo(client, headers)
    url = _runs(client, workflow_id)
    send = {**headers, "Content-Type": "application/json"}
    oversized = b"{" + b" " * MAX_RUN_BODY_BYTES
    assert len(oversized) == MAX_RUN_BODY_BYTES + 1
    too_big = client.post(url, content=oversized, headers=send)
    assert too_big.status_code == 413
    assert too_big.json() == {"detail": too_large_detail(MAX_RUN_BODY_BYTES)}
    assert len(too_big.content) < 500
    not_utf8 = client.post(url, content='{"input": {"q": "hi"}}'.encode("utf-16"), headers=send)
    assert not_utf8.status_code == 415
    assert not_utf8.json() == {"detail": NOT_UTF8_DETAIL}
    nul = client.post(url, content=b'{"input": {"q": "a\x00b"}}', headers=send)
    assert nul.status_code == 415
    assert nul.json() == {"detail": NOT_UTF8_DETAIL}
    assert client.get("/api/v1/runs", headers=headers).json()["items"] == []
    assert fake.calls == []


def test_enqueue_503_then_same_run_id_retries(api, fake):
    client, _app, tenant_of = api
    _tenant, headers = tenant_of()
    workflow_id = publish_demo(client, headers)
    fake.fail_times = 1
    run_id = str(uuid.uuid4())
    body = {"input": {"q": "hi"}, "run_id": run_id}
    failed = client.post(_runs(client, workflow_id), json=body, headers=headers)
    assert failed.status_code == 503
    assert failed.json()["detail"] == (
        "Run recorded but could not be started; retry the same request (same run_id)"
    )
    run = client.get(f"/api/v1/runs/{run_id}", headers=headers).json()
    assert run["status"] == "failed"
    assert run["error"] == ENQUEUE_FAILED_ERROR
    assert fake.calls == []
    retried = client.post(_runs(client, workflow_id), json=body, headers=headers)
    assert retried.status_code == 202, retried.text
    assert retried.json()["status"] == "queued"
    assert retried.json()["run_id"] == run_id
    assert len(fake.calls) == 1
    options, args = fake.calls[0]
    assert options["workflow_name"] == "eap.workflow_run"
    assert args[0] == run_id


def test_approve_twice_non_waiting_and_other_tenant(api, fake):
    client, app, tenant_of = api
    tenant, headers = tenant_of()
    workflow_id = publish_demo(client, headers)
    run_id = str(uuid.uuid4())
    assert client.post(
        _runs(client, workflow_id), json={"input": {"q": "hi"}, "run_id": run_id}, headers=headers
    ).status_code == 202
    url = f"/api/v1/workflows/runs/{run_id}/steps/approve/approve"
    early = client.post(url, headers=headers)
    assert early.status_code == 409 and early.json()["detail"] == NOT_WAITING_DETAIL
    missing_step = client.post(
        f"/api/v1/workflows/runs/{run_id}/steps/nope/approve", headers=headers
    )
    assert missing_step.status_code == 409 and missing_step.json()["detail"] == NOT_WAITING_DETAIL

    _store, events = app.state.run_stores
    events.append(
        tenant, uuid.UUID(run_id), "step:approve:waiting", "step_waiting",
        {"step_id": "approve", "workflow_id": run_id},
    )
    _other_tenant, other = tenant_of()
    foreign = client.post(url, headers=other)
    assert foreign.status_code == 404 and foreign.json()["detail"] == "Run not found"
    long_comment = "x" * 2001
    too_long = client.post(url, json={"comment": long_comment}, headers=headers)
    assert too_long.status_code == 422
    assert long_comment not in too_long.text
    assert len(too_long.content) < 2_000
    assert isinstance(too_long.json()["detail"], list)

    decided = client.post(url, headers=headers)
    assert decided.status_code == 200, decided.text
    assert decided.json() == {
        "run_id": run_id,
        "step_id": "approve",
        "decision": "approved",
        "decided_by": "alice",
        "comment": None,
        "notified": True,
    }
    second = client.post(url, headers=headers)
    assert second.status_code == 409 and second.json()["detail"] == NOT_WAITING_DETAIL
    rejected = client.post(
        f"/api/v1/workflows/runs/{run_id}/steps/approve/reject", headers=headers
    )
    assert rejected.status_code == 409 and rejected.json()["detail"] == NOT_WAITING_DETAIL

    tool = client.post("/api/v1/tools/clock.now/runs", json={"input": {}}, headers=headers)
    assert tool.status_code == 202, tool.text
    tool_run = tool.json()["run_id"]
    not_workflow = client.post(
        f"/api/v1/workflows/runs/{tool_run}/steps/approve/approve", headers=headers
    )
    assert not_workflow.status_code == 404 and not_workflow.json()["detail"] == "Run not found"
    unknown = client.post(
        f"/api/v1/workflows/runs/{uuid.uuid4()}/steps/approve/approve", headers=headers
    )
    assert unknown.status_code == 404 and unknown.json()["detail"] == "Run not found"


def test_child_runs_hidden_unless_parent_run_id(api, fake):
    client, app, tenant_of = api
    tenant, headers = tenant_of()
    workflow_id = publish_demo(client, headers)
    run_id = str(uuid.uuid4())
    assert client.post(
        _runs(client, workflow_id), json={"input": {"q": "hi"}, "run_id": run_id}, headers=headers
    ).status_code == 202
    versions = client.get(f"/api/v1/workflows/{workflow_id}/versions", headers=headers).json()
    agent_id = versions[0]["spec"]["steps"][0]["agent_id"]
    child_id = uuid.uuid4()
    run_store, _events = app.state.run_stores
    run_store.create_child_agent_run(
        tenant, agent_id, 1, "hello", child_id, uuid.UUID(run_id), "ask"
    )
    top = client.get("/api/v1/runs", headers=headers).json()["items"]
    assert [item["run_id"] for item in top] == [run_id]
    assert client.get("/api/v1/runs?kind=agent", headers=headers).json()["items"] == []
    listed = client.get(f"/api/v1/runs?parent_run_id={run_id}", headers=headers).json()["items"]
    assert [item["run_id"] for item in listed] == [str(child_id)]
    assert listed[0]["kind"] == "agent"
    assert listed[0]["parent_run_id"] == run_id
    assert listed[0]["parent_step_id"] == "ask"
    assert listed[0]["version"] == 1
    _other, other = tenant_of()
    assert client.get(f"/api/v1/runs?parent_run_id={run_id}", headers=other).json()["items"] == []
    assert client.get(f"/api/v1/runs/{child_id}", headers=other).status_code == 404


@pytest.mark.parametrize(
    ("token", "loc"),
    [
        ("NaN", ["body", "input", "n"]),
        ("Infinity", ["body", "input", "n"]),
        ("-Infinity", ["body", "input", "deep", 0]),
    ],
)
def test_non_finite_input_is_422_and_inserts_no_run(api, fake, token, loc):
    client, app, tenant_of = api
    _tenant, headers = tenant_of()
    workflow_id = publish_demo(client, headers)
    run_id = str(uuid.uuid4())
    if token == "-Infinity":
        raw_input = '{"q": "ok", "deep": [-Infinity]}'
    else:
        raw_input = '{"q": "ok", "n": ' + token + "}"
    body = '{"input": ' + raw_input + ', "run_id": "' + run_id + '"}'
    response = client.post(
        _runs(client, workflow_id),
        content=body,
        headers={**headers, "Content-Type": "application/json"},
    )
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert detail == [{
        "loc": loc,
        "msg": "must be a finite number (NaN and Infinity are not JSON)",
        "type": "finite_number",
    }]
    echoed = response.text.replace("(NaN and Infinity are not JSON)", "")
    assert token not in echoed
    assert fake.calls == []
    with app.state.database_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM runs WHERE id = :r"), {"r": run_id}).scalar_one() == 0
    listed = client.get("/api/v1/runs", headers=headers).json()["items"]
    assert all(item["run_id"] != run_id for item in listed)
