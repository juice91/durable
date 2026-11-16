"""Foreach at the API since LP.11 (LP.10 shipped it behind the removed loops preview flag).

Publish and run start accept foreach at any depth whatever EAP_WORKFLOW_LOOPS_PREVIEW says (the
variable is no longer read); the schema's and the platform's caps still refuse what they refused.
The end-to-end run through HTTP is in the worker acceptance file (a real worker is needed).
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from .run_helpers import requires_pg
from .test_workflow_runs_api import api, fake, publish_demo  # noqa: F401 - fixtures

pytestmark = requires_pg
BASE = "/api/v1/workflows"
FLAG = "EAP_WORKFLOW_LOOPS_PREVIEW"
MARK = "SECRET-MARK-4c1"


def _ref(path):
    return {"$ref": path}


def _tool(sid):
    return {"id": sid, "type": "tool", "tool_id": "clock.now", "input": {}}


def _wf(*steps):
    return {"name": "Each", "input_schema": {"type": "object"}, "start": steps[0]["id"], "steps": list(steps)}


def _foreach(body, sid="each", **kw):
    return {"id": sid, "type": "foreach", "items": _ref("input.items"), "max_items": 3,
            "body": {"start": body[0]["id"], "steps": body}, **kw}


def _loop(body, sid="lp"):
    return {"id": sid, "type": "loop", "max_iterations": 2, "until": {"op": "exists", "left": _ref(f"loop.{sid}.index")},
            "body": {"start": body[0]["id"], "steps": body}}


FOREACH = _wf(_foreach([_tool("x")]))
FOREACH_COLLECT = _wf(_foreach([_tool("x")], concurrency=3, on_item_error="collect"))
FOREACH_IN_LOOP = _wf(_loop([_foreach([_tool("x")], sid="in")]))
LOOP_IN_FOREACH = _wf(_foreach([_loop([_tool("x")], sid="in")]))
SPECS = [FOREACH, FOREACH_COLLECT, FOREACH_IN_LOOP, LOOP_IN_FOREACH]
IDS = ["foreach", "collect", "foreach-in-loop", "loop-in-foreach"]


def _publish(client, headers, spec):
    created = client.post(BASE, json={"spec": spec}, headers=headers)
    assert created.status_code == 201, created.text
    wid = created.json()["id"]
    return wid, client.post(f"{BASE}/{wid}/versions", headers=headers)


def _runs(app, tenant):
    with app.state.database_engine.connect() as conn:
        return conn.execute(text("SELECT count(*) FROM runs WHERE tenant_id = :t"), {"t": tenant}).scalar()


@pytest.mark.parametrize("value", [None, "", "0", "1"])
@pytest.mark.parametrize("spec", SPECS, ids=IDS)
def test_foreach_publishes_whatever_the_old_flag_says(api, monkeypatch, value, spec):
    client, _app, tenant_of = api
    _tenant, headers = tenant_of()
    monkeypatch.delenv(FLAG, raising=False) if value is None else monkeypatch.setenv(FLAG, value)
    _wid, resp = _publish(client, headers, spec)
    assert resp.status_code == 201, resp.text


@pytest.mark.parametrize("spec", [FOREACH, FOREACH_IN_LOOP, LOOP_IN_FOREACH], ids=["foreach", "in-loop", "loop-in"])
def test_foreach_runs_start(api, fake, monkeypatch, spec):
    client, app, tenant_of = api
    tenant, headers = tenant_of()
    monkeypatch.delenv(FLAG, raising=False)
    wid, resp = _publish(client, headers, spec)
    assert resp.status_code == 201, resp.text
    started = client.post(f"{BASE}/{wid}/runs", json={"input": {"items": [1, 2]}}, headers=headers)
    assert started.status_code == 202, started.text
    assert _runs(app, tenant) == 1 and len(fake.calls) == 1


def test_drafts_with_foreach_save(api):
    client, _app, tenant_of = api
    _tenant, headers = tenant_of()
    created = client.post(BASE, json={"spec": FOREACH}, headers=headers)
    assert created.status_code == 201, created.text
    assert client.patch(f"{BASE}/{created.json()['id']}", json={"spec": LOOP_IN_FOREACH}, headers=headers).status_code == 200


def test_the_open_gate_does_not_widen_what_the_schema_refuses(api):
    """Depth 3 and a concurrency product over the run cap stay refused."""
    client, _app, tenant_of = api
    _tenant, headers = tenant_of()
    deep = _wf(_foreach([_loop([_foreach([_tool("x")], sid="c")], sid="b")], sid="a"))
    assert client.post(BASE, json={"spec": deep}, headers=headers).status_code == 422
    inner = _foreach([_tool("x")], sid="in", concurrency=3)
    product = _wf(_foreach([inner], sid="out", concurrency=3))
    _wid, resp = _publish(client, headers, product)
    assert resp.status_code == 422 and "workflow_concurrency_exceeded" in resp.text
    assert MARK not in resp.text
