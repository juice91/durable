"""Loop specs at the API gate (LP.1 review fixes; LP.11).

Draft save (create and patch) validates the full WorkflowSpec, so LP.1's rejections are 422 there.
Since LP.11 (the loop tick: cost cap and cooperative cancel) `loop` and `foreach` steps and `limits`
publish and start for good, with no preview flag (EAP_WORKFLOW_LOOPS_PREVIEW is gone and ignored).
Evaluate steps publish and start since LP.14 (LP.16 removed the API's runtime-pending
gate); all/any/not branch conditions publish since LP.7. What the platform
genuinely does not run is still refused: approval or parallel steps in a loop body and loops inside
a parallel branch (422 at draft save, so never published), a judge model off the allowlist and a
loop over the iteration cap (422 at publish and again at run start).
"""

import pytest

from .run_helpers import requires_pg
from .test_workflow_runs_api import _no_hang, api, fake  # noqa: F401  (fixtures for run start)

pytestmark = requires_pg
BASE = "/api/v1/workflows"


def _ref(path):
    return {"$ref": path}


def _tool(sid, nxt=None, **inp):
    return {"id": sid, "type": "tool", "tool_id": "clock.now", "input": inp, "next": nxt}


def _wf(*steps):
    return {"name": "Loops", "input_schema": {"type": "object"}, "start": steps[0]["id"], "steps": list(steps)}


def _loop(body, **kw):
    kw.setdefault("until", {"op": "exists", "left": _ref("input.a")})
    return {"id": "lp", "type": "loop", "max_iterations": 3, "body": {"start": body[0]["id"], "steps": body}, **kw}


VALID_LOOP = _wf(_loop([_tool("x")]))
COMBINATOR = _wf(
    {"id": "b", "type": "branch", "default": "x",
     "cases": [{"when": {"all": [{"op": "exists", "left": _ref("input.a")}]}, "next": "x"}]},
    _tool("x"),
)

INVALID = [
    (_wf(_loop([{"id": "g", "type": "approval", "prompt": "ok?", "timeout_hours": 1}])), "loop_body_step_not_allowed"),
    (_wf(_loop([_tool("x", None, i=_ref("loop.lp.item"))])), "'.item' exists only in a foreach"),
    (_wf(_loop([{"id": "br", "type": "branch", "default": "d",
                 "cases": [{"when": {"op": "exists", "left": _ref("input.a")}, "next": "c"}]},
                _tool("c", "d"), _tool("d")], until={"op": "exists", "left": _ref("steps.c.output")})),
     "loop_ref_not_on_every_path"),
]


@pytest.mark.parametrize("spec, message", INVALID)
def test_loop_rejections_at_create_and_patch(client, auth_header, spec, message):
    resp = client.post(BASE, json={"spec": spec}, headers=auth_header)
    assert resp.status_code == 422, resp.text
    assert message in resp.text and "step '" in resp.text
    created = client.post(BASE, json={"spec": _wf(_tool("t"))}, headers=auth_header)
    assert created.status_code == 201, created.text
    resp = client.patch(f"{BASE}/{created.json()['id']}", json={"spec": spec}, headers=auth_header)
    assert resp.status_code == 422, resp.text
    assert message in resp.text


FOREACH = _wf({"id": "each", "type": "foreach", "items": _ref("input.list"), "max_items": 3,
               "body": {"start": "x", "steps": [_tool("x")]}})


def _publish(client, auth_header, spec):
    created = client.post(BASE, json={"spec": spec}, headers=auth_header)
    assert created.status_code == 201, created.text
    return client.post(f"{BASE}/{created.json()['id']}/versions", headers=auth_header)


EVALUATE = _wf(
    {**_tool("x", "ev")},
    {"id": "ev", "type": "evaluate", "value": _ref("steps.x.output"),
     "evaluators": [{"kind": "contains", "value": "a"}]},
)
NESTED_FOREACH = _wf(_loop([{"id": "each", "type": "foreach", "items": _ref("input.list"), "max_items": 3,
                             "body": {"start": "y", "steps": [_tool("y")]}}]))
LIMITS = {**VALID_LOOP, "limits": {"max_cost_usd": "1"}}
LOOP_FREE_LIMITS = {**_wf(_tool("t")), "limits": {"max_cost_usd": "1"}}


@pytest.mark.parametrize("value", [None, "", "0", "1", "yes"])
@pytest.mark.parametrize(
    "spec", [VALID_LOOP, FOREACH, NESTED_FOREACH, LIMITS, LOOP_FREE_LIMITS],
    ids=["loop", "foreach", "nested-foreach", "loop-limits", "limits"],
)
def test_loops_and_limits_publish_without_any_flag(client, auth_header, monkeypatch, value, spec):
    """LP.11 removed the loops preview: loop and foreach steps and `limits` publish whatever
    EAP_WORKFLOW_LOOPS_PREVIEW says (the variable is no longer read)."""
    if value is None:
        monkeypatch.delenv("EAP_WORKFLOW_LOOPS_PREVIEW", raising=False)
    else:
        monkeypatch.setenv("EAP_WORKFLOW_LOOPS_PREVIEW", value)
    resp = _publish(client, auth_header, spec)
    assert resp.status_code == 201, resp.text


def test_the_preview_flag_is_gone(monkeypatch):
    """No startup check reads it any more, so a leftover value never fails startup."""
    import eap_api.routes.workflows as routes

    assert not hasattr(routes, "LOOPS_PREVIEW_ENV") and not hasattr(routes, "loops_preview_enabled")
    monkeypatch.setenv("EAP_WORKFLOW_LOOPS_PREVIEW", "yes")
    routes.check_workflow_configuration()


EVALUATE_IN_LOOP = _wf(_loop([_tool("x", "ev"), EVALUATE["steps"][1]]))
JUDGE = {"kind": "llm_judge", "rubric": "ok", "model": {"provider": "test", "name": "judge"}}
JUDGE_EVALUATE = _wf(
    {**_tool("x", "ev")},
    {"id": "ev", "type": "evaluate", "value": _ref("steps.x.output"), "evaluators": [JUDGE]},
)


@pytest.mark.parametrize("spec", [EVALUATE, EVALUATE_IN_LOOP], ids=["top-level", "in-a-loop"])
def test_evaluate_publishes(client, auth_header, spec):
    """LP.14 landed the evaluate runtime, so the gate is gone: evaluate steps publish at any depth."""
    import eap_api.routes.workflows as routes

    assert not hasattr(routes, "runtime_pending_problems") and not hasattr(routes, "LOOP_RUNTIME_PENDING")
    resp = _publish(client, auth_header, spec)
    assert resp.status_code == 201, resp.text


def test_combinator_branches_publish(client, auth_header):
    """all/any/not in branch conditions need no loop runtime: open since LP.7."""
    resp = _publish(client, auth_header, COMBINATOR)
    assert resp.status_code == 201, resp.text


UNSUPPORTED_BODIES = [
    (_wf(_loop([{"id": "g", "type": "approval", "prompt": "ok?", "timeout_hours": 1}])), "loop_body_step_not_allowed"),
    (_wf(_loop([{"id": "par", "type": "parallel", "branches": ["y", "z"], "join": "j"},
                _tool("y", "j"), _tool("z", "j"), _tool("j")])),
     "loop_body_step_not_allowed"),
    (_wf({"id": "par", "type": "parallel", "branches": ["lp", "z"], "join": "j"},
         {**_loop([_tool("x")]), "next": "j"}, _tool("z", "j"), _tool("j")),
     "loop_in_parallel_not_allowed"),
]


@pytest.mark.parametrize("spec, code", UNSUPPORTED_BODIES, ids=["approval-in-body", "parallel-in-body",
                                                               "loop-in-parallel"])
def test_unsupported_shapes_are_still_refused(client, auth_header, spec, code):
    """Opening the gate let no unsupported shape through: the spec itself refuses them, so they
    never reach publish."""
    resp = client.post(BASE, json={"spec": spec}, headers=auth_header)
    assert resp.status_code == 422, resp.text
    assert code in resp.text


def test_publish_refuses_a_judge_model_off_the_allowlist(client, auth_header, monkeypatch):
    monkeypatch.setenv("EAP_ALLOWED_MODELS", "test:echo")
    resp = _publish(client, auth_header, JUDGE_EVALUATE)
    assert resp.status_code == 422, resp.text
    assert "step 'ev': evaluators.0 (llm_judge): model 'test:judge' is not allowed" in resp.json()["detail"]


def test_run_start_accepts_an_evaluate_step_and_refuses_what_it_cannot_run(api, fake, monkeypatch):
    """At run start an evaluate step passes (202, enqueued), while a judge model taken off the
    allowlist since publish is refused with 422 before any run row or enqueue."""
    client, _, tenant_of = api
    _, headers = tenant_of()
    created = client.post(BASE, json={"spec": JUDGE_EVALUATE}, headers=headers)
    workflow_id = created.json()["id"]
    assert client.post(f"{BASE}/{workflow_id}/versions", headers=headers).status_code == 201
    ok = client.post(f"{BASE}/{workflow_id}/runs", json={"input": {}}, headers=headers)
    assert ok.status_code == 202, ok.text
    enqueued = len(fake.calls)
    monkeypatch.setenv("EAP_ALLOWED_MODELS", "test:echo")
    resp = client.post(f"{BASE}/{workflow_id}/runs", json={"input": {}}, headers=headers)
    assert resp.status_code == 422, resp.text
    assert "step 'ev': evaluators.0 (llm_judge): model 'test:judge' is not allowed" in resp.json()["detail"]
    assert len(fake.calls) == enqueued
