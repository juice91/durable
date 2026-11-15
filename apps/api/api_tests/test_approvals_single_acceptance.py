"""AC.15 acceptance: GET /approvals/{run_id}/{kind}/{key}, the single-approval endpoint.

Reuses the implementer's fixtures (real Postgres, fake DBOS client). Adds what test_approvals_api.py
does not pin: every inbox item (pending, decided by a person, timed out, oversized args) equals its
single read; the 404 body is one shape for wrong kind / unknown / other tenant; 422 for each
malformed part; the key length boundary; a read never changes the item.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from .conftest import get_auth_header  # noqa: F401  (kept for parity with sibling modules)
from .run_helpers import requires_pg
from .test_approvals_api import (  # noqa: F401  (pytest fixtures)
    _no_hang,
    add_tool_wait,
    api,
    fake,
    tool_url,
    waiting_step,
    waiting_tool_call,
)

pytestmark = requires_pg


def _one(client, headers, item) -> dict:
    r = client.get(f"/api/v1/approvals/{item['run_id']}/{item['kind']}/{item['approval_key']}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def test_every_inbox_item_equals_its_single_read_pending_and_decided(api, fake):
    client, app, tenant_of = api
    tenant, headers = tenant_of()
    step_run = waiting_step(client, app, tenant, headers)
    tool_run, _ = waiting_tool_call(client, app, tenant, headers, args={"to": "a@b.c"})
    add_tool_wait(app, tenant, tool_run, "call_t")
    _, event_log = app.state.run_stores
    assert event_log.append_tool_decision(
        tenant, tool_run, "call_t", "tool_approval_timed_out", {"tool_call_id": "call_t"})[0] == "created"
    assert client.post(tool_url(tool_run, "call_1", "deny"), json={"comment": "no"}, headers=headers).status_code == 200
    pending = client.get("/api/v1/approvals", headers=headers).json()["items"]
    decided = client.get("/api/v1/approvals?status=decided", headers=headers).json()["items"]
    assert [i["kind"] for i in pending] == ["step"] and len(decided) == 2
    assert {i["decision"] for i in decided} == {"denied", "timed_out"}
    for item in pending + decided:
        assert _one(client, headers, item) == item
    assert {i["run_id"] for i in pending + decided} == {str(step_run), str(tool_run)}
    timed_out = next(i for i in decided if i["decision"] == "timed_out")
    assert timed_out["decided_by"] is None and timed_out["status"] == "decided"


def test_oversized_arguments_read_back_as_truncated_like_the_inbox(api, fake):
    client, app, tenant_of = api
    tenant, headers = tenant_of()
    run_id, _ = waiting_tool_call(client, app, tenant, headers, args={"blob": "x" * 20_000})
    listed = client.get("/api/v1/approvals", headers=headers).json()["items"][0]
    assert listed["args"] == {"truncated": True}
    assert _one(client, headers, listed) == listed
    assert str(run_id) == listed["run_id"]


def test_not_found_is_one_shape_for_wrong_kind_unknown_and_other_tenant(api, fake):
    client, app, tenant_of = api
    tenant, headers = tenant_of()
    _, other = tenant_of("bob")
    step_run = waiting_step(client, app, tenant, headers)
    tool_run, _ = waiting_tool_call(client, app, tenant, headers)
    urls = [
        (f"/api/v1/approvals/{tool_run}/step/call_1", headers),  # wrong kind
        (f"/api/v1/approvals/{step_run}/tool/approve", headers),  # wrong kind
        (f"/api/v1/approvals/{tool_run}/tool/missing", headers),  # unknown key
        (f"/api/v1/approvals/{uuid.uuid4()}/tool/call_1", headers),  # unknown run
        (f"/api/v1/approvals/{tool_run}/tool/call_1", other),  # another tenant's
        (f"/api/v1/approvals/{step_run}/step/approve", other),
    ]
    bodies = []
    for url, h in urls:
        r = client.get(url, headers=h)
        assert r.status_code == 404, url
        bodies.append(r.json())
    assert all(b == {"detail": "Approval not found"} for b in bodies)


@pytest.mark.parametrize(
    "tail",
    ["not-a-uuid/tool/call_1", "{run}/TOOL/call_1", "{run}/workflow/call_1", "{run}/tool/has%20space", "{run}/tool/" + "k" * 257,
     "{run}/tool/bad%00nul", "{run}/tool/k%C3%A9y"],
)
def test_malformed_input_is_422(api, fake, tail):
    client, app, tenant_of = api
    tenant, headers = tenant_of()
    run_id, _ = waiting_tool_call(client, app, tenant, headers)
    r = client.get("/api/v1/approvals/" + tail.replace("{run}", str(run_id)), headers=headers)
    assert r.status_code == 422, (tail, r.status_code, r.text)


def test_key_of_exactly_256_characters_is_accepted_and_a_missing_one_is_404(api, fake):
    client, app, tenant_of = api
    tenant, headers = tenant_of()
    run_id, _ = waiting_tool_call(client, app, tenant, headers)
    r = client.get(f"/api/v1/approvals/{run_id}/tool/{'k' * 256}", headers=headers)
    assert r.status_code == 404 and r.json() == {"detail": "Approval not found"}


def test_credentials_are_required_and_a_read_changes_nothing(api, fake):
    client, app, tenant_of = api
    tenant, headers = tenant_of()
    run_id, _ = waiting_tool_call(client, app, tenant, headers)
    url = f"/api/v1/approvals/{run_id}/tool/call_1"
    assert client.get(url).status_code == 401
    first = client.get(url, headers=headers).json()
    assert client.get(url, headers=headers).json() == first
    assert first["status"] == "pending" and first["decision"] is None
    assert client.get("/api/v1/approvals", headers=headers).json()["items"] == [first]
    with app.state.database_engine.begin() as conn:
        n = conn.execute(text("SELECT count(*) FROM execution_events WHERE run_id = :r"), {"r": str(run_id)}).scalar()
    assert client.get(url, headers=headers).status_code == 200
    with app.state.database_engine.begin() as conn:
        assert conn.execute(text("SELECT count(*) FROM execution_events WHERE run_id = :r"), {"r": str(run_id)}).scalar() == n
