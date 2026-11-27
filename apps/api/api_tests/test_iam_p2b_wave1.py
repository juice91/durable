"""IAM P2b wave 1: per-resource authorisation in the agent, workflow, run and schedule services.

Three engines are used against the real API on Postgres (no worker: a fake DBOS client records the enqueues):

* an engine that DENIES everything: every protected call is refused and stored data does not change;
* the real `OpenFgaPolicy` over `ModelServer`, an OpenFGA that evaluates the shipped model (roles from a table, grants and
  parent links as tuples): viewer, editor, admin, a runner grant, an approver grant, a non-member;
* an allow-all engine, which is today's behaviour (the rest of the API suite runs on `openaccess`).
"""

from __future__ import annotations

import dataclasses
import os
import uuid

import pytest
from eap_iam import Grant, ResourceRef
from fastapi.testclient import TestClient
from sqlalchemy import text

from eap_api.app import create_app
from eap_api.deps import iam_of
from eap_api.launcher import DBOSRunLauncher, LauncherSettings, get_run_launcher

from .conftest import get_auth_header
from .iam_model_engine import AllowAll, DenyAll, ModelServer, Recording
from .run_helpers import cleanup_tenant, hard_timeout
from .test_workflow_runs_api import AGENT_SPEC, FakeClient, publish_demo

PG_URL = os.environ.get("EAP_TEST_DATABASE_URL") or os.environ.get(
    "EAP_DATABASE_URL", "postgresql+psycopg://eap_app:eap_app_password@localhost:54329/eap_app"
)
API = "/api/v1"
WF_SPEC = {"name": "Tiny", "input_schema": {"type": "object"}, "start": "a",
           "steps": [{"id": "a", "type": "tool", "tool_id": "clock.now", "input": {}}]}
ROLES = {"ann": {"admin"}, "eve": {"editor"}, "vic": {"viewer"}}  # zed is no member


@pytest.fixture(autouse=True)
def _no_hang():
    with hard_timeout(90):
        yield


class World:
    def __init__(self, app: object, client: TestClient, fake: FakeClient) -> None:
        self.app, self.client, self.fake = app, client, fake
        self.tenant = f"t{uuid.uuid4().hex[:10]}"
        self.server = ModelServer()
        self.engine = self.server.policy()
        self.engine.roles = {k: frozenset(v) for k, v in ROLES.items()}

    def use(self, engine: object) -> None:
        """Swap the policy the app asks (the identity chain and everything else stay)."""
        self.app.state.iam = dataclasses.replace(iam_of(self.app), policy=engine)

    def h(self, user: str) -> dict[str, str]:
        return get_auth_header(self.tenant, user)

    def seed(self) -> tuple[str, str]:
        """An agent and a published workflow made by the admin ann (so they carry her ownership tuples)."""
        self.use(self.engine)
        workflow_id = publish_demo(self.client, self.h("ann"))
        agent_id = self.client.get(f"{API}/agents", headers=self.h("ann")).json()[0]["id"]
        return agent_id, workflow_id

    def count(self, table: str) -> int:
        with self.app.state.database_engine.connect() as conn:
            return conn.execute(text(f"SELECT count(*) FROM {table} WHERE tenant_id = :t"), {"t": self.tenant}).scalar_one()

    def grant(self, user: str, relation: str, kind: str, resource_id: str) -> None:
        import asyncio

        asyncio.run(self.engine.grant(Grant(user, relation, ResourceRef(kind, resource_id, self.tenant))))


@pytest.fixture
def world():
    fake = FakeClient()
    app = create_app(database_url=PG_URL)
    app.dependency_overrides[get_run_launcher] = lambda: DBOSRunLauncher(
        LauncherSettings("postgresql://unused/x", "dbos", "p2b-test"), client=fake
    )
    with TestClient(app) as client:
        w = World(app, client, fake)
        yield w
    engine = app.state.database_engine
    cleanup_tenant(engine, w.tenant)
    with engine.begin() as conn:
        for table in ("workflow_schedules", "workflow_versions", "workflows"):
            conn.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": w.tenant})
    engine.dispose()


def _create_agent(w: World, user: str, name: str = "Helper"):
    return w.client.post(f"{API}/agents", json={"spec": {**AGENT_SPEC, "name": name}}, headers=w.h(user))


def _start_workflow_run(w: World, user: str, workflow_id: str):
    return w.client.post(
        f"{API}/workflows/{workflow_id}/runs", json={"input": {"q": "hi"}, "run_id": str(uuid.uuid4())}, headers=w.h(user)
    )


def _schedule_body(name: str = "nightly") -> dict:
    return {"name": name, "cron": "0 3 * * *", "timezone": "UTC", "input": {"q": "x"}}


# --------------------------------------------------------------------------------- an engine that denies everything


def test_deny_all_refuses_every_protected_call_and_changes_nothing(world: World):
    agent_id, workflow_id = world.seed()
    before = {t: world.count(t) for t in ("agents", "agent_versions", "workflows", "workflow_versions", "runs")}
    revision = world.client.get(f"{API}/agents/{agent_id}", headers=world.h("ann")).headers["etag"]
    world.use(DenyAll())
    c, h = world.client, world.h("ann")

    # Lists show nothing; an unreadable resource is a 404, whatever the verb.
    assert c.get(f"{API}/agents", headers=h).json() == []
    assert c.get(f"{API}/workflows", headers=h).json() == []
    assert c.get(f"{API}/runs", headers=h).json()["items"] == []
    assert c.get(f"{API}/workflows/schedules", headers=h).json()["items"] == []
    assert c.get(f"{API}/approvals", headers=h).json()["items"] == []
    a, wf = f"{API}/agents/{agent_id}", f"{API}/workflows/{workflow_id}"
    for method, url, body in [
        ("get", a, None), ("get", f"{a}/versions", None), ("get", f"{a}/versions/1", None),
        ("get", f"{a}/versions/compare?base=1&target=draft", None),
        ("patch", a, {"spec": AGENT_SPEC}), ("post", f"{a}/archive", None), ("post", f"{a}/unarchive", None),
        ("post", f"{a}/versions", None), ("post", f"{a}/runs", {"input": "hi", "run_id": str(uuid.uuid4())}),
        ("post", f"{a}/conversations", {}),
        ("get", wf, None), ("get", f"{wf}/versions", None), ("get", f"{wf}/versions/1", None),
        ("patch", wf, {"spec": WF_SPEC}), ("post", f"{wf}/versions", None),
        ("post", f"{wf}/archive", None), ("post", f"{wf}/unarchive", None),
        ("post", f"{wf}/runs", {"input": {"q": "hi"}}), ("get", f"{wf}/schedules", None),
        ("post", f"{wf}/schedules", _schedule_body()),
    ]:
        r = getattr(c, method)(url, headers=h, **({"json": body} if body is not None else {}))
        assert r.status_code == 404, (method, url, r.status_code, r.text)
    # Creating has no resource to hide: refused as forbidden.
    assert _create_agent(world, "ann", "Nope").status_code == 403
    assert c.post(f"{API}/workflows", json={"spec": WF_SPEC}, headers=h).status_code == 403

    after = {t: world.count(t) for t in before}
    assert after == before  # no agent, version, workflow or run was written
    world.use(world.engine)
    assert c.get(f"{API}/agents/{agent_id}", headers=world.h("ann")).headers["etag"] == revision  # not even a revision bump


def test_deny_all_refuses_the_agui_and_mcp_entry_points_too(world: World):
    agent_id, workflow_id = world.seed()
    world.use(DenyAll())
    h = world.h("ann")
    body = {"threadId": "t1", "runId": "r1", "messages": [{"id": "m1", "role": "user", "content": "hi"}], "tools": [],
            "context": [], "state": {}, "forwardedProps": {}}
    assert world.client.post(f"{API}/ag-ui/agents/{agent_id}", json=body, headers=h).status_code == 404
    assert world.client.get(f"{API}/ag-ui/agents/{agent_id}/threads/t1", headers=h).status_code == 404
    assert world.client.post(f"{API}/ag-ui/workflows/{workflow_id}", json=body, headers=h).status_code == 404
    assert world.count("runs") == 0


def test_a_failing_engine_fails_closed_with_503(world: World):
    agent_id, _ = world.seed()
    def broken(request):
        import httpx2

        raise httpx2.ConnectError("down")

    world.engine.engine.client._http = __import__("httpx2").AsyncClient(transport=__import__("httpx2").MockTransport(broken))
    for r in (
        world.client.get(f"{API}/agents", headers=world.h("ann")),
        world.client.get(f"{API}/agents/{agent_id}", headers=world.h("ann")),
        _create_agent(world, "ann", "Late"),
    ):
        assert r.status_code == 503, r.text


# --------------------------------------------------------------------------------------- allow-all is today


def test_an_allow_all_engine_reproduces_todays_behaviour(world: World):
    world.use(AllowAll())
    h = world.h("anyone")
    agent = _create_agent(world, "anyone").json()
    assert world.client.post(f"{API}/agents/{agent['id']}/versions", headers=h).status_code == 201
    assert world.client.post(
        f"{API}/agents/{agent['id']}/runs", json={"input": "hi", "run_id": str(uuid.uuid4())}, headers=h
    ).status_code == 201
    assert [a["id"] for a in world.client.get(f"{API}/agents", headers=h).json()] == [agent["id"]]


# ---------------------------------------------------------------------------------- the real model: roles


def test_a_viewer_lists_and_reads_but_cannot_draft_publish_run_or_archive(world: World):
    agent_id, workflow_id = world.seed()
    c, h = world.client, world.h("vic")
    before = {t: world.count(t) for t in ("agents", "agent_versions", "workflows", "workflow_versions", "runs")}
    assert [a["id"] for a in c.get(f"{API}/agents", headers=h).json()] == [agent_id]
    assert [w["id"] for w in c.get(f"{API}/workflows", headers=h).json()] == [workflow_id]
    assert c.get(f"{API}/agents/{agent_id}", headers=h).status_code == 200
    assert c.get(f"{API}/agents/{agent_id}/versions", headers=h).status_code == 200
    assert c.get(f"{API}/workflows/{workflow_id}/versions/1", headers=h).status_code == 200
    assert c.get(f"{API}/runs", headers=h).status_code == 200

    forbidden = [
        ("post", f"{API}/agents", {"spec": AGENT_SPEC}),
        ("patch", f"{API}/agents/{agent_id}", {"spec": AGENT_SPEC}),
        ("post", f"{API}/agents/{agent_id}/versions", None),
        ("post", f"{API}/agents/{agent_id}/archive", None),
        ("post", f"{API}/agents/{agent_id}/runs", {"input": "hi", "run_id": str(uuid.uuid4())}),
        ("post", f"{API}/agents/{agent_id}/conversations", {}),
        ("post", f"{API}/workflows/{workflow_id}/versions", None),
        ("post", f"{API}/workflows/{workflow_id}/archive", None),
        ("post", f"{API}/workflows/{workflow_id}/runs", {"input": {"q": "hi"}}),
        ("post", f"{API}/workflows/{workflow_id}/schedules", _schedule_body()),
    ]
    for method, url, body in forbidden:
        r = getattr(c, method)(url, headers=h, **({"json": body} if body is not None else {}))
        assert r.status_code == 403, (method, url, r.status_code, r.text)
        assert r.json()["detail"] == "not allowed"
    assert {t: world.count(t) for t in before} == before


def test_an_editor_drafts_publishes_runs_and_archives_what_it_owns(world: World):
    agent_id, workflow_id = world.seed()  # ann's
    c, h = world.client, world.h("eve")
    mine = _create_agent(world, "eve", "Mine")
    assert mine.status_code == 201
    mine_id = mine.json()["id"]
    assert ("user:eve", "owner", f"agent:{world.tenant}/{mine_id}") in world.server.tuples
    assert (f"tenant:{world.tenant}", "tenant", f"agent:{world.tenant}/{mine_id}") in world.server.tuples  # before the response
    assert c.patch(f"{API}/agents/{mine_id}", json={"spec": {**AGENT_SPEC, "name": "Mine2"}}, headers=h).status_code == 200
    assert c.post(f"{API}/agents/{mine_id}/versions", headers=h).status_code == 201
    assert c.post(f"{API}/agents/{agent_id}/runs", json={"input": "hi", "run_id": str(uuid.uuid4())}, headers=h).status_code == 201
    assert _start_workflow_run(world, "eve", workflow_id).status_code == 202
    assert c.patch(f"{API}/agents/{agent_id}", json={"spec": {**AGENT_SPEC, "name": "Edited"}}, headers=h).status_code == 200
    # Archive is the owner's or an admin's: not ann's agent, but her own.
    assert c.post(f"{API}/agents/{agent_id}/archive", headers=h).status_code == 403
    assert c.post(f"{API}/agents/{mine_id}/archive", headers=h).status_code == 200
    # Approving is the admin's (or an approver grant): an editor can run, not decide.
    run_id = _start_workflow_run(world, "eve", workflow_id).json()["run_id"]
    assert c.post(f"{API}/workflows/runs/{run_id}/steps/approve/approve", headers=h).status_code == 403
    # Cancel is `run`: allowed (the fake launcher cannot cascade, so the cancel itself answers 503, not a refusal).
    assert c.post(f"{API}/runs/{run_id}/cancel", headers=h).status_code not in (403, 404)


def test_an_editor_makes_a_schedule_and_owns_it(world: World):
    _, workflow_id = world.seed()
    r = world.client.post(f"{API}/workflows/{workflow_id}/schedules", json=_schedule_body(), headers=world.h("eve"))
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    assert ("user:eve", "owner", f"schedule:{world.tenant}/{sid}") in world.server.tuples
    assert world.client.get(f"{API}/workflows/schedules/{sid}", headers=world.h("vic")).status_code == 200
    assert world.client.patch(f"{API}/workflows/schedules/{sid}", json={"active": False}, headers=world.h("vic")).status_code == 403
    assert world.client.delete(f"{API}/workflows/schedules/{sid}", headers=world.h("vic")).status_code == 403
    assert world.client.patch(f"{API}/workflows/schedules/{sid}", json={"active": False}, headers=world.h("eve")).status_code == 200
    assert world.client.delete(f"{API}/workflows/schedules/{sid}", headers=world.h("eve")).status_code == 204
    assert not [t for t in world.server.tuples if t[2] == f"schedule:{world.tenant}/{sid}"]  # unregistered on delete


def test_an_admin_does_everything(world: World):
    agent_id, workflow_id = world.seed()
    c, h = world.client, world.h("ann")
    assert c.post(f"{API}/agents/{agent_id}/archive?force=true", headers=h).status_code == 200  # a workflow runs it
    assert c.post(f"{API}/agents/{agent_id}/unarchive", headers=h).status_code == 200
    assert c.post(f"{API}/workflows/{workflow_id}/archive", headers=h).status_code == 200
    assert c.post(f"{API}/workflows/{workflow_id}/unarchive", headers=h).status_code == 200


def test_a_runner_grant_on_one_workflow_lets_a_viewer_run_only_that_workflow(world: World):
    _, workflow_id = world.seed()
    other = publish_demo(world.client, world.h("ann"))
    assert _start_workflow_run(world, "vic", workflow_id).status_code == 403
    world.grant("vic", "runner", "workflow", workflow_id)
    assert _start_workflow_run(world, "vic", workflow_id).status_code == 202
    assert _start_workflow_run(world, "vic", other).status_code == 403  # a viewer, no grant on this one
    assert world.client.post(f"{API}/workflows/{workflow_id}/versions", headers=world.h("vic")).status_code == 403  # no publish


def test_an_approver_grant_lets_a_viewer_decide_the_runs_of_that_workflow(world: World):
    from eap_run_store import EventLog

    _, workflow_id = world.seed()
    run_id = _start_workflow_run(world, "ann", workflow_id).json()["run_id"]
    EventLog(world.app.state.database_engine).append(
        world.tenant, uuid.UUID(run_id), "step:approve:waiting", "step_waiting", {"step_id": "approve", "workflow_id": run_id}
    )
    url = f"{API}/workflows/runs/{run_id}/steps/approve/approve"
    assert world.client.post(url, headers=world.h("vic")).status_code == 403
    world.grant("vic", "approver", "workflow", workflow_id)
    assert world.client.post(url, headers=world.h("vic")).status_code == 200
    # An approver is not a runner: the cancel of the same run stays refused.
    r = world.client.post(f"{API}/runs/{run_id}/cancel", headers=world.h("vic"))
    assert r.status_code == 403, r.text


def test_a_non_member_sees_only_what_it_was_granted(world: World):
    agent_id, workflow_id = world.seed()
    other = publish_demo(world.client, world.h("ann"))
    c, h = world.client, world.h("zed")
    assert c.get(f"{API}/workflows", headers=h).json() == []
    assert c.get(f"{API}/agents", headers=h).json() == []
    world.grant("zed", "runner", "workflow", workflow_id)
    assert [w["id"] for w in c.get(f"{API}/workflows", headers=h).json()] == [workflow_id]
    assert c.get(f"{API}/workflows/{workflow_id}", headers=h).status_code == 200
    assert c.get(f"{API}/workflows/{other}", headers=h).status_code == 404  # unreadable: indistinguishable from unknown
    assert c.get(f"{API}/agents/{agent_id}", headers=h).status_code == 404
    assert c.post(f"{API}/agents/{agent_id}/runs", json={"input": "hi", "run_id": str(uuid.uuid4())}, headers=h).status_code == 404
    run_id = _start_workflow_run(world, "zed", workflow_id).json()["run_id"]
    mine = [r["run_id"] for r in c.get(f"{API}/runs", headers=h).json()["items"]]
    assert mine == [run_id]
    ann_run = _start_workflow_run(world, "ann", other).json()["run_id"]
    assert ann_run not in [r["run_id"] for r in c.get(f"{API}/runs", headers=h).json()["items"]]
    assert c.get(f"{API}/runs/{ann_run}", headers=h).status_code == 404
    assert c.get(f"{API}/runs/{run_id}", headers=h).status_code == 200


def test_another_tenants_principal_with_this_tenants_id_is_a_404(world: World):
    agent_id, workflow_id = world.seed()
    stranger = get_auth_header(f"t{uuid.uuid4().hex[:10]}", "ann")
    assert world.client.get(f"{API}/agents/{agent_id}", headers=stranger).status_code == 404
    assert world.client.get(f"{API}/workflows/{workflow_id}", headers=stranger).status_code == 404


# ------------------------------------------------------------------------------------------------- engine cost


def test_a_list_costs_a_constant_number_of_engine_calls(world: World):
    world.seed()
    recording = Recording(world.engine)
    world.use(recording)
    h = world.h("vic")

    def calls_for(path: str) -> tuple[int, int]:
        recording.calls.clear()
        before = sum(world.server.calls.values())
        assert world.client.get(path, headers=h).status_code == 200
        return sum(recording.calls.values()), sum(world.server.calls.values()) - before

    one = {p: calls_for(p) for p in (f"{API}/agents", f"{API}/workflows", f"{API}/runs", f"{API}/workflows/schedules")}
    for i in range(12):
        assert _create_agent(world, "ann", f"Extra {i}").status_code == 201
        publish_demo(world.client, world.h("ann"))
    many = {p: calls_for(p) for p in one}
    assert many == one  # 13 agents and 13 workflows cost exactly what 1 and 1 did
    assert one[f"{API}/agents"][0] == 1 and one[f"{API}/workflows"][0] == 1  # one engine call a list
    assert one[f"{API}/runs"][0] == 2  # agents and workflows


# -------------------------------------------------------------------------------- write-before-response


def test_a_failed_registration_is_not_a_silent_success(world: World):
    world.use(world.engine)
    original = world.server.handle

    def fail_writes(request):
        import httpx2

        if request.url.path.endswith("/write"):
            return httpx2.Response(500, json={"code": "internal_error"})
        return original(request)

    world.engine.engine.client._http = __import__("httpx2").AsyncClient(
        transport=__import__("httpx2").MockTransport(fail_writes)
    )
    r = _create_agent(world, "eve", "Orphan")
    assert r.status_code == 503  # the row exists but the creator was never told it was made: surfaced, retried by reconcile
    assert not [t for t in world.server.tuples if t[1] == "owner"]


# ------------------------------------------------------------------------------------------- MCP and keys


def test_the_mcp_tools_bind_the_callers_access_to_every_resource_service():
    from types import SimpleNamespace

    from eap_api.mcp_control.routing import bind
    from eap_api.services.agents import AgentService
    from eap_api.services.workflows import WorkflowService

    sentinel = object()
    ctx = SimpleNamespace(access=sentinel)
    assert bind(ctx, AgentService(None))._access is sentinel
    assert bind(ctx, WorkflowService(None))._access is sentinel
    other = object()
    assert bind(ctx, other) is other  # a service with no per-resource rights is untouched


def test_a_key_with_allowlists_sees_only_its_own_agents_in_a_list():
    import asyncio

    from eap_iam import Only, Principal

    from eap_api.services.authz import AuthzService

    key = Principal(tenant_id="t", kind="api_key", subject="k", provider="x", agent_ids=("a1",), workflow_ids=())
    assert asyncio.run(AuthzService(AllowAll()).filter_ids(key, "read", "agent")) == Only(("a1",))
