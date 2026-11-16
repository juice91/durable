"""Workflow schedules (parity round 2, D2): create/list/patch/delete, cron + timezone + input
validation against the latest published spec, tenant scoping, paging."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from eap_api.app import create_app
from eap_api.launcher import DBOSRunLauncher, LauncherSettings, get_run_launcher

from .conftest import get_auth_header
from .run_helpers import PG_URL, cleanup_tenant, hard_timeout, requires_pg
from .test_workflow_runs_api import FakeClient, publish_demo

pytestmark = requires_pg
API = "/api/v1"


@pytest.fixture(autouse=True)
def _no_hang():
    with hard_timeout(60):
        yield


@pytest.fixture
def api():
    app = create_app(database_url=PG_URL)
    fake = FakeClient()
    app.dependency_overrides[get_run_launcher] = lambda: DBOSRunLauncher(
        LauncherSettings("postgresql://unused/x", "dbos", "m1-test"), client=fake
    )
    tenants: list[str] = []

    def tenant_of(user: str = "alice") -> tuple[str, dict[str, str]]:
        t = f"t{uuid.uuid4().hex[:10]}"
        tenants.append(t)
        return t, get_auth_header(t, user)

    with TestClient(app) as client:
        yield client, app, tenant_of
    engine = app.state.database_engine
    for t in tenants:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM workflow_schedules WHERE tenant_id = :t"), {"t": t})
        cleanup_tenant(engine, t)
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM workflow_versions WHERE tenant_id = :t"), {"t": t})
            conn.execute(text("DELETE FROM workflows WHERE tenant_id = :t"), {"t": t})
    engine.dispose()


def make(client, h, wf, name="hourly", **over):
    body = {"name": name, "cron": "5 * * * *", "timezone": "Asia/Taipei", "input": {"q": "x"}, **over}
    return client.post(f"{API}/workflows/{wf}/schedules", json=body, headers=h)


def test_create_schedule_computes_next_fire_in_timezone(api):
    client, _, tenant_of = api
    _, h = tenant_of()
    wf = publish_demo(client, h)
    r = make(client, h, wf)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["next_fire_at"].endswith("Z") and body["active"] is True and body["last_error"] is None
    assert body["workflow_id"] == wf and body["workflow_name"] == "Triage" and body["timezone"] == "Asia/Taipei"
    nxt = datetime.fromisoformat(body["next_fire_at"].replace("Z", "+00:00"))
    assert nxt > datetime.now(timezone.utc) and nxt.minute == 5  # Taipei is UTC+8: whole hours
    # the default timezone is UTC
    r2 = client.post(f"{API}/workflows/{wf}/schedules", json={"name": "d", "cron": "0 9 * * 1", "input": {"q": "x"}}, headers=h)
    assert r2.status_code == 201 and r2.json()["timezone"] == "UTC"
    assert datetime.fromisoformat(r2.json()["next_fire_at"].replace("Z", "+00:00")).hour == 9


def test_create_rejects_bad_cron_timezone_input_name_and_unpublished(api):
    client, _, tenant_of = api
    _, h = tenant_of()
    wf = publish_demo(client, h)
    for cron in ("x", "* * * *", "* * * * * *", "61 * * * *"):
        r = make(client, h, wf, cron=cron)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_cron", (cron, r.text)
    r = make(client, h, wf, timezone="Mars/Base")
    assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_timezone"
    r = make(client, h, wf, input={"wrong": 1})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "input_schema_errors"
    assert make(client, h, wf).status_code == 201
    r = make(client, h, wf)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "schedule_name_taken"
    draft = client.post(f"{API}/workflows", json={"spec": {
        "name": "Draft", "input_schema": {"type": "object"}, "start": "a",
        "steps": [{"id": "a", "type": "tool", "tool_id": "clock.now", "input": {}}]}}, headers=h).json()["id"]
    assert make(client, h, draft, input={}).status_code == 404  # no published version
    assert make(client, h, str(uuid.uuid4())).status_code == 404


def test_schedules_are_tenant_scoped(api):
    client, _, tenant_of = api
    _, h = tenant_of()
    _, other = tenant_of("mallory")
    wf = publish_demo(client, h)
    sid = make(client, h, wf).json()["id"]
    assert client.patch(f"{API}/workflows/schedules/{sid}", json={"active": False}, headers=other).status_code == 404
    assert client.delete(f"{API}/workflows/schedules/{sid}", headers=other).status_code == 404
    assert client.get(f"{API}/workflows/{wf}/schedules", headers=other).status_code == 404
    assert make(client, other, wf).status_code == 404
    assert client.get(f"{API}/workflows/schedules", headers=other).json()["items"] == []
    assert [i["id"] for i in client.get(f"{API}/workflows/{wf}/schedules", headers=h).json()["items"]] == [sid]
    assert client.delete(f"{API}/workflows/schedules/{sid}", headers=h).status_code == 204
    assert client.delete(f"{API}/workflows/schedules/{sid}", headers=h).status_code == 404


def test_patch_active_false_then_true_clears_last_error(api):
    client, app, tenant_of = api
    tenant, h = tenant_of()
    wf = publish_demo(client, h)
    s = make(client, h, wf).json()
    with app.state.database_engine.begin() as conn:  # what the tick does when it stops a schedule
        conn.execute(text("UPDATE workflow_schedules SET active = false, last_error = 'input no longer valid',"
                          " next_fire_at = now() - interval '1 day' WHERE id = :i"), {"i": s["id"]})
    r = client.patch(f"{API}/workflows/schedules/{s['id']}", json={"active": True}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["active"] is True and r.json()["last_error"] is None
    assert datetime.fromisoformat(r.json()["next_fire_at"].replace("Z", "+00:00")) > datetime.now(timezone.utc)
    r = client.patch(f"{API}/workflows/schedules/{s['id']}", json={"active": False}, headers=h)
    assert r.json()["active"] is False
    r = client.patch(f"{API}/workflows/schedules/{s['id']}", json={"cron": "0 0 * * *", "timezone": "UTC"}, headers=h)
    nxt = datetime.fromisoformat(r.json()["next_fire_at"].replace("Z", "+00:00"))
    assert (nxt.hour, nxt.minute) == (0, 0)
    for bad, code in (({"cron": "nope"}, "invalid_cron"), ({"timezone": "Nope/Zone"}, "invalid_timezone"),
                      ({"input": {"wrong": 1}}, "input_schema_errors")):
        r = client.patch(f"{API}/workflows/schedules/{s['id']}", json=bad, headers=h)
        assert r.status_code == 422 and r.json()["detail"]["code"] == code, (bad, r.text)
    assert client.patch(f"{API}/workflows/schedules/{s['id']}", json={"input": {"q": "new"}, "name": "renamed"},
                        headers=h).json()["input"] == {"q": "new"}
    other = make(client, h, wf, name="second").json()["id"]
    r = client.patch(f"{API}/workflows/schedules/{other}", json={"name": "renamed"}, headers=h)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "schedule_name_taken"


def test_global_list_includes_workflow_name_and_pages(api):
    client, _, tenant_of = api
    _, h = tenant_of()
    wf = publish_demo(client, h)
    ids = [make(client, h, wf, name=f"s{i}").json()["id"] for i in range(5)]
    first = client.get(f"{API}/workflows/schedules?limit=2", headers=h).json()
    assert [i["id"] for i in first["items"]] == ids[::-1][:2] and first["next_before"]
    assert first["items"][0]["workflow_name"] == "Triage"
    seen = [i["id"] for i in first["items"]]
    nxt = first["next_before"]
    while nxt:
        page = client.get(f"{API}/workflows/schedules", params={"limit": 2, "before": nxt}, headers=h).json()
        seen += [i["id"] for i in page["items"]]
        nxt = page["next_before"]
    assert seen == ids[::-1]
    assert client.get(f"{API}/workflows/schedules?before=garbage", headers=h).status_code == 422
