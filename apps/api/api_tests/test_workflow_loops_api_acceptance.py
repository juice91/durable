"""LP.7 acceptance (tester) for the loops preview gate at the API: publish, run start, startup.

The implementer's test_workflow_loops_gate.py stays as is. A fake DBOS client records enqueues (no worker);
the 3-iteration end-to-end run through the API is in the worker acceptance file.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from eap_api.app import create_app

from .run_helpers import requires_pg
from .test_workflow_runs_api import PG_URL, api, fake, publish_demo  # noqa: F401 - fixtures

pytestmark = requires_pg
BASE = "/api/v1/workflows"
FLAG = "EAP_WORKFLOW_LOOPS_PREVIEW"
MARK = "SECRET-MARK-9f3"


def _ref(path):
    return {"$ref": path}


def _tool(sid, **kw):
    return {"id": sid, "type": "tool", "tool_id": "clock.now", "input": {}, **kw}


def _wf(*steps):
    return {"name": "Loops", "input_schema": {"type": "object"}, "start": steps[0]["id"], "steps": list(steps)}


def _loop(body, lid="lp", **kw):
    kw.setdefault("until", {"op": "exists", "left": _ref(f"loop.{lid}.index")})
    return {"id": lid, "type": "loop", "max_iterations": 3, "body": {"start": body[0]["id"], "steps": body}, **kw}


LOOP = _wf(_loop([_tool("x")]))
NESTED = _wf(_loop([_loop([_tool("x")], lid="in")]))
STATE_LOOP = _wf(_loop([_tool("x")], state={"s": MARK}))


def _publish(client, headers, spec):
    created = client.post(BASE, json={"spec": spec}, headers=headers)
    assert created.status_code == 201, created.text
    wid = created.json()["id"]
    return wid, client.post(f"{BASE}/{wid}/versions", headers=headers)


def _runs(app, tenant):
    with app.state.database_engine.connect() as conn:
        return conn.execute(text("SELECT count(*) FROM runs WHERE tenant_id = :t"), {"t": tenant}).scalar()


@pytest.mark.parametrize("value", [None, "", "0", "  "])
@pytest.mark.parametrize("spec", [LOOP, NESTED], ids=["loop", "nested"])
def test_flag_off_refuses_loop_specs_at_publish(api, monkeypatch, value, spec):
    client, _app, tenant_of = api
    _tenant, headers = tenant_of()
    monkeypatch.delenv(FLAG, raising=False) if value is None else monkeypatch.setenv(FLAG, value)
    _wid, resp = _publish(client, headers, spec)
    assert resp.status_code == 422, resp.text
    assert "the loop step is not available yet" in resp.json()["detail"] and f"{FLAG}=1" in resp.json()["detail"]


@pytest.mark.parametrize("value", ["1", " 1 "])
@pytest.mark.parametrize("spec", [LOOP, NESTED], ids=["loop", "nested"])
def test_flag_on_publishes_loop_specs(api, monkeypatch, value, spec):
    client, _app, tenant_of = api
    _tenant, headers = tenant_of()
    monkeypatch.setenv(FLAG, value)
    _wid, resp = _publish(client, headers, spec)
    assert resp.status_code == 201, resp.text


def test_flag_on_starts_a_loop_run_and_off_refuses_before_any_row(api, fake, monkeypatch):
    client, app, tenant_of = api
    tenant, headers = tenant_of()
    monkeypatch.setenv(FLAG, "1")
    wid, resp = _publish(client, headers, LOOP)
    assert resp.status_code == 201, resp.text
    started = client.post(f"{BASE}/{wid}/runs", json={"input": {}}, headers=headers)
    assert started.status_code == 202, started.text
    assert _runs(app, tenant) == 1 and len(fake.calls) == 1
    monkeypatch.setenv(FLAG, "0")
    refused = client.post(f"{BASE}/{wid}/runs", json={"input": {}}, headers=headers)
    assert refused.status_code == 422 and f"{FLAG}=1" in refused.json()["detail"]
    assert _runs(app, tenant) == 1 and len(fake.calls) == 1  # no run row, no enqueue


def test_drafts_with_loops_save_regardless_of_the_flag(api, monkeypatch):
    client, _app, tenant_of = api
    _tenant, headers = tenant_of()
    monkeypatch.delenv(FLAG, raising=False)
    created = client.post(BASE, json={"spec": LOOP}, headers=headers)
    assert created.status_code == 201, created.text
    patched = client.patch(f"{BASE}/{created.json()['id']}", json={"spec": NESTED}, headers=headers)
    assert patched.status_code == 200, patched.text


def test_loop_free_workflows_are_unaffected_by_the_flag(api, monkeypatch):
    client, _app, tenant_of = api
    _tenant, headers = tenant_of()
    for value in ("0", "1"):
        monkeypatch.setenv(FLAG, value)
        wid = publish_demo(client, headers)
        assert client.post(f"{BASE}/{wid}/runs", json={"input": {"q": "x"}}, headers=headers).status_code == 202


def test_refusal_does_not_echo_spec_values(api, monkeypatch):
    client, _app, tenant_of = api
    _tenant, headers = tenant_of()
    monkeypatch.setenv(FLAG, "0")
    _wid, resp = _publish(client, headers, STATE_LOOP)
    assert resp.status_code == 422 and MARK not in resp.text


@pytest.mark.parametrize("value", ["yes", "true", "2", "-1", "on"])
def test_a_malformed_flag_fails_app_startup(monkeypatch, value):
    monkeypatch.setenv(FLAG, value)
    with pytest.raises(ValueError, match=f"{FLAG} must be 0 or 1"):
        create_app(database_url=PG_URL)


@pytest.mark.parametrize("value", [None, "", "0", "1"])
def test_a_valid_flag_starts_the_app(monkeypatch, value):
    monkeypatch.delenv(FLAG, raising=False) if value is None else monkeypatch.setenv(FLAG, value)
    app = create_app(database_url=PG_URL)
    app.state.database_engine.dispose()
