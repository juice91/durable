"""AC.8 implementer tests: GET /approvals and POST /runs/{id}/tool-approvals/{call}/approve|deny,
plus the workflow approve/reject routes going through the shared helper and closing the inbox row.

No worker: waiting approvals are written through EventLog.append_approval_waiting (as the worker's
steps do), and the launcher is the real DBOSRunLauncher around a fake DBOSClient that records sends.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from eap_api.app import create_app
from eap_api.launcher import DBOSRunLauncher, LauncherSettings, get_run_launcher

from .conftest import get_auth_header
from .run_helpers import PG_URL, cleanup_tenant, hard_timeout, requires_pg
from .test_workflow_runs_api import FakeClient, publish_demo

pytestmark = requires_pg

AGENT_SPEC = {"name": "Gated", "instructions": "Help.", "model": {"provider": "test", "name": "echo"}}


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

    def tenant_of(user: str = "alice") -> tuple[str, dict[str, str]]:
        t = f"t{uuid.uuid4().hex[:10]}"
        tenants.append(t)
        return t, get_auth_header(t, user)

    with TestClient(app) as client:
        yield client, app, tenant_of
    engine = app.state.database_engine
    for t in tenants:
        cleanup_tenant(engine, t)
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM workflow_versions WHERE tenant_id = :t"), {"t": t})
            conn.execute(text("DELETE FROM workflows WHERE tenant_id = :t"), {"t": t})
    engine.dispose()


def _deadline() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()


def waiting_tool_call(client, app, tenant, headers, call_id="call_1", tool_name="send_email", args=None):
    """A running agent run of a published agent, waiting on gated call `call_id`. Returns (run_id, agent_id)."""
    r = client.post("/api/v1/agents", json={"spec": AGENT_SPEC}, headers=headers)
    agent_id = r.json()["id"]
    assert client.post(f"/api/v1/agents/{agent_id}/versions", headers=headers).status_code == 201
    run_store, _ = app.state.run_stores
    run_id = uuid.uuid4()
    run_store.create_run(tenant, uuid.UUID(agent_id), 1, "hi", run_id)
    assert run_store.mark_running(tenant, run_id)
    add_tool_wait(app, tenant, run_id, call_id, tool_name, args)
    return run_id, agent_id


def add_tool_wait(app, tenant, run_id, call_id, tool_name="send_email", args=None):
    _, event_log = app.state.run_stores
    event_log.append_approval_waiting(
        tenant, run_id, "tool", call_id,
        {"tool_call_id": call_id, "tool_name": tool_name, "args": args if args is not None else {"to": "x"},
         "deadline": _deadline(), "workflow_id": str(run_id)},
    )


def waiting_step(client, app, tenant, headers, step_id="approve"):
    """A running workflow run waiting on approval step `step_id`. Returns the run id."""
    workflow_id = publish_demo(client, headers)
    run_id = uuid.uuid4()
    r = client.post(f"/api/v1/workflows/{workflow_id}/runs", json={"input": {"q": "x"}, "run_id": str(run_id)},
                    headers=headers)
    assert r.status_code == 202, r.text
    run_store, event_log = app.state.run_stores
    assert run_store.mark_running(tenant, run_id)
    event_log.append_approval_waiting(
        tenant, run_id, "step", step_id,
        {"step_id": step_id, "prompt": "OK?", "timeout_hours": 1, "workflow_id": str(run_id), "deadline": _deadline()},
    )
    return run_id


def tool_url(run_id, call_id, action):
    return f"/api/v1/runs/{run_id}/tool-approvals/{call_id}/{action}"


# ------------------------------------------------------------------------------ decisions


def test_approve_records_the_decision_wakes_the_run_and_a_second_decision_is_409(api, fake):
    client, app, tenant_of = api
    tenant, headers = tenant_of()
    run_id, _ = waiting_tool_call(client, app, tenant, headers)
    assert client.get(f"/api/v1/runs/{run_id}", headers=headers).json()["display_status"] == "waiting_approval"

    r = client.post(tool_url(run_id, "call_1", "approve"), headers=headers)
    assert r.status_code == 200, r.text
    assert r.json() == {"run_id": str(run_id), "tool_call_id": "call_1", "decision": "approved",
                        "decided_by": "alice", "comment": None, "notified": True}
    assert fake.sent == [(str(run_id), {"tool_call_id": "call_1", "decision": "approved"},
                          f"tool_approval:{run_id}")]
    _, event_log = app.state.run_stores
    event = event_log.get_event(tenant, run_id, "tool:call_1:approval:decision")
    assert event.type == "tool_approved"
    assert event.data == {"tool_call_id": "call_1", "decided_by": "alice", "comment": None}
    assert client.get(f"/api/v1/runs/{run_id}", headers=headers).json()["display_status"] == "running"

    for action in ("approve", "deny"):
        r = client.post(tool_url(run_id, "call_1", action), headers=headers)
        assert r.status_code == 409
        assert r.json() == {"detail": "tool call is already decided", "decision": "approved",
                            "decided_by": "alice", "decided_at": event.ts.isoformat()}
    assert len(fake.sent) == 1


def test_deny_with_comment_and_the_comment_cap(api, fake):
    client, app, tenant_of = api
    tenant, headers = tenant_of()
    run_id, _ = waiting_tool_call(client, app, tenant, headers)
    r = client.post(tool_url(run_id, "call_1", "deny"), json={"comment": "x" * 501}, headers=headers)
    assert r.status_code == 422
    assert client.post(tool_url(run_id, "call_1", "deny"), json={"other": 1}, headers=headers).status_code == 422
    r = client.post(tool_url(run_id, "call_1", "deny"), json={"comment": "y" * 500}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["decision"] == "denied" and r.json()["comment"] == "y" * 500
    _, event_log = app.state.run_stores
    event = event_log.get_event(tenant, run_id, "tool:call_1:approval:decision")
    assert event.type == "tool_denied" and event.data["comment"] == "y" * 500
    assert fake.sent[0][1] == {"tool_call_id": "call_1", "decision": "denied"}


def test_unknown_call_unknown_run_other_tenant_and_bad_ids(api, fake):
    client, app, tenant_of = api
    tenant, headers = tenant_of()
    _other, other_headers = tenant_of("bob")
    run_id, _ = waiting_tool_call(client, app, tenant, headers)
    r = client.post(tool_url(run_id, "call_nope", "approve"), headers=headers)
    assert r.status_code == 404 and r.json()["detail"] == "Tool call not found"
    r = client.post(tool_url(uuid.uuid4(), "call_1", "approve"), headers=headers)
    assert r.status_code == 404 and r.json()["detail"] == "Run not found"
    r = client.post(tool_url(run_id, "call_1", "approve"), headers=other_headers)
    assert r.status_code == 404 and r.json()["detail"] == "Run not found"
    assert client.get("/api/v1/approvals", headers=other_headers).json() == {"items": [], "next_cursor": None}
    assert client.post(tool_url(run_id, "a%20b", "approve"), headers=headers).status_code == 422
    assert client.post(tool_url(run_id, "x" * 257, "approve"), headers=headers).status_code == 422
    assert client.post(tool_url("not-a-uuid", "call_1", "approve"), headers=headers).status_code == 422
    assert fake.sent == []
    # Still waiting, for its own tenant.
    assert client.post(tool_url(run_id, "call_1", "approve"), headers=headers).status_code == 200


def test_a_call_of_an_ended_run_is_409_and_a_timed_out_call_is_already_decided(api, fake):
    client, app, tenant_of = api
    tenant, headers = tenant_of()
    run_id, _ = waiting_tool_call(client, app, tenant, headers)
    add_tool_wait(app, tenant, run_id, "call_2")
    _, event_log = app.state.run_stores
    assert event_log.append_tool_decision(
        tenant, run_id, "call_2", "tool_approval_timed_out", {"tool_call_id": "call_2"})[0] == "created"
    r = client.post(tool_url(run_id, "call_2", "approve"), headers=headers)
    assert r.status_code == 409 and r.json()["detail"] == "tool call is already decided"
    # The timeout decided it: no person.
    assert r.json()["decision"] == "timed_out" and r.json()["decided_by"] is None
    datetime.fromisoformat(r.json()["decided_at"])

    with app.state.database_engine.begin() as conn:
        conn.execute(text("UPDATE runs SET status = 'failed' WHERE id = :r"), {"r": str(run_id)})
    r = client.post(tool_url(run_id, "call_1", "approve"), headers=headers)
    assert r.status_code == 409 and r.json()["detail"] == "tool call is not waiting for approval"
    assert fake.sent == []


def test_a_failed_wake_up_is_logged_and_the_decision_stands(api, fake, caplog):
    client, app, tenant_of = api
    tenant, headers = tenant_of()
    run_id, _ = waiting_tool_call(client, app, tenant, headers)
    fake.fail_send = True
    with caplog.at_level("ERROR", logger="eap_api"):
        r = client.post(tool_url(run_id, "call_1", "approve"), headers=headers)
    assert r.status_code == 200 and r.json()["notified"] is False
    assert any("approval wake-up failed" in rec.getMessage() for rec in caplog.records)
    _, event_log = app.state.run_stores
    assert event_log.get_event(tenant, run_id, "tool:call_1:approval:decision").type == "tool_approved"
    assert client.post(tool_url(run_id, "call_1", "approve"), headers=headers).status_code == 409


# ------------------------------------------------------------------------------ the inbox


def test_inbox_lists_both_kinds_with_filters_and_a_cursor(api, fake):
    client, app, tenant_of = api
    tenant, headers = tenant_of()
    step_run = waiting_step(client, app, tenant, headers)
    tool_run, agent_id = waiting_tool_call(client, app, tenant, headers, args={"to": "a@b.c"})

    r = client.get("/api/v1/approvals", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["next_cursor"] is None
    assert [(i["kind"], i["run_id"]) for i in body["items"]] == [("tool", str(tool_run)), ("step", str(step_run))]
    tool, step = body["items"]
    assert tool | {"requested_at": None, "deadline": None} == {
        "run_id": str(tool_run), "kind": "tool", "approval_key": "call_1", "tool_call_id": "call_1",
        "step_id": None, "root_run_id": str(tool_run), "agent_id": agent_id, "workflow_id": None,
        "tool_name": "send_email", "args": {"to": "a@b.c"}, "prompt": None, "status": "pending",
        "requested_at": None, "deadline": None, "decided_at": None, "decision": None, "decided_by": None,
        "run_status": "running",
    }
    assert step["step_id"] == "approve" and step["tool_call_id"] is None and step["prompt"] == "OK?"
    assert step["workflow_id"] is not None and step["tool_name"] is None and step["args"] is None
    datetime.fromisoformat(step["deadline"])

    def kinds(query: str) -> list[str]:
        return [i["kind"] for i in client.get(f"/api/v1/approvals?{query}", headers=headers).json()["items"]]

    assert kinds("kind=tool") == ["tool"]
    assert kinds("kind=step") == ["step"]
    assert kinds(f"agent_id={agent_id}") == ["tool"]
    assert kinds(f"agent_id={uuid.uuid4()}") == []
    assert kinds("status=decided") == []

    page1 = client.get("/api/v1/approvals?limit=1", headers=headers).json()
    assert [i["kind"] for i in page1["items"]] == ["tool"] and page1["next_cursor"]
    page2 = client.get(f"/api/v1/approvals?limit=1&cursor={page1['next_cursor']}", headers=headers).json()
    assert [i["kind"] for i in page2["items"]] == ["step"] and page2["next_cursor"] is None

    # A token names its list: a pending token is not valid for the decided list.
    r = client.get(f"/api/v1/approvals?status=decided&cursor={page1['next_cursor']}", headers=headers)
    assert r.status_code == 422 and "cursor" in r.json()["detail"]

    for bad in ("status=open", "kind=other", "agent_id=nope", "limit=0", "limit=201", "cursor=garbage"):
        assert client.get(f"/api/v1/approvals?{bad}", headers=headers).status_code == 422, bad
    assert client.get("/api/v1/approvals").status_code == 401

    # Decide both through the API: the inbox moves them to decided, newest decision first.
    assert client.post(f"/api/v1/workflows/runs/{step_run}/steps/approve/reject", json={"comment": "no"},
                       headers=headers).status_code == 200
    assert client.post(tool_url(tool_run, "call_1", "approve"), headers=headers).status_code == 200
    assert client.get("/api/v1/approvals", headers=headers).json()["items"] == []
    decided = client.get("/api/v1/approvals?status=decided", headers=headers).json()["items"]
    assert [(i["kind"], i["decision"], i["decided_by"], i["status"]) for i in decided] == [
        ("tool", "approved", "alice", "decided"), ("step", "denied", "alice", "decided")]
    assert all(i["decided_at"] for i in decided)


def test_inbox_hides_approvals_of_runs_that_ended(api, fake):
    client, app, tenant_of = api
    tenant, headers = tenant_of()
    run_id, _ = waiting_tool_call(client, app, tenant, headers)
    with app.state.database_engine.begin() as conn:
        conn.execute(text("UPDATE runs SET status = 'failed' WHERE id = :r"), {"r": str(run_id)})
    assert client.get("/api/v1/approvals", headers=headers).json()["items"] == []


# ------------------------------------------------------------------------------ workflow routes


def test_workflow_routes_keep_their_answers_and_close_the_inbox_row(api, fake):
    client, app, tenant_of = api
    tenant, headers = tenant_of()
    run_id = waiting_step(client, app, tenant, headers)
    r = client.post(f"/api/v1/workflows/runs/{run_id}/steps/approve/approve", json={"comment": "ok"},
                    headers=headers)
    assert r.status_code == 200, r.text
    assert r.json() == {"run_id": str(run_id), "step_id": "approve", "decision": "approved",
                        "decided_by": "alice", "comment": "ok", "notified": True}
    assert fake.sent == [(str(run_id), {"decision": "approved"}, "approval:approve")]
    r = client.post(f"/api/v1/workflows/runs/{run_id}/steps/approve/reject", headers=headers)
    assert r.status_code == 409 and r.json()["detail"] == "step is not waiting for approval"
    assert (r.json()["decision"], r.json()["decided_by"]) == ("approved", "alice")
    # A tool route never decides a workflow step, and a step route never a tool call.
    assert client.post(tool_url(run_id, "approve", "approve"), headers=headers).status_code == 404
    tool_run, _ = waiting_tool_call(client, app, tenant, headers)
    assert client.post(f"/api/v1/workflows/runs/{tool_run}/steps/call_1/approve",
                       headers=headers).status_code == 404  # not a workflow run
    rows = client.get("/api/v1/approvals?status=decided&kind=step", headers=headers).json()["items"]
    assert [(r["run_id"], r["decision"], r["decided_by"]) for r in rows] == [(str(run_id), "approved", "alice")]


def test_the_launcher_uses_the_shared_tool_approval_topic(fake):
    from eap_api import launcher as launcher_module
    from eap_run_store.approvals import tool_approval_topic

    assert launcher_module.tool_approval_topic is tool_approval_topic
    assert tool_approval_topic("abc") == "tool_approval:abc"
    launcher = DBOSRunLauncher(LauncherSettings("postgresql://unused/x", "dbos", "m1-test"), client=fake)
    launcher.notify_tool_approval(run_id="r1", tool_call_id="c1", decision="denied")
    launcher.notify_tool_approval(run_id="r2", tool_call_id="c2", decision="approved", workflow_id="w2")
    assert fake.sent == [("r1", {"tool_call_id": "c1", "decision": "denied"}, "tool_approval:r1"),
                         ("w2", {"tool_call_id": "c2", "decision": "approved"}, "tool_approval:r2")]
