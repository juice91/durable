"""AGUI.8: AG-UI workflow endpoints and approvals as interrupts (workflow steps, agent tool calls,
child runs of agent steps). Implementer's focused tests; the acceptance suite adds more.

No worker: a scripted DBOS client stand-in writes the durable events. On enqueue it plays the run up
to its approval wait; on the wake-up `send` (after `decide` recorded the decision) it plays the rest.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from eap_api.ag_ui.conformance import events_of, lifecycle_errors, parse_frames, schema_errors
from eap_api.app import create_app
from eap_api.launcher import DBOSRunLauncher, LauncherSettings, get_run_launcher
from eap_api.routes.ag_ui import AgUiRouteSettings, get_agui_settings
from eap_api.routes.ag_ui_workflows import derive_workflow_run_id
from eap_api.sse import StreamSettings, get_delta_source, get_stream_settings, no_deltas
from eap_run_store import EventLog, RunStore, append_event_in_session
from eap_run_store.approvals import insert_waiting_in_session
from sqlalchemy.orm import Session

from .agui4_harness import SCHEMA
from .conftest import get_auth_header
from .run_helpers import PG_URL, cleanup_tenant, hard_timeout, requires_pg

pytestmark = requires_pg

AGENT_SPEC = {"name": "Helper", "instructions": "Help.", "model": {"provider": "test", "name": "echo"}}


@pytest.fixture(autouse=True)
def _no_hang():
    with hard_timeout(90):
        yield


def _deadline() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()


LIVE_THREADS: list[threading.Thread] = []
LIVE_ERRORS: list[BaseException] = []


class _SlowLog:
    """An EventLog whose writes wait `delay` first: with Script.live_delay the scripted run writes
    from a background thread while the AG-UI stream is open, so live interleaving is exercised."""

    def __init__(self, log: EventLog, delay: float) -> None:
        self._log, self._delay = log, delay

    def __getattr__(self, name: str) -> Any:
        target = getattr(self._log, name)
        if not name.startswith("append"):
            return target

        def slow(*args: Any, **kwargs: Any) -> Any:
            time.sleep(self._delay)
            return target(*args, **kwargs)

        return slow


class Script:
    """The DBOS client stand-in. `mode` picks what a workflow enqueue plays:
    `step` (agent step, then an approval step waiting), `child` (an agent step whose child run waits
    for a gated tool call). An agent enqueue plays a turn that waits for gated call `call-1`.
    With `live_delay` > 0 every enqueue and wake-up plays in a background thread, one write every
    `live_delay` seconds, while the stream that started it is open (MAJOR 2 of the review)."""

    def __init__(self) -> None:
        self.app: Any = None
        self.mode = "step"
        self.agent_id: str | None = None
        self.enqueues: list[tuple[str, tuple[Any, ...]]] = []
        self.sent: list[tuple[str, Any, str | None]] = []
        self.live_delay = 0.0

    @property
    def log(self) -> Any:
        log = EventLog(self.app.state.database_engine)
        return _SlowLog(log, self.live_delay) if self.live_delay else log

    def _play(self, target: Any, *args: Any) -> None:
        if not self.live_delay:
            target(*args)
            return

        def run() -> None:
            try:
                target(*args)
            except BaseException as exc:  # noqa: BLE001 - reported by the fixture
                LIVE_ERRORS.append(exc)

        thread = threading.Thread(target=run, daemon=True)
        LIVE_THREADS.append(thread)
        thread.start()

    @property
    def store(self) -> RunStore:
        return RunStore(self.app.state.database_engine)

    def enqueue(self, options: dict[str, Any], *args: Any) -> None:
        self.enqueues.append((options["workflow_name"], args))
        if options["workflow_name"] == "eap.workflow_run":
            self._play(self._workflow, args[0], args[1])
        else:
            self._play(self._agent_turn, args[0], args[1])

    def send(self, destination_id: str, message: Any, topic: str | None = None, idempotency_key: str | None = None) -> None:
        self.sent.append((destination_id, message, topic))
        if topic and topic.startswith("approval:"):
            self._play(self._finish_workflow, destination_id, message["decision"])
        elif topic and topic.startswith("tool_approval:"):
            self._play(self._finish_tool, destination_id, message)

    # ------------------------------------------------------------------ scripts

    def _gated_turn(self, tenant: str, rid: uuid.UUID) -> None:
        log = self.log
        log.append(tenant, rid, "start", "run_started", {})
        call = {"tool_call_id": "call-1", "tool_name": "send_email", "args": {"to": "x"}}
        log.append(tenant, rid, "model:0:final", "model_message", {
            "model_request": 0, "text": "",
            "message": {"parts": [{"part_kind": "tool-call", "tool_call_id": "call-1", "tool_name": "send_email",
                                   "args": {"to": "x"}}]},
        })
        log.append_approval_waiting(tenant, rid, "tool", "call-1", {
            **call, "deadline": _deadline(), "workflow_id": str(rid), "batch": ["call-1"],
        })

    def _agent_turn(self, run_id: str, tenant: str) -> None:
        rid = uuid.UUID(run_id)
        if self.log.get_event(tenant, rid, "start") is not None:
            return
        self.store.mark_running(tenant, rid)
        self._gated_turn(tenant, rid)

    def _finish_tool(self, run_id: str, message: dict[str, Any]) -> None:
        tenant = self._tenant_of(run_id)
        rid = uuid.UUID(run_id)
        log = self.log
        approved = message["decision"] == "approved"
        log.append(tenant, rid, "tool:call-1:result", "tool_result", {
            "tool_call_id": "call-1", "tool_name": "send_email",
            "content": "sent" if approved else "The call was denied.", **({} if approved else {"status": "denied"}),
        })
        log.append(tenant, rid, "model:1:final", "model_message", {"model_request": 1, "text": "done"})
        log.append(tenant, rid, "end", "run_completed", {"output": "done"})
        self.store.set_status(tenant, rid, "completed", output="done")
        run = self.store.get_run(tenant, rid)
        if run and run.get("parent_run_id"):
            self._after_ask(tenant, uuid.UUID(run["parent_run_id"]))

    def _tenant_of(self, run_id: str) -> str:
        with self.app.state.database_engine.connect() as conn:
            return conn.execute(text("SELECT tenant_id FROM runs WHERE id = :r"), {"r": run_id}).scalar_one()

    def _workflow(self, run_id: str, tenant: str) -> None:
        rid = uuid.UUID(run_id)
        log = self.log
        if log.get_event(tenant, rid, "start") is not None:
            return
        self.store.mark_running(tenant, rid)
        log.append(tenant, rid, "start", "run_started", {"kind": "workflow"})
        child = uuid.uuid4()
        log.append(tenant, rid, "step:ask:started", "step_started",
                   {"step_id": "ask", "step_type": "agent", "child_run_id": str(child)})
        self.store.create_child_agent_run(tenant, uuid.UUID(self.agent_id), 1, "hi", child, rid, "ask")
        self.store.mark_running(tenant, child)
        if self.mode in ("child", "mixed"):
            self._gated_turn(tenant, child)
            if self.mode == "mixed":  # a parallel branch waits on an approval step too
                self._approval_wait(tenant, rid)
            return
        log.append(tenant, child, "start", "run_started", {})
        log.append(tenant, child, "model:0:final", "model_message", {"model_request": 0, "text": "answer"})
        log.append(tenant, child, "end", "run_completed", {"output": "answer"})
        self.store.set_status(tenant, child, "completed", output="answer")
        self._after_ask(tenant, rid)

    def _after_ask(self, tenant: str, rid: uuid.UUID) -> None:
        log = self.log
        log.append(tenant, rid, "step:ask:completed", "step_completed", {"step_id": "ask", "output": "answer"})
        if self.mode == "child":
            log.append(tenant, rid, "end", "run_completed", {"output": "answer", "result": {"ok": True}})
            self.store.set_status(tenant, rid, "completed", output='"answer"')
            return
        if self.mode == "step":
            self._approval_wait(tenant, rid)

    def _approval_wait(self, tenant: str, rid: uuid.UUID) -> None:
        log = self.log
        log.append(tenant, rid, "step:approve:started", "step_started", {"step_id": "approve", "step_type": "approval"})
        log.append_approval_waiting(tenant, rid, "step", "approve", {
            "step_id": "approve", "prompt": "OK?", "timeout_hours": 1, "workflow_id": str(rid), "deadline": _deadline(),
        })

    def _finish_workflow(self, workflow_id: str, decision: str) -> None:
        tenant = self._tenant_of(workflow_id)
        rid = uuid.UUID(workflow_id)
        log = self.log
        if decision != "approved":
            log.append(tenant, rid, "step:approve:failed", "step_failed",
                       {"step_id": "approve", "status": "failed", "error": "rejected", "error_code": "approval_rejected"})
            log.append(tenant, rid, "end", "run_failed", {"error": "The approval was rejected.", "error_code": "approval_rejected"})
            self.store.set_status(tenant, rid, "failed")
            return
        log.append(tenant, rid, "step:approve:completed", "step_completed", {"step_id": "approve", "output": {"approved": True}})
        log.append(tenant, rid, "end", "run_completed", {"output": {"approved": True}, "result": {"approved": True}})
        self.store.set_status(tenant, rid, "completed", output='{"approved":true}')


@pytest.fixture
def script() -> Script:
    return Script()


@pytest.fixture
def ag(script):
    app = create_app(database_url=PG_URL)
    script.app = app
    app.dependency_overrides[get_run_launcher] = lambda: DBOSRunLauncher(
        LauncherSettings("postgresql://unused/x", "dbos", "agui8-test"), client=script
    )
    app.dependency_overrides[get_stream_settings] = lambda: StreamSettings(poll_interval=0.02, heartbeat_interval=0.5)
    app.dependency_overrides[get_delta_source] = lambda: no_deltas
    app.dependency_overrides[get_agui_settings] = lambda: AgUiRouteSettings(previous_turn_wait_s=2.0)
    tenants: list[str] = []
    app.state.test_tenants = tenants
    with TestClient(app) as client:
        yield client, app
    for thread in LIVE_THREADS:
        thread.join(30)
    LIVE_THREADS.clear()
    errors, LIVE_ERRORS[:] = list(LIVE_ERRORS), []
    engine = app.state.database_engine
    for t in tenants:
        cleanup_tenant(engine, t)
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM workflow_versions WHERE tenant_id = :t"), {"t": t})
            conn.execute(text("DELETE FROM workflows WHERE tenant_id = :t"), {"t": t})
    engine.dispose()
    assert errors == [], errors


@pytest.fixture
def tenant(ag):
    t = f"t{uuid.uuid4().hex[:10]}"
    ag[1].state.test_tenants.append(t)
    return t


def publish(client: TestClient, headers: dict[str, str], script: Script) -> str:
    r = client.post("/api/v1/agents", json={"spec": AGENT_SPEC}, headers=headers)
    agent_id = r.json()["id"]
    assert client.post(f"/api/v1/agents/{agent_id}/versions", headers=headers).status_code == 201
    script.agent_id = agent_id
    spec = {
        "name": "Flow",
        "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"],
                         "additionalProperties": False},
        "start": "ask",
        "steps": [
            {"id": "ask", "type": "agent", "agent_id": agent_id, "input": {"p": {"$ref": "input.q"}}, "next": "approve"},
            {"id": "approve", "type": "approval", "prompt": "OK?", "timeout_hours": 1},
        ],
    }
    r = client.post("/api/v1/workflows", json={"spec": spec}, headers=headers)
    assert r.status_code == 201, r.text
    workflow_id = r.json()["id"]
    assert client.post(f"/api/v1/workflows/{workflow_id}/versions", headers=headers).status_code == 201
    return workflow_id


def body(thread: str, run: str, *, resume: list[dict[str, Any]] | None = None, messages=(), **pydanticos: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "threadId": thread, "runId": run, "messages": list(messages), "tools": [], "context": [], "state": None,
        "forwardedProps": {"pydanticos": pydanticos} if pydanticos else {},
    }
    if resume is not None:
        out["resume"] = resume
    return out


def post(client, url: str, payload: dict[str, Any], headers: dict[str, str]):
    with client.stream("POST", url, json=payload, headers=headers) as response:
        raw = response.read()
        if response.status_code != 200:
            return response.status_code, json.loads(raw), []
        return 200, None, parse_frames([raw.decode()])


def types(frames) -> list[str]:
    out = []
    for f in frames:
        if f.event is None:
            continue
        t = f.event["type"]
        out.append(f"{t}:{f.event['name']}" if t == "CUSTOM" else t)
    return out


def check(frames, **kw: Any) -> list[dict[str, Any]]:
    events = events_of(frames)
    assert schema_errors(events, SCHEMA) == []
    assert lifecycle_errors(frames, **kw) == []
    return events


def finished(frames) -> dict[str, Any]:
    return next(f.event for f in frames if f.event and f.event["type"] == "RUN_FINISHED")


def decisions(app, tenant: str) -> list[dict[str, Any]]:
    with app.state.database_engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(
            "SELECT run_id, type, data FROM execution_events WHERE tenant_id = :t AND type IN "
            "('step_approved','step_rejected','tool_approved','tool_denied') ORDER BY seq"), {"t": tenant}).mappings()]


def agui_rows(app, tenant: str) -> list[tuple[str, int]]:
    with app.state.database_engine.connect() as conn:
        return [tuple(r) for r in conn.execute(text(
            "SELECT external_run_id, from_seq FROM ag_ui_runs WHERE tenant_id = :t ORDER BY from_seq, created_at"),
            {"t": tenant})]


def wf_url(workflow_id: str) -> str:
    return f"/api/v1/ag-ui/workflows/{workflow_id}"


# --------------------------------------------------------------------------- workflow steps


def test_workflow_steps_state_and_the_step_interrupt(ag, tenant, script):
    client, app = ag
    h = get_auth_header(tenant, "alice")
    wf = publish(client, h, script)
    status, detail, frames = post(client, wf_url(wf), body("th-1", "r1", input={"q": "hi"}), h)
    assert status == 200, detail
    check(frames)
    run_id = derive_workflow_run_id(tenant, wf, "th-1")
    assert types(frames) == [
        "RUN_STARTED", "CUSTOM:pydanticos.run", "CUSTOM:pydanticos.status", "STATE_SNAPSHOT",
        "STEP_STARTED", "STATE_DELTA", "TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_END",
        "STEP_FINISHED", "STATE_DELTA", "STEP_STARTED", "STATE_DELTA", "STATE_DELTA",
        "CUSTOM:pydanticos.approval_required", "STEP_FINISHED", "RUN_FINISHED",
    ]
    text_start = next(f.event for f in frames if f.event and f.event["type"] == "TEXT_MESSAGE_START")
    assert text_start["messageId"] == "r1-ask-m0"
    outcome = finished(frames)["outcome"]
    assert outcome["type"] == "interrupt"
    (interrupt,) = outcome["interrupts"]
    assert interrupt["id"] == f"approval:{run_id}:approve" and interrupt["reason"] == "confirmation"
    assert interrupt["responseSchema"]["required"] == ["approved"]
    assert [i[0] for i in script.enqueues] == ["eap.workflow_run"]
    # Child-run frames carry no id; the last frame carries the step_waiting seq.
    assert frames[-1].id is not None and frames[-1].id.startswith("r1:")

    # The thread GET shows the run, its state and the agent step's messages.
    thread = client.get(f"{wf_url(wf)}/threads/th-1", headers=h).json()
    assert thread["run_id"] == run_id and thread["activeRunId"] == "r1"
    assert thread["state"]["steps"]["approve"]["status"] == "waiting"
    assert [m["id"] for m in thread["messages"]] == ["r1-ask-m0"]


def test_resume_records_once_wakes_the_run_and_streams_from_the_waiting_seq(ag, tenant, script):
    client, app = ag
    h = get_auth_header(tenant, "alice")
    wf = publish(client, h, script)
    _, _, first = post(client, wf_url(wf), body("th", "r1", input={"q": "hi"}), h)
    iid = finished(first)["outcome"]["interrupts"][0]["id"]
    waiting_seq = int(first[-1].id.rsplit(":", 1)[1])
    resume = [{"interruptId": iid, "status": "resolved", "payload": {"approved": True, "reason": "looks fine"}}]
    status, _, frames = post(client, wf_url(wf), body("th", "r2", resume=resume), h)
    assert status == 200
    check(frames)
    assert types(frames) == [
        "RUN_STARTED", "CUSTOM:pydanticos.run", "STATE_SNAPSHOT", "MESSAGES_SNAPSHOT", "STEP_STARTED",
        "CUSTOM:pydanticos.status",
        "STATE_DELTA", "CUSTOM:pydanticos.approval_decided", "STEP_FINISHED", "STATE_DELTA", "STATE_DELTA",
        "RUN_FINISHED",
    ]
    assert finished(frames)["outcome"] == {"type": "success"} and finished(frames)["result"] == {"approved": True}
    assert all(int(f.id.rsplit(":", 1)[1]) > waiting_seq for f in frames if f.id)
    (decision,) = decisions(app, tenant)
    assert decision["type"] == "step_approved"
    assert decision["data"]["comment"] == "looks fine" and decision["data"]["decided_by"] == "alice"
    assert agui_rows(app, tenant) == [("r1", -1), ("r2", waiting_seq)]

    # The same resume again (same runId): attaches; a new runId: accepted, the decision stands.
    status, _, again = post(client, wf_url(wf), body("th", "r2", resume=resume), h)
    assert status == 200 and finished(again)["outcome"]["type"] == "success"
    status, _, other = post(client, wf_url(wf), body("th", "r3", resume=resume), h)
    assert status == 200 and "CUSTOM:pydanticos.approval_decided" in types(other)
    assert len(decisions(app, tenant)) == 1 and len(script.sent) == 1
    # The first AG-UI run still attaches and ends at its interrupt.
    with client.stream("GET", f"{wf_url(wf)}/threads/th/runs/r1/stream", headers=h) as r:
        attached = parse_frames([r.read().decode()])
    assert finished(attached)["outcome"]["type"] == "interrupt"
    # A cursor past the interrupt names frames r1 never sent: 400.
    r = client.get(f"{wf_url(wf)}/threads/th/runs/r1/stream", headers={**h, "Last-Event-ID": f"r1:{waiting_seq + 1}"})
    assert r.status_code == 400


def test_cancelled_is_a_rejection_and_the_payload_rules(ag, tenant, script):
    client, app = ag
    h = get_auth_header(tenant, "alice")
    wf = publish(client, h, script)
    _, _, first = post(client, wf_url(wf), body("th", "r1", input={"q": "hi"}), h)
    iid = finished(first)["outcome"]["interrupts"][0]["id"]
    for payload, code in (
        ({"approved": True, "editedArgs": {"a": 1}}, "edited_args_unsupported"),
        ({"approved": "yes"}, "resume_payload_invalid"),
        ({"approved": True, "reason": "x" * 501}, "resume_payload_invalid"),
        (None, "resume_payload_invalid"),
    ):
        entry: dict[str, Any] = {"interruptId": iid, "status": "resolved"}
        if payload is not None:
            entry["payload"] = payload
        status, detail, _ = post(client, wf_url(wf), body("th", "rx", resume=[entry]), h)
        assert status == 422 and detail["detail"][0]["type"] == code, (payload, detail)
    other = f"approval:{uuid.uuid4()}:approve"
    status, detail, _ = post(client, wf_url(wf), body("th", "rx", resume=[
        {"interruptId": iid, "status": "cancelled"}, {"interruptId": other, "status": "cancelled"}]), h)
    assert status == 422 and detail["detail"][0]["type"] == "resume_unknown_interrupt"
    assert decisions(app, tenant) == []

    status, _, frames = post(client, wf_url(wf), body("th", "r2", resume=[{"interruptId": iid, "status": "cancelled"}]), h)
    assert status == 200
    check(frames)
    assert frames[-1].event["type"] == "RUN_ERROR" and frames[-1].event["code"] == "approval_rejected"
    (decision,) = decisions(app, tenant)
    assert decision["type"] == "step_rejected" and decision["data"]["comment"] == "Cancelled by user."


def test_a_new_run_id_without_resume_re_sends_the_interrupt_and_records_nothing(ag, tenant, script):
    client, app = ag
    h = get_auth_header(tenant, "alice")
    wf = publish(client, h, script)
    _, _, first = post(client, wf_url(wf), body("th", "r1", input={"q": "hi"}), h)
    status, _, frames = post(client, wf_url(wf), body("th", "r2", messages=[{"id": "u", "role": "user", "content": "?"}]), h)
    assert status == 200
    check(frames)
    assert types(frames)[0] == "RUN_STARTED" and "STATE_SNAPSHOT" in types(frames)
    assert finished(frames)["outcome"]["interrupts"] == finished(first)["outcome"]["interrupts"]
    assert agui_rows(app, tenant) == [("r1", -1)] and decisions(app, tenant) == []
    # New workflow input on the thread is 409 even while it waits (one run per thread, D3).
    status, detail, _ = post(client, wf_url(wf), body("th", "r3", input={"q": "again"}), h)
    assert status == 409 and detail["detail"][0]["type"] == "workflow_thread_running"
    assert "new threadId" in detail["detail"][0]["msg"]
    assert len(script.enqueues) == 1


def test_workflow_input_and_options_are_validated(ag, tenant, script):
    client, app = ag
    h = get_auth_header(tenant, "alice")
    wf = publish(client, h, script)
    cases = [
        (body("th", "r1", input={"q": 1}), 422, "json_schema.type"),
        (body("th", "r1", input=["q"]), 422, "workflow_input_invalid"),
        (body("th", "r1", messages=[{"id": "u", "role": "user", "content": "not json"}]), 422, "workflow_input_invalid"),
        (body("th", "r1", input={"q": "hi"}, model={"provider": "test", "name": "echo"}), 422, "run_option_unsupported"),
        (body("th", "r1", input={"q": "hi"}, version="draft"), 422, "run_option_unsupported"),
        (body("th", "r1", input={"q": "hi"}, resume=[{"interruptId": f"approval:{uuid.uuid4()}:approve",
                                                       "status": "cancelled"}]), 422, "resume_unknown_interrupt"),
    ]
    for payload, code, kind in cases:
        status, detail, _ = post(client, wf_url(wf), payload, h)
        assert status == code and detail["detail"][0]["type"] == kind, (payload, detail)
    assert script.enqueues == [] and agui_rows(app, tenant) == []
    # The last user message as JSON is the input.
    status, _, frames = post(client, wf_url(wf), body("th", "r1", messages=[
        {"id": "u", "role": "user", "content": '{"q": "hi"}'}]), h)
    assert status == 200 and finished(frames)["outcome"]["type"] == "interrupt"
    # Unknown workflow and another tenant's: 404.
    assert post(client, wf_url(str(uuid.uuid4())), body("t", "r", input={"q": "hi"}), h)[0] == 404
    other = get_auth_header(f"t{uuid.uuid4().hex[:10]}", "bob")
    assert post(client, wf_url(wf), body("th", "r1", input={"q": "hi"}), other)[0] == 404


def test_rest_decision_while_the_client_waits_is_shown_on_attach(ag, tenant, script):
    client, app = ag
    h = get_auth_header(tenant, "alice")
    wf = publish(client, h, script)
    post(client, wf_url(wf), body("th", "r1", input={"q": "hi"}), h)
    run_id = derive_workflow_run_id(tenant, wf, "th")
    r = client.post(f"/api/v1/workflows/runs/{run_id}/steps/approve/reject", json={"comment": "no"}, headers=h)
    assert r.status_code == 200, r.text
    # The thread now has no open interrupt: a resume of the decided interrupt still streams the outcome.
    status, _, frames = post(client, wf_url(wf), body("th", "r2", resume=[
        {"interruptId": f"approval:{run_id}:approve", "status": "resolved", "payload": {"approved": True}}]), h)
    assert status == 200
    decided = next(f.event for f in frames if f.event and f.event.get("name") == "pydanticos.approval_decided")
    assert decided["value"] == {"kind": "step", "key": "approve", "decision": "rejected"}
    assert frames[-1].event["type"] == "RUN_ERROR"
    assert [d["type"] for d in decisions(app, tenant)] == ["step_rejected"]


# --------------------------------------------------------------------------- tool approvals


def test_child_run_tool_interrupt_resumes_the_child(ag, tenant, script):
    client, app = ag
    h = get_auth_header(tenant, "alice")
    wf = publish(client, h, script)
    script.mode = "child"
    _, _, first = post(client, wf_url(wf), body("th", "r1", input={"q": "hi"}), h)
    check(first)
    assert "TOOL_CALL_START" in types(first)
    (interrupt,) = finished(first)["outcome"]["interrupts"]
    child = interrupt["metadata"]["pydanticos"]["run_id"]
    assert interrupt["id"] == f"approval:{child}:tool:call-1" and interrupt["toolCallId"] == "call-1"
    resume = [{"interruptId": interrupt["id"], "status": "resolved", "payload": {"approved": False, "reason": "no"}}]
    status, _, frames = post(client, wf_url(wf), body("th", "r2", resume=resume), h)
    assert status == 200
    check(frames, known_tool_calls=["call-1"])
    t = types(frames)
    assert t.index("CUSTOM:pydanticos.approval_decided") < t.index("TOOL_CALL_RESULT") < t.index("STEP_FINISHED")
    assert "TOOL_CALL_START" not in t  # the call was announced by r1
    assert finished(frames)["outcome"] == {"type": "success"}
    (decision,) = decisions(app, tenant)
    assert decision["type"] == "tool_denied" and str(decision["run_id"]) == child and decision["data"]["comment"] == "no"
    # An attach of r2 later is the same stream: the answered child continues after its batch.
    with client.stream("GET", f"{wf_url(wf)}/threads/th/runs/r2/stream", headers=h) as r:
        attached = parse_frames([r.read().decode()])
    check(attached, known_tool_calls=["call-1"])
    no_status = lambda fs: [x for x in types(fs) if x != "CUSTOM:pydanticos.status"]  # noqa: E731
    assert no_status(attached) == no_status(frames)
    # The run has ended: a new runId without resume is 409 and says what to send.
    status, detail, _ = post(client, wf_url(wf), body("th", "r9", messages=[{"id": "u", "role": "user", "content": "?"}]), h)
    assert status == 409 and detail["detail"][0]["type"] == "workflow_thread_finished"
    assert "new threadId" in detail["detail"][0]["msg"]


def test_mixed_open_interrupts_are_all_sent_in_order(ag, tenant, script):
    client, app = ag
    h = get_auth_header(tenant, "alice")
    wf = publish(client, h, script)
    script.mode = "mixed"
    run_id = derive_workflow_run_id(tenant, wf, "th")
    status, _, first = post(client, wf_url(wf), body("th", "r1", input={"q": "hi"}), h)
    assert status == 200
    check(first)
    ids = [i["id"] for i in finished(first)["outcome"]["interrupts"]]
    child = next(i["metadata"]["pydanticos"]["run_id"] for i in finished(first)["outcome"]["interrupts"]
                 if i["reason"] == "tool_call")
    expected = [f"approval:{child}:tool:call-1", f"approval:{run_id}:approve"]  # the order requested
    # r1 closed at the gated call; the approval step was raised after that point in the global
    # order, so r1 names only the call (a replay names the same), and Q5 names both.
    assert ids == expected[:1]
    # Q5: a plain client's next message gets both again, in the same order.
    status, _, again = post(client, wf_url(wf), body("th", "r2", messages=[{"id": "u", "role": "user", "content": "?"}]), h)
    assert status == 200
    check(again)
    assert [i["id"] for i in finished(again)["outcome"]["interrupts"]] == expected
    snapshot = next(f.event for f in again if f.event["type"] == "MESSAGES_SNAPSHOT")
    assert any(c["id"] == "call-1" for m in snapshot["messages"] for c in m.get("toolCalls", []))
    assert agui_rows(app, tenant) == [("r1", -1)]
    # Answering one is 422 (both are open); answering both continues the run.
    one = [{"interruptId": expected[1], "status": "resolved", "payload": {"approved": True}}]
    assert post(client, wf_url(wf), body("th", "r3", resume=one), h)[1]["detail"][0]["type"] == "resume_missing_interrupt"
    both = [{"interruptId": i, "status": "resolved", "payload": {"approved": True}} for i in expected]
    status, _, frames = post(client, wf_url(wf), body("th", "r3", resume=both), h)
    assert status == 200
    check(frames, known_tool_calls=["call-1"])
    assert finished(frames)["outcome"]["type"] == "success"
    assert sorted(d["type"] for d in decisions(app, tenant)) == ["step_approved", "tool_approved"]
    # Both AG-UI runs replay exactly from every offset (an open child, a parallel approval).
    assert_attach_matches_at_every_offset(client, f"{wf_url(wf)}/threads/th/runs/r1/stream", h, "r1", first)
    assert_attach_matches_at_every_offset(client, f"{wf_url(wf)}/threads/th/runs/r3/stream", h, "r3", frames)


def test_agent_tool_interrupt_resume_and_open_interrupt_rule(ag, tenant, script):
    client, app = ag
    h = get_auth_header(tenant, "alice")
    publish(client, h, script)
    agent = script.agent_id
    url = f"/api/v1/ag-ui/agents/{agent}"
    status, _, first = post(client, url, {**body("chat", "a1"), "messages": [{"id": "u1", "role": "user", "content": "send it"}]}, h)
    assert status == 200
    check(first)
    (interrupt,) = finished(first)["outcome"]["interrupts"]
    assert interrupt["reason"] == "tool_call" and interrupt["toolCallId"] == "call-1"
    waiting = int(first[-1].id.rsplit(":", 1)[1])

    # Q5: a plain client's next message re-sends the interrupt; nothing recorded.
    status, _, again = post(client, url, {**body("chat", "a2"), "messages": [{"id": "u2", "role": "user", "content": "?"}]}, h)
    assert status == 200
    check(again)
    assert types(again)[:3] == ["RUN_STARTED", "CUSTOM:pydanticos.run", "MESSAGES_SNAPSHOT"]
    # The snapshot names the gated call before the interrupt that names it.
    snapshot = next(f.event for f in again if f.event["type"] == "MESSAGES_SNAPSHOT")
    assert any(c["id"] == "call-1" for m in snapshot["messages"] for c in m.get("toolCalls", []))
    assert "TOOL_CALL_START" not in types(again)
    assert [m["id"] for m in snapshot["messages"]][0] == "u1"
    assert finished(again)["outcome"]["interrupts"] == [interrupt]
    assert [r[0] for r in agui_rows(app, tenant)] == ["a1"]

    # editedArgs and a resume with a new message are 422; nothing decided.
    bad = [{"interruptId": interrupt["id"], "status": "resolved", "payload": {"approved": True, "editedArgs": {}}}]
    assert post(client, url, body("chat", "a3", resume=bad), h)[1]["detail"][0]["type"] == "edited_args_unsupported"
    ok = [{"interruptId": interrupt["id"], "status": "resolved", "payload": {"approved": True}}]
    with_msg = {**body("chat", "a3", resume=ok), "messages": [{"id": "u3", "role": "user", "content": "x"}]}
    assert post(client, url, with_msg, h)[1]["detail"][0]["type"] == "resume_with_input"
    assert decisions(app, tenant) == []

    status, _, frames = post(client, url, body("chat", "a3", resume=ok), h)
    assert status == 200
    check(frames, known_tool_calls=["call-1"])
    assert types(frames) == [
        "RUN_STARTED", "CUSTOM:pydanticos.run", "CUSTOM:pydanticos.status", "CUSTOM:pydanticos.approval_decided",
        "TOOL_CALL_RESULT", "TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_END", "RUN_FINISHED",
    ]
    result = next(f.event for f in frames if f.event["type"] == "TOOL_CALL_RESULT")
    assert result["messageId"] == "a1-t-call-1"  # the turn's ids keep the first AG-UI run's prefix
    text_start = next(f.event for f in frames if f.event["type"] == "TEXT_MESSAGE_START")
    assert text_start["messageId"] == "a1-m1"
    (decision,) = decisions(app, tenant)
    assert decision["type"] == "tool_approved" and decision["data"]["decided_by"] == "alice"
    assert agui_rows(app, tenant) == [("a1", -1), ("a3", waiting)]
    assert len([e for e in script.enqueues if e[0] == "eap.agent_run"]) == 1
    # The thread GET lists one turn, prefixed by its first AG-UI run.
    thread = client.get(f"{url}/threads/chat", headers=h).json()
    assert [r["runId"] for r in thread["runs"]] == ["a1"]


OPENING = {"RUN_STARTED", "CUSTOM:pydanticos.run", "STATE_SNAPSHOT", "MESSAGES_SNAPSHOT"}


def _kind(f) -> str:
    return f"{f.event['type']}:{f.event['name']}" if f.event["type"] == "CUSTOM" else f.event["type"]


def comparable(frames) -> list[tuple[str | None, str]]:
    """A stream as (frame id, event) pairs for comparison: keep-alives, status frames and the
    opening (RUN_STARTED, CUSTOM run, snapshots) left out; a RUN_FINISHED compared by its outcome
    type (the interrupt list's extras depend on what is open when it is sent)."""
    out = []
    events = [f for f in frames if f.event is not None and _kind(f) != "CUSTOM:pydanticos.status"]
    i = 0
    while i < len(events) and _kind(events[i]) in OPENING:
        i += 1
    for f in events[i:]:
        event = dict(f.event)
        if event["type"] == "RUN_FINISHED":
            event = {"type": "RUN_FINISHED", "outcome": (event.get("outcome") or {}).get("type")}
        out.append((f.id, json.dumps(event, sort_keys=True)))
    return out


def suffix_after(frames, frame_id: str):
    """The frames of a stream after the one carrying `frame_id`."""
    index = next(i for i, f in enumerate(frames) if f.id == frame_id)
    return frames[index + 1:]


def assert_attach_matches_at_every_offset(client, url: str, headers, run: str, live) -> None:
    """D7: an attach from the start replays the live stream, and a continuation after each of its
    frame ids is exactly the live stream's suffix (child-run frames, tool calls, results, CUSTOM
    extras and the end included); a cursor past the end is 400."""
    with client.stream("GET", url, headers=headers) as r:
        assert r.status_code == 200
        full = parse_frames([r.read().decode()])
    assert comparable(full) == comparable(live), (types(full), types(live))
    ids = [f.id for f in live if f.id]
    for n in ids:
        with client.stream("GET", url, headers={**headers, "Last-Event-ID": n}) as r:
            assert r.status_code == 200, n
            part = parse_frames([r.read().decode()])
        want = comparable(suffix_after(live, n))
        got = comparable(part)
        if not want:  # past the last frame: the end is sent again
            assert [k for _, k in got][-1:] == [k for _, k in comparable(live)][-1:], n
        else:
            assert got == want, (n, types(part), types(suffix_after(live, n)))
    last = int(ids[-1].rsplit(":", 1)[1])
    r = client.get(url, headers={**headers, "Last-Event-ID": f"{run}:{last + 1}"})
    assert r.status_code == 400


class ParallelScript(Script):
    """The reviewer's probe: after the child's tool decision the child keeps running (no end yet),
    and a parallel branch of the workflow starts and waits on approval step "other"."""

    def _finish_tool(self, run_id, message):
        tenant = self._tenant_of(run_id)
        rid = uuid.UUID(run_id)
        self.log.append(tenant, rid, "tool:call-1:result", "tool_result",
                        {"tool_call_id": "call-1", "tool_name": "send_email", "content": "sent"})
        run = self.store.get_run(tenant, rid)
        parent = uuid.UUID(run["parent_run_id"])
        self.log.append(tenant, parent, "step:other:started", "step_started", {"step_id": "other", "step_type": "approval"})
        self.log.append_approval_waiting(tenant, parent, "step", "other", {
            "step_id": "other", "prompt": "OK?", "timeout_hours": 1, "workflow_id": str(parent), "deadline": _deadline()})
        self.log.append(tenant, rid, "model:1:final", "model_message", {"model_request": 1, "text": "still working"})


def test_parallel_branch_and_open_child_replay_exactly_from_every_offset(ag, tenant):
    client, app = ag
    script = ParallelScript()
    script.app = app
    script.mode = "child"
    script.live_delay = 0.05  # every write lands while the stream is open
    app.dependency_overrides[get_run_launcher] = lambda: DBOSRunLauncher(
        LauncherSettings("postgresql://unused/x", "dbos", "agui8-test"), client=script
    )
    h = get_auth_header(tenant, "alice")
    wf = publish(client, h, script)
    run_id = derive_workflow_run_id(tenant, wf, "th")
    status, _, r1 = post(client, wf_url(wf), body("th", "r1", input={"q": "hi"}), h)
    assert status == 200
    check(r1)
    (x,) = finished(r1)["outcome"]["interrupts"]
    assert ":tool:" in x["id"] and "TOOL_CALL_START" in types(r1)
    resume = [{"interruptId": x["id"], "status": "resolved", "payload": {"approved": True}}]
    status, _, r2 = post(client, wf_url(wf), body("th", "r2", resume=resume), h)
    assert status == 200
    check(r2, known_tool_calls=["call-1"])
    t2 = types(r2)
    assert t2.index("CUSTOM:pydanticos.approval_decided") < t2.index("TOOL_CALL_RESULT") < t2.index("RUN_FINISHED")
    assert [i["id"] for i in finished(r2)["outcome"]["interrupts"]] == [f"approval:{run_id}:other"]
    # r1 ended at the child interrupt: its attach never goes past it, at any offset.
    assert_attach_matches_at_every_offset(client, f"{wf_url(wf)}/threads/th/runs/r1/stream", h, "r1", r1)
    assert_attach_matches_at_every_offset(client, f"{wf_url(wf)}/threads/th/runs/r2/stream", h, "r2", r2)


def test_continuation_inside_an_open_agent_step_is_the_exact_suffix(ag, tenant, script):
    client, app = ag
    h = get_auth_header(tenant, "alice")
    wf = publish(client, h, script)
    payload = body("th", "r1", input={"q": "hi"})
    _, _, first = post(client, wf_url(wf), payload, h)
    ask_started = next(f.id for f in first if f.id and f.event["type"] == "STATE_DELTA"
                       and f.event["delta"][0]["path"] == "/steps/ask")
    with client.stream("POST", wf_url(wf), json=payload, headers={**h, "Last-Event-ID": ask_started}) as r:
        assert r.status_code == 200
        frames = parse_frames([r.read().decode()])
    check(frames, continuation=True, run_id="r1", known_open_steps=["ask"])
    t = types(frames)
    assert t[:2] == ["CUSTOM:pydanticos.run", "STATE_SNAPSHOT"] and "RUN_STARTED" not in t
    # The child's text came after that frame live, so it is replayed, not only snapshotted.
    assert comparable(frames) == comparable(suffix_after(first, ask_started))
    assert finished(frames)["outcome"]["type"] == "interrupt"
    assert len(script.enqueues) == 2  # the same runId re-sends the hand-off (DBOS dedupes it)
    assert_attach_matches_at_every_offset(client, f"{wf_url(wf)}/threads/th/runs/r1/stream", h, "r1", first)



class TwoBranchScript(Script):
    """The reviewer's probe 3: two parallel agent steps. A's gated call is raised first but its
    transaction commits 1.5 s late; B's is raised 0.15 s later. All while r1's stream is open."""

    def _workflow(self, run_id: str, tenant: str) -> None:
        rid = uuid.UUID(run_id)
        log = EventLog(self.app.state.database_engine)
        self.store.mark_running(tenant, rid)
        log.append(tenant, rid, "start", "run_started", {"kind": "workflow"})
        children = {}
        for step in ("a", "b"):
            child = uuid.uuid4()
            log.append(tenant, rid, f"step:{step}:started", "step_started",
                       {"step_id": step, "step_type": "agent", "child_run_id": str(child)})
            self.store.create_child_agent_run(tenant, uuid.UUID(self.agent_id), 1, "hi", child, rid, step)
            self.store.mark_running(tenant, child)
            log.append(tenant, child, "start", "run_started", {})
            children[step] = child
        data = lambda call: {"tool_call_id": call, "tool_name": "send_email", "args": {"to": call},  # noqa: E731
                             "deadline": _deadline(), "batch": [call]}

        def late_a() -> None:
            with Session(self.app.state.database_engine) as session:
                event, _ = append_event_in_session(session, tenant, children["a"], "tool:ca:approval:waiting",
                                                   "tool_approval_waiting", {**data("ca"), "workflow_id": str(children["a"])})
                insert_waiting_in_session(session, tenant_id=tenant, run_id=str(children["a"]), kind="tool",
                                          approval_key="ca", data=data("ca"), requested_at=event.ts)
                time.sleep(1.5)
                session.commit()

        a = threading.Thread(target=late_a)
        a.start()
        time.sleep(0.15)
        log.append_approval_waiting(tenant, children["b"], "tool", "cb", {**data("cb"), "workflow_id": str(children["b"])})
        a.join(10)


def test_a_late_commit_in_a_parallel_branch_never_moves_the_runs_end(ag, tenant):
    client, app = ag
    script = TwoBranchScript()
    script.app = app
    script.live_delay = 0.01
    app.dependency_overrides[get_run_launcher] = lambda: DBOSRunLauncher(
        LauncherSettings("postgresql://unused/x", "dbos", "agui8-test"), client=script
    )
    h = get_auth_header(tenant, "alice")
    wf = publish(client, h, script)
    status, _, r1 = post(client, wf_url(wf), body("th", "r1", input={"q": "hi"}), h)
    assert status == 200
    check(r1)
    # The root lock ordered B after A's late commit: the live run ends at A, and so does every replay.
    ids = [i["id"] for i in finished(r1)["outcome"]["interrupts"]]
    assert len(ids) == 1 and ids[0].endswith(":tool:ca"), ids
    for thread in LIVE_THREADS:
        thread.join(30)
    assert_attach_matches_at_every_offset(client, f"{wf_url(wf)}/threads/th/runs/r1/stream", h, "r1", r1)
    # Q5 names both, A first (the order requested and committed).
    status, _, again = post(client, wf_url(wf), body("th", "r2", messages=[{"id": "u", "role": "user", "content": "?"}]), h)
    assert status == 200
    names = [i["id"].rsplit(":", 1)[1] for i in finished(again)["outcome"]["interrupts"]]
    assert names == ["ca", "cb"]


def test_a_workflow_q5_cut_after_run_started_resumes_with_minus_one(ag, tenant, script):
    """D7, as on the agent path: the Q5 re-send records nothing, so a client that lost it after
    RUN_STARTED re-POSTs with `{runId}:-1` and gets the interrupts again, without RUN_STARTED."""
    client, app = ag
    h = get_auth_header(tenant, "alice")
    wf = publish(client, h, script)
    _, _, first = post(client, wf_url(wf), body("th", "r1", input={"q": "hi"}), h)
    q5 = body("th", "r2", messages=[{"id": "u", "role": "user", "content": "?"}])
    with client.stream("POST", wf_url(wf), json=q5, headers={**h, "Last-Event-ID": "r2:-1"}) as r:
        assert r.status_code == 200
        frames = parse_frames([r.read().decode()])
    check(frames, continuation=True, run_id="r2")
    assert "RUN_STARTED" not in types(frames)
    assert finished(frames)["outcome"]["interrupts"] == finished(first)["outcome"]["interrupts"]
    assert agui_rows(app, tenant) == [("r1", -1)]  # still nothing recorded
    # Any other cursor on an unknown runId is 404, and records nothing either.
    for cursor in ("r2:0", "r2:5", "other:-1"):
        with client.stream("POST", wf_url(wf), json=q5, headers={**h, "Last-Event-ID": cursor}) as r:
            assert r.status_code == 404, cursor
            r.read()
    assert agui_rows(app, tenant) == [("r1", -1)]


def test_thread_and_run_ids_must_be_addressable_url_path_segments(ag, tenant, script):
    """A threadId or runId is also a path segment of the attach routes: '/', '\\', '..', a bare
    '.' and non-ASCII are refused with 422 before anything runs; the GET routes answer 404."""
    client, app = ag
    h = get_auth_header(tenant, "alice")
    wf = publish(client, h, script)
    agent = f"/api/v1/ag-ui/agents/{script.agent_id}"
    user = [{"id": "u", "role": "user", "content": "hi"}]
    for bad in ("org/thread", "..", ".", "a..b", "back\\slash", "thé"):
        for url, payload in ((wf_url(wf), body(bad, "r1", input={"q": "hi"})), (agent, body(bad, "r1", messages=user))):
            status, detail, _ = post(client, url, payload, h)
            assert status == 422 and detail["detail"][0]["type"] == "thread_id_invalid", (bad, url, detail)
            assert detail["detail"][0]["loc"] == ["body", "threadId"]
        status, detail, _ = post(client, wf_url(wf), body("th", bad, input={"q": "hi"}), h)
        assert status == 422 and detail["detail"][0]["type"] == "run_id_invalid", (bad, detail)
    assert script.enqueues == [] and agui_rows(app, tenant) == []
    # Ordinary ids (spaces, dots, colons and other printable ASCII) still work end to end.
    thread = "org:team th.1~"
    status, _, frames = post(client, wf_url(wf), body(thread, "run 1.a", input={"q": "hi"}), h)
    assert status == 200 and finished(frames)["outcome"]["type"] == "interrupt"
    from urllib.parse import quote
    url = f"{wf_url(wf)}/threads/{quote(thread, safe='')}/runs/{quote('run 1.a', safe='')}/stream"
    with client.stream("GET", url, headers=h) as r:
        body_ = r.read()
        assert r.status_code == 200, (url, body_)
    assert client.get(f"{wf_url(wf)}/threads/a..b", headers=h).status_code == 404
