"""The Architect, end to end (Task 9): the acceptance tests of the milestone.

Real Postgres, the in-process worker and the real API (TestClient on the worker's DBOS schema). The
models are scripted: the Architect runs as `test:approval` (it calls the tool named in the prompt JSON,
one per run), the created agents run as `test:echo`. The drafter run is finished by a launcher that
returns the output the test scripts for the description (no model); every other run goes to the real
launcher, so the Architect's gated calls wait for a real decision through the API.
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from typing import Any

import pytest
from sqlalchemy import text

from worker_tests.conftest import APP_VERSION, wait_terminal
from worker_tests.test_tool_approval import wait_waiting
from worker_tests.test_workflow_run_acceptance import auth_header, workflow_api

API = "/api/v1"


@pytest.fixture
def schema(worker_settings) -> str:
    return worker_settings.dbos_system_schema


class World:
    """A tenant, its API client and the helpers the tests share."""

    def __init__(self, worker, client, api, tenant: str) -> None:
        self.worker, self.client, self.api, self.tenant = worker, client, api, tenant
        self.h = auth_header(tenant)
        self.draft_output: Any = None
        self.draft_inputs: list[str] = []
        self.drafter_id: str | None = None
        self.architect_id: str | None = None

    # ------------------------------------------------------------------ API
    def seed(self) -> dict[str, Any]:
        r = self.api.post(f"{API}/architect", headers=self.h)
        assert r.status_code in (200, 201), r.text
        body = r.json()
        self.architect_id, self.drafter_id = body["architect_agent_id"], body["drafter_agent_id"]
        return body

    def get(self, path: str) -> Any:
        r = self.api.get(f"{API}{path}", headers=self.h)
        assert r.status_code == 200, (path, r.text)
        return r.json()

    def post(self, path: str, body: dict[str, Any] | None = None, status: int | tuple[int, ...] = (200, 201)) -> Any:
        r = self.api.post(f"{API}{path}", json=body or {}, headers=self.h)
        assert r.status_code in ((status,) if isinstance(status, int) else status), (path, r.status_code, r.text)
        return r.json() if r.content else None

    def page_agent(self, spec: dict[str, Any], publish: bool = True, expected_version: int | None = None) -> dict[str, Any]:
        body = {"spec": spec, "publish": publish}
        if expected_version is not None:
            body["expected_version"] = expected_version
        return self.post("/architect/agents", body)

    def page_workflow(self, spec: dict[str, Any], publish: bool = True) -> dict[str, Any]:
        return self.post("/architect/workflows", {"spec": spec, "publish": publish})

    # ------------------------------------------------------------- Architect runs
    def architect_run(self, tool: str, args: dict[str, Any]) -> str:
        """Start one Architect run that calls `tool` once (the scripted `test:approval` model)."""
        run_id = str(uuid.uuid4())
        self.post(
            f"/agents/{self.architect_id}/runs",
            {"run_id": run_id, "input": json.dumps({"tool": tool, "args": args}), "model": {"provider": "test", "name": "approval"}},
        )
        return run_id

    def finish(self, run_id: str, timeout: float = 60.0) -> dict[str, Any]:
        run = wait_terminal(self.worker.run_store, self.tenant, uuid.UUID(run_id), timeout=timeout)
        assert run["status"] == "completed", run["error"]
        return run

    def decide(self, run_id: str, approve: bool) -> dict[str, Any]:
        (waiting,) = wait_waiting(self.worker, uuid.UUID(run_id))
        verb = "approve" if approve else "deny"
        r = self.api.post(
            f"{API}/runs/{run_id}/tool-approvals/{waiting['tool_call_id']}/{verb}", json={}, headers=self.h
        )
        assert r.status_code == 200, r.text
        return waiting

    def approved_call(self, tool: str, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        run_id = self.architect_run(tool, args)
        self.decide(run_id, True)
        return run_id, self.finish(run_id)

    def free_call(self, tool: str, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        run_id = self.architect_run(tool, args)
        return run_id, self.finish(run_id)

    # --------------------------------------------------------------- database
    def sql(self, query: str, **params: Any) -> list[Any]:
        with self.worker.engine.connect() as conn:
            return list(conn.execute(text(query), {"t": self.tenant, **params}).mappings().all())

    def agent_row(self, slug: str) -> dict[str, Any]:
        (row,) = self.sql("SELECT * FROM agents WHERE tenant_id = :t AND slug = :s", s=slug)
        return dict(row)

    def agent_versions(self, slug: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.sql(
            "SELECT v.version, v.created_via, v.created_by, v.spec FROM agent_versions v JOIN agents a ON a.id = v.agent_id "
            "WHERE a.tenant_id = :t AND a.slug = :s ORDER BY v.version", s=slug)]


class DrafterLauncher:
    """The real launcher, except that the drafter's run is finished with the scripted draft."""

    def __init__(self, real, world: World) -> None:
        self._real, self._world = real, world

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def launch(self, *, run_id, tenant_id, agent_id, version, spec_json, input_text, traceparent=None) -> None:
        w = self._world
        if str(agent_id) != w.drafter_id:
            return self._real.launch(
                run_id=run_id, tenant_id=tenant_id, agent_id=agent_id, version=version, spec_json=spec_json,
                input_text=input_text, traceparent=traceparent,
            )
        w.draft_inputs.append(input_text)
        w.worker.run_store.finish_with_end(
            tenant_id, run_id, "completed", output=json.dumps(w.draft_output), output_format="json"
        )


@contextmanager
def world_for(worker, client, tenant: str, schema: str):
    from eap_api.launcher import get_run_launcher

    with workflow_api(schema, APP_VERSION) as api:
        w = World(worker, client, api, tenant)
        real = api.app.dependency_overrides[get_run_launcher]()
        api.app.dependency_overrides[get_run_launcher] = lambda: DrafterLauncher(real, w)
        try:
            yield w
        finally:
            with worker.engine.begin() as conn:
                for table in ("workflow_schedules", "agent_versions", "agents", "workflow_versions", "workflows"):
                    conn.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tenant})


@pytest.fixture
def world(worker, client, tenant, schema):
    with world_for(worker, client, tenant, schema) as w:
        yield w


GREETER = {
    "name": "Greeter", "id": "greeter", "description": "Greets people.", "instructions": "Greet the person warmly.",
    "model": "test:echo", "tools": ["clock.now"],
}


def tool_result(run: dict[str, Any], tool: str) -> dict[str, Any]:
    """The JSON a tool returned, read from the scripted model's one-line answer."""
    line = run["output"].splitlines()[0]
    prefix = f"Tool {tool} outcome=success content="
    assert line.startswith(prefix), line
    return json.loads(line[len(prefix):])


# ---------------------------------------------------------------------------------------------- seed


def test_seed_makes_three_builtin_agents_and_is_idempotent(world):
    first = world.seed()
    assert {"architect_agent_id", "drafter_agent_id", "workflow_drafter_agent_id", "model"} <= set(first)
    again = world.post("/architect")
    assert {k: again[k] for k in first} == first
    rows = world.sql("SELECT slug, builtin FROM agents WHERE tenant_id = :t ORDER BY slug")
    assert [(r["slug"], r["builtin"]) for r in rows] == [
        ("architect", True), ("architect-drafter", True), ("architect-workflow-drafter", True)]
    state = world.get("/architect")
    assert state["seeded"] is True and state["agents"] == [] and state["workflows"] == []
    spec = world.agent_row("architect")["draft_spec"]
    assert "architect.publish_agent" in spec["approvals"]["tools"]
    assert world.get("/architect/options")["default_model"]


# ------------------------------------------------------------------- describe, draft, save, publish, chat


def test_describe_draft_save_publish_via_approval_then_the_new_agent_answers(world):
    world.seed()
    world.draft_output = {
        "name": "Greeter", "instructions": "Greet the person warmly.", "description": "Greets people.",
        "tools": ["clock.now"], "assumptions": ["Uses the clock to say good morning or evening."],
    }
    drafted = world.post("/architect/draft", {"description": "an agent that greets people"})
    assert drafted["spec"]["name"] == "Greeter" and drafted["run_id"]
    assert drafted["assumptions"] and "an agent that greets people" in world.draft_inputs[0]

    # Save the draft through the Architect run (no approval: a save is only a draft)...
    spec = {**drafted["spec"], "id": "greeter"}
    _, saved_run = world.free_call("architect.save_agent", {"spec": spec})
    assert tool_result(saved_run, "architect__save_agent")["status"] == "draft"
    assert world.agent_versions("greeter") == []

    # ...publishing waits for a person; the card says what would happen.
    run_id = world.architect_run("architect.publish_agent", {"id": "greeter"})
    waiting = world.decide(run_id, True)
    assert waiting["tool_name"] == "architect__publish_agent"
    assert "Greeter" in waiting.get("summary", "") and "version 1" in waiting["summary"]
    published = tool_result(world.finish(run_id), "architect__publish_agent")
    assert published["version"] == 1 and published["status"] == "published"
    (v1,) = world.agent_versions("greeter")
    assert v1["created_via"] == f"architect:{run_id}"

    # The studio's list shows it and who made the latest version.
    item = next(a for a in world.get("/architect")["agents"] if a["id"] == "greeter")
    assert item["versions"] == 1 and item["latest_created_via"] == f"architect:{run_id}" and item["draft"] is False

    # The new agent answers a chat run like any other agent.
    chat_id = str(uuid.uuid4())
    agent_row_id = str(world.agent_row("greeter")["id"])
    world.post(f"/agents/{agent_row_id}/runs", {"run_id": chat_id, "input": "hello there"})
    chat = world.finish(chat_id)
    assert chat["output"] == "Echo: hello there"


# --------------------------------------------------------------------------------------- workflow run


def test_a_created_workflow_with_an_agent_step_and_a_tool_step_runs(world):
    world.seed()
    agent = world.page_agent(GREETER)
    assert agent["version"] == 1
    saved = world.page_workflow({
        "name": "Greet and stamp", "id": "greet-and-stamp", "description": "Greets, then reads the clock.",
        "input_description": "Who to greet.",
        "steps": [
            {"kind": "agent", "id": "greet", "agent": "greeter", "prompt": "Say hello to {{input}}"},
            {"kind": "tool", "id": "stamp", "tool": "clock.now", "params": {}},
        ],
    })
    assert saved["version"] == 1
    run_id = str(uuid.uuid4())
    r = world.post(f"/workflows/{saved['workflow_id']}/runs", {"input": {"input": "Ada"}, "run_id": run_id}, 202)
    assert r["run_id"] == run_id
    run = world.finish(run_id)
    assert run["status"] == "completed"
    events = world.sql("SELECT event_key, type, data FROM execution_events WHERE run_id = :r ORDER BY seq", r=run_id)
    keys = [e["event_key"] for e in events]
    assert any(k.startswith("step:greet") for k in keys) and any(k.startswith("step:stamp") for k in keys), keys
    done = {e["event_key"]: e["data"] for e in events if e["type"] != "step_started"}
    # The agent step ran the created agent (test:echo) on the prompt with the input filled in...
    greeted = done["step:greet:completed"]["output"]
    assert greeted.startswith("Echo: ") and "Ada" in greeted and "Say hello to" in greeted, greeted
    # ...and the tool step read the clock; the workflow's output is the last step's.
    stamp = done["step:stamp:completed"]["output"]
    assert stamp.startswith("20") and "T" in stamp, stamp
    assert run["output"] and stamp in run["output"]


# ------------------------------------------------------------------------------- archive and schedules


def test_archive_via_approval_pauses_the_workflows_schedule(world):
    world.seed()
    world.page_agent(GREETER)
    saved = world.page_workflow({
        "name": "Morning greeting", "id": "morning",
        "steps": [{"kind": "agent", "id": "greet", "agent": "greeter", "prompt": "{{input}}"}],
    })
    wf_id = saved["workflow_id"]
    schedule = world.post(f"/workflows/{wf_id}/schedules", {
        "name": "Daily", "cron": "30 9 * * *", "timezone": "UTC", "input": {"input": "good morning"},
    })
    assert schedule["active"] is True

    # The agent cannot be archived while a published workflow runs it.
    run_id = world.architect_run("architect.archive_agent", {"id": "greeter"})
    world.decide(run_id, True)
    refused = tool_result(world.finish(run_id), "architect__archive_agent")
    assert refused["error"]["code"] == "conflict"

    run_id = world.architect_run("architect.archive_workflow", {"id": "morning"})
    waiting = world.decide(run_id, True)
    assert "Daily" in waiting["summary"] and "pauses" in waiting["summary"]
    done = tool_result(world.finish(run_id), "architect__archive_workflow")
    assert done["archived"] is True and done["paused_schedules"] == 1
    (row,) = world.sql("SELECT archived FROM workflows WHERE tenant_id = :t AND slug = 'morning'")
    assert row["archived"] is True
    (sched,) = world.sql("SELECT active FROM workflow_schedules WHERE tenant_id = :t")
    assert sched["active"] is False


# ------------------------------------------------------------------------------------------- denied


def test_a_denied_approval_changes_nothing(world):
    world.seed()
    world.page_agent(GREETER)
    # A new draft is waiting to be published.
    world.page_agent({**GREETER, "instructions": "Greet the person very formally."}, publish=False,
                    expected_version=1)
    versions_before = world.agent_versions("greeter")
    draft_before = world.agent_row("greeter")["draft_spec"]
    assert len(versions_before) == 1 and draft_before["instructions"].endswith("formally.")

    run_id = world.architect_run("architect.publish_agent", {"id": "greeter"})
    world.decide(run_id, False)
    run = world.finish(run_id)
    assert "outcome=denied" in run["output"]

    assert world.agent_versions("greeter") == versions_before
    assert world.agent_row("greeter")["draft_spec"] == draft_before
    assert world.sql("SELECT key FROM side_effects WHERE tenant_id = :t AND key LIKE 'architect:%'") == []


# --------------------------------------------------------------------------------- self-edit refusals


def test_the_architect_refuses_to_edit_itself_and_the_drafters(world):
    world.seed()
    before = {s: world.agent_row(s)["draft_spec"] for s in
              ("architect", "architect-drafter", "architect-workflow-drafter")}
    versions_query = ("SELECT 1 FROM agent_versions v JOIN agents a ON a.id = v.agent_id "
                      "WHERE a.tenant_id = :t AND a.builtin")
    versions_before = world.sql(versions_query)

    # Through a real Architect run: saving over its own id is refused in the tool result.
    _, run = world.free_call("architect.save_agent", {"spec": {**GREETER, "id": "architect"}})
    assert "outcome=success" in run["output"] and tool_result(run, "architect__save_agent")["error"]["code"] in (
        "reserved_id", "builtin_agent")

    from worker_tests.test_architect_gated_tools import run as run_async
    from worker_tests.test_architect_tools import agent_spec, allowlist
    from eap_worker.architect_tools import (
        archive_agent, publish_agent, restore_agent_version, save_agent, save_workflow)
    from eap_schemas.architect import ArchitectWorkflowSpec

    engine, tenant = world.worker.engine, world.tenant
    for slug in before:
        saved = run_async(save_agent(engine, tenant, agent_spec(id=slug), allowlist=allowlist()))
        assert saved["error"]["code"] in ("reserved_id", "builtin_agent"), (slug, saved)
        for call in (publish_agent(engine, tenant, slug), archive_agent(engine, tenant, slug),
                     restore_agent_version(engine, tenant, slug, 1)):
            assert run_async(call)["error"]["code"] == "builtin_agent", slug
    wf = ArchitectWorkflowSpec(name="Mine", id="architect", steps=[{"kind": "tool", "id": "s", "tool": "clock.now"}])
    assert run_async(save_workflow(engine, tenant, wf, allowlist=allowlist()))["error"]["code"] == "reserved_id"

    # The page's save route refuses them too, and the three definitions are untouched.
    for slug in before:
        r = world.api.post(f"{API}/architect/agents", json={"spec": {**GREETER, "id": slug}, "publish": True},
                           headers=world.h)
        assert r.status_code == 422 and r.json()["code"] == "reserved_id", (slug, r.text)
    assert {s: world.agent_row(s)["draft_spec"] for s in before} == before
    assert world.sql(versions_query) == versions_before


# ---------------------------------------------------------------------------- hand-edited definitions


def test_a_hand_edited_definition_requires_confirmation(world):
    world.seed()
    world.page_agent(GREETER)  # version 1, made by the page
    row_id = str(world.agent_row("greeter")["id"])

    # A person edits it by hand: patch the draft, publish through the plain route (no marker).
    spec = world.agent_row("greeter")["draft_spec"]
    r = world.api.patch(f"{API}/agents/{row_id}", json={"spec": {**spec, "instructions": "Greet them in rhyme."}},
                        headers=world.h)
    assert r.status_code == 200, r.text
    world.post(f"/agents/{row_id}/versions", status=201)
    v1, v2 = world.agent_versions("greeter")
    assert v1["created_via"] == "architect-page" and v2["created_via"] is None

    edit = {**GREETER, "instructions": "Greet the person warmly and briefly."}
    _, first = world.free_call("architect.save_agent", {"spec": edit})
    first_result = tool_result(first, "architect__save_agent")
    assert first_result["status"] == "needs_confirmation" and first_result["reason"] == "edited_by_hand"
    assert world.agent_row("greeter")["draft_spec"]["instructions"] == "Greet them in rhyme."  # unchanged

    confirmed_run, second = world.free_call("architect.save_agent", {"spec": edit, "confirm": True})
    assert tool_result(second, "architect__save_agent")["status"] == "draft"
    assert world.agent_row("greeter")["draft_spec"]["instructions"] == edit["instructions"]

    run_id = world.architect_run("architect.publish_agent", {"id": "greeter"})
    world.decide(run_id, True)
    assert tool_result(world.finish(run_id), "architect__publish_agent")["version"] == 3
    v3 = world.agent_versions("greeter")[-1]
    assert v3["created_via"] == f"architect:{run_id}"
    item = next(a for a in world.get("/architect")["agents"] if a["id"] == "greeter")
    assert item["versions"] == 3 and item["latest_created_via"] == f"architect:{run_id}"
    # Now that the latest version is marked, the next save needs no confirmation.
    _, again = world.free_call("architect.save_agent", {"spec": {**edit, "instructions": "Greet kindly."}})
    assert tool_result(again, "architect__save_agent")["status"] == "draft"
