"""Recorded workflow-loop event fixtures in the loops D4 shapes (LP.9), for pure trace tests.

Every key comes from the eap_schemas.workflow_scope helpers, exactly as eap_worker.workflow_run
writes them: body step events `loop:{S}:step:{id}:started|completed|failed` (data adds `scope`),
iteration events `loop:{S}:started|completed|failed` with `{step_id, scope (the enclosing
iterations), index, state | item}`, and child agent runs `uuid5("{run_id}:{S}:{id}")` with
parent_step_id `{S}/{id}`. All events of a run, the iterations' included, are in the workflow run's
own log.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from eap_schemas.workflow_scope import (
    child_scope,
    iteration_event_key,
    scope_to_data,
    scoped_child_run_id,
    scoped_event_key,
    scoped_parent_step_id,
)

from eap_run_store.trace import RunData

T0 = datetime(2026, 11, 15, 9, 0, 0, tzinfo=timezone.utc)
RUN = "11111111-1111-4111-8111-111111111111"


class Log:
    """Builds one run's event list in append order, one second apart."""

    def __init__(self, start: datetime = T0) -> None:
        self.events: list[dict[str, Any]] = []
        self.start = start

    def add(self, key: str, etype: str, data: dict[str, Any]) -> dict[str, Any]:
        event = {
            "seq": len(self.events) + 1, "event_key": key, "type": etype, "data": data,
            "ts": self.start + timedelta(seconds=len(self.events) + 1),
        }
        self.events.append(event)
        return event

    @property
    def now(self) -> datetime:
        return self.start + timedelta(seconds=len(self.events))

    # -- steps
    def step_started(self, scope, step_id, step_type, **extra):
        data = {"step_id": step_id, "step_type": step_type, **extra}
        if scope:
            data["scope"] = scope_to_data(scope)
        return self.add(scoped_event_key(scope, step_id, "started"), "step_started", data)

    def step_completed(self, scope, step_id, output):
        data = {"step_id": step_id, "output": output}
        if scope:
            data["scope"] = scope_to_data(scope)
        return self.add(scoped_event_key(scope, step_id, "completed"), "step_completed", data)

    def step_failed(self, scope, step_id, code, error="the step failed"):
        data = {"step_id": step_id, "status": "failed", "error": error, "error_code": code}
        if scope:
            data["scope"] = scope_to_data(scope)
        return self.add(scoped_event_key(scope, step_id, "failed"), "step_failed", data)

    # -- iterations
    def iteration_started(self, outer, loop_id, index, *, state=None, item=None, foreach=False):
        data = {"step_id": loop_id, "scope": scope_to_data(outer), "index": index}
        if foreach:
            data["item"] = item
        else:
            data["state"] = state
        return self.add(iteration_event_key(child_scope(outer, loop_id, index), "started"), "loop_iteration_started", data)

    def iteration_completed(self, outer, loop_id, index, last_key, *, state=None, until=None, foreach=False):
        data = {"step_id": loop_id, "scope": scope_to_data(outer), "index": index, "last_key": last_key}
        if not foreach:
            data["state"] = state
            if until is not None:
                data["until"] = until
        return self.add(
            iteration_event_key(child_scope(outer, loop_id, index), "completed"), "loop_iteration_completed", data
        )

    def iteration_failed(self, outer, loop_id, index, code, failed_step_id, error="the step failed"):
        data = {
            "step_id": loop_id, "scope": scope_to_data(outer), "index": index, "status": "failed", "error": error,
            "error_code": code, "failed_step_id": failed_step_id,
        }
        return self.add(iteration_event_key(child_scope(outer, loop_id, index), "failed"), "loop_iteration_failed", data)


def workflow_run(status: str = "completed", **kw: Any) -> dict[str, Any]:
    base = {
        "id": RUN, "kind": "workflow", "status": status, "display_status": status, "workflow_id": "wf",
        "workflow_version": 1, "created_at": T0, "started_at": T0,
        "finished_at": T0 + timedelta(hours=1) if status in ("completed", "failed", "needs_attention") else None,
        "updated_at": T0 + timedelta(hours=1), "input": {}, "output": None,
    }
    base.update(kw)
    return base


def child_run(scope, step_id: str, started: datetime, status: str = "completed") -> RunData:
    """The agent run of body agent step `step_id` in iteration `scope` (its own log)."""
    rid = scoped_child_run_id(RUN, scope, step_id)
    log = Log(started)
    log.add("start", "run_started", {"input": "q"})
    log.add("model:0:final", "model_message", {"model_request": 0, "text": "a", "message": {"parts": []}})
    if status == "completed":
        log.add("end", "run_completed", {"output": "a"})
    run = {
        "id": rid, "kind": "agent", "status": status, "display_status": status, "agent_id": "writer",
        "agent_version": 1, "parent_run_id": RUN, "parent_step_id": scoped_parent_step_id(scope, step_id),
        "created_at": started, "started_at": started,
        "finished_at": log.now if status == "completed" else None, "updated_at": log.now, "input": "q",
        "output": "a",
    }
    return RunData(run=run, events=log.events, requests={})


def body_agent(log: Log, scope, step_id: str, children: list[RunData], output: Any = "a") -> str:
    """A body agent step with its child run; returns its completed event key."""
    log.step_started(scope, step_id, "agent", agent_id="writer", agent_version=1,
                     child_run_id=scoped_child_run_id(RUN, scope, step_id), input={"q": "x"})
    children.append(child_run(scope, step_id, log.now))
    return log.step_completed(scope, step_id, output)["event_key"]


def refine_loop(iterations: int = 3, status: str = "completed") -> tuple[dict[str, Any], list[dict], list[RunData]]:
    """A top-level `loop` step `refine` (body: agent `draft` -> tool `check`) running `iterations`
    iterations, the last one meeting `until`; then the run ends."""
    log = Log()
    children: list[RunData] = []
    log.add("start", "run_started", {"kind": "workflow", "input": {}})
    log.step_started((), "refine", "loop")
    state = {"attempt": 0}
    last_key = None
    for i in range(iterations):
        scope = child_scope((), "refine", i)
        log.iteration_started((), "refine", i, state=state)
        body_agent(log, scope, "draft", children)
        log.step_started(scope, "check", "tool", tool_id="lint", input={"text": "a"})
        last_key = log.step_completed(scope, "check", {"ok": i == iterations - 1})["event_key"]
        state = {"attempt": i + 1}
        log.iteration_completed((), "refine", i, last_key, state=state, until="true" if i == iterations - 1 else "false")
    if status == "completed":
        log.step_completed((), "refine", {"iterations": iterations, "exited": "condition", "last": {"ok": True},
                                           "state": state})
        log.add("end", "run_completed", {"output": {"ok": True}})
    return workflow_run(status), log.events, children


__all__ = ["Log", "RUN", "T0", "body_agent", "child_run", "refine_loop", "workflow_run"]
