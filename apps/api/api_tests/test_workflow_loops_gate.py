"""Loop specs at the API before the loops runtime lands (LP.1 review fixes).

Draft save (create and patch) validates the full WorkflowSpec, so LP.1's rejections are 422 there.
A valid foreach spec saves as a draft but is refused at publish (`runtime_pending_problems`) until the
worker can run it (LP.10). `loop` steps publish only with the loops preview
(EAP_WORKFLOW_LOOPS_PREVIEW=1) until LP.11; all/any/not branch conditions publish since LP.7.
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


@pytest.mark.parametrize("spec, step", [(FOREACH, "each")])
def test_valid_loop_specs_save_but_publish_is_refused_until_the_runtime_lands(client, auth_header, spec, step):
    created = client.post(BASE, json={"spec": spec}, headers=auth_header)
    assert created.status_code == 201, created.text
    resp = client.post(f"{BASE}/{created.json()['id']}/versions", headers=auth_header)
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert f"step '{step}'" in detail and "workflow loops runtime has not landed" in detail


def _publish(client, auth_header, spec):
    created = client.post(BASE, json={"spec": spec}, headers=auth_header)
    assert created.status_code == 201, created.text
    return client.post(f"{BASE}/{created.json()['id']}/versions", headers=auth_header)


@pytest.mark.parametrize("value", [None, "", "0"])
def test_loop_steps_are_refused_without_the_loops_preview(client, auth_header, monkeypatch, value):
    """LP.7 runs `loop` steps, but without the cost cap and cooperative cancel (LP.11), so they
    publish only with EAP_WORKFLOW_LOOPS_PREVIEW=1."""
    if value is None:
        monkeypatch.delenv("EAP_WORKFLOW_LOOPS_PREVIEW", raising=False)
    else:
        monkeypatch.setenv("EAP_WORKFLOW_LOOPS_PREVIEW", value)
    resp = _publish(client, auth_header, VALID_LOOP)
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert "step 'lp'" in detail and "EAP_WORKFLOW_LOOPS_PREVIEW=1" in detail


def test_loop_steps_publish_with_the_loops_preview(client, auth_header, monkeypatch):
    monkeypatch.setenv("EAP_WORKFLOW_LOOPS_PREVIEW", "1")
    resp = _publish(client, auth_header, VALID_LOOP)
    assert resp.status_code == 201, resp.text


def test_foreach_stays_refused_with_the_loops_preview(client, auth_header, monkeypatch):
    monkeypatch.setenv("EAP_WORKFLOW_LOOPS_PREVIEW", "1")
    resp = _publish(client, auth_header, FOREACH)
    assert resp.status_code == 422 and "workflow loops runtime has not landed" in resp.json()["detail"]


def test_combinator_branches_publish_without_the_preview(client, auth_header, monkeypatch):
    """all/any/not in branch conditions need no loop runtime: open since LP.7."""
    monkeypatch.delenv("EAP_WORKFLOW_LOOPS_PREVIEW", raising=False)
    resp = _publish(client, auth_header, COMBINATOR)
    assert resp.status_code == 201, resp.text


def test_a_malformed_loops_preview_fails_startup(monkeypatch):
    from eap_api.routes.workflows import check_workflow_configuration

    monkeypatch.setenv("EAP_WORKFLOW_LOOPS_PREVIEW", "yes")
    with pytest.raises(ValueError, match="EAP_WORKFLOW_LOOPS_PREVIEW must be 0 or 1"):
        check_workflow_configuration()


def test_limits_are_refused_at_publish_too(client, auth_header):
    created = client.post(BASE, json={"spec": {**_wf(_tool("t")), "limits": {"max_cost_usd": "1"}}},
                          headers=auth_header)
    assert created.status_code == 201, created.text
    resp = client.post(f"{BASE}/{created.json()['id']}/versions", headers=auth_header)
    assert resp.status_code == 422 and "limits is not available yet" in resp.json()["detail"]


def test_run_start_refuses_a_loop_without_the_loops_preview(client, auth_header, monkeypatch):
    """The gate holds at run start too: a version published under the preview does not start
    once it is off (422 before any run row or enqueue)."""
    monkeypatch.setenv("EAP_WORKFLOW_LOOPS_PREVIEW", "1")
    created = client.post(BASE, json={"spec": VALID_LOOP}, headers=auth_header)
    workflow_id = created.json()["id"]
    assert client.post(f"{BASE}/{workflow_id}/versions", headers=auth_header).status_code == 201
    monkeypatch.setenv("EAP_WORKFLOW_LOOPS_PREVIEW", "0")
    resp = client.post(f"{BASE}/{workflow_id}/runs", json={"input": {}}, headers=auth_header)
    assert resp.status_code == 422, resp.text
    assert "EAP_WORKFLOW_LOOPS_PREVIEW=1" in resp.json()["detail"]
