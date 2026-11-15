"""AGUI.15: AG-UI streams across killed processes (the crash half of the crash, isolation and
security suite; the API half is apps/api/api_tests/test_agui15_security.py).

Everything is real, as in the rest of this directory (README.md): the API runs in its own process
(`api_server.py`, uvicorn, real HTTP) and the workers are `python -m eap_worker` processes killed by
their own crash hook (`EAP_CRASH_AT`, `os._exit(137)`). An AG-UI stream (`POST /api/v1/ag-ui/...`)
is read in a background thread while the worker dies, so each test sees what a connected client
sees. Plan: docs/plans/2026-11-13-milestone-ag-ui.md, "### AGUI.15" and the Resume protocol (D7).

| AGUI.15 item | test |
|---|---|
| kill the worker during a model request with an open stream: it stays open, shows recovering, the run finishes once, a divergence is corrected by a snapshot, side effects once | `test_worker_kill_during_a_model_request_keeps_the_agent_stream_open` |
| kill the worker after a client tool call (around the pending end; and after the result of the next turn) | `test_worker_kill_around_a_client_tool_pending_end_and_its_result` |
| kill the worker during a tool approval interrupt and during its resume | `test_worker_kill_during_a_tool_approval_interrupt_and_its_resume` |
| kill the worker during a workflow approval wait, and in the resumed run's child step | `test_worker_kill_during_a_workflow_approval_wait_and_its_resume` |
| kill the worker mid workflow stream inside a child agent run | `test_worker_kill_inside_a_child_run_mid_workflow_stream` |
| kill the API mid-stream: the D7 re-POST with `Last-Event-ID` splices with no duplicate or missing message | `test_api_kill_mid_stream_resumes_by_re_post_without_duplicates` |

Each scenario checks the vendored AG-UI 1.0 schema and the lifecycle checker on every stream it
read (a continuation as a continuation), exactly-once durable state (one `end`, gap-free seq, one
event per key, one DBOS workflow in SUCCESS) and the external side effects (fake tickets).
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from typing import Any

import pytest

from conftest import REPO_ROOT, TICKET_SPEC, Harness, wait_for

from eap_api.ag_ui.conformance import Frame, lifecycle_errors, schema_errors

SCHEMA = json.loads((REPO_ROOT / "tests/fixtures/ag-ui/schema-1.0.json").read_text())
ENV: dict[str, str | None] = {
    "EAP_ALLOWED_MODELS": "test:*",
    "EAP_APPROVAL_POLL_SECONDS": "1",
    "EAP_WORKFLOW_HOUR_SECONDS": "3600",
}
ECHO_SPEC = {"name": "Echo", "instructions": "Repeat.", "model": {"provider": "test", "name": "echo"}, "tools": []}
FRONTEND_SPEC = {"name": "Front", "instructions": "Help.", "model": {"provider": "test", "name": "frontend_tool"},
                 "tools": []}
TICKET_ARGS = {"title": "Laptop broken", "body": "It will not boot."}
GATED_SPEC = {"name": "Gated", "instructions": "Help.", "model": {"provider": "test", "name": "approval"},
              "tools": ["tickets.create"], "approvals": {"tools": ["tickets.create"]}}
GATED_PROMPT = json.dumps({"tool": "tickets.create", "args": TICKET_ARGS})
WEATHER = {
    "name": "get_weather", "description": "Ask the browser for the weather.",
    "parameters": {"type": "object", "properties": {"city": {"type": "string", "minLength": 1}},
                   "required": ["city"], "additionalProperties": False},
}
Q_SCHEMA = {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}


# --------------------------------------------------------------------------- an AG-UI client


class AgUiStream:
    """One AG-UI request read to its end in a background thread, frame by frame (a frame is
    recorded only once fully read, like ResumableHttpAgent's id tracking)."""

    def __init__(self, base_url: str, method: str, url: str, headers: dict[str, str],
                 payload: dict[str, Any] | None = None) -> None:
        import httpx2

        self.frames: list[Frame] = []
        self.status: int | None = None
        self.detail: Any = None
        self.error: BaseException | None = None
        self._client = httpx2.Client(base_url=base_url, timeout=httpx2.Timeout(10.0, read=180.0))
        self._args = (method, url, headers, payload)
        self._thread = threading.Thread(target=self._run, name="agui-stream", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        method, url, headers, payload = self._args
        try:
            kwargs: dict[str, Any] = {"headers": headers}
            if payload is not None:
                kwargs["json"] = payload
            with self._client.stream(method, url, **kwargs) as response:
                self.status = response.status_code
                if response.status_code != 200:
                    self.detail = json.loads(response.read())
                    return
                event: dict[str, Any] | None = None
                frame_id: str | None = None
                comment: str | None = None
                for line in response.iter_lines():
                    if line == "":
                        if event is not None or comment is not None:
                            self.frames.append(Frame(event, frame_id, comment))
                        event, frame_id, comment = None, None, None
                    elif line.startswith(":"):
                        comment = line[1:].strip()
                    else:
                        name, _, value = line.partition(":")
                        value = value[1:] if value.startswith(" ") else value
                        if name == "data":
                            event = json.loads(value)
                        elif name == "id":
                            frame_id = value
        except BaseException as exc:  # noqa: BLE001 - a killed API is expected in one scenario
            self.error = exc
        finally:
            self._client.close()

    @property
    def events(self) -> list[dict[str, Any]]:
        return [f.event for f in list(self.frames) if f.event is not None]

    def types(self) -> list[str]:
        return [f"{e['type']}:{e['name']}" if e["type"] == "CUSTOM" else e["type"] for e in self.events]

    @property
    def open(self) -> bool:
        return self._thread.is_alive()

    def ended(self) -> bool:
        return bool(self.events) and self.events[-1]["type"] in ("RUN_FINISHED", "RUN_ERROR")

    def wait(self, predicate: Callable[[AgUiStream], Any], what: str, timeout: float = 60) -> Any:
        def check() -> Any:
            if self.error is not None and not self.open:
                raise AssertionError(f"stream failed while waiting for {what}: {self.error!r}")
            return predicate(self)

        return wait_for(check, timeout=timeout, what=what)

    def join(self, timeout: float = 120) -> AgUiStream:
        self._thread.join(timeout)
        assert not self._thread.is_alive(), f"stream still open after {timeout}s: {self.types()}"
        assert self.status == 200, (self.status, self.detail)
        assert self.error is None, self.error
        return self

    def abort(self) -> None:
        self._client.close()


def check(frames: list[Frame], *, continuation: bool = False, run_id: str | None = None) -> None:
    events = [f.event for f in frames if f.event is not None]
    assert schema_errors(events, SCHEMA) == []
    assert lifecycle_errors(frames, continuation=continuation, run_id=run_id) == []


def body(thread: str, run: str, *, messages: list[dict[str, Any]] = (), tools: list[dict[str, Any]] = (),
         resume: list[dict[str, Any]] | None = None, **pydanticos: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"threadId": thread, "runId": run, "messages": list(messages), "tools": list(tools),
                           "context": [], "state": None,
                           "forwardedProps": {"pydanticos": pydanticos} if pydanticos else {}}
    if resume is not None:
        out["resume"] = resume
    return out


def user(mid: str, content: str) -> dict[str, Any]:
    return {"id": mid, "role": "user", "content": content}


def approve(ids: list[str]) -> list[dict[str, Any]]:
    return [{"interruptId": i, "status": "resolved", "payload": {"approved": True}} for i in ids]


def finished(stream: AgUiStream) -> dict[str, Any]:
    return next(e for e in reversed(stream.events) if e["type"] == "RUN_FINISHED")


def interrupt_ids(stream: AgUiStream) -> list[str]:
    outcome = finished(stream).get("outcome") or {}
    return [i["id"] for i in outcome.get("interrupts", [])] if outcome.get("type") == "interrupt" else []


def text_of(events: list[dict[str, Any]]) -> str:
    return "".join(e.get("delta", "") for e in events if e["type"] == "TEXT_MESSAGE_CONTENT")


# --------------------------------------------------------------------------- the world


class AgUi:
    """The harness with the API process started, AG-UI helpers and durable-state checks."""

    def __init__(self, h: Harness) -> None:
        self.h = h
        self.api = h.start_api(extra_env=ENV)

    def stream(self, url: str, payload: dict[str, Any] | None = None, *, method: str = "POST",
               extra: dict[str, str] | None = None) -> AgUiStream:
        return AgUiStream(self.api.base_url, method, url, {**self.h.headers, **(extra or {})}, payload)

    def native_run(self, external_run_id: str) -> str:
        def found() -> str | None:
            rows = self.h.sql("SELECT run_id FROM ag_ui_runs WHERE tenant_id = :t AND external_run_id = :r",
                              t=self.h.tenant, r=external_run_id)
            return str(rows[0][0]) if rows else None

        run_id = wait_for(found, timeout=30, what=f"the ag_ui_runs row of {external_run_id}")
        if run_id not in self.h.run_ids:
            self.h.run_ids.append(run_id)
        return run_id

    def crash_worker(self, name: str, crash_at: str, event_key: str | None = None) -> Any:
        self.h.crash_marker.unlink(missing_ok=True)  # each scenario may crash more than once
        return self.h.start_worker(name, crash_at=crash_at, event_key=event_key, extra_env=ENV)

    def worker(self, name: str) -> Any:
        return self.h.start_worker(name, extra_env=ENV)

    def expect_crash(self, worker: Any, crash_at: str) -> None:
        assert worker.wait_exit() == 137, worker.log()[-4000:]
        assert f"EAP_CRASH_AT={crash_at} reached" in worker.log()

    def backdate_heartbeat(self, run_id: str) -> None:
        """What 20 s without a worker look like (store.RECOVERING_AFTER): the run reads `recovering`."""
        self.h.sql_exec("UPDATE runs SET heartbeat_at = now() - interval '5 minutes' WHERE id = :r", r=run_id)

    def assert_once(self, run_id: str, *, status: str = "completed") -> list[Any]:
        run = self.h.wait_terminal(run_id)
        assert run["status"] == status, (run, self.h.worker_logs())
        events = self.h.events(run_id)
        seqs, keys = [e.seq for e in events], [e.event_key for e in events]
        assert seqs == list(range(len(events))), seqs
        assert len(keys) == len(set(keys)), keys
        assert keys.count("end") == 1 and keys[-1] == "end"
        assert [w[0] for w in self.h.workflows(run_id)] == ["SUCCESS"], self.h.workflows(run_id)
        return events


def _sql_exec(self: Harness, query: str, **params: Any) -> None:
    from sqlalchemy import text

    with self.engine.begin() as conn:
        conn.execute(text(query), params)


Harness.sql_exec = _sql_exec  # type: ignore[attr-defined]


@pytest.fixture
def ag(harness: Harness) -> Any:
    try:
        yield AgUi(harness)
    finally:
        with harness.engine.begin() as conn:
            from sqlalchemy import text

            conn.execute(text("DELETE FROM api_keys WHERE tenant_id = :t"), {"t": harness.tenant})
            conn.execute(text("DELETE FROM pending_approvals WHERE tenant_id = :t"), {"t": harness.tenant})


def agent_url(agent_id: str) -> str:
    return f"/api/v1/ag-ui/agents/{agent_id}"


def wf_url(workflow_id: str) -> str:
    return f"/api/v1/ag-ui/workflows/{workflow_id}"


# --------------------------------------------------------------------------- 1. model request


def test_worker_kill_during_a_model_request_keeps_the_agent_stream_open(ag: AgUi, report) -> None:
    """test:ticket: request 0 calls tickets.create, request 1 streams the answer. The worker dies
    right after the first text delta of request 1 (published over NOTIFY, nothing checkpointed)."""
    h = ag.h
    agent = h.create_published_agent(TICKET_SPEC)
    crashing = ag.crash_worker("worker1-crashing", "model_request")
    stream = ag.stream(agent_url(agent), body("t", "r1", messages=[user("u1", "my laptop is broken")]))
    run_id = ag.native_run("r1")
    ag.expect_crash(crashing, "model_request")
    # The client saw the first delta of the crashed attempt; the stream stays open.
    stream.wait(lambda s: "TEXT_MESSAGE_CONTENT" in s.types(), "the crashed attempt's first delta")
    time.sleep(1.5)
    assert stream.open and not stream.ended(), stream.types()
    ag.backdate_heartbeat(run_id)
    stream.wait(lambda s: any(e["type"] == "CUSTOM" and e["name"] == "pydanticos.status"
                              and e["value"]["display_status"] == "recovering" for e in s.events), "recovering")
    assert stream.open and not stream.ended()
    ag.worker("worker2-recovering")
    stream.join()
    events = stream.events
    check(stream.frames, run_id="r1")
    assert events[-1]["type"] == "RUN_FINISHED" and events[-1]["outcome"]["type"] == "success", events[-1]
    # Exactly once: one run, one end, one ticket (the tool call was checkpointed before the crash).
    durable = ag.assert_once(run_id)
    output = h.run_row(run_id)["output"]
    assert len(h.tickets(run_id)) == 1 and h.ticket_calls(run_id) == 1
    assert [e.type for e in durable].count("tool_result") == 1
    assert [e["type"] for e in events].count("TOOL_CALL_START") == 1
    assert [e["type"] for e in events].count("TOOL_CALL_RESULT") == 1
    # The divergence (a second attempt of request 1) is corrected by a snapshot after the text,
    # whose last assistant message is the run's answer; the streamed text is never doubled.
    first_text = next(i for i, e in enumerate(events) if e["type"] == "TEXT_MESSAGE_START" and e["messageId"].endswith("-m1"))
    snapshots = [i for i, e in enumerate(events) if e["type"] == "MESSAGES_SNAPSHOT" and i > first_text]
    assert snapshots, stream.types()
    last = [m for m in events[snapshots[-1]]["messages"] if m["role"] == "assistant"][-1]
    assert last["content"] == output
    assert len(text_of(events)) <= len(output)
    # A fresh attach of the finished run is a conformant, complete stream with the same answer.
    again = ag.stream(f"{agent_url(agent)}/threads/t/runs/r1/stream", method="GET").join()
    check(again.frames, run_id="r1")
    assert text_of(again.events) == output and "MESSAGES_SNAPSHOT" in again.types()
    report("AGUI.15-1 model_request kill: stream open, recovering shown, snapshot corrected, tickets=1")


# --------------------------------------------------------------------------- 2. client tools


def _tool_answer(call_id: str, content: str) -> list[dict[str, Any]]:
    return [
        user("u1", "weather?"),
        {"id": "r1-m0", "role": "assistant",
         "toolCalls": [{"id": call_id, "type": "function", "function": {"name": "get_weather", "arguments": "{}"}}]},
        {"id": "tm1", "role": "tool", "toolCallId": call_id, "content": content},
    ]


@pytest.mark.parametrize("event_key", ["tool:*:call", "end"], ids=["after-the-call-event", "after-the-pending-end"])
def test_worker_kill_around_a_client_tool_pending_end_and_its_result(ag: AgUi, report, event_key: str) -> None:
    h = ag.h
    agent = h.create_published_agent(FRONTEND_SPEC)
    crashing = ag.crash_worker("worker1-crashing", "after_event_write", event_key)
    stream = ag.stream(agent_url(agent), body("t", "r1", messages=[user("u1", "weather?")], tools=[WEATHER]))
    first = ag.native_run("r1")
    ag.expect_crash(crashing, "after_event_write")
    time.sleep(1.0)
    if event_key == "tool:*:call":
        assert stream.open and not stream.ended(), stream.types()  # the run has not ended
    recovering = ag.worker("worker2-recovering")
    stream.join()
    check(stream.frames, run_id="r1")
    assert finished(stream)["outcome"]["type"] == "success"
    starts = [e for e in stream.events if e["type"] == "TOOL_CALL_START"]
    assert [s["toolCallName"] for s in starts] == ["get_weather"]
    assert "TOOL_CALL_RESULT" not in stream.types()  # the browser runs it, never the server
    call_id = starts[0]["toolCallId"]
    durable = ag.assert_once(first)
    (end,) = [e for e in durable if e.type == "run_completed"]
    assert end.data["pending_tool_call_ids"] == [call_id]
    assert not [e for e in durable if e.type == "tool_result"]
    # Turn 2 carries the browser's result; the worker dies right after recording it.
    recovering.kill()
    crashing = ag.crash_worker("worker3-crashing", "after_event_write", "tool:*:result")
    stream2 = ag.stream(agent_url(agent), body("t", "r2", messages=_tool_answer(call_id, "sunny"), tools=[WEATHER]))
    second = ag.native_run("r2")
    ag.expect_crash(crashing, "after_event_write")
    time.sleep(1.0)
    assert stream2.open and not stream2.ended(), stream2.types()
    ag.worker("worker4-recovering")
    stream2.join()
    check(stream2.frames, run_id="r2")
    assert finished(stream2)["outcome"]["type"] == "success" and text_of(stream2.events) == "tool said: sunny"
    durable2 = ag.assert_once(second)
    assert [e.event_key for e in durable2].count(f"tool:{call_id}:result") == 1
    assert [e.event_key for e in durable2 if e.event_key.startswith("model:")] == ["model:0:final"]
    # The thread holds each message once.
    snap = h.client.get(f"{agent_url(agent)}/threads/t", headers=h.headers).json()
    ids = [m["id"] for m in snap["messages"]]
    assert len(ids) == len(set(ids)) and [m["role"] for m in snap["messages"]].count("tool") == 1, snap["messages"]
    report(f"AGUI.15-2 client tool kill ({event_key}, then tool:*:result): one pending end, one result, answered once")


# --------------------------------------------------------------------------- 3. tool approvals


def test_worker_kill_during_a_tool_approval_interrupt_and_its_resume(ag: AgUi, report) -> None:
    h = ag.h
    agent = h.create_published_agent(GATED_SPEC)
    crashing = ag.crash_worker("worker1-crashing", "tool_approval_waiting")
    stream = ag.stream(agent_url(agent), body("t", "a1", messages=[user("u1", GATED_PROMPT)]))
    run_id = ag.native_run("a1")
    ag.expect_crash(crashing, "tool_approval_waiting")
    # The waiting event was written before the crash: the client gets its interrupt anyway.
    stream.join()
    check(stream.frames, run_id="a1")
    (iid,) = interrupt_ids(stream)
    assert iid.startswith(f"approval:{run_id}:tool:")
    assert h.run_row(run_id)["status"] == "running" and h.tickets(run_id) == []
    # The resume is decided while no worker runs; the woken worker dies after the tool's result.
    resume = ag.stream(agent_url(agent), body("t", "a2", resume=approve([iid])))
    resume.wait(lambda s: "CUSTOM:pydanticos.run" in s.types(), "the resume run's start")
    decided = wait_for(lambda: [e for e in h.events(run_id) if e.type == "tool_approved"] or None, timeout=30,
                       what="the decision event")
    assert len(decided) == 1 and resume.open
    crashing = ag.crash_worker("worker2-crashing", "after_event_write", "tool:*:result")
    ag.expect_crash(crashing, "after_event_write")
    time.sleep(1.0)
    assert resume.open and not resume.ended(), resume.types()
    ag.worker("worker3-recovering")
    resume.join()
    check(resume.frames, continuation=False, run_id="a2")
    assert finished(resume)["outcome"]["type"] == "success"
    durable = ag.assert_once(run_id)
    keys = [e.event_key for e in durable]
    call = iid.rsplit(":", 1)[1]
    assert keys.count(f"tool:{call}:approval:waiting") == 1 and keys.count(f"tool:{call}:result") == 1
    assert [e.type for e in durable].count("tool_approved") == 1
    assert len(h.tickets(run_id)) == 1 and h.ticket_calls(run_id) == 1
    assert [e["type"] for e in resume.events].count("TOOL_CALL_RESULT") == 1
    assert h.sql("SELECT count(*) FROM pending_approvals WHERE tenant_id = :t AND decided_at IS NULL",
                 t=h.tenant)[0][0] == 0
    # The same resume re-POSTed decides nothing new.
    again = ag.stream(agent_url(agent), body("t", "a2", resume=approve([iid]))).join()
    check(again.frames, run_id="a2")
    assert [e.type for e in h.events(run_id)].count("tool_approved") == 1 and len(h.tickets(run_id)) == 1
    report("AGUI.15-3 tool approval kill (waiting, then after the result): interrupt shown, decided once, ticket once")


# --------------------------------------------------------------------------- 4. workflow approvals


def _approval_workflow(h: Harness, echo: str) -> str:
    """ask (agent) -> t (tickets.create) -> approve (approval) -> done (agent)."""
    return h.create_published_workflow({
        "name": "Approval", "input_schema": Q_SCHEMA, "start": "ask",
        "steps": [
            {"id": "ask", "type": "agent", "agent_id": echo, "input": {"prompt": {"$ref": "input.q"}}, "next": "t"},
            {"id": "t", "type": "tool", "tool_id": "tickets.create",
             "input": {"title": "Workflow ticket", "body": {"$ref": "steps.ask.output"}}, "next": "approve"},
            {"id": "approve", "type": "approval", "prompt": "Ship it?", "timeout_hours": 1, "next": "done"},
            {"id": "done", "type": "agent", "agent_id": echo, "input": {"prompt": {"$ref": "steps.ask.output"}}},
        ],
    })


def test_worker_kill_during_a_workflow_approval_wait_and_its_resume(ag: AgUi, report) -> None:
    h = ag.h
    workflow = _approval_workflow(h, h.create_published_agent(ECHO_SPEC))
    crashing = ag.crash_worker("worker1-crashing", "workflow_waiting", "step:approve:waiting")
    stream = ag.stream(wf_url(workflow), body("th", "w1", input={"q": "printer on fire"}))
    run_id = ag.native_run("w1")
    ag.expect_crash(crashing, "workflow_waiting")
    stream.join()
    check(stream.frames, run_id="w1")
    assert interrupt_ids(stream) == [f"approval:{run_id}:approve"]
    assert len(h.tickets(run_id)) == 1
    # Decided while no worker runs; the woken run dies right after inserting the `done` child.
    resume = ag.stream(wf_url(workflow), body("th", "w2", resume=approve([f"approval:{run_id}:approve"])))
    wait_for(lambda: [e for e in h.events(run_id) if e.type == "step_approved"] or None, timeout=30, what="decision")
    crashing = ag.crash_worker("worker2-crashing", "after_child_run_insert", "step:done:child")
    ag.expect_crash(crashing, "after_child_run_insert")
    time.sleep(1.0)
    assert resume.open and not resume.ended(), resume.types()
    ag.worker("worker3-recovering")
    resume.join()
    check(resume.frames, run_id="w2")
    assert finished(resume)["outcome"]["type"] == "success"
    durable = ag.assert_once(run_id)
    assert [e.type for e in durable].count("step_approved") == 1 and len(h.tickets(run_id)) == 1
    children = h.child_runs(run_id)
    assert sorted(c[1] for c in children) == ["ask", "done"] and all(c[2] == "completed" for c in children), children
    for child_id, *_ in children:
        assert [w[0] for w in h.workflows(child_id)] == ["SUCCESS"]
    # The `done` step's answer is in the resume stream once.
    started = [e["messageId"] for e in resume.events if e["type"] == "TEXT_MESSAGE_START"]
    assert len(started) == len(set(started)) and started, resume.types()
    assert [e["stepName"] for e in resume.events if e["type"] == "STEP_FINISHED"].count("done") == 1
    report("AGUI.15-4 workflow kill (waiting, then after the child insert): interrupt shown, decided once, children once")


# --------------------------------------------------------------------------- 5. child runs


def test_worker_kill_inside_a_child_run_mid_workflow_stream(ag: AgUi, report) -> None:
    """ask (echo) -> t (tickets.create) -> done (echo): the worker dies right after the first
    streamed delta of the `ask` child; the open workflow stream carries the child's correction."""
    h = ag.h
    echo = h.create_published_agent(ECHO_SPEC)
    workflow = h.create_published_workflow({
        "name": "Linear", "input_schema": Q_SCHEMA, "start": "ask",
        "steps": [
            {"id": "ask", "type": "agent", "agent_id": echo, "input": {"prompt": {"$ref": "input.q"}}, "next": "t"},
            {"id": "t", "type": "tool", "tool_id": "tickets.create",
             "input": {"title": "Workflow ticket", "body": {"$ref": "steps.ask.output"}}, "next": "done"},
            {"id": "done", "type": "agent", "agent_id": echo, "input": {"prompt": {"$ref": "steps.ask.output"}}},
        ],
    })
    crashing = ag.crash_worker("worker1-crashing", "model_request")
    stream = ag.stream(wf_url(workflow), body("th", "w1", input={"q": "printer on fire, please help"}))
    run_id = ag.native_run("w1")
    ag.expect_crash(crashing, "model_request")
    stream.wait(lambda s: "TEXT_MESSAGE_CONTENT" in s.types(), "the child's first delta")
    time.sleep(1.0)
    assert stream.open and not stream.ended(), stream.types()
    ag.worker("worker2-recovering")
    stream.join()
    check(stream.frames, run_id="w1")
    assert finished(stream)["outcome"]["type"] == "success"
    ag.assert_once(run_id)
    children = h.child_runs(run_id)
    assert sorted(c[1] for c in children) == ["ask", "done"] and all(c[2] == "completed" for c in children)
    for child_id, *_ in children:
        ag.assert_once(child_id)
    assert len(h.tickets(run_id)) == 1 and h.ticket_calls(run_id) == 1
    steps = [e["stepName"] for e in stream.events if e["type"] == "STEP_STARTED"]
    assert steps.count("ask") == 1 and steps.count("done") == 1, steps
    # No message is started twice, and the client's final view holds each child's answer exactly
    # once and undoubled (the crashed attempt's partial text was corrected, not appended to).
    started = [e["messageId"] for e in stream.events if e["type"] == "TEXT_MESSAGE_START"]
    assert len(started) == len(set(started)), started
    final = [m.get("content") for m in reduce_messages(stream.events) if m["role"] == "assistant" and m.get("content")]
    for child_id, step_id, *_ in children:
        output = h.events(child_id)[-1].data["output"]
        assert final.count(output) == 1, (step_id, final)
        assert not [t for t in final if t != output and t.startswith(output)], (step_id, final)
    report("AGUI.15-5 kill inside a child run: workflow stream stayed open, children and ticket once")


# --------------------------------------------------------------------------- 6. API kill


def reduce_messages(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The messages a client holds after these events (AG-UI's apply semantics, enough for text,
    tool calls, tool results and snapshots)."""
    messages: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}

    def put(message: dict[str, Any]) -> dict[str, Any]:
        if message["id"] in by_id:
            return by_id[message["id"]]
        messages.append(message)
        by_id[message["id"]] = message
        return message

    for e in events:
        t = e["type"]
        if t == "MESSAGES_SNAPSHOT":
            messages, by_id = [], {}
            for m in e["messages"]:
                put(json.loads(json.dumps(m)))
        elif t == "TEXT_MESSAGE_START":
            put({"id": e["messageId"], "role": e.get("role", "assistant"), "content": ""})
        elif t == "TEXT_MESSAGE_CONTENT":
            m = by_id[e["messageId"]]
            m["content"] = (m.get("content") or "") + e["delta"]
        elif t == "TOOL_CALL_START":
            parent = put({"id": e.get("parentMessageId") or e["toolCallId"], "role": "assistant", "content": ""})
            parent.setdefault("toolCalls", []).append(
                {"id": e["toolCallId"], "type": "function", "function": {"name": e["toolCallName"], "arguments": ""}})
        elif t == "TOOL_CALL_ARGS":
            for m in messages:
                for c in m.get("toolCalls") or []:
                    if c["id"] == e["toolCallId"]:
                        c["function"]["arguments"] += e["delta"]
        elif t == "TOOL_CALL_RESULT":
            put({"id": e["messageId"], "role": "tool", "toolCallId": e["toolCallId"], "content": e["content"]})
    return messages


def _shape(messages: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    return [(m["id"], m["role"], m.get("content") or "", tuple(c["id"] for c in m.get("toolCalls") or []),
             m.get("toolCallId")) for m in messages]


def test_api_kill_mid_stream_resumes_by_re_post_without_duplicates(ag: AgUi, report) -> None:
    """The client half of D7 as ResumableHttpAgent does it (apps/studio/src/lib/agui/): keep the
    frames up to the last fully read `id:`, re-POST the identical body with `Last-Event-ID` to a
    new API process, splice the continuation in. The run is held mid-stream by a worker killed
    inside its model request, so the API dies while the stream is really open."""
    h = ag.h
    agent = h.create_published_agent(TICKET_SPEC)
    crashing = ag.crash_worker("worker1-crashing", "model_request")
    payload = body("t", "r1", messages=[user("u1", "my laptop is broken")])
    stream = ag.stream(agent_url(agent), payload)
    run_id = ag.native_run("r1")
    ag.expect_crash(crashing, "model_request")
    stream.wait(lambda s: any(f.id for f in s.frames) and "TEXT_MESSAGE_CONTENT" in s.types(), "frames with ids")
    ag.api.kill()  # the API dies with the stream open
    wait_for(lambda: not stream.open, timeout=30, what="the client to see the connection drop")
    assert not stream.ended(), stream.types()
    held = list(stream.frames)
    last = max(i for i, f in enumerate(held) if f.id)
    cursor = held[last].id
    prefix = held[: last + 1]
    # A partial text message the client holds is closed locally (ResumableHttpAgent's synthetic
    # END); the continuation's snapshot then replaces it.
    ag.api = h.start_api("api2", extra_env=ENV)
    ag.worker("worker2-recovering")
    cont = ag.stream(agent_url(agent), payload, extra={"Last-Event-ID": cursor}).join()
    check(cont.frames, continuation=True, run_id="r1")
    assert "RUN_STARTED" not in cont.types() and cont.types()[0] == "CUSTOM:pydanticos.run"
    assert cont.types()[1] == "MESSAGES_SNAPSHOT"
    cont_ids = [f.id for f in cont.frames if f.id]
    prefix_ids = [f.id for f in prefix if f.id]
    seq = lambda i: int(i.rsplit(":", 1)[1])  # noqa: E731
    assert not set(cont_ids) & set(prefix_ids)
    assert [seq(i) for i in prefix_ids + cont_ids] == sorted(seq(i) for i in prefix_ids + cont_ids)
    spliced = [f.event for f in prefix if f.event] + [f.event for f in cont.frames if f.event]
    ag.assert_once(run_id)
    server = h.client.get(f"{agent_url(agent)}/threads/t", headers=h.headers).json()["messages"]
    assert _shape(reduce_messages(spliced)) == _shape(server), (reduce_messages(spliced), server)
    assert len(h.tickets(run_id)) == 1
    # Every other offset splices to the same messages (the continuation is exactly the suffix).
    full = ag.stream(f"{agent_url(agent)}/threads/t/runs/r1/stream", method="GET").join()
    ids = [f.id for f in full.frames if f.id]
    for n, frame_id in enumerate(ids):
        tail = ag.stream(agent_url(agent), payload, extra={"Last-Event-ID": frame_id}).join()
        upto = full.frames[: [f.id for f in full.frames].index(frame_id) + 1]
        merged = [f.event for f in upto if f.event] + [f.event for f in tail.frames if f.event]
        assert _shape(reduce_messages(merged)) == _shape(server), (n, frame_id)
    # A runId with percent signs is addressable over real HTTP (Starlette's TestClient decodes
    # twice, so the in-process suite cannot check this).
    from urllib.parse import quote

    pct = ag.stream(agent_url(agent), body("t", "%2e%2e%25", messages=[user("u2", "again")])).join()
    check(pct.frames, run_id="%2e%2e%25")
    attach = ag.stream(f"{agent_url(agent)}/threads/t/runs/{quote('%2e%2e%25', safe='')}/stream", method="GET").join()
    assert attach.events[0]["runId"] == "%2e%2e%25"
    report(f"AGUI.15-6 API kill mid-stream: re-POST from {cursor} spliced to the server's messages; all offsets agree")
