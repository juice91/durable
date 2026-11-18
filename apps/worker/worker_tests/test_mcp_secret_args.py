"""Secret references in MCP tool call arguments: resolved at the call, redacted everywhere else.

The agent passes `secret:<name>` in an argument of an MCP tool (a login form's password field). The
recorded arguments, the approval card and the events keep the reference; the value is substituted
inside the call step, after the approval, and joins the redaction variants of the call's result.
The local MCP server's `lookup_order` echoes its `order_id` argument, which is what shows the value
reaching the server and being scrubbed on the way back. All values are obviously fake."""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from eap_secrets import load_keyring
from worker_tests.conftest import APP_VERSION, enqueue_run, wait_terminal
from worker_tests.mcp_helpers import mcp_spec, register_mcp_server
from worker_tests.test_secrets_request import FAKE, everything_recorded
from worker_tests.test_tool_approval import decide, events, wait_waiting
from worker_tests.workflow_run_helpers import cleanup_definitions, publish_agent

REF = "secret:pwd-example"
TOOL = "mcp__orders__lookup_order"


@pytest.fixture
def cleanup(worker):
    tenants: list[str] = []
    yield tenants
    with worker.engine.begin() as conn:
        for t in tenants:
            conn.execute(text("DELETE FROM tenant_secrets WHERE tenant_id = :t"), {"t": t})
    for t in tenants:
        cleanup_definitions(worker.engine, t)


def store_secret(worker, tenant, value=FAKE, name="pwd-example"):
    from eap_run_store.secrets import SecretStore

    SecretStore(worker.engine, load_keyring()).put(tenant, name, value, "alice")


def run_call(worker, client, tenant, server, args, *, gated=False):
    register_mcp_server(worker.engine, tenant, "orders", server.url)
    spec = mcp_spec(("orders", "lookup_order"), model="approval")
    if gated:
        spec = {**spec, "approvals": {"tools": ["orders/lookup_order"]}}
    agent_id = publish_agent(worker.engine, tenant, spec)
    return enqueue_run(client, worker.run_store, tenant_id=tenant, spec=spec, app_version=APP_VERSION,
                       input_text=json.dumps({"tool": TOOL, "args": args}), agent_id=uuid.UUID(agent_id))


def test_the_server_gets_the_value_while_events_and_approval_keep_the_reference(
    worker, client, tenant, mcp_env, local_mcp_server, cleanup,
):
    cleanup.append(tenant)
    store_secret(worker, tenant)
    run_id = run_call(worker, client, tenant, local_mcp_server, {"order_id": REF}, gated=True)
    (waiting,) = wait_waiting(worker, run_id)
    assert REF in json.dumps(waiting) and FAKE not in json.dumps(waiting)
    assert local_mcp_server.call_count("lookup_order") == 0  # nothing before the approval
    decide(worker, client, tenant, run_id, waiting["tool_call_id"])
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "completed", run["error"]
    assert local_mcp_server.calls == [("lookup_order", {"order_id": FAKE})]  # the value arrived
    recorded = everything_recorded(worker, tenant, run_id)
    assert REF in recorded and FAKE not in recorded  # args, events, approval, result: reference only
    result = [d for _s, _k, t, d in _rows(worker, run_id) if t == "tool_result"]
    assert len(result) == 1 and result[0]["status"] == "ok"
    assert FAKE not in json.dumps(result[0]["content"]) and "REDACTED" in json.dumps(result[0]["content"]).upper()


def _rows(worker, run_id):
    from worker_tests.conftest import event_rows

    return event_rows(worker.engine, run_id)


def test_nested_strings_resolve_and_a_non_reference_is_untouched(worker, client, tenant, mcp_env, local_mcp_server, cleanup):
    cleanup.append(tenant)  # no stored secret at all: a plain string that merely mentions secret: is sent as is
    run_id = run_call(worker, client, tenant, local_mcp_server, {"order_id": "my secret:pwd-example, not a ref"})
    assert wait_terminal(worker.run_store, tenant, run_id)["status"] == "completed"
    assert local_mcp_server.calls == [("lookup_order", {"order_id": "my secret:pwd-example, not a ref"})]


def test_an_unknown_reference_is_refused_with_secret_not_found_and_nothing_is_sent(
    worker, client, tenant, mcp_env, local_mcp_server, cleanup,
):
    cleanup.append(tenant)
    run_id = run_call(worker, client, tenant, local_mcp_server, {"order_id": "secret:nope-not-stored"})
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "completed", run["error"]
    assert local_mcp_server.call_count("lookup_order") == 0
    failed = [d for _s, _k, t, d in _rows(worker, run_id) if t == "tool_result"]
    assert "secret_not_found: secret:nope-not-stored" in json.dumps(failed)


def test_a_second_tenants_secret_cannot_be_referenced(worker, client, tenant, mcp_env, local_mcp_server, cleanup):
    other = f"t_{uuid.uuid4().hex[:10]}"
    cleanup.extend([tenant, other])
    store_secret(worker, tenant)  # only the first tenant has it
    run_id = run_call(worker, client, other, local_mcp_server, {"order_id": REF})
    wait_terminal(worker.run_store, other, run_id)
    assert local_mcp_server.call_count("lookup_order") == 0
    recorded = everything_recorded(worker, other, run_id)
    assert "secret_not_found" in recorded and FAKE not in recorded
