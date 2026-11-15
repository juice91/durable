"""Loop specs at the API gate (LP.1 review fixes; LP.11).

Draft save (create and patch) validates the full WorkflowSpec, so LP.1's rejections are 422 there.
Since LP.11 (the loop tick: cost cap and cooperative cancel) `loop` and `foreach` steps and `limits`
publish and start for good, with no preview flag (EAP_WORKFLOW_LOOPS_PREVIEW is gone and ignored).
Evaluate steps stay refused until LP.14 (`runtime_pending_problems`); all/any/not branch conditions
publish since LP.7.
"""

import pytest

from .run_helpers import requires_pg

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


@pytest.mark.parametrize("spec", [EVALUATE, _wf(_loop([_tool("x", "ev"), EVALUATE["steps"][1]]))],
                         ids=["top-level", "in-a-loop"])
def test_evaluate_stays_refused_until_lp14(client, auth_header, spec):
    resp = _publish(client, auth_header, spec)
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert "step 'ev': the evaluate step" in detail and "workflow loops runtime has not landed" in detail
    assert "step 'lp'" not in detail  # the loop itself passes the gate


def test_combinator_branches_publish(client, auth_header):
    """all/any/not in branch conditions need no loop runtime: open since LP.7."""
    resp = _publish(client, auth_header, COMBINATOR)
    assert resp.status_code == 201, resp.text


def test_run_start_refuses_an_evaluate_step(client, auth_header, monkeypatch):
    """The gate holds at run start too: a version with an evaluate step that got published anyway
    (the gate patched out here) does not start (422 before any run row or enqueue)."""
    import eap_api.routes.workflows as routes

    created = client.post(BASE, json={"spec": EVALUATE}, headers=auth_header)
    workflow_id = created.json()["id"]
    with monkeypatch.context() as patched:
        patched.setattr(routes, "runtime_pending_problems", lambda spec: [])
        assert client.post(f"{BASE}/{workflow_id}/versions", headers=auth_header).status_code == 201
    resp = client.post(f"{BASE}/{workflow_id}/runs", json={"input": {}}, headers=auth_header)
    assert resp.status_code == 422, resp.text
    assert "step 'ev': the evaluate step" in resp.json()["detail"]
