"""P0.7: GET /runs/{run_id}/trace (implementer tests: route, tenant scoping, spec-based nesting)."""

from __future__ import annotations

import uuid
from decimal import Decimal

from fastapi.testclient import TestClient

from .run_helpers import requires_pg
from .test_workflow_runs_api import _no_hang, api, fake  # noqa: F401 - fixtures

pytestmark = requires_pg

AGENT_SPEC = {"name": "Helper", "instructions": "Help.", "model": {"provider": "test", "name": "echo"}}


def _publish_parallel(client: TestClient, headers: dict[str, str]) -> tuple[str, str]:
    r = client.post("/api/v1/agents", json={"spec": AGENT_SPEC}, headers=headers)
    agent_id = r.json()["id"]
    assert client.post(f"/api/v1/agents/{agent_id}/versions", headers=headers).status_code == 201
    spec = {
        "name": "Fan",
        "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"],
                         "additionalProperties": False},
        "start": "fan",
        "steps": [
            {"id": "fan", "type": "parallel", "branches": ["ask", "now"], "join": "done"},
            {"id": "ask", "type": "agent", "agent_id": agent_id, "input": {"p": {"$ref": "input.q"}}, "next": "done"},
            {"id": "now", "type": "tool", "tool_id": "clock.now", "input": {}, "next": "done"},
            {"id": "done", "type": "tool", "tool_id": "clock.now", "input": {}},
        ],
    }
    r = client.post("/api/v1/workflows", json={"spec": spec}, headers=headers)
    assert r.status_code == 201, r.text
    workflow_id = r.json()["id"]
    assert client.post(f"/api/v1/workflows/{workflow_id}/versions", headers=headers).status_code == 201
    return agent_id, workflow_id


def test_workflow_trace_nests_branches_and_child_run(api):
    from eap_run_store import EventLog, RunStore

    client, app, tenant_of = api
    tenant, h = tenant_of()
    agent_id, workflow_id = _publish_parallel(client, h)
    r = client.post(f"/api/v1/workflows/{workflow_id}/runs", json={"input": {"q": "hi"}}, headers=h)
    assert r.status_code == 202, r.text
    run_id = r.json()["run_id"]
    engine = app.state.database_engine
    store, log = RunStore(engine), EventLog(engine)
    child = uuid.uuid4()
    store.create_child_agent_run(tenant, agent_id, 1, "hi", child, run_id, "ask")
    rid = uuid.UUID(run_id)
    for key, type_, data in [
        ("start", "run_started", {"kind": "workflow", "input": {"q": "hi"}}),
        ("step:fan:started", "step_started", {"step_id": "fan", "step_type": "parallel"}),
        ("step:ask:started", "step_started", {"step_id": "ask", "step_type": "agent", "child_run_id": str(child)}),
        ("step:now:started", "step_started", {"step_id": "now", "step_type": "tool", "input": {}}),
    ]:
        log.append(tenant, rid, key, type_, data)
    log.append(tenant, child, "start", "run_started", {"input": "hi"})
    log.append(tenant, child, "model:0:final", "model_message", {"model_request": 0, "text": "hello"}, {
        "request_key": "model:0", "model_id": "test:echo", "provider_model_name": "echo", "input_tokens": 4,
        "output_tokens": 2, "cache_read_tokens": 0, "cache_write_tokens": 0, "usage_measured": True,
        "cost_usd": Decimal("0.00001"), "price_source": "override",
    })

    resp = client.get(f"/api/v1/runs/{run_id}/trace", headers=h)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"run", "spans", "totals", "truncated", "limits"}
    assert body["run"]["run_id"] == run_id and body["run"]["kind"] == "workflow"
    spans = {s["id"]: s for s in body["spans"]}
    assert spans[f"{run_id}:step:ask"]["parent_id"] == f"{run_id}:step:fan"
    assert spans[f"{run_id}:step:now"]["parent_id"] == f"{run_id}:step:fan"
    assert spans[f"run:{child}"]["kind"] == "child_run"
    assert spans[f"run:{child}"]["parent_id"] == f"{run_id}:step:ask"
    model = spans[f"{child}:model:0"]
    assert model["attributes"]["model_id"] == "test:echo" and model["attributes"]["cost_usd"] == "0.0000100000"
    assert body["totals"]["input_tokens"] == 4 and body["totals"]["cost_usd"] == "0.0000100000"
    assert body["limits"]["max_spans"] == 2000

    # Another tenant: the run and its child are 404; malformed ids are 422.
    _other, h2 = tenant_of()
    assert client.get(f"/api/v1/runs/{run_id}/trace", headers=h2).status_code == 404
    assert client.get(f"/api/v1/runs/{child}/trace", headers=h2).status_code == 404
    assert client.get(f"/api/v1/runs/{uuid.uuid4()}/trace", headers=h).status_code == 404
    assert client.get("/api/v1/runs/not-a-uuid/trace", headers=h).status_code == 422
    assert client.get(f"/api/v1/runs/{run_id}/trace").status_code == 401


def test_trace_statement_timeout_is_503(api, monkeypatch):
    from eap_run_store.trace import TraceQueryTimeout, TraceReader

    client, _app, tenant_of = api
    _tenant, h = tenant_of()

    def timed_out(self, tenant_id, run_id):
        raise TraceQueryTimeout(5000)

    monkeypatch.setattr(TraceReader, "load", timed_out)
    resp = client.get(f"/api/v1/runs/{uuid.uuid4()}/trace", headers=h)
    assert resp.status_code == 503
    assert resp.json() == {"detail": "Trace query timed out; retry later"}
