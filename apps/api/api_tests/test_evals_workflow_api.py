"""Workflow eval suites and runs (parity round 2, D5): create/list suites by workflow, JSON case
input checked against the workflow's input schema, eval runs stored with the workflow target, and
the agent routes refusing a workflow suite (and the other way round)."""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from eap_api.app import create_app
from eap_api.launcher import DBOSRunLauncher, LauncherSettings, get_run_launcher

from .conftest import get_auth_header
from .run_helpers import PG_URL, cleanup_tenant, hard_timeout, requires_pg
from .test_workflow_runs_api import FakeClient, publish_demo

pytestmark = requires_pg
API = "/api/v1"
AGENT_SPEC = {"name": "Evaluated", "instructions": "Help.", "model": {"provider": "test", "name": "echo"}}


@pytest.fixture
def wf_api():
    app = create_app(database_url=PG_URL)
    fake = FakeClient()
    app.dependency_overrides[get_run_launcher] = lambda: DBOSRunLauncher(
        LauncherSettings("postgresql://unused/x", "dbos", "m1-test"), client=fake
    )
    tenants: list[str] = []

    def tenant_of(user: str = "alice") -> tuple[str, dict[str, str]]:
        t = f"t{uuid.uuid4().hex[:10]}"
        tenants.append(t)
        return t, get_auth_header(t, user)

    with hard_timeout(60), TestClient(app) as client:
        yield client, app, fake, tenant_of
    engine = app.state.database_engine
    for t in tenants:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM eval_suites WHERE tenant_id = :t"), {"t": t})
        cleanup_tenant(engine, t)
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM workflow_versions WHERE tenant_id = :t"), {"t": t})
            conn.execute(text("DELETE FROM workflows WHERE tenant_id = :t"), {"t": t})
    engine.dispose()


def workflow_suite(client, h, workflow_id, name="wf smoke", cases=('{"q": "a"}', '{"q": "b"}')) -> str:
    r = client.post(
        f"{API}/workflows/{workflow_id}/evals/suites",
        json={"name": name, "evaluators": [{"kind": "contains", "value": "x"}]},
        headers=h,
    )
    assert r.status_code == 201, r.text
    suite_id = r.json()["id"]
    for i, text_ in enumerate(cases):
        r = client.post(
            f"{API}/eval-suites/{suite_id}/cases", json={"name": f"c{i}", "input": text_, "expected_output": "x"},
            headers=h,
        )
        assert r.status_code == 201, r.text
    return suite_id


def agent_suite(client, h, name="agent smoke") -> tuple[str, str]:
    r = client.post(f"{API}/agents", json={"spec": AGENT_SPEC}, headers=h)
    agent_id = r.json()["id"]
    assert client.post(f"{API}/agents/{agent_id}/versions", headers=h).status_code == 201
    r = client.post(f"{API}/agents/{agent_id}/eval-suites", json={"name": name}, headers=h)
    assert r.status_code == 201, r.text
    suite_id = r.json()["id"]
    assert client.post(
        f"{API}/eval-suites/{suite_id}/cases", json={"name": "c", "input": "hi"}, headers=h
    ).status_code == 201
    return agent_id, suite_id


def test_create_workflow_suite_and_case_validates_json_input(wf_api):
    client, _, _, tenant_of = wf_api
    _, h = tenant_of()
    wf = publish_demo(client, h)
    r = client.post(f"{API}/workflows/{wf}/evals/suites", json={"name": "s1"}, headers=h)
    assert r.status_code == 201, r.text
    suite = r.json()
    assert (suite["workflow_id"], suite["agent_id"], suite["target_kind"], suite["target_name"]) == (
        wf, None, "workflow", "Triage")
    assert client.post(f"{API}/workflows/{wf}/evals/suites", json={"name": "s1"}, headers=h).status_code == 409
    listed = client.get(f"{API}/workflows/{wf}/evals/suites", headers=h).json()["items"]
    assert [s["id"] for s in listed] == [suite["id"]]

    cases = f"{API}/eval-suites/{suite['id']}/cases"
    ok = client.post(cases, json={"name": "ok", "input": '{"q": "hello"}'}, headers=h)
    assert ok.status_code == 201
    for bad, kind in (
        ("not json", "json_invalid"),
        ('["q"]', "json_object"),
        ('{"q": 1}', "json_schema.type"),
        ('{"q": "x", "extra": true}', "json_schema.additionalProperties"),
    ):
        r = client.post(cases, json={"name": "bad", "input": bad}, headers=h)
        assert r.status_code == 422, (bad, r.text)
        detail = r.json()["detail"]
        assert detail["code"] == "input_schema_errors" and detail["case"] == "bad"
        assert detail["errors"][0]["type"] == kind
        assert "extra" not in json.dumps(detail["errors"]).replace("additionalProperties", "")
    # An edit that breaks the input is refused too; one that keeps it valid goes through.
    case_id = ok.json()["id"]
    assert client.patch(f"{cases}/{case_id}", json={"input": '{"q": 2}'}, headers=h).status_code == 422
    assert client.patch(f"{cases}/{case_id}", json={"input": '{"q": "z"}'}, headers=h).status_code == 200
    assert client.patch(f"{cases}/{case_id}", json={"expected_output": "y"}, headers=h).status_code == 200


def test_workflow_suite_of_another_tenant_or_workflow_is_not_found(wf_api):
    client, _, _, tenant_of = wf_api
    _, h = tenant_of()
    _, other = tenant_of("bob")
    wf = publish_demo(client, h)
    assert client.post(f"{API}/workflows/{wf}/evals/suites", json={"name": "x"}, headers=other).status_code == 404
    assert client.get(f"{API}/workflows/{wf}/evals/suites", headers=other).status_code == 404
    unknown = uuid.uuid4()
    assert client.get(f"{API}/workflows/{unknown}/evals/suites", headers=h).status_code == 404


def test_start_workflow_eval_run_enqueues_eval_kind_with_workflow_target(wf_api):
    client, app, fake, tenant_of = wf_api
    tenant, h = tenant_of()
    wf = publish_demo(client, h)
    suite = workflow_suite(client, h, wf)
    run_id = str(uuid.uuid4())
    body = {"run_id": run_id, "suite_id": suite, "max_concurrency": 2}
    r = client.post(f"{API}/workflows/{wf}/evals/runs", json=body, headers=h)
    assert r.status_code == 201, r.text
    out = r.json()
    assert (out["agent_id"], out["workflow_id"], out["version"], out["cases"], out["draft"]) == (None, wf, 1, 2, False)
    run = app.state.run_stores[0].get_run(tenant, uuid.UUID(run_id))
    assert (run["kind"], run["workflow_id"], run["workflow_version"], run["agent_id"], run["eval_suite_id"]) == (
        "eval", wf, 1, None, suite)
    # A retry with the same body is 200; a different body is 409.
    assert client.post(f"{API}/workflows/{wf}/evals/runs", json=body, headers=h).status_code == 200
    assert client.post(
        f"{API}/workflows/{wf}/evals/runs", json={**body, "max_concurrency": 3}, headers=h
    ).status_code == 409
    # No draft target, unknown version, empty suite.
    fresh = {"run_id": str(uuid.uuid4()), "suite_id": suite}
    assert client.post(f"{API}/workflows/{wf}/evals/runs", json={**fresh, "version": "draft"}, headers=h).status_code == 422
    assert client.post(f"{API}/workflows/{wf}/evals/runs", json={**fresh, "version": 9}, headers=h).status_code == 404
    empty = client.post(f"{API}/workflows/{wf}/evals/suites", json={"name": "empty"}, headers=h).json()["id"]
    r = client.post(f"{API}/workflows/{wf}/evals/runs", json={**fresh, "suite_id": empty}, headers=h)
    assert r.status_code == 422


def test_agent_routes_refuse_a_workflow_suite(wf_api):
    client, _, _, tenant_of = wf_api
    _, h = tenant_of()
    wf = publish_demo(client, h)
    ws = workflow_suite(client, h, wf)
    agent_id, as_ = agent_suite(client, h)
    r = client.post(f"{API}/agents/{agent_id}/evals/runs", json={"run_id": str(uuid.uuid4()), "suite_id": ws}, headers=h)
    assert r.status_code == 404
    r = client.post(f"{API}/workflows/{wf}/evals/runs", json={"run_id": str(uuid.uuid4()), "suite_id": as_}, headers=h)
    assert r.status_code == 404
    assert [s["id"] for s in client.get(f"{API}/agents/{agent_id}/eval-suites", headers=h).json()["items"]] == [as_]


def test_a_case_that_no_longer_fits_the_schema_fails_the_run_start_with_its_name(wf_api):
    client, _, _, tenant_of = wf_api
    _, h = tenant_of()
    wf = publish_demo(client, h)
    suite = workflow_suite(client, h, wf, cases=('{"q": "a"}',))
    # Publish v2 whose input needs a number.
    spec = client.get(f"{API}/workflows/{wf}", headers=h).json()
    draft = spec.get("draft_spec") or spec.get("spec")
    draft["input_schema"] = {"type": "object", "properties": {"q": {"type": "number"}}, "required": ["q"]}
    assert client.patch(f"{API}/workflows/{wf}", json={"spec": draft}, headers=h).status_code == 200
    assert client.post(f"{API}/workflows/{wf}/versions", headers=h).status_code == 201
    r = client.post(f"{API}/workflows/{wf}/evals/runs", json={"run_id": str(uuid.uuid4()), "suite_id": suite}, headers=h)
    assert r.status_code == 422 and r.json()["detail"]["case"] == "c0"
    # Version 1 still accepts it.
    r = client.post(
        f"{API}/workflows/{wf}/evals/runs", json={"run_id": str(uuid.uuid4()), "suite_id": suite, "version": 1}, headers=h
    )
    assert r.status_code == 201, r.text


def test_patch_and_import_responses_carry_target_name(wf_api):
    client, _, _, tenant_of = wf_api
    _, h = tenant_of()
    wf = publish_demo(client, h)
    suite = workflow_suite(client, h, wf)
    r = client.patch(f"{API}/eval-suites/{suite}", json={"description": "d"}, headers=h)
    assert r.status_code == 200 and r.json()["target_name"] == "Triage" and r.json()["target_kind"] == "workflow"
    r = client.post(f"{API}/eval-suites/{suite}/import?format=csv", content=b'name,input\nn,"{""q"": ""x""}"\n', headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["suite"]["target_name"] == "Triage"
    agent_id, as_ = agent_suite(client, h, name="a2")
    assert client.patch(f"{API}/eval-suites/{as_}", json={"description": "d"}, headers=h).json()["target_name"] == "Evaluated"
