"""Secret input (worker half): `secrets.request`, refs resolved at the point of use, values redacted.

All secret values here are obviously fake. A value must never appear in a run's events, its row, the
DBOS step outputs, or a tool result; only the reference `secret:<name>` does."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from sqlalchemy import create_engine, text

from eap_schemas.tools import ALWAYS_GATED_TOOL_IDS, ungated_always_gated_tools
from eap_secrets import load_keyring
from worker_tests.conftest import DBOS_DB_URL, APP_VERSION, enqueue_run, event_rows, ticket_spec, wait_terminal
from worker_tests.mcp_helpers import LocalMcpServer, mcp_spec, register_mcp_server
from worker_tests.test_mcp_tools import _insert_agent_and_version
from worker_tests.test_tool_approval import decide, events, gated_spec, start, wait_waiting

FAKE = "FAKE-not-a-real-credential-7f3a9c1e"
OTHER = "FAKE-other-tenant-credential-55d2"
ECHO_ID = "test.echo_secret"
RECEIVED: list[str] = []


def store(worker):
    from eap_run_store.secrets import SecretStore

    return SecretStore(worker.engine, load_keyring())


@pytest.fixture(autouse=True)
def _keyring(mcp_env):
    return mcp_env


@pytest.fixture
def echo_tool(worker):
    """A test tool that receives its argument, remembers it and returns it (so redaction has work)."""
    RECEIVED.clear()

    def echo_secret(text: str) -> dict[str, str]:
        """Echo the text."""
        RECEIVED.append(text)
        if text.endswith("!fail"):
            raise RuntimeError(f"echo failed on {text}")
        return {"echo": text}

    worker.registry.register(ECHO_ID, echo_secret, description="Echo the text", side_effect=False)
    worker.__dict__.pop("allowed_tools", None)
    yield
    worker.registry._tools.pop(ECHO_ID, None)
    worker.registry._tool_names.pop("test__echo_secret", None)
    worker.__dict__.pop("allowed_tools", None)


def echo_spec() -> dict[str, Any]:
    return ticket_spec(tools=(ECHO_ID,), model="approval")


def echo_prompt(value: str) -> str:
    return json.dumps({"tool": "test__echo_secret", "args": {"text": value}})


def request_prompt(name: str = "slack-token") -> str:
    return json.dumps({"tool": "secrets__request", "args": {"name": name, "purpose": "post to Slack"}})


def everything_recorded(worker, tenant, run_id) -> str:
    """The run's events, its row, and the DBOS step outputs as one searchable string."""
    parts = [json.dumps(e[3], default=str) for e in event_rows(worker.engine, run_id)]
    with worker.engine.connect() as conn:
        row = conn.execute(text("SELECT to_jsonb(r)::text FROM runs r WHERE id = :r AND tenant_id = :t"),
                           {"r": str(run_id), "t": tenant}).scalar_one()
    parts.append(row)
    dbos = create_engine(DBOS_DB_URL.replace("postgresql://", "postgresql+psycopg://", 1))
    try:
        with dbos.connect() as conn:
            schema = worker.settings.dbos_system_schema
            for table, column in (("operation_outputs", "output"), ("workflow_status", "output")):
                try:
                    rows = conn.execute(text(f'SELECT {column}::text FROM "{schema}".{table}')).all()
                except Exception:  # noqa: BLE001 - a DBOS version without that table
                    conn.rollback()
                    continue
                parts.extend(str(r[0]) for r in rows if r[0])
    finally:
        dbos.dispose()
    return "\n".join(parts)


# --------------------------------------------------------------------------- gating


def test_secrets_request_is_always_gated(worker, client, tenant):
    assert "secrets.request" in ALWAYS_GATED_TOOL_IDS and "architect.publish_agent" in ALWAYS_GATED_TOOL_IDS
    assert ungated_always_gated_tools(["clock.now", "secrets.request"], None) == ["secrets.request"]
    assert ungated_always_gated_tools(["secrets.request"], ["secrets.request"]) == []
    # The worker refuses a run whose spec lists it without an approvals entry: it never starts the tool.
    spec = ticket_spec(tools=("secrets.request",), model="approval")
    run_id = start(worker, client, tenant, spec, request_prompt())
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "failed" and "secrets.request" in run["error"], run
    assert not [e for e in event_rows(worker.engine, run_id) if e[2] == "tool_result"]
    # Gated, it waits for a decision and the tool has not run (no result) until then.
    run_id = start(worker, client, tenant, gated_spec(tools=("secrets.request",), gate=("secrets.request",)),
                   request_prompt())
    (waiting,) = wait_waiting(worker, run_id)
    assert waiting["tool_name"] == "secrets__request"
    assert not [e for e in event_rows(worker.engine, run_id) if e[2] == "tool_result"]
    assert decide(worker, client, tenant, run_id, waiting["tool_call_id"], approve=False) == "created"
    assert wait_terminal(worker.run_store, tenant, run_id)["status"] == "completed"


def test_approval_summary_words_the_request(worker, client, tenant):
    run_id = start(worker, client, tenant, gated_spec(tools=("secrets.request",), gate=("secrets.request",)),
                   request_prompt("slack-token"))
    (waiting,) = wait_waiting(worker, run_id)
    assert waiting["summary"] == "Store a secret named 'slack-token' for: post to Slack"
    decide(worker, client, tenant, run_id, waiting["tool_call_id"], approve=False)
    wait_terminal(worker.run_store, tenant, run_id)


def test_approved_request_returns_ref_when_stored(worker, client, tenant):
    store(worker).put(tenant, "slack-token", FAKE, "alice")  # what the card's PUT does before it approves
    run_id = start(worker, client, tenant, gated_spec(tools=("secrets.request",), gate=("secrets.request",)),
                   request_prompt())
    (waiting,) = wait_waiting(worker, run_id)
    decide(worker, client, tenant, run_id, waiting["tool_call_id"])
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "completed", run["error"]
    result = events(worker, run_id)[f"tool:{waiting['tool_call_id']}:result"][1]
    assert result["status"] == "ok" and result["content"] == {"ref": "secret:slack-token", "stored": True}
    assert FAKE not in everything_recorded(worker, tenant, run_id)


def test_approved_request_without_store_is_secret_missing(worker, client, tenant):
    run_id = start(worker, client, tenant, gated_spec(tools=("secrets.request",), gate=("secrets.request",)),
                   request_prompt())
    (waiting,) = wait_waiting(worker, run_id)
    decide(worker, client, tenant, run_id, waiting["tool_call_id"])  # approved from AG-UI, nothing stored
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "completed", run["error"]
    content = events(worker, run_id)[f"tool:{waiting['tool_call_id']}:result"][1]["content"]
    assert content["error"]["code"] == "secret_missing"


def test_request_with_an_invalid_name_is_secret_name_invalid(worker, client, tenant):
    run_id = start(worker, client, tenant, gated_spec(tools=("secrets.request",), gate=("secrets.request",)),
                   request_prompt("Not A Name"))
    (waiting,) = wait_waiting(worker, run_id)
    decide(worker, client, tenant, run_id, waiting["tool_call_id"])
    wait_terminal(worker.run_store, tenant, run_id)
    content = events(worker, run_id)[f"tool:{waiting['tool_call_id']}:result"][1]["content"]
    assert content["error"]["code"] == "secret_name_invalid"


# --------------------------------------------------------------------------- resolution and redaction


def test_tool_arg_ref_is_resolved_and_value_redacted(worker, client, tenant, echo_tool):
    store(worker).put(tenant, "slack-token", FAKE, "alice")
    nested = json.dumps({"tool": "test__echo_secret", "args": {"text": "secret:slack-token"}})
    run_id = start(worker, client, tenant, echo_spec(), nested)
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "completed", run["error"]
    assert RECEIVED == [FAKE]  # the tool got the value ...
    (result_key,) = [k for k in events(worker, run_id) if k.endswith(":result")]
    content = events(worker, run_id)[result_key][1]["content"]
    assert content == {"echo": "[REDACTED]"}  # ... and its result was scrubbed
    assert FAKE not in everything_recorded(worker, tenant, run_id)
    call = [d for t, d in events(worker, run_id).values() if t == "tool_call"][0]
    assert "secret:slack-token" in json.dumps(call)  # the recorded call keeps the reference


def test_ticket_tool_receives_the_value_through_a_ref(worker, client, tenant):
    store(worker).put(tenant, "slack-token", FAKE, "alice")
    args = {"title": "secret:slack-token", "body": "see title"}
    run_id = start(worker, client, tenant, ticket_spec(model="approval"),
                   json.dumps({"tool": "tickets.create", "args": args}))
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "completed", run["error"]
    with worker.engine.connect() as conn:
        titles = [r[0] for r in conn.execute(text("SELECT title FROM fake_external_tickets WHERE tenant_id = :t"),
                                              {"t": tenant})]
    assert titles == [FAKE]
    assert FAKE not in everything_recorded(worker, tenant, run_id)


def test_failure_message_is_redacted(worker, client, tenant, echo_tool):
    store(worker).put(tenant, "slack-token", FAKE, "alice")
    # The ref is the whole argument, so the tool fails on the plain value only when it ends with !fail.
    store(worker).put(tenant, "failing", FAKE + "!fail", "alice")
    run_id = start(worker, client, tenant, echo_spec(), echo_prompt("secret:failing"))
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert FAKE not in everything_recorded(worker, tenant, run_id)
    assert run["status"] in {"completed", "failed", "needs_attention"}


def test_unknown_ref_fails_without_leaking(worker, client, tenant, echo_tool):
    store(worker).put(tenant, "other", FAKE, "alice")
    run_id = start(worker, client, tenant, echo_spec(), echo_prompt("secret:slack-token"))
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "completed", run["error"]
    assert RECEIVED == []  # the tool never ran
    assert "secret_not_found" in run["output"] and "secret:slack-token" in run["output"]
    assert FAKE not in everything_recorded(worker, tenant, run_id)


def test_another_tenants_secret_is_not_resolved(worker, client, tenant, echo_tool):
    other_tenant = f"t_{uuid.uuid4().hex[:10]}"
    try:
        store(worker).put(other_tenant, "slack-token", OTHER, "mallory")
        run_id = start(worker, client, tenant, echo_spec(), echo_prompt("secret:slack-token"))
        run = wait_terminal(worker.run_store, tenant, run_id)
        assert RECEIVED == [] and "secret_not_found" in run["output"]
        assert OTHER not in everything_recorded(worker, tenant, run_id)
    finally:
        with worker.engine.begin() as conn:
            conn.execute(text("DELETE FROM tenant_secrets WHERE tenant_id = :t"), {"t": other_tenant})


def test_mcp_header_ref_is_resolved(worker, client, tenant):
    sentinel = f"Bearer {FAKE}"
    store(worker).put(tenant, "mcp-token", sentinel, "alice")
    with LocalMcpServer(required_auth=sentinel) as server:
        register_mcp_server(worker.engine, tenant, "auth_orders", server.url,
                            headers={"Authorization": "secret:mcp-token"})
        agent_id = uuid.uuid4()
        spec = mcp_spec(("auth_orders", "lookup_order"))
        with worker.engine.begin() as conn:
            _insert_agent_and_version(conn, agent_id, tenant, spec)
        run_id = enqueue_run(client, worker.run_store, tenant_id=tenant, spec=spec,
                             input_text=json.dumps({"tool": "lookup_order", "args": {"order_id": "456"}}),
                             app_version=APP_VERSION, agent_id=agent_id, version=1)
        run = wait_terminal(worker.run_store, tenant, run_id)
        assert run["status"] == "completed", run["error"]
        assert any(sentinel in h.values() for h in server.seen_headers), server.seen_headers
    assert FAKE not in everything_recorded(worker, tenant, run_id)


def test_mcp_header_ref_without_a_secret_is_unavailable(worker, client, tenant):
    with LocalMcpServer() as server:
        register_mcp_server(worker.engine, tenant, "auth_orders", server.url,
                            headers={"Authorization": "secret:missing"})
        spec = mcp_spec(("auth_orders", "lookup_order"))
        agent_id = uuid.uuid4()
        with worker.engine.begin() as conn:
            _insert_agent_and_version(conn, agent_id, tenant, spec)
        run_id = enqueue_run(client, worker.run_store, tenant_id=tenant, spec=spec,
                             input_text=json.dumps({"tool": "lookup_order", "args": {"order_id": "1"}}),
                             app_version=APP_VERSION, agent_id=agent_id, version=1)
        run = wait_terminal(worker.run_store, tenant, run_id)
        assert run["status"] == "failed" and "secret_not_found" in run["error"], run
        assert server.call_count() == 0


def test_secret_value_absent_from_events(worker, client, tenant, echo_tool):
    """One run through request, approval, an echo that returns the value and a ticket that receives it."""
    store(worker).put(tenant, "slack-token", FAKE, "alice")
    spec = gated_spec(tools=("secrets.request", ECHO_ID, "tickets.create"), gate=("secrets.request",))
    body = json.dumps({"calls": [
        {"tool": "secrets__request", "args": {"name": "slack-token", "purpose": "post"}},
        {"tool": "test__echo_secret", "args": {"text": "secret:slack-token"}},
        {"tool": "tickets__create", "args": {"title": "secret:slack-token", "body": "b"}},
    ]})
    run_id = start(worker, client, tenant, spec, body)
    (waiting,) = wait_waiting(worker, run_id)
    decide(worker, client, tenant, run_id, waiting["tool_call_id"])
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "completed", run["error"]
    assert RECEIVED == [FAKE]
    assert FAKE not in everything_recorded(worker, tenant, run_id)
    assert "secret:slack-token" in everything_recorded(worker, tenant, run_id)
