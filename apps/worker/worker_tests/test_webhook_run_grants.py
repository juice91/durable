"""A webhook run and agent-bound grants (docs/webhooks.md, "The identity of a webhook run"): the run acts as its trigger,
for nobody, and what it may use is what its WORKFLOW was granted: gated with enforcement on, as before with it off."""

from __future__ import annotations

import json
import uuid

import httpx2
import pytest
from eap_iam.testing import FakeGrantEngine
from eap_run_store import RunActor

from eap_worker import grants, http_tool
from worker_tests.conftest import APP_VERSION, event_rows, wait_terminal
from worker_tests.workflow_run_helpers import canonical, cleanup_definitions, publish_workflow

SPEC = {
    "name": "Hook post", "input_schema": {"type": "object"}, "start": "post",
    "steps": [{"id": "post", "type": "tool", "tool_id": "http.request", "input": {
        "method": "GET", "url": "https://api.example.com/x", "headers": {"Authorization": "Bearer secret:crm_token"}}}],
}


@pytest.fixture
def engine():
    fake = FakeGrantEngine()
    grants.set_policy_engine(fake)
    yield fake
    grants.set_policy_engine(None)


@pytest.fixture
def sent(monkeypatch):
    requests: list = []

    def handler(request):
        requests.append(request)
        return httpx2.Response(200, json={})

    monkeypatch.setattr(http_tool, "secret_lookup", lambda ctx: {"crm_token": "FAKE-token-1"}.get)
    monkeypatch.setattr(http_tool, "new_client", lambda policy, t: httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))
    return requests


@pytest.fixture
def published(worker, tenant):
    yield publish_workflow(worker.engine, tenant, SPEC)
    cleanup_definitions(worker.engine, tenant)


def _webhook_run(worker, client, tenant, published) -> tuple[uuid.UUID, str]:
    workflow_id, version_id = published
    run_id, trigger_id = uuid.uuid4(), str(uuid.uuid4())
    input_json = canonical({})
    worker.run_store.create_workflow_run(
        tenant, workflow_id, 1, input_json, run_id, trigger_id=trigger_id, actor=RunActor(kind="webhook", subject=trigger_id)
    )
    client.enqueue(
        {"queue_name": "eap-agent-runs", "workflow_name": "eap.workflow_run", "workflow_id": str(run_id), "app_version": APP_VERSION},
        str(run_id), tenant, version_id, input_json,
    )
    return run_id, trigger_id


def _denied(worker, run_id):
    return [d for _s, _k, t, d in event_rows(worker.engine, run_id) if t == "permission_denied"]


def _assert_identity(worker, tenant, run_id, trigger_id):
    actor = worker.run_store.get_run_actor(tenant, run_id)
    assert actor.principal == f"webhook:{trigger_id}" and actor.on_behalf_of is None and actor.roles is None


def test_enforced_an_ungranted_secret_is_refused_and_nothing_is_sent(
    worker, client, tenant, engine, sent, published, monkeypatch
):
    monkeypatch.setenv("EAP_AGENT_GRANTS_ENFORCE", "1")
    run_id, trigger_id = _webhook_run(worker, client, tenant, published)
    wait_terminal(worker.run_store, tenant, run_id)
    (denial,) = _denied(worker, run_id)
    assert denial["name"] == "crm_token" and denial["subject_kind"] == "workflow" and sent == []
    assert worker.run_store.get_grant_set(tenant, run_id)["secrets"] == []
    assert "FAKE-token-1" not in json.dumps([d for *_x, d in event_rows(worker.engine, run_id)])
    _assert_identity(worker, tenant, run_id, trigger_id)


def test_enforced_the_workflows_grant_lets_the_webhook_run_use_the_secret(
    worker, client, tenant, engine, sent, published, monkeypatch
):
    monkeypatch.setenv("EAP_AGENT_GRANTS_ENFORCE", "1")
    engine.grants.add(("workflow", published[0], "secret", "crm_token"))
    run_id, trigger_id = _webhook_run(worker, client, tenant, published)
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "completed", run["error"]
    assert len(sent) == 1 and _denied(worker, run_id) == []
    assert worker.run_store.get_grant_set(tenant, run_id)["secrets"] == ["crm_token"]
    _assert_identity(worker, tenant, run_id, trigger_id)


def test_not_enforced_the_webhook_run_behaves_as_any_run_did_before(
    worker, client, tenant, engine, sent, published, monkeypatch
):
    monkeypatch.delenv("EAP_AGENT_GRANTS_ENFORCE", raising=False)
    run_id, trigger_id = _webhook_run(worker, client, tenant, published)
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "completed", run["error"]
    assert len(sent) == 1 and worker.run_store.get_grant_set(tenant, run_id) is None and engine.asked == 0
    _assert_identity(worker, tenant, run_id, trigger_id)


def test_a_child_of_a_webhook_run_acts_for_nobody(worker, tenant):
    parent = uuid.uuid4()
    trigger_id = str(uuid.uuid4())
    worker.run_store.create_workflow_run(
        tenant, uuid.uuid4(), 1, "{}", parent, trigger_id=trigger_id, actor=RunActor(kind="webhook", subject=trigger_id)
    )
    child = uuid.uuid4()
    worker.run_store.create_child_agent_run(tenant, uuid.uuid4(), 1, "x", child, parent, "step")
    actor = worker.run_store.get_run_actor(tenant, child)
    assert actor.principal == f"run:{parent}" and actor.on_behalf_of is None and actor.roles is None
