"""W2.1: input steps (a step with a `resume_schema`) are answered with data through
POST /workflows/runs/{run}/steps/{step}/resume. Validation against the recorded schema (422, never
echoing values), the audit of the decision (who, when, what), idempotence (409) and tenant scoping
(404). Approval steps stay approve/reject. No worker: the waiting event is written as the worker does.
"""

from __future__ import annotations

import json
import uuid

import pytest

from .run_helpers import requires_pg
from .test_approvals_api import _deadline, api, fake, waiting_step  # noqa: F401  (fixtures by name)

pytestmark = requires_pg

SCHEMA = {
    "type": "object",
    "properties": {
        "email": {"type": "string"},
        "count": {"type": "integer", "minimum": 1},
        "tier": {"enum": ["free", "pro"]},
        "address": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
    },
    "required": ["email", "count"],
    "additionalProperties": False,
}
GOOD = {"email": "a@example.com", "count": 2, "tier": "pro", "address": {"city": "Oslo"}}


def waiting_input(client, app, tenant, headers, step_id="ask", schema=None):
    run_id = waiting_step(client, app, tenant, headers, step_id="__unused")
    _, event_log = app.state.run_stores
    event_log.append_approval_waiting(
        tenant, run_id, "step", step_id,
        {"step_id": step_id, "prompt": "Details?", "timeout_hours": 1, "workflow_id": str(run_id),
         "deadline": _deadline(), "resume_schema": SCHEMA if schema is None else schema},
    )
    return run_id


def url(run_id, step="ask", action="resume"):
    return f"/api/v1/workflows/runs/{run_id}/steps/{step}/{action}"


def test_resume_records_an_audited_decision_and_wakes_the_run(api, fake):
    client, app, tenant_of = api
    tenant, headers = tenant_of("alice")
    run_id = waiting_input(client, app, tenant, headers)
    r = client.post(url(run_id), json={"data": GOOD, "comment": "from the call"}, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["decision"] == "approved" and body["decided_by"] == "alice" and body["notified"] is True
    _, event_log = app.state.run_stores
    event = event_log.get_event(tenant, run_id, "step:ask:decision")
    assert event.type == "step_approved"
    assert event.data == {"step_id": "ask", "decided_by": "alice", "comment": "from the call", "resume_data": GOOD}
    assert body["decided_at"] == event.ts.isoformat()
    assert len(fake.sent) == 1

    item = client.get(f"/api/v1/approvals/{run_id}/step/ask", headers=headers).json()
    assert item["decision"] == "approved" and item["decided_by"] == "alice" and item["decided_at"]
    assert item["resume_data"] == GOOD and item["comment"] == "from the call"
    assert item["resume_schema"] == SCHEMA


def test_a_second_answer_is_409_with_who_and_when_and_sends_nothing(api, fake):
    client, app, tenant_of = api
    tenant, headers = tenant_of("alice")
    run_id = waiting_input(client, app, tenant, headers)
    assert client.post(url(run_id), json={"data": GOOD}, headers=headers).status_code == 200
    for action, payload in (("resume", {"data": GOOD}), ("reject", {})):
        r = client.post(url(run_id, action=action), json=payload, headers=headers)
        assert r.status_code == 409
        assert r.json()["decision"] == "approved" and r.json()["decided_by"] == "alice" and r.json()["decided_at"]
    assert len(fake.sent) == 1


def test_invalid_data_is_a_compact_422_that_never_echoes_values(api, fake):
    client, app, tenant_of = api
    tenant, headers = tenant_of()
    run_id = waiting_input(client, app, tenant, headers)
    secret = "s3cret-value-9137"
    bad = {"email": 5, "count": 0, "tier": secret, "address": {}, "extra": secret}
    r = client.post(url(run_id), json={"data": bad}, headers=headers)
    assert r.status_code == 422
    assert secret not in r.text
    errors = r.json()["detail"]
    locs = {tuple(e["loc"]) for e in errors}
    assert ("body", "data", "email") in locs and ("body", "data", "count") in locs
    assert ("body", "data", "tier") in locs and ("body", "data", "address") in locs
    assert all(set(e) == {"loc", "msg", "type"} for e in errors)
    assert fake.sent == []
    # The step still waits: a valid answer then works.
    assert client.post(url(run_id), json={"data": GOOD}, headers=headers).status_code == 200


def test_missing_data_unknown_body_keys_and_non_finite_numbers_are_422(api, fake):
    client, app, tenant_of = api
    tenant, headers = tenant_of()
    run_id = waiting_input(client, app, tenant, headers, schema={"type": "object", "properties": {"n": {"type": "number"}}})
    assert client.post(url(run_id), json={}, headers=headers).status_code == 422
    assert client.post(url(run_id), json={"data": {}, "other": 1}, headers=headers).status_code == 422
    assert client.post(url(run_id), json={"data": {}, "comment": "x" * 2001}, headers=headers).status_code == 422
    r = client.post(url(run_id), content=b'{"data": {"n": NaN}}', headers={**headers, "content-type": "application/json"})
    assert r.status_code == 422 and "NaN" not in json.dumps(r.json()["detail"]).replace("NaN and Infinity", "")


def test_oversized_data_is_422(api, fake):
    client, app, tenant_of = api
    tenant, headers = tenant_of()
    run_id = waiting_input(client, app, tenant, headers, schema={"type": "object"})
    r = client.post(url(run_id), json={"data": {"blob": "x" * (33 * 1024)}}, headers=headers)
    assert r.status_code == 422


def test_other_tenants_run_is_404_and_unknown_run_is_404(api, fake):
    client, app, tenant_of = api
    tenant, headers = tenant_of("alice")
    _, other = tenant_of("mallory")
    run_id = waiting_input(client, app, tenant, headers)
    assert client.post(url(run_id), json={"data": GOOD}, headers=other).status_code == 404
    assert client.post(url(uuid.uuid4()), json={"data": GOOD}, headers=headers).status_code == 404
    assert client.get(f"/api/v1/approvals/{run_id}/step/ask", headers=other).status_code == 404
    assert fake.sent == []


def test_a_step_that_is_not_waiting_is_409(api, fake):
    client, app, tenant_of = api
    tenant, headers = tenant_of()
    run_id = waiting_input(client, app, tenant, headers)
    assert client.post(url(run_id, step="nowhere"), json={"data": GOOD}, headers=headers).status_code == 409


def test_an_input_step_cannot_be_approved_bare_but_can_be_rejected(api, fake):
    client, app, tenant_of = api
    tenant, headers = tenant_of()
    run_id = waiting_input(client, app, tenant, headers)
    r = client.post(url(run_id, action="approve"), headers=headers)
    assert r.status_code == 422 and r.json()["detail"][0]["type"] == "resume_data_required"
    assert fake.sent == []
    assert client.post(url(run_id, action="reject"), json={"comment": "no"}, headers=headers).status_code == 200


def test_an_approval_step_takes_no_data(api, fake):
    client, app, tenant_of = api
    tenant, headers = tenant_of()
    run_id = waiting_step(client, app, tenant, headers)  # step "approve", no resume_schema
    r = client.post(url(run_id, step="approve"), json={"data": {"a": 1}}, headers=headers)
    assert r.status_code == 409 and "takes no data" in r.json()["detail"]
    assert client.post(url(run_id, step="approve", action="approve"), headers=headers).status_code == 200
    item = client.get(f"/api/v1/approvals/{run_id}/step/approve", headers=headers).json()
    assert item["resume_schema"] is None and item["resume_data"] is None and item["decided_by"] == "alice"


def test_the_inbox_lists_the_schema_of_a_pending_input_step(api, fake):
    client, app, tenant_of = api
    tenant, headers = tenant_of()
    run_id = waiting_input(client, app, tenant, headers)
    items = client.get("/api/v1/approvals?kind=step", headers=headers).json()["items"]
    mine = [i for i in items if i["approval_key"] == "ask"]
    assert mine and mine[0]["resume_schema"] == SCHEMA
