"""LP.5 acceptance: loop caps at publish and run start, body walk, judge models, D8a warning, output
refs into body agents and `loop.<L>.item`, tenant isolation, loop-free specs unchanged, env vars."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.responses import JSONResponse
from sqlalchemy import text

from eap_api.routes import workflows as wr

from .conftest import get_auth_header
from .run_helpers import requires_pg
from .test_workflow_runs_api import _no_hang, api, fake  # noqa: F401  (fixtures for run start)

BASE = "/api/v1/workflows"
TENANT = "tenant-lp5a"
OTHER = "tenant-lp5b"
ITER, EXEC, CONC = "EAP_WORKFLOW_MAX_LOOP_ITERATIONS", "EAP_WORKFLOW_MAX_STEP_EXECUTIONS", "EAP_WORKFLOW_MAX_RUN_CONCURRENCY"


def _h(tenant: str = TENANT) -> dict[str, str]:
    return get_auth_header(tenant, "alice")


def _ref(p: str) -> dict[str, str]:
    return {"$ref": p}


def _tool(sid: str, nxt: str | None = None, tool: str = "clock.now", **inp: Any) -> dict[str, Any]:
    return {"id": sid, "type": "tool", "tool_id": tool, "input": inp, "next": nxt}


def _agent_step(sid: str, agent_id: str, version: int | None, nxt: str | None = None, **inp: Any) -> dict[str, Any]:
    return {"id": sid, "type": "agent", "agent_id": agent_id, "version": version, "input": inp, "next": nxt}


def _foreach(body, items="input.items", sid="each", nxt=None, **kw) -> dict[str, Any]:
    kw.setdefault("max_items", 5)
    step = {"id": sid, "type": "foreach", "items": _ref(items), "body": {"start": body[0]["id"], "steps": body}, **kw}
    return {**step, "next": nxt} if nxt else step


def _until(body, sid="lp", **kw) -> dict[str, Any]:
    kw.setdefault("max_iterations", 5)
    return {"id": sid, "type": "loop", "until": {"op": "exists", "left": _ref("input.a")},
            "body": {"start": body[0]["id"], "steps": body}, **kw}


def _wf(*steps) -> dict[str, Any]:
    return {"name": "Loops", "input_schema": {"type": "object"}, "start": steps[0]["id"], "steps": list(steps)}


@pytest.fixture
def no_gate(monkeypatch):
    monkeypatch.setattr(wr, "runtime_pending_problems", lambda spec: [])


def _agent(client, *, schema=None, approvals=False, tenant=TENANT, tools=None) -> str:
    spec: dict[str, Any] = {"name": "A", "model": {"provider": "test", "name": "structured"}}
    if schema is not None:
        spec["output"] = {"json_schema": schema}
    if approvals:
        spec["tools"] = tools or ["tickets.create"]
        spec["approvals"] = {"tools": spec["tools"]}
    r = client.post("/api/v1/agents", json={"spec": spec}, headers=_h(tenant))
    assert r.status_code == 201, r.text
    aid = r.json()["id"]
    assert client.post(f"/api/v1/agents/{aid}/versions", headers=_h(tenant)).status_code == 201
    return aid


def _create(client, spec, tenant=TENANT) -> str:
    r = client.post(BASE, json={"spec": spec}, headers=_h(tenant))
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _publish(client, spec, tenant=TENANT):
    return client.post(f"{BASE}/{_create(client, spec, tenant)}/versions", headers=_h(tenant))


# --- caps: exact boundaries at publish ---------------------------------------------------------


@requires_pg
@pytest.mark.parametrize("n,code", [(10, 201), (11, 422)])
def test_iteration_cap_boundary_foreach_and_loop(client, no_gate, monkeypatch, n, code):
    monkeypatch.setenv(ITER, "10")
    r = _publish(client, _wf(_foreach([_tool("x")], max_items=n)))
    assert r.status_code == code, r.text
    r2 = _publish(client, _wf(_until([_tool("x")], max_iterations=n)))
    assert r2.status_code == code, r2.text
    if code == 422:
        assert "max_items 11" in r.json()["detail"] and "EAP_WORKFLOW_MAX_LOOP_ITERATIONS" in r.json()["detail"]
        assert "max_iterations 11" in r2.json()["detail"]


@requires_pg
@pytest.mark.parametrize("cap,code", [(11, 201), (10, 422)])
def test_execution_cap_boundary(client, no_gate, monkeypatch, cap, code):
    # one foreach, 10 items, one body step: 1 + 10 * 1 = 11 executions
    monkeypatch.setenv(EXEC, str(cap))
    r = _publish(client, _wf(_foreach([_tool("x")], max_items=10)))
    assert r.status_code == code, r.text
    if code == 422:
        d = r.json()["detail"]
        assert "workflow_too_many_executions" in d and "up to 11" in d and "limit is 10" in d


@requires_pg
@pytest.mark.parametrize("cap,code", [(6, 201), (5, 422)])
def test_concurrency_cap_boundary(client, no_gate, monkeypatch, cap, code):
    inner = _foreach([_tool("x")], items="loop.outer.item", sid="inner", max_items=2, concurrency=2)
    spec = _wf(_foreach([inner], sid="outer", max_items=2, concurrency=3))
    monkeypatch.setenv(CONC, str(cap))
    r = _publish(client, spec)
    assert r.status_code == code, r.text
    if code == 422:
        assert "workflow_concurrency_exceeded" in r.json()["detail"] and "up to 6" in r.json()["detail"]


@requires_pg
def test_draft_save_accepts_over_cap_specs(client, no_gate, monkeypatch):
    monkeypatch.setenv(ITER, "2")
    wid = _create(client, _wf(_foreach([_tool("x")], max_items=50)))
    got = client.get(f"{BASE}/{wid}", headers=_h())
    assert got.status_code == 200


@requires_pg
def test_all_three_caps_reported_together(client, no_gate, monkeypatch):
    for v in (ITER, EXEC, CONC):
        monkeypatch.setenv(v, "1")
    inner = _foreach([_tool("x")], items="loop.outer.item", sid="inner", max_items=2, concurrency=2)
    d = _publish(client, _wf(_foreach([inner], sid="outer", max_items=2, concurrency=2))).json()["detail"]
    assert "loop limit of 1" in d and "workflow_too_many_executions" in d and "workflow_concurrency_exceeded" in d


# --- caps at run start --------------------------------------------------------------------------


def _run_ready(api, spec):
    client, _, tenant_of = api
    _, headers = tenant_of()
    r = client.post(BASE, json={"spec": spec}, headers=headers)
    assert r.status_code == 201, r.text
    wid = r.json()["id"]
    pub = client.post(f"{BASE}/{wid}/versions", headers=headers)
    assert pub.status_code == 201, pub.text
    return client, headers, wid


@requires_pg
def test_cap_lowered_after_publish_refuses_run_start(api, fake, no_gate, monkeypatch):
    client, headers, wid = _run_ready(api, _wf(_foreach([_tool("x")], max_items=10)))
    ok = client.post(f"{BASE}/{wid}/runs", json={"input": {"items": [1]}}, headers=headers)
    assert ok.status_code == 202, ok.text
    monkeypatch.setenv(ITER, "9")
    bad = client.post(f"{BASE}/{wid}/runs", json={"input": {"items": [1]}}, headers=headers)
    assert bad.status_code == 422
    assert "max_items 10 is above this platform's loop limit of 9" in bad.json()["detail"]
    monkeypatch.setenv(ITER, "10")  # exact boundary starts again
    assert client.post(f"{BASE}/{wid}/runs", json={"input": {"items": [1]}}, headers=headers).status_code == 202


@requires_pg
@pytest.mark.parametrize("var,low,word", [(EXEC, "10", "workflow_too_many_executions"), (CONC, "2", "workflow_concurrency_exceeded")])
def test_run_start_refuses_lowered_execution_and_concurrency_caps(api, fake, no_gate, monkeypatch, var, low, word):
    spec = _wf(_foreach([_tool("x")], max_items=10, concurrency=3))
    client, headers, wid = _run_ready(api, spec)
    monkeypatch.setenv(var, low)
    r = client.post(f"{BASE}/{wid}/runs", json={"input": {"items": [1]}}, headers=headers)
    assert r.status_code == 422 and word in r.json()["detail"], r.text
    assert fake.calls == []  # nothing enqueued


# --- body walk ----------------------------------------------------------------------------------


def _nested(*inner) -> dict[str, Any]:
    return _foreach([_foreach(list(inner), items="loop.outer.item", sid="inner")], sid="outer")


@requires_pg
def test_nested_body_unknown_agent_and_tool_refused(client, no_gate):
    missing = str(uuid.uuid4())
    r = _publish(client, _wf(_nested(_agent_step("a", missing, None, "t"), _tool("t", tool="no.such_tool"))))
    d = r.json()["detail"]
    assert r.status_code == 422
    assert f"step 'a': agent '{missing}' was not found" in d and "step 't': tool 'no.such_tool' does not exist" in d


@requires_pg
def test_until_loop_body_is_walked_too(client, no_gate):
    r = _publish(client, _wf(_until([_tool("t", tool="no.such_tool")])))
    assert r.status_code == 422 and "step 't': tool 'no.such_tool' does not exist" in r.json()["detail"]


@requires_pg
def test_foreign_tenant_agent_in_nested_body_reads_like_missing(client, no_gate):
    theirs = _agent(client, tenant=OTHER)
    r = _publish(client, _wf(_nested(_agent_step("a", theirs, 1))))
    assert r.status_code == 422 and f"step 'a': agent '{theirs}' was not found" in r.json()["detail"]
    mine = _agent(client)
    assert _publish(client, _wf(_nested(_agent_step("a", mine, 1)))).status_code == 201


@requires_pg
def test_archived_body_agent_refused(client, no_gate):
    aid = _agent(client)
    with client.app.state.database_engine.begin() as conn:
        conn.execute(text("UPDATE agents SET archived = true WHERE id = :i"), {"i": aid})
    r = _publish(client, _wf(_foreach([_agent_step("a", aid, None)])))
    assert r.status_code == 422 and f"agent '{aid}'" in r.json()["detail"]


@requires_pg
def test_tool_allowlist_applies_inside_bodies(client, no_gate, monkeypatch):
    monkeypatch.setenv("EAP_ALLOWED_TOOLS", "clock.now")
    r = _publish(client, _wf(_foreach([_tool("t", tool="tickets.create", title="x", body="y")])))
    assert r.status_code == 422 and "step 't': tool 'tickets.create' is not allowed on this platform" in r.json()["detail"]
    assert _publish(client, _wf(_foreach([_tool("t")]))).status_code == 201


def _judge(model: dict[str, str]) -> dict[str, Any]:
    return {"id": "ev", "type": "evaluate", "value": _ref("steps.x.output"),
            "evaluators": [{"kind": "llm_judge", "rubric": "good?", "model": model}]}


@requires_pg
def test_disallowed_judge_model_in_nested_body_refused_allowed_ok(client, no_gate, monkeypatch):
    monkeypatch.setenv("EAP_ALLOWED_MODELS", "test:*")
    bad = _publish(client, _wf(_nested(_tool("x", "ev"), _judge({"provider": "openai", "name": "gpt-x"}))))
    assert bad.status_code == 422
    assert "step 'ev': evaluators.0 (llm_judge): model 'openai:gpt-x' is not allowed" in bad.json()["detail"]
    ok = _publish(client, _wf(_nested(_tool("x", "ev"), _judge({"provider": "test", "name": "m"}))))
    assert ok.status_code == 201, ok.text


@requires_pg
def test_judge_model_checked_at_top_level_and_non_judge_evaluators_ignored(client, no_gate, monkeypatch):
    monkeypatch.setenv("EAP_ALLOWED_MODELS", "test:*")
    top = _wf(_tool("x", "ev"), _judge({"provider": "openai", "name": "gpt-x"}))
    assert _publish(client, top).status_code == 422
    # non-judge evaluators have no model: they are skipped, and only the judge's index is named
    plain = [{"kind": "contains", "value": "x"}, {"kind": "regex", "pattern": "^a"}]
    only_plain = {"id": "ev", "type": "evaluate", "value": _ref("steps.x.output"), "evaluators": plain}
    assert _publish(client, _wf(_tool("x", "ev"), only_plain)).status_code == 201
    mixed = {**only_plain, "evaluators": [*plain, _judge({"provider": "openai", "name": "gpt-x"})["evaluators"][0]]}
    r = _publish(client, _wf(_nested(_tool("x", "ev"), mixed)))
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "step 'ev': evaluators.2 (llm_judge): model 'openai:gpt-x' is not allowed" in detail
    assert "evaluators.0" not in detail and "evaluators.1" not in detail


# --- D8a ----------------------------------------------------------------------------------------


@requires_pg
def test_d8a_warns_once_per_step_for_pinned_body_agents_and_never_blocks(client, no_gate):
    aid = _agent(client, approvals=True)
    spec = _wf(_foreach([_agent_step("a", aid, 1, "b"), _agent_step("b", aid, 1)]))
    r = _publish(client, spec)
    assert r.status_code == 201, r.text
    got = [(w["code"], w["path"]) for w in r.json()["warnings"]]
    assert got == [("loop_agent_approvals_denied", "steps[0].body.steps[0]"),
                   ("loop_agent_approvals_denied", "steps[0].body.steps[1]")]
    assert "loop 'each'" in r.json()["warnings"][0]["message"]


@requires_pg
def test_d8a_nested_path_innermost_loop_and_single_warning_with_refs(client, no_gate):
    schema = {"type": "object", "properties": {"k": {"type": "string"}}, "additionalProperties": False}
    aid = _agent(client, schema=schema, approvals=True)
    body = [_agent_step("a", aid, 1, "t"), _tool("t", None, v=_ref("steps.a.output.k"), w=_ref("steps.a.output.k"))]
    r = _publish(client, _wf(_nested(*body)))
    assert r.status_code == 201, r.text
    ws = r.json()["warnings"]
    assert [w["code"] for w in ws] == ["loop_agent_approvals_denied"]
    assert ws[0]["path"] == "steps[0].body.steps[0].body.steps[0]" and "loop 'inner'" in ws[0]["message"]


@requires_pg
def test_d8a_not_for_policyless_unpinned_or_top_level_agents(client, no_gate):
    plain, gated = _agent(client), _agent(client, approvals=True)
    spec = _wf(_agent_step("top", gated, 1, "each"),
               _foreach([_agent_step("a", plain, 1, "b"), _agent_step("b", gated, None)]))
    r = _publish(client, spec)
    assert r.status_code == 201 and r.json()["warnings"] == []


@requires_pg
def test_d8a_lists_every_gated_tool(client, no_gate):
    aid = _agent(client, approvals=True, tools=["tickets.create", "clock.now"])
    w = _publish(client, _wf(_foreach([_agent_step("a", aid, 1)]))).json()["warnings"]
    assert "'tickets.create', 'clock.now'" in w[0]["message"]


# --- output refs into body agents and loop.<L>.item ---------------------------------------------

CLOSED = {"type": "object", "additionalProperties": False, "properties": {
    "priority": {"type": "string"},
    "owner": {"type": "object", "additionalProperties": False, "properties": {"name": {"type": "string"}}},
    "tickets": {"type": "array", "items": {"type": "object", "additionalProperties": False, "properties": {
        "urgency": {"type": "string"}, "who": {"type": "object", "additionalProperties": False,
                                               "properties": {"id": {"type": "string"}}}}}}}}
OPEN = {"type": "object", "properties": {"tickets": {"type": "array", "items": {
    "type": "object", "properties": {"urgency": {"type": "string"}}}}}}


def _items_wf(aid, field: str, version: int | None = 1, schema_items="steps.triage.output.tickets"):
    return _wf(_agent_step("triage", aid, version, "each"),
               _foreach([_tool("x", None, u=_ref(f"loop.each.item.{field}"))], items=schema_items))


@requires_pg
@pytest.mark.parametrize("field", ["urgency", "who", "who.id"])
def test_item_valid_paths_publish_clean(client, no_gate, field):
    r = _publish(client, _items_wf(_agent(client, schema=CLOSED), field))
    assert r.status_code == 201, r.text
    assert r.json()["warnings"] == []


@requires_pg
@pytest.mark.parametrize("field", ["urgncy", "who.name", "urgency.deep"])
def test_item_invalid_paths_are_422_with_via_note(client, no_gate, field):
    r = _publish(client, _items_wf(_agent(client, schema=CLOSED), field))
    assert r.status_code == 422, r.text
    d = r.json()["detail"]
    assert f"step 'x': $ref 'loop.each.item.{field}' (an item of foreach 'each' over 'steps.triage.output.tickets')" in d


@requires_pg
def test_whole_item_and_non_agent_items_are_not_checked(client, no_gate):
    aid = _agent(client, schema=CLOSED)
    whole = _wf(_agent_step("triage", aid, 1, "each"),
                _foreach([_tool("x", None, u=_ref("loop.each.item"))], items="steps.triage.output.tickets"))
    assert _publish(client, whole).status_code == 201
    r = _publish(client, _wf(_foreach([_tool("x", None, u=_ref("loop.each.item.anything.at.all"))])))
    assert r.status_code == 201 and r.json()["warnings"] == []


@requires_pg
def test_item_open_object_warns_unlisted_field_and_never_blocks(client, no_gate):
    r = _publish(client, _items_wf(_agent(client, schema=OPEN), "other"))
    assert r.status_code == 201, r.text
    [w] = r.json()["warnings"]
    assert w["code"] == "unlisted_output_field" and w["path"] == "steps[1].body.steps[0]"
    assert "(an item of foreach 'each'" in w["message"]


@requires_pg
def test_item_text_agent_warns_text_output_ref(client, no_gate):
    r = _publish(client, _items_wf(_agent(client), "urgency"))
    assert r.status_code == 201, r.text
    codes = {(w["code"], w["path"]) for w in r.json()["warnings"]}
    assert ("text_agent_output_ref", "steps[1].body.steps[0]") in codes


@requires_pg
def test_item_unpinned_warns_pinned_blocks_same_spec(client, no_gate):
    aid = _agent(client, schema=CLOSED)
    un = _publish(client, _items_wf(aid, "urgncy", version=None))
    assert un.status_code == 201
    assert ("unpinned_agent_output_ref", "steps[1].body.steps[0]") in {(w["code"], w["path"]) for w in un.json()["warnings"]}
    assert _publish(client, _items_wf(aid, "urgncy", version=1)).status_code == 422


@requires_pg
def test_item_versions_are_checked_against_the_pinned_version(client, no_gate):
    aid = _agent(client, schema=CLOSED)
    spec = {"name": "A", "model": {"provider": "test", "name": "structured"},
            "output": {"json_schema": {**CLOSED, "properties": {**CLOSED["properties"], "extra": {"type": "string"}}}}}
    assert client.patch(f"/api/v1/agents/{aid}", json={"spec": spec}, headers=_h()).status_code == 200
    assert client.post(f"/api/v1/agents/{aid}/versions", headers=_h()).status_code == 201
    assert _publish(client, _wf(_agent_step("triage", aid, 2, "e"), _foreach(
        [_tool("x", None, u=_ref("steps.triage.output.extra"))], sid="e"))).status_code == 201
    assert _publish(client, _wf(_agent_step("triage", aid, 1, "e"), _foreach(
        [_tool("x", None, u=_ref("steps.triage.output.extra"))], sid="e"))).status_code == 422


@requires_pg
def test_body_agent_output_refs_valid_invalid_and_until_exit_refs(client, no_gate):
    aid = _agent(client, schema=CLOSED)

    def spec(path: str) -> dict[str, Any]:
        return _wf(_foreach([_agent_step("a", aid, 1, "t"), _tool("t", None, v=_ref(path))]))

    assert _publish(client, spec("steps.a.output.owner.name")).status_code == 201
    bad = _publish(client, spec("steps.a.output.owner.nom"))
    assert bad.status_code == 422 and "step 't': $ref 'steps.a.output.owner.nom'" in bad.json()["detail"]
    # a ref in the until condition of a loop is checked too
    cond = {"op": "exists", "left": _ref("steps.a.output.prority")}
    loop = {"id": "lp", "type": "loop", "max_iterations": 3, "until": cond,
            "body": {"start": "a", "steps": [_agent_step("a", aid, 1)]}}
    r = _publish(client, _wf(loop))
    assert r.status_code == 422 and "steps.a.output.prority" in r.json()["detail"]


@requires_pg
def test_nested_loops_item_over_item_and_warning_paths(client, no_gate):
    schema = {"type": "object", "additionalProperties": False, "properties": {"groups": {
        "type": "array", "items": {"type": "object", "additionalProperties": False, "properties": {
            "members": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                   "properties": {"name": {"type": "string"}}}}}}}}}

    def spec(aid, field: str, version=1) -> dict[str, Any]:
        inner = _foreach([_tool("x", None, n=_ref(f"loop.inner.item.{field}"))], items="loop.outer.item.members", sid="inner")
        return _wf(_agent_step("t", aid, version, "outer"), _foreach([inner], items="steps.t.output.groups", sid="outer"))

    aid = _agent(client, schema=schema)
    assert _publish(client, spec(aid, "name")).json()["warnings"] == []
    bad = _publish(client, spec(aid, "nme"))
    assert bad.status_code == 422 and "(an item of foreach 'inner' over 'loop.outer.item.members')" in bad.json()["detail"]
    un = _publish(client, spec(aid, "nme", None))
    assert un.status_code == 201
    paths = {w["path"] for w in un.json()["warnings"]}
    assert "steps[1].body.steps[0].body.steps[0]" in paths


@requires_pg
def test_item_ref_over_input_items_ignores_same_named_agent_step(client, no_gate):
    # items come from input.*, so there is no agent schema to check the item field against
    aid = _agent(client, schema=CLOSED)
    spec = _wf(_agent_step("triage", aid, 1, "each"),
               _foreach([_tool("x", None, u=_ref("loop.each.item.urgency"))], items="input.items"))
    assert _publish(client, spec).status_code == 201


# --- tenant isolation ---------------------------------------------------------------------------


@requires_pg
def test_tenant_isolation_publish_and_run_start(api, fake, no_gate):
    client, _, tenant_of = api
    _, mine = tenant_of()
    _, theirs = tenant_of()
    r = client.post("/api/v1/agents", json={"spec": {"name": "A", "model": {"provider": "test", "name": "structured"}}}, headers=theirs)
    aid = r.json()["id"]
    assert client.post(f"/api/v1/agents/{aid}/versions", headers=theirs).status_code == 201
    wid = client.post(BASE, json={"spec": _wf(_foreach([_agent_step("a", aid, 1)]))}, headers=mine).json()["id"]
    pub = client.post(f"{BASE}/{wid}/versions", headers=mine)
    assert pub.status_code == 422 and f"agent '{aid}' was not found" in pub.json()["detail"]
    assert aid not in str(pub.json()).replace(f"agent '{aid}' was not found", "")
    own = client.post(BASE, json={"spec": _wf(_foreach([_tool("x")]))}, headers=theirs).json()["id"]
    assert client.post(f"{BASE}/{own}/versions", headers=theirs).status_code == 201
    assert client.post(f"{BASE}/{own}/versions", headers=mine).status_code == 404
    assert client.post(f"{BASE}/{own}/runs", json={"input": {"items": []}}, headers=mine).status_code == 404


@requires_pg
def test_d8a_only_reads_the_callers_agent_versions(client, no_gate):
    mine_plain = _agent(client)
    theirs_gated = _agent(client, approvals=True, tenant=OTHER)
    r = _publish(client, _wf(_foreach([_agent_step("a", mine_plain, 1)])))
    assert r.status_code == 201 and r.json()["warnings"] == []
    r = _publish(client, _wf(_foreach([_agent_step("a", theirs_gated, 1)])))
    assert r.status_code == 422  # foreign agent: refusal, not a warning that would leak its policy
    detail = r.json()["detail"]
    assert detail == f"step 'a': agent '{theirs_gated}' was not found"
    assert "tickets.create" not in r.text and "loop_agent_approvals_denied" not in r.text
    assert "approval" not in r.text


# --- old workflows publish exactly as before (AC.9 behaviour) -----------------------------------


def _old_wf(aid, version, path):
    return _wf(_agent_step("a", aid, version, "b"), _tool("b", None, v=_ref(path)))


@requires_pg
def test_loop_free_workflows_ignore_loop_caps_and_judge_free_specs(client, monkeypatch):
    for v in (ITER, EXEC, CONC):
        monkeypatch.setenv(v, "1")
    aid = _agent(client, schema=CLOSED, approvals=True)  # an approval policy at top level never warns
    r = _publish(client, _old_wf(aid, 1, "steps.a.output.priority"))
    assert r.status_code == 201, r.text
    assert r.json()["warnings"] == []
    assert wr.loop_limit_problems(__import__("eap_schemas.workflows", fromlist=["x"]).WorkflowSpec.model_validate(
        _old_wf(aid, 1, "steps.a.output"))) == []


@requires_pg
def test_loop_free_warning_shapes_match_ac9(client):
    # unpinned -> unpinned_agent_output_ref at steps[1], message without any loop wording
    aid = _agent(client, schema=CLOSED)
    r = _publish(client, _old_wf(aid, None, "steps.a.output.prority"))
    assert r.status_code == 201, r.text
    [w] = r.json()["warnings"]
    assert w["code"] == "unpinned_agent_output_ref" and w["path"] == "steps[1]"
    assert set(w) == {"code", "path", "message"}
    assert "an item of foreach" not in w["message"] and "step 'b': $ref 'steps.a.output.prority'" in w["message"]
    # text agent, pinned -> text_agent_output_ref; closed bad path pinned -> 422
    text_agent = _agent(client)
    [w2] = _publish(client, _old_wf(text_agent, 1, "steps.a.output.x")).json()["warnings"]
    assert w2["code"] == "text_agent_output_ref" and w2["path"] == "steps[1]"
    bad = _publish(client, _old_wf(aid, 1, "steps.a.output.prority"))
    assert bad.status_code == 422 and "does not match the output schema of agent step 'a'" in bad.json()["detail"]
    assert "(an item of foreach" not in bad.json()["detail"]


@requires_pg
def test_loop_free_still_gated_message_unchanged_and_get_identical(client):
    # the runtime gate only concerns loop specs; a loop-free spec publishes without the monkeypatch
    r = _publish(client, _wf(_tool("a", "b"), _tool("b")))
    assert r.status_code == 201 and r.json()["warnings"] == []
    first = client.get(f"{BASE}/{r.json()['workflow_id']}/versions/1", headers=_h())
    again = client.get(f"{BASE}/{r.json()['workflow_id']}/versions/1", headers=_h())
    published = {k: v for k, v in r.json().items() if k != "warnings"}
    # byte-identical: the GET body is exactly the publish body minus `warnings`, and stable
    assert first.content == JSONResponse(content=published).body == again.content


@requires_pg
def test_loop_free_run_start_unaffected_by_cap_env(api, fake, monkeypatch):
    client, headers, wid = _run_ready(api, _wf(_tool("a")))
    for v in (ITER, EXEC, CONC):
        monkeypatch.setenv(v, "1")
    assert client.post(f"{BASE}/{wid}/runs", json={"input": {}}, headers=headers).status_code == 202


# --- environment variables ----------------------------------------------------------------------


@pytest.mark.parametrize("var", [ITER, EXEC, CONC])
@pytest.mark.parametrize("value", ["abc", "0", "-1", "1.5"])
def test_invalid_env_values_raise_naming_the_variable(monkeypatch, var, value):
    # Coordinator decision (LP.5 test round): a malformed cap fails, it does not fall back.
    monkeypatch.setenv(var, value)
    setting = {ITER: wr.LOOP_ITERATIONS_ENV, EXEC: wr.STEP_EXECUTIONS_ENV, CONC: wr.RUN_CONCURRENCY_ENV}[var]
    with pytest.raises(ValueError, match=var):
        wr._env_cap(setting)


@pytest.mark.parametrize("var,default", [(ITER, 100), (EXEC, 2000), (CONC, 8)])
@pytest.mark.parametrize("value", ["", "  "])
def test_blank_env_values_mean_the_default(monkeypatch, var, default, value):
    monkeypatch.setenv(var, value)
    setting = {ITER: wr.LOOP_ITERATIONS_ENV, EXEC: wr.STEP_EXECUTIONS_ENV, CONC: wr.RUN_CONCURRENCY_ENV}[var]
    assert wr._env_cap(setting) == default


@pytest.mark.parametrize("var,default", [(ITER, 100), (EXEC, 2000), (CONC, 8)])
def test_env_defaults_and_valid_values(monkeypatch, var, default):
    setting = {ITER: wr.LOOP_ITERATIONS_ENV, EXEC: wr.STEP_EXECUTIONS_ENV, CONC: wr.RUN_CONCURRENCY_ENV}[var]
    monkeypatch.delenv(var, raising=False)
    assert wr._env_cap(setting) == default and setting == (var, default)
    monkeypatch.setenv(var, "7")
    assert wr._env_cap(setting) == 7
    monkeypatch.setenv(var, "1")
    assert wr._env_cap(setting) == 1


def test_dev_env_example_lists_the_three_vars_commented_out():
    from pathlib import Path

    text_ = (Path(__file__).resolve().parents[3] / ".dev.env.example").read_text()
    for var in (ITER, EXEC, CONC):
        assert f"\n# {var}=\n" in text_
        assert f"\n{var}=" not in text_


@requires_pg
@pytest.mark.parametrize("var", [ITER, EXEC, CONC])
def test_invalid_cap_value_fails_app_startup(monkeypatch, var):
    # A malformed cap fails create_app (check_workflow_configuration), naming the variable.
    from eap_api.app import create_app

    from .run_helpers import PG_URL

    monkeypatch.setenv(var, "not-a-number")
    with pytest.raises(ValueError, match=f"{var} must be a whole number"):
        create_app(database_url=PG_URL)
