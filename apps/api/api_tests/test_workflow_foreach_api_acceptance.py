"""LP.10 acceptance (tester): foreach at the API behind the loops preview flag.

Publish and run start refuse foreach (at any depth) with the flag off, before any run row or enqueue,
and allow it with EAP_WORKFLOW_LOOPS_PREVIEW=1. The end-to-end run through HTTP is in the worker
acceptance file (a real worker is needed).
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


@pytest.mark.parametrize("value", [None, "", "0"])
@pytest.mark.parametrize("spec", SPECS, ids=IDS)
def test_flag_off_refuses_foreach_at_publish(api, monkeypatch, value, spec):
    client, _app, tenant_of = api
    _tenant, headers = tenant_of()
    monkeypatch.delenv(FLAG, raising=False) if value is None else monkeypatch.setenv(FLAG, value)
    _wid, resp = _publish(client, headers, spec)
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert "the foreach step is not available yet" in detail and f"{FLAG}=1" in detail
    assert "'each'" in detail or "'in'" in detail


@pytest.mark.parametrize("spec", SPECS, ids=IDS)
def test_flag_on_publishes_foreach(api, monkeypatch, spec):
    client, _app, tenant_of = api
    _tenant, headers = tenant_of()
    monkeypatch.setenv(FLAG, "1")
    _wid, resp = _publish(client, headers, spec)
    assert resp.status_code == 201, resp.text


@pytest.mark.parametrize("spec", [FOREACH, FOREACH_IN_LOOP, LOOP_IN_FOREACH], ids=["foreach", "in-loop", "loop-in"])
def test_run_start_follows_the_flag_and_off_leaves_no_row_or_enqueue(api, fake, monkeypatch, spec):
    client, app, tenant_of = api
    tenant, headers = tenant_of()
    monkeypatch.setenv(FLAG, "1")
    wid, resp = _publish(client, headers, spec)
    assert resp.status_code == 201, resp.text
    started = client.post(f"{BASE}/{wid}/runs", json={"input": {"items": [1, 2]}}, headers=headers)
    assert started.status_code == 202, started.text
    assert _runs(app, tenant) == 1 and len(fake.calls) == 1
    monkeypatch.delenv(FLAG)
    refused = client.post(f"{BASE}/{wid}/runs", json={"input": {"items": [1]}}, headers=headers)
    assert refused.status_code == 422 and f"{FLAG}=1" in refused.json()["detail"]
    assert _runs(app, tenant) == 1 and len(fake.calls) == 1


def test_drafts_with_foreach_save_whatever_the_flag(api, monkeypatch):
    client, _app, tenant_of = api
    _tenant, headers = tenant_of()
    monkeypatch.delenv(FLAG, raising=False)
    created = client.post(BASE, json={"spec": FOREACH}, headers=headers)
    assert created.status_code == 201, created.text
    assert client.patch(f"{BASE}/{created.json()['id']}", json={"spec": LOOP_IN_FOREACH}, headers=headers).status_code == 200


def test_the_flag_does_not_widen_what_the_schema_refuses(api, monkeypatch):
    """With the flag on, depth 3 and a concurrency product over the run cap stay refused."""
    client, _app, tenant_of = api
    _tenant, headers = tenant_of()
    monkeypatch.setenv(FLAG, "1")
    deep = _wf(_foreach([_loop([_foreach([_tool("x")], sid="c")], sid="b")], sid="a"))
    assert client.post(BASE, json={"spec": deep}, headers=headers).status_code == 422
    inner = _foreach([_tool("x")], sid="in", concurrency=3)
    product = _wf(_foreach([inner], sid="out", concurrency=3))
    _wid, resp = _publish(client, headers, product)
    assert resp.status_code == 422 and "workflow_concurrency_exceeded" in resp.text


def test_the_refusal_names_the_step_but_echoes_no_spec_value(api, monkeypatch):
    client, _app, tenant_of = api
    _tenant, headers = tenant_of()
    monkeypatch.setenv(FLAG, "0")
    spec = _wf(_foreach([{**_tool("x"), "input": {"note": MARK}}]))
    _wid, resp = _publish(client, headers, spec)
    assert resp.status_code == 422 and MARK not in resp.text


def test_loop_free_workflows_and_while_until_are_unaffected_by_the_flag(api, monkeypatch):
    client, _app, tenant_of = api
    _tenant, headers = tenant_of()
    for value in ("0", "1"):
        monkeypatch.setenv(FLAG, value)
        wid = publish_demo(client, headers)
        assert client.post(f"{BASE}/{wid}/runs", json={"input": {"q": "x"}}, headers=headers).status_code == 202
    monkeypatch.setenv(FLAG, "0")
    _wid, resp = _publish(client, headers, _wf(_loop([_tool("x")])))
    assert resp.status_code == 422 and "the loop step is not available yet" in resp.json()["detail"]
