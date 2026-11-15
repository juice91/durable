"""Loop specs at the API before the loops runtime lands (LP.1 review fixes).

Draft save (create and patch) validates the full WorkflowSpec, so LP.1's rejections are 422 there.
A valid loop spec saves as a draft but is refused at publish (`runtime_pending_problems`) until the
worker can run it.
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


@pytest.mark.parametrize("spec, step", [(VALID_LOOP, "lp"), (COMBINATOR, "b")])
def test_valid_loop_specs_save_but_publish_is_refused_until_the_runtime_lands(client, auth_header, spec, step):
    created = client.post(BASE, json={"spec": spec}, headers=auth_header)
    assert created.status_code == 201, created.text
    resp = client.post(f"{BASE}/{created.json()['id']}/versions", headers=auth_header)
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert f"step '{step}'" in detail and "workflow loops runtime has not landed" in detail


def test_limits_are_refused_at_publish_too(client, auth_header):
    created = client.post(BASE, json={"spec": {**_wf(_tool("t")), "limits": {"max_cost_usd": "1"}}},
                          headers=auth_header)
    assert created.status_code == 201, created.text
    resp = client.post(f"{BASE}/{created.json()['id']}/versions", headers=auth_header)
    assert resp.status_code == 422 and "limits is not available yet" in resp.json()["detail"]
