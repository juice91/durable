"""Agent-bound grants at run time (docs/iam.md, "Agent and workflow grants"): the start-time set, the gates on MCP calls and
secret references, child runs, an engine that cannot answer, and the switch that keeps all of it inert.

The policy engine is an in-memory `FakeGrantEngine` (the OpenFGA semantics are tested in the plug-in and the API suite);
the worker, DBOS, the run store and the local MCP server are real."""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest
from eap_iam.testing import FakeGrantEngine

from eap_worker import grants
from worker_tests.conftest import APP_VERSION, enqueue_run, event_rows, wait_terminal
from worker_tests.mcp_helpers import mcp_spec, register_mcp_server
from worker_tests.workflow_run_helpers import cleanup_definitions, publish_agent

TOOL = "mcp__orders__lookup_order"


@pytest.fixture
def engine(monkeypatch):
    fake = FakeGrantEngine()
    grants.set_policy_engine(fake)
    monkeypatch.setenv("EAP_AGENT_GRANTS_ENFORCE", "1")
    yield fake
    grants.set_policy_engine(None)


@pytest.fixture
def cleanup(worker):
    tenants: list[str] = []
    yield tenants
    for t in tenants:
        cleanup_definitions(worker.engine, t)


def _run_mcp(worker, client, tenant, server, args=None):
    server_id = register_mcp_server(worker.engine, tenant, "orders", server.url)
    spec = mcp_spec(("orders", "lookup_order"), model="approval")
    agent_id = publish_agent(worker.engine, tenant, spec)

    def start() -> uuid.UUID:
        return enqueue_run(client, worker.run_store, tenant_id=tenant, spec=spec, app_version=APP_VERSION,
                           input_text=json.dumps({"tool": TOOL, "args": args or {"order_id": "A-1"}}), agent_id=uuid.UUID(agent_id))

    return server_id, agent_id, start


def _denials(worker, run_id):
    return [(k, d) for _s, k, t, d in event_rows(worker.engine, run_id) if t == "permission_denied"]


def _grant(engine, kind, subject_id, resource_kind, resource_id):
    engine.grants.add((kind, str(subject_id), resource_kind, str(resource_id)))


# ------------------------------------------------------------------------------------------ MCP servers


def test_an_ungranted_server_is_refused_with_a_readable_error_and_nothing_is_sent(
    worker, client, tenant, mcp_env, local_mcp_server, engine, cleanup,
):
    cleanup.append(tenant)
    server_id, agent_id, start = _run_mcp(worker, client, tenant, local_mcp_server)
    run_id = start()
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "completed", run["error"]  # a refusal is a tool error, never a failed run
    assert local_mcp_server.calls == [] and local_mcp_server.seen_headers == []  # not even a tools/list went out
    (key, data), = _denials(worker, run_id)
    assert key == "permission_denied:mcp_server:orders" and data["reason"] == "not_granted"
    assert data["subject_kind"] == "agent" and data["subject_id"] == agent_id
    assert "is not authorized to use MCP server orders" in data["message"] and data["message"].startswith("agent ")
    assert worker.run_store.get_grant_set(tenant, run_id)["mcp_servers"] == []
    results = [d for _s, _k, t, d in event_rows(worker.engine, run_id) if t == "tool_result"]
    assert "permission_denied: agent " in json.dumps(results)


def test_a_granted_server_works_and_a_revoke_reaches_the_next_run_only(
    worker, client, tenant, mcp_env, local_mcp_server, engine, cleanup,
):
    cleanup.append(tenant)
    server_id, agent_id, start = _run_mcp(worker, client, tenant, local_mcp_server)
    _grant(engine, "agent", agent_id, "mcp_server", server_id)  # only the server: its credentials are not tuples
    first = start()
    assert wait_terminal(worker.run_store, tenant, first)["status"] == "completed"
    assert local_mcp_server.calls == [("lookup_order", {"order_id": "A-1"})] and _denials(worker, first) == []
    assert worker.run_store.get_grant_set(tenant, first)["mcp_servers"] == [server_id]

    engine.grants.clear()  # revoked
    assert worker.run_store.get_grant_set(tenant, first)["mcp_servers"] == [server_id]  # the run in flight / done keeps its set
    second = start()
    wait_terminal(worker.run_store, tenant, second)
    assert local_mcp_server.call_count("lookup_order") == 1  # the second call never left the worker
    assert [d["reason"] for _k, d in _denials(worker, second)] == ["not_granted"]


def test_a_server_grant_carries_the_servers_bound_credentials_without_any_secret_grant(
    worker, client, tenant, mcp_env, engine, cleanup,
):
    from worker_tests.mcp_helpers import LocalMcpServer

    cleanup.append(tenant)
    with LocalMcpServer(required_auth="Bearer FAKE-bound-token-31ab") as server:
        server_id = register_mcp_server(worker.engine, tenant, "orders", server.url, headers={"Authorization": "Bearer FAKE-bound-token-31ab"})
        spec = mcp_spec(("orders", "lookup_order"), model="approval")
        agent_id = publish_agent(worker.engine, tenant, spec)
        _grant(engine, "agent", agent_id, "mcp_server", server_id)
        run_id = enqueue_run(client, worker.run_store, tenant_id=tenant, spec=spec, app_version=APP_VERSION,
                             input_text=json.dumps({"tool": TOOL, "args": {"order_id": "A-2"}}), agent_id=uuid.UUID(agent_id))
        assert wait_terminal(worker.run_store, tenant, run_id)["status"] == "completed"
        assert server.calls == [("lookup_order", {"order_id": "A-2"})]
        assert worker.run_store.get_grant_set(tenant, run_id)["secrets"] == []  # no secret tuple was needed


def test_an_engine_that_cannot_answer_fails_new_runs_closed(worker, client, tenant, mcp_env, local_mcp_server, engine, cleanup):
    cleanup.append(tenant)
    server_id, agent_id, start = _run_mcp(worker, client, tenant, local_mcp_server)
    _grant(engine, "agent", agent_id, "mcp_server", server_id)
    engine.unavailable = True
    run_id = start()
    wait_terminal(worker.run_store, tenant, run_id)
    stored = worker.run_store.get_grant_set(tenant, run_id)
    assert stored["unavailable"] is True and stored["mcp_servers"] == [] and local_mcp_server.calls == []
    assert [d["reason"] for _k, d in _denials(worker, run_id)] == ["authz_unavailable"]
    assert "authorization is unavailable" in _denials(worker, run_id)[0][1]["message"]
    engine.unavailable = False  # it is back: the next run is whole again
    again = start()
    wait_terminal(worker.run_store, tenant, again)
    assert local_mcp_server.call_count("lookup_order") == 1


def test_nothing_is_stored_or_gated_while_enforcement_is_off(worker, client, tenant, mcp_env, local_mcp_server, engine, cleanup, monkeypatch):
    cleanup.append(tenant)
    monkeypatch.delenv("EAP_AGENT_GRANTS_ENFORCE")
    _server_id, _agent_id, start = _run_mcp(worker, client, tenant, local_mcp_server)
    run_id = start()
    assert wait_terminal(worker.run_store, tenant, run_id)["status"] == "completed"
    assert local_mcp_server.call_count("lookup_order") == 1  # as before: no grant, still allowed
    assert worker.run_store.get_grant_set(tenant, run_id) is None and engine.asked == 0 and _denials(worker, run_id) == []


# ------------------------------------------------------------------------------------------ secrets


def _http_spec():
    return {"spec_version": 1, "name": "Poster", "instructions": "Post it.", "model": {"provider": "test", "name": "approval"},
            "tools": ["http.request"], "model_settings": {}}


def _post(worker, client, tenant, agent_id, spec):
    args = {"method": "POST", "url": "https://hooks.example.com/x", "headers": {"Authorization": "Bearer secret:crm_token"}}
    return enqueue_run(client, worker.run_store, tenant_id=tenant, spec=spec, app_version=APP_VERSION,
                       input_text=json.dumps({"tool": "http__request", "args": args}), agent_id=uuid.UUID(agent_id))


def test_an_ungranted_secret_ref_in_http_request_asks_no_approval_and_is_refused(worker, client, tenant, engine, cleanup, monkeypatch):
    from eap_worker import http_tool

    cleanup.append(tenant)
    monkeypatch.setattr(http_tool, "new_client", lambda *a, **k: pytest.fail("nothing may be sent"))
    spec = _http_spec()
    agent_id = publish_agent(worker.engine, tenant, spec)
    run_id = _post(worker, client, tenant, agent_id, spec)
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "completed", run["error"]  # no approval card was raised: no grant, no approval
    (key, data), = _denials(worker, run_id)
    assert key == "permission_denied:secret:crm_token" and data["name"] == "crm_token"
    assert "agent Poster is not authorized to use secret crm_token" in data["message"]
    assert not [1 for _s, _k, t, _d in event_rows(worker.engine, run_id) if t == "tool_approval_waiting"]


def test_a_granted_secret_ref_goes_on_to_the_approval_card(worker, client, tenant, engine, cleanup):
    from worker_tests.test_tool_approval import wait_waiting

    cleanup.append(tenant)
    spec = _http_spec()
    agent_id = publish_agent(worker.engine, tenant, spec)
    _grant(engine, "agent", agent_id, "secret", "crm_token")
    run_id = _post(worker, client, tenant, agent_id, spec)
    (waiting,) = wait_waiting(worker, run_id)  # the existing approval rule applies once the grant is there
    assert "secret:crm_token" in json.dumps(waiting) and _denials(worker, run_id) == []


# ------------------------------------------------------------------------------------------ unit level


def _rt(worker):
    return SimpleNamespace(run_store=worker.run_store)


def _row(worker, tenant, *, parent=None):
    run_id = uuid.uuid4()
    if parent is None:
        worker.run_store.create_run(tenant, uuid.uuid4(), 1, "x", run_id)
    else:
        worker.run_store.create_child_agent_run(tenant, uuid.uuid4(), 1, "x", run_id, parent, "step1")
    return run_id


def _workflow_row(worker, tenant):
    run_id = uuid.uuid4()
    worker.run_store.create_workflow_run(tenant, uuid.uuid4(), 1, "{}", run_id)
    return run_id


def test_a_child_run_takes_the_intersection_with_its_parent_agent_run(worker, tenant, engine, cleanup):
    cleanup.append(tenant)
    for rid in ("s1", "s2"):
        _grant(engine, "agent", "parent", "mcp_server", rid)
    for rid in ("s2", "s3"):
        _grant(engine, "agent", "child", "mcp_server", rid)
    _grant(engine, "agent", "parent", "secret", "a")
    _grant(engine, "agent", "child", "secret", "b")
    parent = _row(worker, tenant)
    asyncio.run(grants.ensure_grant_set(_rt(worker), tenant, str(parent), "agent", "parent", "Parent"))
    child = _row(worker, tenant, parent=parent)
    asyncio.run(grants.ensure_grant_set(_rt(worker), tenant, str(child), "agent", "child", "Child"))
    stored = worker.run_store.get_grant_set(tenant, child)
    assert stored["mcp_servers"] == ["s2"] and stored["secrets"] == [] and stored["subject"]["name"] == "Child"
    # The set is written once: asking again (a replayed start) returns the stored one, whatever the engine says now.
    engine.grants.clear()
    again = asyncio.run(grants.ensure_grant_set(_rt(worker), tenant, str(child), "agent", "child", "Child"))
    assert again == stored


def test_a_workflows_agent_step_uses_the_agents_own_grants_not_the_workflows(worker, tenant, engine, cleanup):
    cleanup.append(tenant)
    _grant(engine, "workflow", "wf", "secret", "only_workflow")
    _grant(engine, "agent", "ag", "mcp_server", "srv")
    workflow_run = _workflow_row(worker, tenant)
    asyncio.run(grants.ensure_grant_set(_rt(worker), tenant, str(workflow_run), "workflow", "wf", "Flow"))
    step = _row(worker, tenant, parent=workflow_run)
    asyncio.run(grants.ensure_grant_set(_rt(worker), tenant, str(step), "agent", "ag", "Agent"))
    assert worker.run_store.get_grant_set(tenant, workflow_run)["secrets"] == ["only_workflow"]
    stored = worker.run_store.get_grant_set(tenant, step)
    assert stored["mcp_servers"] == ["srv"] and stored["secrets"] == []  # the workflow's secret does not flow into the agent


def test_workflow_steps_use_the_workflows_grants_in_http_request(worker, tenant, engine, cleanup, monkeypatch):
    """A workflow tool step runs with the workflow RUN's id: its secrets come from the workflow's set, not from any agent's."""
    from eap_worker import http_tool

    cleanup.append(tenant)
    _grant(engine, "workflow", "wf", "secret", "crm_token")
    run = _workflow_row(worker, tenant)
    asyncio.run(grants.ensure_grant_set(_rt(worker), tenant, str(run), "workflow", "wf", "Flow"))
    sent = []
    monkeypatch.setattr(http_tool, "secret_lookup", lambda ctx: {"crm_token": "FAKE-token-1"}.get)
    import httpx2

    monkeypatch.setattr(http_tool, "new_client", lambda policy, t: httpx2.AsyncClient(transport=httpx2.MockTransport(lambda r: (sent.append(r), httpx2.Response(200, json={}))[1])))
    ctx = SimpleNamespace(deps=SimpleNamespace(tenant_id=tenant, run_id=run))
    ok = asyncio.run(http_tool.http_request(ctx, method="POST", url="https://hooks.example.com/x", headers={"Authorization": "Bearer secret:crm_token"}))
    assert "error" not in ok and len(sent) == 1
    refused = asyncio.run(http_tool.http_request(ctx, method="POST", url="https://hooks.example.com/x", headers={"Authorization": "Bearer secret:other"}))
    assert refused["error"]["code"] == "permission_denied" and refused["error"]["message"] == "workflow Flow is not authorized to use secret other"
    assert len(sent) == 1  # the refused call sent nothing and read no value (not even to say it does not exist)


def test_the_names_in_a_tool_argument_are_found_at_any_depth():
    assert grants.secret_names_in({"a": "secret:x", "b": [{"c": "secret:y"}, "plain", "my secret:z here"], "d": 3}) == ["x", "y"]
