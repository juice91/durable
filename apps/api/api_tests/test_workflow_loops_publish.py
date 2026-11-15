"""LP.5: workflow publish checks loop specs: environment caps, body references, judge models, the
D8a approval warning and `loop.<L>.item` field refs.

The loops runtime gate (`runtime_pending_problems`) still refuses every loop spec at publish until
LP.7/LP.10 land; these tests switch it off to reach the LP.5 checks behind it.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from eap_api.routes import workflows as workflow_routes
from eap_schemas.workflows import WorkflowSpec

from .conftest import get_auth_header
from .run_helpers import requires_pg

BASE = "/api/v1/workflows"
TENANT_USER = ("tenant-lp5", "alice")

TICKETS = {
    "type": "object",
    "properties": {
        "tickets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"urgency": {"type": "string"}, "title": {"type": "string"}},
                "additionalProperties": False,
            },
        },
    },
    "additionalProperties": False,
}


def _ref(path: str) -> dict[str, str]:
    return {"$ref": path}


def _tool(sid: str, nxt: str | None = None, **inp: Any) -> dict[str, Any]:
    return {"id": sid, "type": "tool", "tool_id": "clock.now", "input": inp, "next": nxt}


def _agent_step(sid: str, agent_id: str, version: int | None, nxt: str | None = None, **inp: Any) -> dict[str, Any]:
    return {"id": sid, "type": "agent", "agent_id": agent_id, "version": version, "input": inp, "next": nxt}


def _foreach(body: list[dict[str, Any]], items: str = "input.items", sid: str = "each", **kw: Any) -> dict[str, Any]:
    kw.setdefault("max_items", 5)
    return {"id": sid, "type": "foreach", "items": _ref(items), "body": {"start": body[0]["id"], "steps": body}, **kw}


def _wf(*steps: dict[str, Any]) -> dict[str, Any]:
    return {"name": "Loops", "input_schema": {"type": "object"}, "start": steps[0]["id"], "steps": list(steps)}


def _auth() -> dict[str, str]:
    return get_auth_header(*TENANT_USER)


@pytest.fixture
def no_runtime_gate(monkeypatch):
    monkeypatch.setattr(workflow_routes, "runtime_pending_problems", lambda spec: [])


# --- pure caps ---------------------------------------------------------------------------------


def test_caps_default_accept_loop_specs():
    spec = WorkflowSpec.model_validate(_wf(_foreach([_tool("x")], max_items=100, concurrency=8)))
    assert workflow_routes.loop_limit_problems(spec) == []


def test_loop_free_spec_has_no_cap_problems(monkeypatch):
    monkeypatch.setenv("EAP_WORKFLOW_MAX_STEP_EXECUTIONS", "1")
    spec = WorkflowSpec.model_validate(_wf(_tool("a", "b"), _tool("b")))
    assert workflow_routes.loop_limit_problems(spec) == []


def test_iteration_cap(monkeypatch):
    monkeypatch.setenv("EAP_WORKFLOW_MAX_LOOP_ITERATIONS", "10")
    ok = WorkflowSpec.model_validate(_wf(_foreach([_tool("x")], max_items=10)))
    assert workflow_routes.loop_limit_problems(ok) == []
    over = WorkflowSpec.model_validate(_wf(_foreach([_tool("x")], max_items=11)))
    [problem] = workflow_routes.loop_limit_problems(over)
    assert "step 'each': max_items 11 is above this platform's loop limit of 10" in problem
    loop = {"id": "lp", "type": "loop", "max_iterations": 12, "until": {"op": "exists", "left": _ref("input.a")},
            "body": {"start": "x", "steps": [_tool("x")]}}
    [problem] = workflow_routes.loop_limit_problems(WorkflowSpec.model_validate(_wf(loop)))
    assert "step 'lp': max_iterations 12" in problem


def test_execution_and_concurrency_caps(monkeypatch):
    inner = _foreach([_tool("x")], items="loop.outer.item", sid="inner", max_items=50, concurrency=4)
    spec = WorkflowSpec.model_validate(_wf(_foreach([inner], sid="outer", max_items=50, concurrency=4)))
    problems = workflow_routes.loop_limit_problems(spec)
    # 1 + 50 * (1 + 50 * 1) = 2551 > 2000; 4 * 4 = 16 > 8
    assert any(p.startswith("workflow_too_many_executions:") and "2551" in p for p in problems), problems
    assert any(p.startswith("workflow_concurrency_exceeded:") and "16" in p for p in problems), problems
    monkeypatch.setenv("EAP_WORKFLOW_MAX_STEP_EXECUTIONS", "3000")
    monkeypatch.setenv("EAP_WORKFLOW_MAX_RUN_CONCURRENCY", "16")
    assert workflow_routes.loop_limit_problems(spec) == []


@pytest.mark.parametrize("value", ["abc", "0", "-3", "1.5", "+5", "1_0", " 7", "٣", "1001"])
def test_invalid_cap_values_raise_naming_the_variable(monkeypatch, value):
    monkeypatch.setenv("EAP_WORKFLOW_MAX_LOOP_ITERATIONS", value)
    with pytest.raises(ValueError, match="EAP_WORKFLOW_MAX_LOOP_ITERATIONS must be a whole number from 1 to 1000"):
        workflow_routes.check_workflow_configuration()


@pytest.mark.parametrize("value", ["", "  "])
def test_blank_cap_values_mean_the_default(monkeypatch, value):
    monkeypatch.setenv("EAP_WORKFLOW_MAX_LOOP_ITERATIONS", value)
    workflow_routes.check_workflow_configuration()
    spec = WorkflowSpec.model_validate(_wf(_foreach([_tool("x")], max_items=101)))
    [problem] = workflow_routes.loop_limit_problems(spec)
    assert "loop limit of 100" in problem


def test_cap_bounds(monkeypatch):
    monkeypatch.setenv("EAP_WORKFLOW_MAX_RUN_CONCURRENCY", "64")
    workflow_routes.check_workflow_configuration()
    monkeypatch.setenv("EAP_WORKFLOW_MAX_RUN_CONCURRENCY", "65")
    with pytest.raises(ValueError, match="EAP_WORKFLOW_MAX_RUN_CONCURRENCY must be a whole number from 1 to 64"):
        workflow_routes.check_workflow_configuration()
    monkeypatch.delenv("EAP_WORKFLOW_MAX_RUN_CONCURRENCY")
    monkeypatch.setenv("EAP_WORKFLOW_MAX_STEP_EXECUTIONS", "1000001")
    with pytest.raises(ValueError, match="EAP_WORKFLOW_MAX_STEP_EXECUTIONS"):
        workflow_routes.check_workflow_configuration()


def test_judge_model_allowlist(monkeypatch):
    monkeypatch.setenv("EAP_ALLOWED_MODELS", "test:*")
    judge = {"kind": "llm_judge", "rubric": "good?", "model": {"provider": "openai", "name": "gpt-x"}}
    ev = {"id": "ev", "type": "evaluate", "value": _ref("steps.x.output"), "evaluators": [judge]}
    spec = WorkflowSpec.model_validate(_wf(_foreach([_tool("x", "ev"), ev])))
    [problem] = workflow_routes.judge_model_problems(spec)
    assert "step 'ev': evaluators.0 (llm_judge): model 'openai:gpt-x' is not allowed" in problem
    monkeypatch.setenv("EAP_ALLOWED_MODELS", "test:*,openai:gpt-x")
    assert workflow_routes.judge_model_problems(spec) == []


# --- through the API ---------------------------------------------------------------------------


def _agent(client, *, schema: dict[str, Any] | None = None, approvals: bool = False) -> str:
    spec: dict[str, Any] = {"name": "Triage", "model": {"provider": "test", "name": "structured"}}
    if schema is not None:
        spec["output"] = {"json_schema": schema}
    if approvals:
        spec["tools"] = ["tickets.create"]
        spec["approvals"] = {"tools": ["tickets.create"]}
    resp = client.post("/api/v1/agents", json={"spec": spec}, headers=_auth())
    assert resp.status_code == 201, resp.text
    agent_id = resp.json()["id"]
    assert client.post(f"/api/v1/agents/{agent_id}/versions", headers=_auth()).status_code == 201
    return agent_id


def _create(client, spec):
    created = client.post(BASE, json={"spec": spec}, headers=_auth())
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _publish(client, spec):
    return client.post(f"{BASE}/{_create(client, spec)}/versions", headers=_auth())


@requires_pg
def test_gate_still_refuses_loops(client):
    resp = _publish(client, _wf(_foreach([_tool("x")])))
    assert resp.status_code == 422 and "loops runtime has not landed" in resp.json()["detail"]


@requires_pg
def test_draft_accepts_over_cap_but_publish_refuses(client, no_runtime_gate, monkeypatch):
    monkeypatch.setenv("EAP_WORKFLOW_MAX_LOOP_ITERATIONS", "10")
    workflow_id = _create(client, _wf(_foreach([_tool("x")], max_items=11)))
    resp = client.post(f"{BASE}/{workflow_id}/versions", headers=_auth())
    assert resp.status_code == 422 and "max_items 11" in resp.json()["detail"]


@requires_pg
def test_missing_body_agent_and_tool(client, no_runtime_gate):
    missing = str(uuid.uuid4())
    body = [_agent_step("a", missing, None, "t"), {**_tool("t"), "tool_id": "no.such_tool"}]
    resp = _publish(client, _wf(_foreach(body)))
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert f"step 'a': agent '{missing}' was not found" in detail
    assert "step 't': tool 'no.such_tool' does not exist" in detail


@requires_pg
def test_loop_spec_publishes_and_get_is_identical(client, no_runtime_gate):
    agent_id = _agent(client)
    resp = _publish(client, _wf(_foreach([_agent_step("a", agent_id, 1, q=_ref("loop.each.item"))])))
    assert resp.status_code == 201, resp.text
    assert resp.json()["warnings"] == []
    got = client.get(f"{BASE}/{resp.json()['workflow_id']}/versions/1", headers=_auth())
    assert got.status_code == 200
    published = {k: v for k, v in resp.json().items() if k != "warnings"}
    assert got.json() == published


@requires_pg
def test_pinned_body_agent_with_approvals_warns(client, no_runtime_gate):
    agent_id = _agent(client, approvals=True)
    resp = _publish(client, _wf(_tool("first", "each"), _foreach([_agent_step("a", agent_id, 1)])))
    assert resp.status_code == 201, resp.text
    [warning] = resp.json()["warnings"]
    assert warning["code"] == "loop_agent_approvals_denied"
    assert warning["path"] == "steps[1].body.steps[0]"
    assert "'tickets.create'" in warning["message"] and "denied" in warning["message"]
    # top-level and unpinned agents do not get it
    resp = _publish(client, _wf(_agent_step("top", agent_id, 1, "each"), _foreach([_agent_step("a", agent_id, None)])))
    assert resp.status_code == 201, resp.text
    assert resp.json()["warnings"] == []


def _item_workflow(agent_id: str, version: int | None, field: str) -> dict[str, Any]:
    return _wf(
        _agent_step("triage", agent_id, version, "each"),
        _foreach([_tool("x", None, u=_ref(f"loop.each.item.{field}"))], items="steps.triage.output.tickets"),
    )


@requires_pg
def test_loop_item_field_check(client, no_runtime_gate):
    agent_id = _agent(client, schema=TICKETS)
    ok = _publish(client, _item_workflow(agent_id, 1, "urgency"))
    assert ok.status_code == 201, ok.text
    assert ok.json()["warnings"] == []

    bad = _publish(client, _item_workflow(agent_id, 1, "urgncy"))
    assert bad.status_code == 422
    detail = bad.json()["detail"]
    assert "step 'x': $ref 'loop.each.item.urgncy'" in detail
    assert "an item of foreach 'each' over 'steps.triage.output.tickets'" in detail

    unpinned = _publish(client, _item_workflow(agent_id, None, "urgncy"))
    assert unpinned.status_code == 201, unpinned.text
    codes = [(w["code"], w["path"]) for w in unpinned.json()["warnings"]]
    # the items ref itself (steps[1]) and the item field ref (steps[1].body.steps[0]) both warn
    assert ("unpinned_agent_output_ref", "steps[1].body.steps[0]") in codes
    assert ("unpinned_agent_output_ref", "steps[1]") in codes


@requires_pg
def test_nested_loop_item_field_check(client, no_runtime_gate):
    schema = {"type": "object", "additionalProperties": False, "properties": {"groups": {
        "type": "array", "items": {"type": "object", "additionalProperties": False, "properties": {
            "members": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                   "properties": {"name": {"type": "string"}}}}}}}}}
    agent_id = _agent(client, schema=schema)

    def spec(field: str) -> dict[str, Any]:
        inner = _foreach([_tool("x", None, n=_ref(f"loop.inner.item.{field}"))], items="loop.outer.item.members",
                         sid="inner")
        return _wf(_agent_step("triage", agent_id, 1, "outer"),
                   _foreach([inner], items="steps.triage.output.groups", sid="outer"))

    assert _publish(client, spec("name")).status_code == 201
    bad = _publish(client, spec("nme"))
    assert bad.status_code == 422 and "'nme' is not a field" in bad.json()["detail"]


@requires_pg
def test_body_ref_into_a_body_agent_is_checked(client, no_runtime_gate):
    agent_id = _agent(client, schema=TICKETS)
    body = [_agent_step("a", agent_id, 1, "x"), _tool("x", None, t=_ref("steps.a.output.tikets"))]
    resp = _publish(client, _wf(_foreach(body)))
    assert resp.status_code == 422 and "step 'x': $ref 'steps.a.output.tikets'" in resp.json()["detail"]
