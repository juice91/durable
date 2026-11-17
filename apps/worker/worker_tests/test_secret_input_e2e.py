"""Secret input, end to end (Task D): the flow a person goes through, through the real API.

An agent with `secrets.request` gated asks for `slack-token`. The person's card PUTs the value
(`PUT /api/v1/secrets/slack-token`) and then approves (`POST .../tool-approvals/{id}/approve`). The
tool result is the reference. A later `tickets.create` call carrying `secret:slack-token` receives the
value. The value is in no recorded event, run row, DBOS output, span or log record. A second tenant
cannot resolve the reference. All values are obviously fake.

Not covered here: the managed-MCP env case. Its fake ToolHive adapter lives in the API suite
(`apps/api/api_tests/test_mcp_managed*.py`, `test_secrets_api.py`) and runs against API fixtures that
the worker suite does not have; the managed env resolution is tested there.
"""

from __future__ import annotations

import json
import logging
import uuid

import pytest
from sqlalchemy import text

from worker_tests.conftest import APP_VERSION, enqueue_run, ticket_spec, wait_terminal
from worker_tests.test_ac18_no_values_in_telemetry import texts
from worker_tests.test_secrets_request import FAKE, everything_recorded
from worker_tests.test_telemetry import telemetry  # noqa: F401 - fixture
from worker_tests.test_tool_approval import events, gated_spec, start, wait_waiting
from worker_tests.test_workflow_run_acceptance import auth_header, workflow_api

API = "/api/v1"
REF = "secret:slack-token"


@pytest.fixture(autouse=True)
def _keyring(mcp_env):
    return mcp_env


@pytest.fixture
def api(worker_settings):
    with workflow_api(worker_settings.dbos_system_schema, APP_VERSION) as client:
        yield client


@pytest.fixture
def cleanup(worker):
    tenants: list[str] = []
    yield tenants
    with worker.engine.begin() as conn:
        for t in tenants:
            conn.execute(text("DELETE FROM tenant_secrets WHERE tenant_id = :t"), {"t": t})


def tickets(worker, tenant: str) -> list[str]:
    with worker.engine.connect() as conn:
        return [r[0] for r in conn.execute(
            text("SELECT title FROM fake_external_tickets WHERE tenant_id = :t"), {"t": tenant})]


def test_request_put_approve_use_and_the_value_stays_out_of_every_record(
    worker, client, api, tenant, cleanup, telemetry, caplog,  # noqa: F811
):
    cleanup.append(tenant)
    _provider, finished = telemetry
    caplog.set_level(logging.DEBUG)
    h = auth_header(tenant)
    spec = gated_spec(tools=("secrets.request",), gate=("secrets.request",))
    body = json.dumps({"tool": "secrets__request", "args": {"name": "slack-token", "purpose": "post to Slack"}})
    run_id = start(worker, client, tenant, spec, body)

    # 1. The gated request waits; nothing has run and the secret does not exist yet.
    (waiting,) = wait_waiting(worker, run_id)
    assert waiting["tool_name"] == "secrets__request"
    assert not [k for k, (t, _d) in events(worker, run_id).items() if t == "tool_result"]
    assert api.get(f"{API}/secrets", headers=h).json() == []

    # 2. The card stores the value, then the approval is recorded.
    put = api.put(f"{API}/secrets/slack-token", json={"value": FAKE}, headers=h)
    assert put.status_code == 200, put.text
    approve = api.post(f"{API}/runs/{run_id}/tool-approvals/{waiting['tool_call_id']}/approve", json={}, headers=h)
    assert approve.status_code == 200, approve.text
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "completed", run["error"]

    # 3. The request's result is the reference; a later run's tool call carrying it receives the value.
    by_key = events(worker, run_id)
    assert by_key[f"tool:{waiting['tool_call_id']}:result"][1]["content"] == {"ref": REF, "stored": True}
    assert tickets(worker, tenant) == []
    use_id = start(worker, client, tenant, ticket_spec(tools=("tickets.create",), model="approval"),
                   json.dumps({"tool": "tickets.create", "args": {"title": REF, "body": "uses the stored token"}}))
    assert wait_terminal(worker.run_store, tenant, use_id)["status"] == "completed"
    assert tickets(worker, tenant) == [FAKE]

    # 4. The value is nowhere in what was recorded, returned by the API, or exported.
    for rid in (run_id, use_id):
        recorded = everything_recorded(worker, tenant, rid)
        assert FAKE not in recorded and REF in recorded
    listing = api.get(f"{API}/secrets", headers=h)
    assert [s["name"] for s in listing.json()] == ["slack-token"] and FAKE not in listing.text
    assert FAKE not in api.get(f"{API}/runs/{use_id}", headers=h).text
    spans, logs = finished()
    assert any(s.name == "eap.run" for s in spans), "telemetry was not captured"
    for form in {FAKE, json.dumps(FAKE)[1:-1]}:
        assert not [t for t in texts(spans, logs, caplog.records) if form in t]


def test_a_second_tenant_cannot_resolve_the_reference(worker, client, api, tenant, cleanup):
    other = f"t_{uuid.uuid4().hex[:10]}"
    cleanup.extend([tenant, other])
    assert api.put(f"{API}/secrets/slack-token", json={"value": FAKE}, headers=auth_header(tenant)).status_code == 200
    assert api.get(f"{API}/secrets", headers=auth_header(other)).json() == []
    spec = ticket_spec(tools=("tickets.create",), model="approval")
    run_id = enqueue_run(client, worker.run_store, tenant_id=other, spec=spec, app_version=APP_VERSION,
                         input_text=json.dumps({"tool": "tickets.create", "args": {"title": REF, "body": "b"}}))
    run = wait_terminal(worker.run_store, other, run_id)
    assert "secret_not_found" in (run["output"] or "") + (run["error"] or ""), run
    assert tickets(worker, other) == []
    assert FAKE not in everything_recorded(worker, other, run_id)
    # The owner's own reference still works.
    own = enqueue_run(client, worker.run_store, tenant_id=tenant, spec=spec, app_version=APP_VERSION,
                      input_text=json.dumps({"tool": "tickets.create", "args": {"title": REF, "body": "b"}}))
    assert wait_terminal(worker.run_store, tenant, own)["status"] == "completed"
    assert tickets(worker, tenant) == [FAKE]
