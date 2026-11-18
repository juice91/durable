"""AC.18: the agent capabilities milestone's routes, across tenants, and the decision-route caps.

1. Inventory: the app's routes that serve approvals, evals and agent output schemas are exactly the
   ones listed here (a new route of that kind fails this test until it gets a cross-tenant check).
2. Cross-tenant: tenant A owns a waiting gated tool call, a waiting workflow approval step, an eval
   suite with a case, an eval run with a recorded result, and a published structured agent. Tenant B
   calls EVERY route of the inventory (and the pre-existing routes the milestone changed: the
   workflow approve/reject routes, `GET /runs/{id}`, `GET /runs?kind=eval`, the trace and the
   metrics breakdown) with A's ids: a 404 for everything addressed by id, empty pages for B's own
   lists; then A's world is unchanged (both approvals still pending, no wake-up sent, the suite and
   its case intact, no new run).
3. Comment caps: every decision route refuses a comment one character over its cap (500 for tool
   calls, the pre-milestone 2000 for workflow steps) with 422, and a huge one with 422 or 413,
   records nothing and sends no wake-up, and accepts exactly the cap.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

import pytest
from sqlalchemy import text

from eap_run_store.evals import CaseResultWrite, EvalStore
from eap_run_store.spans import SpanStore

from eap_api.app import create_app
from eap_api.launcher import DBOSRunLauncher, LauncherSettings, get_run_launcher

from .conftest import get_auth_header
from .run_helpers import PG_URL, cleanup_tenant, hard_timeout, requires_pg
from .test_approvals_api import tool_url, waiting_step, waiting_tool_call
from .test_workflow_runs_api import FakeClient, publish_demo

pytestmark = requires_pg
API = "/api/v1"

# (method, path) of every route the milestone added.
NEW_ROUTES = {
    ("GET", "/api/v1/approvals"),
    ("GET", "/api/v1/approvals/{run_id}/{kind}/{key}"),
    ("POST", "/api/v1/runs/{run_id}/tool-approvals/{tool_call_id}/approve"),
    ("POST", "/api/v1/runs/{run_id}/tool-approvals/{tool_call_id}/deny"),
    ("GET", "/api/v1/workflows/agent-output-schema"),
    ("GET", "/api/v1/agents/{agent_id}/eval-suites"),
    ("POST", "/api/v1/agents/{agent_id}/eval-suites"),
    ("GET", "/api/v1/eval-suites/{suite_id}"),
    ("PATCH", "/api/v1/eval-suites/{suite_id}"),
    ("DELETE", "/api/v1/eval-suites/{suite_id}"),
    ("GET", "/api/v1/eval-suites/{suite_id}/cases"),
    ("POST", "/api/v1/eval-suites/{suite_id}/cases"),
    ("GET", "/api/v1/eval-suites/{suite_id}/cases/{case_id}"),
    ("PATCH", "/api/v1/eval-suites/{suite_id}/cases/{case_id}"),
    ("DELETE", "/api/v1/eval-suites/{suite_id}/cases/{case_id}"),
    ("GET", "/api/v1/eval-suites/{suite_id}/export"),
    ("POST", "/api/v1/eval-suites/{suite_id}/import"),
    ("GET", "/api/v1/workflows/{workflow_id}/evals/suites"),
    ("POST", "/api/v1/workflows/{workflow_id}/evals/suites"),
    ("POST", "/api/v1/workflows/{workflow_id}/evals/runs"),
    ("GET", "/api/v1/workflows/{workflow_id}/evals/runs"),
    ("GET", "/api/v1/workflows/{workflow_id}/evals/runs/{run_id}"),
    ("GET", "/api/v1/workflows/{workflow_id}/evals/runs/{run_id}/results"),
    ("POST", "/api/v1/agents/{agent_id}/evals/runs"),
    ("GET", "/api/v1/agents/{agent_id}/evals/runs"),
    ("GET", "/api/v1/agents/{agent_id}/evals/runs/{run_id}"),
    ("GET", "/api/v1/agents/{agent_id}/evals/runs/{run_id}/results"),
    ("GET", "/api/v1/agents/{agent_id}/evals/compare"),
    ("GET", "/api/v1/agents/{agent_id}/evals/warnings"),
    # W1.5: tenant-wide lists behind the Evaluation pages (cross-tenant cases in test_evals_overview_api.py).
    ("GET", "/api/v1/eval-suites"),
    ("GET", "/api/v1/evals/runs"),
    ("GET", "/api/v1/evals/scorers"),
    # Parity round 2, D2: workflow schedules.
    ("GET", "/api/v1/workflows/schedules"),
    ("GET", "/api/v1/workflows/{workflow_id}/schedules"),
    ("POST", "/api/v1/workflows/{workflow_id}/schedules"),
    ("PATCH", "/api/v1/workflows/schedules/{schedule_id}"),
    ("DELETE", "/api/v1/workflows/schedules/{schedule_id}"),
    # Architect milestone: workflow archive, unarchive and restore (shared with the Architect's tools).
    ("POST", "/api/v1/workflows/{workflow_id}/archive"),
    ("POST", "/api/v1/workflows/{workflow_id}/unarchive"),
    ("POST", "/api/v1/workflows/{workflow_id}/versions/{version_num}/restore"),
    # Parity round 2, D7: spans list, runs histogram, score on demand.
    ("GET", "/api/v1/spans"),
    ("GET", "/api/v1/runs/histogram"),
    ("POST", "/api/v1/runs/{run_id}/score"),
}
# Pre-milestone decision routes that now go through the shared decision helper (AC.8).
CHANGED_DECISION_ROUTES = {
    ("POST", "/api/v1/workflows/runs/{run_id}/steps/{step_id}/approve"),
    ("POST", "/api/v1/workflows/runs/{run_id}/steps/{step_id}/reject"),
}
SCHEMA = {"type": "object", "properties": {"priority": {"type": "string", "enum": ["low", "high"]}},
          "required": ["priority"], "additionalProperties": False}


@pytest.fixture
def fake() -> FakeClient:
    return FakeClient()


@pytest.fixture
def api(fake):
    from fastapi.testclient import TestClient

    app = create_app(database_url=PG_URL)
    app.dependency_overrides[get_run_launcher] = lambda: DBOSRunLauncher(
        LauncherSettings("postgresql://unused/x", "dbos", "m1-test"), client=fake
    )
    tenants: list[str] = []

    def tenant_of(user: str = "alice") -> tuple[str, dict[str, str]]:
        t = f"t{uuid.uuid4().hex[:10]}"
        tenants.append(t)
        return t, get_auth_header(t, user)

    with hard_timeout(120), TestClient(app) as client:
        yield client, app, tenant_of
    engine = app.state.database_engine
    for t in tenants:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM spans WHERE tenant_id = :t"), {"t": t})
            conn.execute(text("DELETE FROM workflow_schedules WHERE tenant_id = :t"), {"t": t})
            conn.execute(text("DELETE FROM eval_suites WHERE tenant_id = :t"), {"t": t})
            conn.execute(text("DELETE FROM workflow_versions WHERE tenant_id = :t"), {"t": t})
            conn.execute(text("DELETE FROM workflows WHERE tenant_id = :t"), {"t": t})
        cleanup_tenant(engine, t)
    engine.dispose()


def app_routes(app) -> set[tuple[str, str]]:
    """(METHOD, path) of every documented route (included routers are wrapped, so read the OpenAPI)."""
    return {(m.upper(), path) for path, ops in app.openapi()["paths"].items() for m in ops}


def test_the_inventory_is_every_approval_eval_and_output_schema_route(api):
    _client, app, _ = api
    routes = app_routes(app)
    assert NEW_ROUTES <= routes, NEW_ROUTES - routes
    assert CHANGED_DECISION_ROUTES <= routes
    kinds = ("approval", "eval", "output-schema", "schedules", "spans", "histogram")
    of_kind = {(m, p) for m, p in routes if any(k in p for k in kinds) or p.endswith("/score")}
    assert of_kind <= NEW_ROUTES | CHANGED_DECISION_ROUTES, of_kind - NEW_ROUTES - CHANGED_DECISION_ROUTES


def owner_world(client, app, tenant: str, h: dict[str, str]) -> dict[str, Any]:
    """Tenant A's data behind every route: a waiting tool call, a waiting workflow step, a structured
    agent, an eval suite with a case, an eval run with a recorded result."""
    run_id, agent_id = waiting_tool_call(client, app, tenant, h, call_id="call_1", tool_name="tickets__create")
    step_run = waiting_step(client, app, tenant, h, step_id="approve")
    structured = client.post(f"{API}/agents", json={"spec": {
        "name": "Structured", "instructions": "Triage.", "model": {"provider": "test", "name": "structured"},
        "output": {"json_schema": SCHEMA}}}, headers=h).json()["id"]
    assert client.post(f"{API}/agents/{structured}/versions", headers=h).status_code == 201
    r = client.post(f"{API}/agents/{agent_id}/eval-suites", json={"name": "Triage", "evaluators": [
        {"kind": "contains", "value": "x"}]}, headers=h)
    assert r.status_code == 201, r.text
    suite_id = r.json()["id"]
    r = client.post(f"{API}/eval-suites/{suite_id}/cases", json={"name": "c0", "input": "hi"}, headers=h)
    assert r.status_code == 201, r.text
    case_id = r.json()["id"]
    eval_run = str(uuid.uuid4())
    r = client.post(f"{API}/agents/{agent_id}/evals/runs", json={"run_id": eval_run, "suite_id": suite_id}, headers=h)
    assert r.status_code == 201, r.text
    EvalStore(app.state.database_engine).record_case_result(tenant, eval_run, CaseResultWrite(
        case_id=case_id, case_name="c0", position=0, status="passed",
        assertions={"contains": {"value": True, "reason": None}}, scores={}, duration_ms=5))
    with app.state.database_engine.begin() as conn:  # a judge request of the eval run (metrics `judge`)
        conn.execute(text(
            "INSERT INTO run_model_requests (tenant_id, run_id, request_key, model_id, input_tokens, output_tokens,"
            " cache_read_tokens, cache_write_tokens, usage_measured, created_at, operation)"
            " VALUES (:t, :r, :k, 'test:judge', 200, 50, 0, 0, true, now() - interval '1 hour', 'chat')"),
            {"t": tenant, "r": eval_run, "k": f"judge:{case_id}:1"})
    SpanStore(app.state.database_engine).replace_for_run(tenant, run_id, [{
        "span_id": f"run:{run_id}", "parent_id": None, "kind": "run", "name": "Secret span", "start": datetime.now(timezone.utc),
        "end": None, "status": "ok", "agent_id": agent_id, "workflow_id": None, "model": None}])
    workflow = publish_demo(client, h)
    r = client.post(f"{API}/workflows/{workflow}/schedules", headers=h,
                    json={"name": "nightly", "cron": "0 3 * * *", "input": {"q": "x"}})
    assert r.status_code == 201, r.text
    return {"workflow": workflow, "schedule": r.json()["id"],
            "run": str(run_id), "agent": agent_id, "step_run": str(step_run), "structured": structured,
            "suite": suite_id, "case": case_id, "eval_run": eval_run}


def cross_tenant_calls(w: dict[str, Any]) -> list[tuple[tuple[str, str], str, str, Any, int]]:
    """((method, route), method, concrete path, body, expected status for ANOTHER tenant)."""
    a, s, c, e = w["agent"], w["suite"], w["case"], w["eval_run"]
    return [
        (("GET", "/api/v1/approvals"), "GET", "/approvals", None, 200),
        (("GET", "/api/v1/approvals/{run_id}/{kind}/{key}"), "GET", f"/approvals/{w['run']}/tool/call_1", None, 404),
        (("GET", "/api/v1/approvals/{run_id}/{kind}/{key}"), "GET", f"/approvals/{w['step_run']}/step/approve", None, 404),
        (("POST", "/api/v1/runs/{run_id}/tool-approvals/{tool_call_id}/approve"), "POST",
         f"/runs/{w['run']}/tool-approvals/call_1/approve", {"comment": "mine now"}, 404),
        (("POST", "/api/v1/runs/{run_id}/tool-approvals/{tool_call_id}/deny"), "POST",
         f"/runs/{w['run']}/tool-approvals/call_1/deny", {"comment": "no"}, 404),
        (("GET", "/api/v1/workflows/agent-output-schema"), "GET", f"/workflows/agent-output-schema?agent_id={w['structured']}",
         None, 404),
        (("GET", "/api/v1/agents/{agent_id}/eval-suites"), "GET", f"/agents/{a}/eval-suites", None, 404),
        (("POST", "/api/v1/agents/{agent_id}/eval-suites"), "POST", f"/agents/{a}/eval-suites", {"name": "x"}, 404),
        (("GET", "/api/v1/eval-suites/{suite_id}"), "GET", f"/eval-suites/{s}", None, 404),
        (("PATCH", "/api/v1/eval-suites/{suite_id}"), "PATCH", f"/eval-suites/{s}", {"name": "x"}, 404),
        (("DELETE", "/api/v1/eval-suites/{suite_id}"), "DELETE", f"/eval-suites/{s}", None, 404),
        (("GET", "/api/v1/eval-suites/{suite_id}/cases"), "GET", f"/eval-suites/{s}/cases", None, 404),
        (("POST", "/api/v1/eval-suites/{suite_id}/cases"), "POST", f"/eval-suites/{s}/cases", {"name": "z", "input": "z"}, 404),
        (("GET", "/api/v1/eval-suites/{suite_id}/cases/{case_id}"), "GET", f"/eval-suites/{s}/cases/{c}", None, 404),
        (("PATCH", "/api/v1/eval-suites/{suite_id}/cases/{case_id}"), "PATCH", f"/eval-suites/{s}/cases/{c}",
         {"input": "z"}, 404),
        (("DELETE", "/api/v1/eval-suites/{suite_id}/cases/{case_id}"), "DELETE", f"/eval-suites/{s}/cases/{c}", None, 404),
        (("GET", "/api/v1/eval-suites/{suite_id}/export"), "GET", f"/eval-suites/{s}/export", None, 404),
        (("POST", "/api/v1/eval-suites/{suite_id}/import"), "IMPORT", f"/eval-suites/{s}/import", b"cases: []\n", 404),
        (("POST", "/api/v1/agents/{agent_id}/evals/runs"), "POST", f"/agents/{a}/evals/runs",
         {"run_id": str(uuid.uuid4()), "suite_id": s}, 404),
        (("POST", "/api/v1/agents/{agent_id}/evals/runs"), "POST", f"/agents/{a}/evals/runs",  # A's run id replayed
         {"run_id": e, "suite_id": s}, 404),
        (("GET", "/api/v1/agents/{agent_id}/evals/runs"), "GET", f"/agents/{a}/evals/runs", None, 404),
        (("GET", "/api/v1/agents/{agent_id}/evals/runs/{run_id}"), "GET", f"/agents/{a}/evals/runs/{e}", None, 404),
        (("GET", "/api/v1/agents/{agent_id}/evals/runs/{run_id}/results"), "GET", f"/agents/{a}/evals/runs/{e}/results",
         None, 404),
        (("GET", "/api/v1/agents/{agent_id}/evals/compare"), "GET", f"/agents/{a}/evals/compare?base={e}&target={e}",
         None, 404),
        (("GET", "/api/v1/agents/{agent_id}/evals/warnings"), "GET", f"/agents/{a}/evals/warnings", None, 404),
        # W1.5 tenant-wide lists: another tenant sees an empty page; naming A's agent or suite is a 404.
        (("GET", "/api/v1/eval-suites"), "GET", "/eval-suites", None, 200),
        (("GET", "/api/v1/eval-suites"), "GET", f"/eval-suites?agent_id={a}", None, 404),
        (("GET", "/api/v1/evals/runs"), "GET", "/evals/runs", None, 200),
        (("GET", "/api/v1/evals/runs"), "GET", f"/evals/runs?agent_id={a}", None, 404),
        (("GET", "/api/v1/evals/scorers"), "GET", "/evals/scorers", None, 200),
        (("POST", "/api/v1/workflows/runs/{run_id}/steps/{step_id}/approve"), "POST",
         f"/workflows/runs/{w['step_run']}/steps/approve/approve", {"comment": "x"}, 404),
        (("POST", "/api/v1/workflows/runs/{run_id}/steps/{step_id}/reject"), "POST",
         f"/workflows/runs/{w['step_run']}/steps/approve/reject", {}, 404),
        (("GET", "/api/v1/workflows/{workflow_id}/evals/suites"), "GET", f"/workflows/{w['workflow']}/evals/suites",
         None, 404),
        (("POST", "/api/v1/workflows/{workflow_id}/evals/suites"), "POST", f"/workflows/{w['workflow']}/evals/suites",
         {"name": "x"}, 404),
        (("GET", "/api/v1/workflows/{workflow_id}/evals/runs"), "GET", f"/workflows/{w['workflow']}/evals/runs", None, 404),
        (("GET", "/api/v1/workflows/{workflow_id}/evals/runs/{run_id}"), "GET",
         f"/workflows/{w['workflow']}/evals/runs/{e}", None, 404),
        (("GET", "/api/v1/workflows/{workflow_id}/evals/runs/{run_id}/results"), "GET",
         f"/workflows/{w['workflow']}/evals/runs/{e}/results", None, 404),
        (("POST", "/api/v1/workflows/{workflow_id}/evals/runs"), "POST", f"/workflows/{w['workflow']}/evals/runs",
         {"run_id": str(uuid.uuid4()), "suite_id": s}, 404),
        (("GET", "/api/v1/workflows/schedules"), "GET", "/workflows/schedules", None, 200),
        (("GET", "/api/v1/workflows/{workflow_id}/schedules"), "GET", f"/workflows/{w['workflow']}/schedules", None, 404),
        (("POST", "/api/v1/workflows/{workflow_id}/schedules"), "POST", f"/workflows/{w['workflow']}/schedules",
         {"name": "x", "cron": "* * * * *", "input": {"priority": "low"}}, 404),
        (("PATCH", "/api/v1/workflows/schedules/{schedule_id}"), "PATCH", f"/workflows/schedules/{w['schedule']}",
         {"active": False}, 404),
        (("DELETE", "/api/v1/workflows/schedules/{schedule_id}"), "DELETE", f"/workflows/schedules/{w['schedule']}",
         None, 404),
        (("POST", "/api/v1/workflows/{workflow_id}/archive"), "POST", f"/workflows/{w['workflow']}/archive", None, 404),
        (("POST", "/api/v1/workflows/{workflow_id}/unarchive"), "POST", f"/workflows/{w['workflow']}/unarchive", None, 404),
        (("POST", "/api/v1/workflows/{workflow_id}/versions/{version_num}/restore"), "POST",
         f"/workflows/{w['workflow']}/versions/1/restore", {"discard_draft": True}, 404),
        (("GET", "/api/v1/spans"), "GET", "/spans", None, 200),
        (("GET", "/api/v1/spans"), "GET", f"/spans?agent_id={a}", None, 200),
        (("GET", "/api/v1/runs/histogram"), "GET", "/runs/histogram", None, 200),
        (("POST", "/api/v1/runs/{run_id}/score"), "POST", f"/runs/{w['run']}/score", {"scorer_ids": [str(uuid.uuid4())]}, 404),
        (("GET", "/api/v1/runs/{run_id}"), "GET", f"/runs/{e}", None, 404),
        (("GET", "/api/v1/runs/{run_id}/trace"), "GET", f"/runs/{e}/trace", None, 404),
    ]


def test_another_tenant_reaches_nothing_on_any_new_route(api, fake):
    client, app, tenant_of = api
    tenant, h = tenant_of()
    _other, other = tenant_of("mallory")
    w = owner_world(client, app, tenant, h)
    sent_before = list(fake.sent)
    calls = cross_tenant_calls(w)
    assert {route for route, *_ in calls} >= NEW_ROUTES | CHANGED_DECISION_ROUTES  # every route is attacked

    for route, method, path, body, expected in calls:
        if method == "IMPORT":
            r = client.post(API + path, content=body, headers=other)
        else:
            r = client.request(method, API + path, json=body, headers=other)
        assert r.status_code == expected, (route, path, r.status_code, r.text)
        for value in (w["suite"], w["case"], "Triage", "tickets__create", "Ship", "Secret span", w["run"]):
            assert expected != 200 or value not in r.text, (route, value)
    for status in ("pending", "decided"):
        assert client.get(f"{API}/approvals?status={status}", headers=other).json()["items"] == []
    for kind in ("eval", "agent", "workflow"):
        r = client.get(f"{API}/runs?kind={kind}", headers=other)
        assert r.status_code == 200 and r.json()["items"] == [], (kind, r.text)
    def operations(headers: dict[str, str]) -> set[str]:
        r = client.get(f"{API}/metrics/breakdown", params={"by": "operation"}, headers=headers)
        assert r.status_code == 200, r.text
        return {i["key"] for i in r.json()["items"]}

    assert "judge" in operations(h) and operations(other) == set()

    # A's world is untouched.
    pending = client.get(f"{API}/approvals", headers=h).json()["items"]
    assert sorted((i["kind"], i["approval_key"], i["decision"]) for i in pending) == [
        ("step", "approve", None), ("tool", "call_1", None)]
    assert fake.sent == sent_before  # no wake-up for a refused decision
    # The refused archive changed nothing: A's workflow is still listed and its schedule still active.
    assert w["workflow"] in [i["id"] for i in client.get(f"{API}/workflows", headers=h).json()]
    mine = client.get(f"{API}/workflows/schedules", headers=h).json()["items"]
    assert [(i["id"], i["active"], i["name"]) for i in mine] == [(w["schedule"], True, "nightly")]
    assert client.get(f"{API}/workflows/schedules", headers=other).json()["items"] == []
    assert client.get(f"{API}/eval-suites/{w['suite']}", headers=h).json()["case_count"] == 1
    assert client.get(f"{API}/eval-suites/{w['suite']}/cases/{w['case']}", headers=h).json()["input"] == "hi"
    runs = client.get(f"{API}/agents/{w['agent']}/evals/runs", headers=h).json()["items"]
    assert [r["run_id"] if "run_id" in r else r["id"] for r in runs] == [w["eval_run"]]
    results = client.get(f"{API}/agents/{w['agent']}/evals/runs/{w['eval_run']}/results", headers=h).json()["items"]
    assert [(i["case_name"], i["status"]) for i in results] == [("c0", "passed")]
    with app.state.database_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM runs WHERE tenant_id = :t"), {"t": _other}).scalar() == 0
        assert conn.execute(text("SELECT count(*) FROM eval_suites WHERE tenant_id = :t"), {"t": _other}).scalar() == 0


# --------------------------------------------------------------------------- comment caps

# The tool-call comment is capped at 500 characters (plan: "Denial message and timeouts"); the
# workflow step routes keep their pre-milestone cap of 2000 (AC.8: behaviour unchanged).
DECISIONS = [("tool", "approve", 500), ("tool", "deny", 500), ("step", "approve", 2000), ("step", "reject", 2000)]


@pytest.mark.parametrize(("kind", "verb", "cap"), DECISIONS)
def test_every_decision_route_caps_the_comment(api, fake, kind, verb, cap):
    client, app, tenant_of = api
    tenant, h = tenant_of()
    if kind == "tool":
        run_id, _agent = waiting_tool_call(client, app, tenant, h)
        url = tool_url(run_id, "call_1", verb)
        key = "tool:call_1:approval:decision"
    else:
        run_id = waiting_step(client, app, tenant, h)
        url = f"{API}/workflows/runs/{run_id}/steps/approve/{verb}"
        key = "step:approve:decision"
    _, event_log = app.state.run_stores

    for too_long in ("x" * (cap + 1), "é" * (cap + 1), "a" * 10_000):
        r = client.post(url, json={"comment": too_long}, headers=h)
        # 422 over the cap; 413 when the body is over the route's byte cap first.
        assert r.status_code in ((422,) if len(too_long) <= cap + 1 else (413, 422)), (url, r.status_code, r.text)
        assert too_long[:50] not in r.text  # the comment is not echoed back
        assert event_log.get_event(tenant, run_id, key) is None
    assert fake.sent == []
    assert [i["decision"] for i in client.get(f"{API}/approvals", headers=h).json()["items"]] == [None]

    r = client.post(url, json={"comment": "y" * cap}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["comment"] == "y" * cap
    assert event_log.get_event(tenant, run_id, key).data["comment"] == "y" * cap


# Attachments round 2: the AG-UI twins of the attachment routes (a key's upload and fetch).
ATTACHMENT_ROUTES = {
    ("POST", "/api/v1/ag-ui/attachments"),
    ("GET", "/api/v1/ag-ui/attachments/{attachment_id}"),
    ("DELETE", "/api/v1/ag-ui/attachments/{attachment_id}"),
}


def test_agui_attachment_routes_are_in_the_inventory_and_closed_to_another_tenant(api):
    client, app, tenant_of = api
    assert ATTACHMENT_ROUTES <= app_routes(app)
    assert {(m, p) for m, p in app_routes(app) if "/ag-ui/attachments" in p} == ATTACHMENT_ROUTES
    tenant, h = tenant_of()
    _other, other = tenant_of("mallory")
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    mine = client.post(f"{API}/ag-ui/attachments", files={"file": ("a.png", png, "image/png")}, headers=h)
    assert mine.status_code == 201, mine.text
    path = f"{API}/ag-ui/attachments/{mine.json()['id']}"
    assert client.get(path, headers=h).content == png
    assert client.get(path, headers=other).status_code == 404  # another tenant's dev bearer
    keyed = client.post(f"{API}/api-keys", json={"name": "k"}, headers=other)
    assert keyed.status_code == 201, keyed.text
    assert client.get(path, headers={"Authorization": f"Bearer {keyed.json()['key']}"}).status_code == 404
    assert client.delete(path, headers=other).status_code == 404  # another tenant cannot discard it
    assert client.delete(path, headers={"Authorization": f"Bearer {keyed.json()['key']}"}).status_code == 404
    assert client.get(path, headers=h).content == png  # untouched
    with app.state.database_engine.begin() as conn:
        conn.execute(text("DELETE FROM chat_attachments WHERE tenant_id = :t"), {"t": tenant})


def test_another_tenant_reaches_no_managed_mcp_server_route(api, monkeypatch):
    """Managed MCP servers: every runtime route, test, tool run, patch and delete of tenant A's server
    is a 404 for tenant B, B's list shows nothing, and A's server is untouched."""
    from eap_api.mcp_servers.managed import get_mcp_runtime

    from .mcp_runtime_helpers import FakeRuntime, make_entry

    from eap_secrets import KEY_ENV, generate_key

    monkeypatch.setenv(KEY_ENV, generate_key())
    client, app, tenant_of = api
    fake = FakeRuntime()
    app.dependency_overrides[get_mcp_runtime] = lambda: fake
    tenant, h = tenant_of()
    _other, other = tenant_of("mallory")
    entry = make_entry()
    from eap_run_store.mcp_catalog import CatalogStore

    CatalogStore(app.state.database_engine).upsert([entry])
    try:
        r = client.post(f"{API}/mcp-servers/from-catalog", headers=h, json={
            "catalog_name": entry.name, "env": {"API_TOKEN": "sntl-x-secret"}})
        assert r.status_code == 201, r.text
        sid = r.json()["id"]
        calls = [("GET", f"/mcp-servers/{sid}"), ("PATCH", f"/mcp-servers/{sid}"), ("DELETE", f"/mcp-servers/{sid}"),
                 ("POST", f"/mcp-servers/{sid}/test"), ("GET", f"/mcp-servers/{sid}/runtime"),
                 ("POST", f"/mcp-servers/{sid}/runtime/start"), ("POST", f"/mcp-servers/{sid}/runtime/stop"),
                 ("POST", f"/mcp-servers/{sid}/runtime/restart"), ("GET", f"/mcp-servers/{sid}/runtime/logs"),
                 ("POST", f"/mcp-servers/{sid}/tools/t/runs")]
        for method, path in calls:
            resp = client.request(method, API + path, json={"enabled": False} if method == "PATCH" else {"input": {}} if method == "POST" else None,
                                  headers=other)
            assert resp.status_code == 404, (method, path, resp.status_code, resp.text)
        assert client.get(f"{API}/mcp-servers", headers=other).json() == []
        assert fake.calls == [("start", fake.calls[0][1])]  # nothing but A's own install touched the cluster
        assert client.get(f"{API}/mcp-servers/{sid}", headers=h).json()["runtime_state"] == "starting"
    finally:
        with app.state.database_engine.begin() as conn:
            conn.execute(text("DELETE FROM mcp_catalog_entries WHERE name = :n"), {"n": entry.name})
            conn.execute(text("DELETE FROM mcp_servers WHERE tenant_id = :t"), {"t": tenant})


ARCHITECT_ROUTES = {
    ("GET", "/api/v1/architect"),
    ("POST", "/api/v1/architect"),
    ("GET", "/api/v1/architect/options"),
    ("POST", "/api/v1/architect/draft"),
    ("POST", "/api/v1/architect/draft-workflow"),  # tenant scoping of what it offers: test_architect_api drop tests
    ("PUT", "/api/v1/architect/model"),
    ("POST", "/api/v1/architect/agents"),
    ("POST", "/api/v1/architect/workflows"),
}


def test_architect_routes_are_in_the_inventory_and_closed_to_another_tenant(api):
    """Every Architect route is listed here (a new one fails this test until it gets a check). Tenant A
    seeds the Architect and makes an agent and a workflow; tenant B sees none of it: its own state is
    empty, its seed makes its own rows, a save under A's id makes B's own row (ids are per tenant) and
    leaves A's untouched, and a workflow of B cannot run A's agent."""
    client, app, tenant_of = api
    assert {(m, p) for m, p in app_routes(app) if p.startswith("/api/v1/architect")} == ARCHITECT_ROUTES
    tenant, h = tenant_of()
    _other, other = tenant_of("mallory")
    seeded = client.post(f"{API}/architect", headers=h).json()
    spec = {"name": "Greeter", "id": "greeter", "instructions": "Greet people.", "model": "test:echo"}
    assert client.post(f"{API}/architect/agents", json={"spec": spec, "publish": True}, headers=h).status_code == 200
    wf = {"name": "Flow", "id": "flow", "steps": [{"kind": "agent", "id": "s", "agent": "greeter", "prompt": "{{input}}"}]}
    assert client.post(f"{API}/architect/workflows", json={"spec": wf, "publish": True}, headers=h).status_code == 200

    state = client.get(f"{API}/architect", headers=other).json()
    assert state["seeded"] is False and state["agents"] == [] and state["workflows"] == []
    assert client.get(f"{API}/architect/options", headers=other).json()["agents"] == []
    mine = client.post(f"{API}/architect", headers=other).json()
    assert mine["architect_agent_id"] != seeded["architect_agent_id"]
    assert client.get(f"{API}/agents/{seeded['architect_agent_id']}", headers=other).status_code == 404
    # the model route touches only the caller's own built-in rows
    assert client.put(f"{API}/architect/model", json={"model": "test:history"}, headers=other).status_code == 200
    assert client.get(f"{API}/architect", headers=other).json()["model"] == "test:history"
    assert client.get(f"{API}/architect", headers=h).json()["model"] == seeded["model"]

    theirs = {**spec, "instructions": "Mallory's greeter."}
    assert client.post(f"{API}/architect/agents", json={"spec": theirs, "publish": False}, headers=other).status_code == 200
    r = client.post(f"{API}/architect/agents", json={"spec": theirs, "publish": False, "expected_version": 1}, headers=other)
    assert r.status_code == 409  # B's greeter has no version: it is B's row, not A's v1
    bad = {**wf, "id": "steal", "steps": [{"kind": "agent", "id": "s", "agent": "greeter", "prompt": "x"}]}
    state_b = client.get(f"{API}/architect", headers=other).json()
    assert [a["id"] for a in state_b["agents"]] == ["greeter"] and state_b["agents"][0]["versions"] == 0
    r = client.post(f"{API}/architect/workflows", json={"spec": bad, "publish": True}, headers=other)
    # "greeter" resolves to B's own row (a draft: allowed in a draft, not published), never to A's published one
    assert r.status_code == 422 and r.json()["code"] == "agent_not_published"
    ghost = {**bad, "steps": [{"kind": "agent", "id": "s", "agent": "only-in-a", "prompt": "x"}]}
    r = client.post(f"{API}/architect/workflows", json={"spec": ghost, "publish": False}, headers=other)
    assert r.status_code == 422 and r.json()["code"] == "agent_not_allowed"

    a_row = client.get(f"{API}/architect", headers=h).json()
    assert [a["versions"] for a in a_row["agents"]] == [1] and [w["versions"] for w in a_row["workflows"]] == [1]
    with app.state.database_engine.connect() as conn:
        draft = conn.execute(text("SELECT draft_spec->>'instructions' FROM agents WHERE tenant_id = :t AND slug = 'greeter'"),
                             {"t": tenant}).scalar()
    assert draft == "Greet people."


SECRET_ROUTES = {
    ("PUT", "/api/v1/secrets/{name}"),
    ("GET", "/api/v1/secrets"),
    ("DELETE", "/api/v1/secrets/{name}"),
}


def test_secret_routes_are_in_the_inventory_and_closed_to_another_tenant(api, monkeypatch):
    """Secret input: the three routes are exactly these; tenant B cannot list, delete or resolve A's secret."""
    from eap_run_store.secrets import SecretStore
    from eap_secrets import KEY_ENV, generate_key, load_keyring

    monkeypatch.setenv(KEY_ENV, generate_key())
    client, app, tenant_of = api
    assert {r for r in app_routes(app) if "/secrets" in r[1] and "mcp" not in r[1]} == SECRET_ROUTES
    tenant, h = tenant_of()
    other_tenant, other = tenant_of("mallory")
    try:
        assert client.put(f"{API}/secrets/slack-token", json={"value": "fake-not-real"}, headers=h).status_code == 200
        assert client.get(f"{API}/secrets", headers=other).json() == []
        assert client.delete(f"{API}/secrets/slack-token", headers=other).status_code == 404
        assert [s["name"] for s in client.get(f"{API}/secrets", headers=h).json()] == ["slack-token"]
        store = SecretStore(app.state.database_engine, load_keyring())
        assert store.resolve(other_tenant, "slack-token") is None
        assert store.names(other_tenant) == []
        assert store.delete(other_tenant, "slack-token") is False
        assert store.resolve(tenant, "slack-token") == "fake-not-real"
    finally:
        with app.state.database_engine.begin() as conn:
            conn.execute(text("DELETE FROM tenant_secrets WHERE tenant_id IN (:a, :b)"), {"a": tenant, "b": other_tenant})


def test_a_managed_server_ref_cannot_name_another_tenants_secret(api, monkeypatch):
    """Secret input: tenant B installing or patching a managed server with `secret:<A's name>` is a 422
    (the name does not exist for B) and nothing is started; A's secret is not reported as used."""
    from eap_api.mcp_servers.managed import get_mcp_runtime
    from eap_run_store.mcp_catalog import CatalogStore
    from eap_secrets import KEY_ENV, generate_key

    from .mcp_runtime_helpers import FakeRuntime, make_entry

    monkeypatch.setenv(KEY_ENV, generate_key())
    client, app, tenant_of = api
    fake = FakeRuntime()
    app.dependency_overrides[get_mcp_runtime] = lambda: fake
    tenant, h = tenant_of()
    other_tenant, other = tenant_of("mallory")
    entry = make_entry()
    CatalogStore(app.state.database_engine).upsert([entry])
    try:
        assert client.put(f"{API}/secrets/slack-token", json={"value": "fake-not-real"}, headers=h).status_code == 200
        r = client.post(f"{API}/mcp-servers/from-catalog", headers=other, json={
            "catalog_name": entry.name, "env": {"API_TOKEN": "secret:slack-token"}})
        assert r.status_code == 422 and "fake-not-real" not in r.text
        assert fake.specs == []
        assert client.get(f"{API}/secrets", headers=h).json()[0]["used_by"] == []
    finally:
        with app.state.database_engine.begin() as conn:
            conn.execute(text("DELETE FROM tenant_secrets WHERE tenant_id IN (:a, :b)"), {"a": tenant, "b": other_tenant})
            conn.execute(text("DELETE FROM mcp_catalog_entries WHERE name = :n"), {"n": entry.name})


# kb source sync (Task 4): connections, sources and syncs.
KB_SOURCE_ROUTES = {
    ("POST", "/api/v1/settings/connections"),
    ("GET", "/api/v1/settings/connections"),
    ("GET", "/api/v1/settings/connections/{connection_id}"),
    ("PATCH", "/api/v1/settings/connections/{connection_id}"),
    ("DELETE", "/api/v1/settings/connections/{connection_id}"),
    ("POST", "/api/v1/settings/connections/{connection_id}/check"),
    ("POST", "/api/v1/knowledge-bases/{kb_id}/sources"),
    ("GET", "/api/v1/knowledge-bases/{kb_id}/sources"),
    ("GET", "/api/v1/knowledge-bases/{kb_id}/sources/{source_id}"),
    ("PATCH", "/api/v1/knowledge-bases/{kb_id}/sources/{source_id}"),
    ("POST", "/api/v1/knowledge-bases/{kb_id}/sources/{source_id}/sync"),
    ("GET", "/api/v1/knowledge-bases/{kb_id}/sources/{source_id}/syncs"),
    ("GET", "/api/v1/knowledge-bases/{kb_id}/sources/{source_id}/syncs/{run_id}"),
}


def test_kb_source_routes_are_in_the_inventory_and_closed_to_another_tenant(api, fake, monkeypatch):
    """Tenant B reaches nothing of A's connection, source or sync through any route: 404 for everything
    addressed by id, empty lists, no secret of A resolved, no enqueue, and A's rows are untouched."""
    from eap_kb_sources.fake import FakeCatalogAdapter
    from eap_kb_sources.registry import register_adapter
    from eap_secrets import KEY_ENV, generate_key

    monkeypatch.setenv(KEY_ENV, generate_key())
    monkeypatch.setenv("EAP_ALLOWED_EMBEDDING_MODELS", "test:*")
    adapter = FakeCatalogAdapter()
    register_adapter("product_catalog", "fake", lambda: adapter)
    client, app, tenant_of = api
    assert {(m, p) for m, p in app_routes(app) if "/settings/connections" in p or p.endswith(("/sources", "/syncs"))
            or "/sources/" in p} == KB_SOURCE_ROUTES
    tenant, h = tenant_of()
    other_tenant, other = tenant_of("mallory")
    engine = app.state.database_engine
    try:
        for name in ("conn-id", "conn-secret"):
            assert client.put(f"{API}/secrets/{name}", json={"value": "fake-not-real"}, headers=h).status_code == 200
        conn = client.post(f"{API}/settings/connections", headers=h, json={
            "name": "ti", "kind": "oauth2_client_credentials", "base_url": "https://x.example.com",
            "config": {"provider": "fake"}, "credential_refs": {"client_id": "secret:conn-id", "client_secret": "secret:conn-secret"}},
        ).json()
        kb = client.post(f"{API}/knowledge-bases", json={"name": "d", "embedding_model": "test:hash"}, headers=h).json()["id"]
        src = client.post(f"{API}/knowledge-bases/{kb}/sources", headers=h, json={
            "connection_id": conn["id"], "name": "p", "kind": "product_catalog", "provider": "fake"}).json()
        run = client.post(f"{API}/knowledge-bases/{kb}/sources/{src['id']}/sync", json={"mode": "full"}, headers=h).json()["run_id"]
        enqueued = len(fake.enqueued) if hasattr(fake, "enqueued") else None

        c, s = f"{API}/settings/connections/{conn['id']}", f"{API}/knowledge-bases/{kb}/sources/{src['id']}"
        assert client.get(f"{API}/settings/connections", headers=other).json() == {"items": []}
        assert client.get(c, headers=other).status_code == 404
        assert client.patch(c, json={"enabled": False}, headers=other).status_code == 404
        assert client.post(f"{c}/check", headers=other).status_code == 404
        assert client.delete(c, headers=other).status_code == 404
        # B's kb does not exist for A's ids; B's own kb cannot see A's source or connection either
        assert client.get(f"{API}/knowledge-bases/{kb}/sources", headers=other).status_code == 404
        assert client.post(f"{API}/knowledge-bases/{kb}/sources", headers=other, json={
            "connection_id": conn["id"], "name": "p", "kind": "product_catalog", "provider": "fake"}).status_code == 404
        b_kb = client.post(f"{API}/knowledge-bases", json={"name": "d", "embedding_model": "test:hash"}, headers=other).json()["id"]
        assert client.post(f"{API}/knowledge-bases/{b_kb}/sources", headers=other, json={
            "connection_id": conn["id"], "name": "p", "kind": "product_catalog", "provider": "fake"}).status_code == 404
        assert client.get(f"{API}/knowledge-bases/{b_kb}/sources", headers=other).json() == {"items": []}
        for kb_path in (kb, b_kb):
            base = f"{API}/knowledge-bases/{kb_path}/sources/{src['id']}"
            assert client.get(base, headers=other).status_code == 404
            assert client.patch(base, json={"enabled": False}, headers=other).status_code == 404
            assert client.post(f"{base}/sync", json={"mode": "full"}, headers=other).status_code == 404
            assert client.get(f"{base}/syncs", headers=other).status_code == 404
            assert client.get(f"{base}/syncs/{run}", headers=other).status_code == 404
        assert client.get(f"{API}/runs/{run}", headers=other).status_code == 404

        assert adapter.secret_values == []  # B's check never resolved A's secrets
        assert client.get(c, headers=h).json()["enabled"] is True
        assert client.get(s, headers=h).json()["enabled"] is True
        assert len(client.get(f"{s}/syncs", headers=h).json()["items"]) == 1
        if enqueued is not None:
            assert len(fake.enqueued) == enqueued  # B enqueued nothing
        with engine.connect() as conn_:
            assert conn_.execute(text("SELECT count(*) FROM runs WHERE tenant_id = :t"), {"t": other_tenant}).scalar() == 0
    finally:
        with engine.begin() as conn_:
            for t in (tenant, other_tenant):
                conn_.execute(text("DELETE FROM execution_events WHERE tenant_id = :t"), {"t": t})
                conn_.execute(text("DELETE FROM runs WHERE tenant_id = :t"), {"t": t})
                conn_.execute(text("DELETE FROM knowledge_bases WHERE tenant_id = :t"), {"t": t})
                conn_.execute(text("DELETE FROM settings_connections WHERE tenant_id = :t"), {"t": t})
                conn_.execute(text("DELETE FROM tenant_secrets WHERE tenant_id = :t"), {"t": t})


# --- the MCP control endpoint (mcp-control milestone) ---------------------------------------------

MCP_PATH = "/api/v1/mcp"
MCP_READ_BY_ID = (
    ("agent.get", lambda ids: {"agent_id": ids["agent"]}),
    ("agent.versions", lambda ids: {"agent_id": ids["agent"]}),
    ("workflow.get", lambda ids: {"workflow_id": ids["workflow"]}),
    ("workflow.versions", lambda ids: {"workflow_id": ids["workflow"]}),
    ("workflow.run.get", lambda ids: {"run_id": ids["run"]}),
    ("workflow.run.wait", lambda ids: {"run_id": ids["run"], "timeout_seconds": 0}),
    ("workflow.run.result", lambda ids: {"run_id": ids["run"]}),
    ("workflow.schedule.list", lambda ids: {"workflow_id": ids["workflow"]}),
)


def _mcp_call(client, key: str, tool: str, arguments: dict) -> dict:
    r = client.post(
        MCP_PATH,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}},
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json, text/event-stream"},
    )
    assert r.status_code == 200, r.text
    text_body = r.text
    if text_body.startswith("event:") or "\ndata:" in text_body:
        text_body = next(line[5:] for line in text_body.splitlines() if line.startswith("data:"))
    return json.loads(text_body)["result"]


def test_the_mcp_endpoint_is_in_the_inventory_and_closed_to_another_tenant(api, fake):
    client, app, tenant_of = api
    # Mounted as an ASGI route, so it is not in the OpenAPI inventory: exactly one route serves /api/v1/mcp.
    assert [r.path for r in app.router.routes if getattr(r, "path", "").startswith(MCP_PATH)] == [MCP_PATH]
    assert client.post(MCP_PATH, json={}).status_code == 401
    assert client.post(MCP_PATH, json={}, headers={"Authorization": "Bearer dev:x:y:z"}).status_code == 401

    _tenant, a = tenant_of()
    _other, b = tenant_of("mallory")
    agent_id = client.post(
        f"{API}/agents", json={"spec": {"name": "A", "instructions": "x", "model": {"provider": "test", "name": "echo"}}},
        headers=a,
    ).json()["id"]
    assert client.post(f"{API}/agents/{agent_id}/versions", headers=a).status_code == 201
    workflow_id = publish_demo(client, a)
    run_id = str(uuid.uuid4())
    assert client.post(f"{API}/workflows/{workflow_id}/runs", json={"input": {"q": "hi"}, "run_id": run_id}, headers=a).status_code == 202
    key_b = client.post(f"{API}/api-keys", json={"name": "k", "scopes": ["agui:run", "manage"]}, headers=b).json()["key"]
    ids = {"agent": agent_id, "workflow": workflow_id, "run": run_id}
    for tool, args in MCP_READ_BY_ID:
        result = _mcp_call(client, key_b, tool, args(ids))
        assert result["isError"] is True, tool
        assert result["structuredContent"]["error"]["code"] == "not_found", tool
    # B's own lists are empty, and A's world is untouched (the run is still queued, one enqueue only).
    assert _mcp_call(client, key_b, "agent.list", {})["structuredContent"]["items"] == []
    assert _mcp_call(client, key_b, "workflow.list", {})["structuredContent"]["items"] == []
    assert len(fake.calls) == 1
