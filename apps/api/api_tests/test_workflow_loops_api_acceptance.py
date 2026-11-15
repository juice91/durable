"""Loop steps at the API since LP.11: publish, run start and startup, with no preview flag.

LP.7 shipped `loop` steps behind EAP_WORKFLOW_LOOPS_PREVIEW; LP.11 (the loop tick: cost cap and
cooperative cancel) removed the flag, so loop specs publish and start whatever the variable says,
and a leftover value never fails startup. A fake DBOS client records enqueues (no worker); the
3-iteration end-to-end run through the API is in the worker acceptance file.
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


@pytest.mark.parametrize("value", [None, "", "0", "1", "yes"])
@pytest.mark.parametrize("spec", [LOOP, NESTED, STATE_LOOP], ids=["loop", "nested", "state"])
def test_loop_specs_publish_whatever_the_old_flag_says(api, monkeypatch, value, spec):
    client, _app, tenant_of = api
    _tenant, headers = tenant_of()
    monkeypatch.delenv(FLAG, raising=False) if value is None else monkeypatch.setenv(FLAG, value)
    _wid, resp = _publish(client, headers, spec)
    assert resp.status_code == 201, resp.text


def test_a_loop_run_starts_with_the_old_flag_off(api, fake, monkeypatch):
    client, app, tenant_of = api
    tenant, headers = tenant_of()
    monkeypatch.setenv(FLAG, "0")
    wid, resp = _publish(client, headers, LOOP)
    assert resp.status_code == 201, resp.text
    for _ in range(2):
        started = client.post(f"{BASE}/{wid}/runs", json={"input": {}}, headers=headers)
        assert started.status_code == 202, started.text
    assert _runs(app, tenant) == 2 and len(fake.calls) == 2


def test_drafts_with_loops_save(api, monkeypatch):
    client, _app, tenant_of = api
    _tenant, headers = tenant_of()
    created = client.post(BASE, json={"spec": LOOP}, headers=headers)
    assert created.status_code == 201, created.text
    patched = client.patch(f"{BASE}/{created.json()['id']}", json={"spec": NESTED}, headers=headers)
    assert patched.status_code == 200, patched.text


def test_loop_free_workflows_start(api):
    client, _app, tenant_of = api
    _tenant, headers = tenant_of()
    wid = publish_demo(client, headers)
    assert client.post(f"{BASE}/{wid}/runs", json={"input": {"q": "x"}}, headers=headers).status_code == 202


@pytest.mark.parametrize("value", [None, "", "0", "1", "yes", "true", "2", "-1", "on"])
def test_any_leftover_flag_value_starts_the_app(monkeypatch, value):
    monkeypatch.delenv(FLAG, raising=False) if value is None else monkeypatch.setenv(FLAG, value)
    app = create_app(database_url=PG_URL)
    app.state.database_engine.dispose()
