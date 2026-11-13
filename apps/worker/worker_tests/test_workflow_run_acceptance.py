"""Milestone acceptance for the workflow interpreter.

The implementer's tests stay as they are. This file drives a published workflow through the
in-process worker and, where a person decides, through the HTTP approve/reject routes (the API
writes the decision event, then DBOS send). Event seq is gap-free and each event key is written
once.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from sqlalchemy import text

from worker_tests.conftest import APP_DB_URL, APP_VERSION, DBOS_DB_URL, event_rows, wait_terminal
from worker_tests.workflow_run_helpers import (
    agent_spec,
    canonical,
    cleanup_definitions,
    decide,
    enqueue_workflow_run,
    publish_agent,
    publish_workflow,
    wait_event,
)
from eap_worker.workflow_run import child_run_id


@pytest.fixture
def defs(worker, tenant):
    yield
    cleanup_definitions(worker.engine, tenant)


def auth_header(tenant: str, user: str = "alice") -> dict[str, str]:
    return {"Authorization": f"Bearer dev:{tenant}:{user}"}


def _contiguous(events) -> None:
    assert [e[0] for e in events] == list(range(len(events)))
    assert len({e[1] for e in events}) == len(events)


def stored_ticket(engine, tenant: str, idempotency_key: str) -> dict[str, str]:
    """The fake external ticket row. The tool result itself is only ticket_id and created."""
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT ticket_id, title, body, idempotency_key FROM fake_external_tickets "
                "WHERE tenant_id = :t AND idempotency_key = :k"
            ),
            {"t": tenant, "k": idempotency_key},
        ).one()
    return {
        "ticket_id": row.ticket_id,
        "title": row.title,
        "body": row.body,
        "idempotency_key": row.idempotency_key,
    }


def child_runs(engine, run_id) -> list[tuple[str, str, str]]:
    """(id, parent_step_id, status) of agent steps spawned by this workflow run."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT id, parent_step_id, status FROM runs WHERE parent_run_id = :r "
                "ORDER BY parent_step_id, id"
            ),
            {"r": str(run_id)},
        ).fetchall()
    return [(str(r.id), r.parent_step_id, r.status) for r in rows]


def milestone_spec(agent_id: str, *, timeout_hours: int = 24) -> dict[str, Any]:
    """Agent, side-effecting tool, branch on the agent's text, approval, then a final agent."""
    return {
        "name": "Triage",
        "input_schema": {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
            "additionalProperties": False,
        },
        "start": "ask",
        "steps": [
            {
                "id": "ask",
                "type": "agent",
                "agent_id": agent_id,
                "input": {"prompt": {"$ref": "input.q"}},
                "next": "ticket",
            },
            {
                "id": "ticket",
                "type": "tool",
                "tool_id": "tickets.create",
                "input": {"title": "T", "body": {"$ref": "input.q"}},
                "next": "route",
            },
            {
                "id": "route",
                "type": "branch",
                "cases": [
                    {
                        "when": {
                            "op": "contains",
                            "left": {"$ref": "steps.ask.output"},
                            "right": "urgent",
                        },
                        "next": "approve",
                    }
                ],
                "default": "done",
            },
            {
                "id": "approve",
                "type": "approval",
                "prompt": "Escalate?",
                "timeout_hours": timeout_hours,
                "next": "done",
            },
            {
                "id": "done",
                "type": "agent",
                "agent_id": agent_id,
                "input": {"prompt": {"$ref": "steps.ask.output"}},
            },
        ],
    }


def _publish_milestone(worker, tenant: str, *, timeout_hours: int = 24) -> tuple[str, str, str]:
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    workflow_id, version_id = publish_workflow(
        worker.engine, tenant, milestone_spec(agent_id, timeout_hours=timeout_hours)
    )
    return agent_id, workflow_id, version_id


@contextmanager
def workflow_api(schema: str, app_version: str) -> Iterator[Any]:
    """TestClient whose launcher enqueues and sends on `schema` (the worker's DBOS schema)."""
    previous = os.environ.get("EAP_AUTH_MODE")
    os.environ["EAP_AUTH_MODE"] = "dev"
    dbos = None
    app = None
    try:
        from dbos import DBOSClient
        from fastapi.testclient import TestClient

        from eap_api.app import create_app
        from eap_api.launcher import DBOSRunLauncher, LauncherSettings, get_run_launcher

        dbos = DBOSClient(system_database_url=DBOS_DB_URL, dbos_system_schema=schema)
        app = create_app(database_url=APP_DB_URL)
        launcher = DBOSRunLauncher(
            LauncherSettings(DBOS_DB_URL, schema, app_version), client=dbos
        )
        app.dependency_overrides[get_run_launcher] = lambda: launcher
        with TestClient(app) as client:
            yield client
    finally:
        if dbos is not None:
            dbos.destroy()
        if app is not None:
            app.state.database_engine.dispose()
        if previous is None:
            os.environ.pop("EAP_AUTH_MODE", None)
        else:
            os.environ["EAP_AUTH_MODE"] = previous


def _start_via_api(api, tenant: str, workflow_id: str, text_in: str) -> str:
    run_id = str(uuid.uuid4())
    response = api.post(
        f"/api/v1/workflows/{workflow_id}/runs",
        json={"input": {"q": text_in}, "run_id": run_id},
        headers=auth_header(tenant),
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert set(body) == {
        "run_id", "kind", "workflow_id", "version", "status", "display_status", "trace_id"
    }
    assert body["run_id"] == run_id
    assert body["kind"] == "workflow"
    assert body["workflow_id"] == workflow_id
    assert body["version"] == 1
    # The in-process worker can claim the run before this response is built.
    assert body["status"] in {"queued", "running"}
    assert body["display_status"] == body["status"]
    return run_id


def _keys(events) -> list[str]:
    return [key for _seq, key, _typ, _data in events]


def test_milestone_agent_tool_branch_approval_completes_once_via_api(
    worker, worker_settings, client, tenant, defs, monkeypatch
):
    """Published agent, tool, branch and approval. Approve on the HTTP route. One completion."""
    monkeypatch.setenv("EAP_APPROVAL_POLL_SECONDS", "1")
    _agent_id, workflow_id, _version_id = _publish_milestone(worker, tenant)
    question = "urgent: disk"
    with workflow_api(worker_settings.dbos_system_schema, APP_VERSION) as api:
        run_id = _start_via_api(api, tenant, workflow_id, question)
        waiting = wait_event(worker.engine, run_id, "step:approve:waiting")
        assert waiting["data"]["prompt"] == "Escalate?"
        assert waiting["data"]["timeout_hours"] == 24
        assert waiting["data"]["workflow_id"] == run_id
        assert "deadline" in waiting["data"]
        assert worker.run_store.get_run(tenant, run_id)["status"] == "running"
        approved = api.post(
            f"/api/v1/workflows/runs/{run_id}/steps/approve/approve",
            json={"comment": "ship it"},
            headers=auth_header(tenant),
        )
        assert approved.status_code == 200, approved.text
        assert approved.json() == {
            "run_id": run_id,
            "step_id": "approve",
            "decision": "approved",
            "decided_by": "alice",
            "comment": "ship it",
            "notified": True,
        }
        again = api.post(
            f"/api/v1/workflows/runs/{run_id}/steps/approve/approve",
            headers=auth_header(tenant),
        )
        assert again.status_code == 409
        assert again.json()["detail"] == "step is not waiting for approval"
        run = wait_terminal(worker.run_store, tenant, uuid.UUID(run_id), timeout=90)
        assert run["status"] == "completed", run["error"]
        assert run["output"] == canonical("Echo: Echo: urgent: disk")
        events = event_rows(worker.engine, run_id)
        _contiguous(events)
        assert [(key, typ) for _seq, key, typ, _data in events] == [
            ("start", "run_started"),
            ("step:ask:started", "step_started"),
            ("step:ask:completed", "step_completed"),
            ("step:ticket:started", "step_started"),
            ("step:ticket:completed", "step_completed"),
            ("step:route:started", "step_started"),
            ("step:route:completed", "step_completed"),
            ("step:approve:started", "step_started"),
            ("step:approve:waiting", "step_waiting"),
            ("step:approve:decision", "step_approved"),
            ("step:approve:completed", "step_completed"),
            ("step:done:started", "step_started"),
            ("step:done:completed", "step_completed"),
            ("end", "run_completed"),
        ]
        by_key = {key: data for _seq, key, _typ, data in events}
        assert by_key["start"]["kind"] == "workflow"
        assert by_key["start"]["workflow_id"] == workflow_id
        assert by_key["start"]["workflow_version"] == 1
        assert by_key["start"]["input"] == {"q": question}
        assert by_key["step:ask:completed"]["output"] == "Echo: urgent: disk"
        ticket = by_key["step:ticket:completed"]["output"]
        stored = stored_ticket(worker.engine, tenant, f"{run_id}:wf_ticket")
        assert ticket == {"ticket_id": stored["ticket_id"], "created": True}
        assert stored["title"] == "T" and stored["body"] == question
        assert by_key["step:route:completed"]["output"] == {"case": 0, "next": "approve"}
        assert by_key["step:approve:decision"] == {
            "step_id": "approve", "decided_by": "alice", "comment": "ship it"
        }
        assert by_key["step:approve:completed"]["output"] == {
            "approved": True, "decided_by": "alice", "comment": "ship it"
        }
        assert by_key["step:done:completed"]["output"] == "Echo: Echo: urgent: disk"
        assert by_key["end"]["output"] == canonical("Echo: Echo: urgent: disk")
        assert by_key["end"]["result"] == "Echo: Echo: urgent: disk"
        ask_child = child_run_id(run_id, "ask")
        done_child = child_run_id(run_id, "done")
        assert child_runs(worker.engine, run_id) == [
            (ask_child, "ask", "completed"),
            (done_child, "done", "completed"),
        ]
        assert worker.run_store.get_run(tenant, ask_child)["input"] == question
        assert worker.tickets.call_count(tenant, f"{run_id}:") == 1
        assert worker.tickets.ticket_count(tenant, f"{run_id}:") == 1
        listed = api.get("/api/v1/runs", headers=auth_header(tenant)).json()["items"]
        assert [item["run_id"] for item in listed] == [run_id]
        children = api.get(
            f"/api/v1/runs?parent_run_id={run_id}", headers=auth_header(tenant)
        ).json()["items"]
        assert {item["run_id"] for item in children} == {ask_child, done_child}
        assert {item["parent_step_id"] for item in children} == {"ask", "done"}
        assert all(item["kind"] == "agent" and item["parent_run_id"] == run_id for item in children)
        assert api.get("/api/v1/runs?kind=agent", headers=auth_header(tenant)).json()["items"] == []
        other = auth_header(tenant + "x", "bob")
        assert api.get(f"/api/v1/runs/{run_id}", headers=other).status_code == 404
        api_events = api.get(f"/api/v1/runs/{run_id}/events", headers=auth_header(tenant)).json()
        assert [(e["seq"], e["type"]) for e in api_events] == [(seq, typ) for seq, _k, typ, _d in events]
        # A late second decision cannot append another end.
        assert decide(
            worker.event_log, client, tenant_id=tenant, run_id=run_id, step_id="approve"
        ) == "exists"
        assert _keys(event_rows(worker.engine, run_id)).count("end") == 1


def test_rejection_ends_approval_rejected_without_the_following_step(
    worker, worker_settings, tenant, defs, monkeypatch
):
    monkeypatch.setenv("EAP_APPROVAL_POLL_SECONDS", "1")
    _agent_id, workflow_id, _version_id = _publish_milestone(worker, tenant)
    with workflow_api(worker_settings.dbos_system_schema, APP_VERSION) as api:
        run_id = _start_via_api(api, tenant, workflow_id, "urgent: printer")
        wait_event(worker.engine, run_id, "step:approve:waiting")
        rejected = api.post(
            f"/api/v1/workflows/runs/{run_id}/steps/approve/reject",
            json={"comment": "no"},
            headers=auth_header(tenant),
        )
        assert rejected.status_code == 200, rejected.text
        assert rejected.json()["decision"] == "rejected"
        assert rejected.json()["decided_by"] == "alice"
        assert rejected.json()["comment"] == "no"
        run = wait_terminal(worker.run_store, tenant, uuid.UUID(run_id), timeout=90)
    assert run["status"] == "failed"
    assert run["error"] == "WorkflowStepError: the approval was rejected"
    events = event_rows(worker.engine, run_id)
    _contiguous(events)
    assert _keys(events) == [
        "start",
        "step:ask:started", "step:ask:completed",
        "step:ticket:started", "step:ticket:completed",
        "step:route:started", "step:route:completed",
        "step:approve:started", "step:approve:waiting", "step:approve:decision",
        "step:approve:failed",
        "end",
    ]
    assert events[-2][2] == "step_failed" and events[-2][3]["error_code"] == "approval_rejected"
    assert events[-1][2] == "run_failed"
    assert events[-1][3]["error_code"] == "approval_rejected" and events[-1][3]["step_id"] == "approve"
    decision = next(data for _s, key, _t, data in events if key == "step:approve:decision")
    assert events[next(i for i, e in enumerate(events) if e[1] == "step:approve:decision")][2] == "step_rejected"
    assert decision["decided_by"] == "alice" and decision["comment"] == "no"
    assert child_runs(worker.engine, run_id) == [(child_run_id(run_id, "ask"), "ask", "completed")]
    assert worker.tickets.call_count(tenant, f"{run_id}:") == 1
    assert _keys(events).count("end") == 1


def test_approval_timeout_ends_approval_timeout(
    worker, worker_settings, tenant, defs, monkeypatch
):
    monkeypatch.setenv("EAP_WORKFLOW_HOUR_SECONDS", "1")
    monkeypatch.setenv("EAP_APPROVAL_POLL_SECONDS", "0.5")
    _agent_id, workflow_id, _version_id = _publish_milestone(worker, tenant, timeout_hours=1)
    with workflow_api(worker_settings.dbos_system_schema, APP_VERSION) as api:
        run_id = _start_via_api(api, tenant, workflow_id, "urgent")
        run = wait_terminal(worker.run_store, tenant, uuid.UUID(run_id), timeout=90)
        late = api.post(
            f"/api/v1/workflows/runs/{run_id}/steps/approve/approve",
            headers=auth_header(tenant),
        )
        assert late.status_code == 409
        assert late.json()["detail"] == "step is not waiting for approval"
    assert run["status"] == "failed"
    assert run["error"] == "WorkflowStepError: the approval was not decided before its timeout"
    events = event_rows(worker.engine, run_id)
    _contiguous(events)
    assert _keys(events) == [
        "start",
        "step:ask:started", "step:ask:completed",
        "step:ticket:started", "step:ticket:completed",
        "step:route:started", "step:route:completed",
        "step:approve:started", "step:approve:waiting", "step:approve:decision",
        "step:approve:failed",
        "end",
    ]
    decision_type = next(typ for _s, key, typ, _d in events if key == "step:approve:decision")
    assert decision_type == "step_timed_out"
    assert events[-1][3]["error_code"] == "approval_timeout" and events[-1][3]["step_id"] == "approve"
    assert "step:done:started" not in _keys(events)
    assert child_runs(worker.engine, run_id) == [(child_run_id(run_id, "ask"), "ask", "completed")]
    assert worker.tickets.call_count(tenant, f"{run_id}:") == 1
    assert _keys(events).count("end") == 1


def test_approve_and_timeout_race_records_exactly_one_decision(
    worker, client, tenant, defs, monkeypatch
):
    """The HTTP decision write and the worker timeout share one event key. One of them wins."""
    monkeypatch.setenv("EAP_WORKFLOW_HOUR_SECONDS", "0.8")
    monkeypatch.setenv("EAP_APPROVAL_POLL_SECONDS", "0.2")
    _agent_id, workflow_id, version_id = _publish_milestone(worker, tenant, timeout_hours=1)
    run_id = enqueue_workflow_run(
        client, worker.run_store, tenant_id=tenant, workflow_id=workflow_id, version_id=version_id,
        input_value={"q": "urgent: race"}, app_version=APP_VERSION,
    )
    wait_event(worker.engine, run_id, "step:approve:waiting")
    outcomes: list[str] = []
    errors: list[BaseException] = []
    stop = threading.Event()

    def spam() -> None:
        try:
            while not stop.is_set():
                outcomes.append(
                    decide(
                        worker.event_log, client, tenant_id=tenant, run_id=run_id,
                        step_id="approve", comment="race",
                    )
                )
        except BaseException as exc:  # noqa: BLE001 - the assertion below reports it
            errors.append(exc)

    thread = threading.Thread(target=spam, daemon=True)
    thread.start()
    try:
        run = wait_terminal(worker.run_store, tenant, run_id, timeout=60)
    finally:
        stop.set()
        thread.join(timeout=5)
    assert errors == []
    assert thread.is_alive() is False
    assert outcomes and set(outcomes) <= {"created", "exists"}
    assert outcomes.count("created") <= 1
    events = event_rows(worker.engine, run_id)
    _contiguous(events)
    assert _keys(events).count("end") == 1
    assert _keys(events).count("step:approve:decision") == 1
    decision_type = next(typ for _s, key, typ, _d in events if key == "step:approve:decision")
    assert worker.tickets.call_count(tenant, f"{run_id}:") == 1
    ask_only = [(child_run_id(str(run_id), "ask"), "ask", "completed")]
    if decision_type == "step_approved":
        assert outcomes.count("created") == 1
        assert run["status"] == "completed", run["error"]
        assert run["output"] == canonical("Echo: Echo: urgent: race")
        assert "step:done:completed" in _keys(events)
        assert "step:approve:failed" not in _keys(events)
        assert child_runs(worker.engine, run_id) == ask_only + [
            (child_run_id(str(run_id), "done"), "done", "completed")
        ]
    else:
        assert decision_type == "step_timed_out"
        assert outcomes.count("created") == 0
        assert run["status"] == "failed"
        assert events[-1][3]["error_code"] == "approval_timeout"
        assert "step:done:started" not in _keys(events)
        assert child_runs(worker.engine, run_id) == ask_only


def test_parallel_join_reads_both_branch_ends_deterministically(worker, client, tenant, defs):
    """Two branches, one of them two steps. The join reads both branch-end outputs, same twice."""
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    spec = {
        "name": "Fan out",
        "input_schema": {"type": "object"},
        "start": "fan",
        "steps": [
            {"id": "fan", "type": "parallel", "branches": ["a1", "b"], "join": "join"},
            {"id": "a1", "type": "agent", "agent_id": agent_id, "input": {"p": "left-start"}, "next": "a2"},
            {"id": "a2", "type": "agent", "agent_id": agent_id, "input": {"p": "left-end"}, "next": "join"},
            {"id": "b", "type": "agent", "agent_id": agent_id, "input": {"p": "right-end"}, "next": "join"},
            {
                "id": "join",
                "type": "agent",
                "agent_id": agent_id,
                "input": {
                    "left": {"$ref": "steps.a2.output"},
                    "right": {"$ref": "steps.b.output"},
                },
            },
        ],
    }
    workflow_id, version_id = publish_workflow(worker.engine, tenant, spec)
    resolved = {"left": "Echo: left-end", "right": "Echo: right-end"}
    expected_output = canonical(f"Echo: {canonical(resolved)}")
    outputs = []
    for _ in range(2):
        run_id = enqueue_workflow_run(
            client, worker.run_store, tenant_id=tenant, workflow_id=workflow_id, version_id=version_id,
            input_value={}, app_version=APP_VERSION,
        )
        run = wait_terminal(worker.run_store, tenant, run_id, timeout=90)
        assert run["status"] == "completed", run["error"]
        assert run["output"] == expected_output
        events = event_rows(worker.engine, run_id)
        _contiguous(events)
        keys = _keys(events)
        assert keys[0] == "start" and keys[-1] == "end"
        fan_started = keys.index("step:fan:started")
        fan_completed = keys.index("step:fan:completed")
        for prefix in ("step:a1:", "step:a2:", "step:b:"):
            branch_keys = [key for key in keys if key.startswith(prefix)]
            assert branch_keys
            for key in branch_keys:
                assert fan_started < keys.index(key) < fan_completed
        assert keys.index("step:a1:started") < keys.index("step:a1:completed")
        assert keys.index("step:a1:completed") < keys.index("step:a2:started")
        assert keys.index("step:a2:started") < keys.index("step:a2:completed")
        assert keys.index("step:b:started") < keys.index("step:b:completed")
        assert fan_completed < keys.index("step:join:started") < keys.index("step:join:completed")
        by_key = {key: data for _seq, key, _typ, data in events}
        assert by_key["step:fan:completed"]["output"] is None
        assert by_key["step:a2:completed"]["output"] == "Echo: left-end"
        assert by_key["step:b:completed"]["output"] == "Echo: right-end"
        assert by_key["step:join:started"]["input"] == resolved
        assert by_key["step:join:completed"]["output"] == f"Echo: {canonical(resolved)}"
        assert child_runs(worker.engine, run_id) == [
            (child_run_id(str(run_id), "a1"), "a1", "completed"),
            (child_run_id(str(run_id), "a2"), "a2", "completed"),
            (child_run_id(str(run_id), "b"), "b", "completed"),
            (child_run_id(str(run_id), "join"), "join", "completed"),
        ]
        top = {row["id"] for row in worker.run_store.list_runs(tenant)}
        assert str(run_id) in top
        assert child_run_id(str(run_id), "a1") not in top
        outputs.append(run["output"])
    assert outputs[0] == outputs[1]


_REF = lambda path: {"$ref": path}  # noqa: E731

# (id, condition, workflow input, which terminal step runs)
_BRANCH_CASES = [
    ("eq_number_one_and_one_point_zero",
     {"op": "eq", "left": _REF("input.n"), "right": _REF("input.m")},
     {"n": 1, "m": 1.0}, "hit"),
    ("eq_true_is_not_one",
     {"op": "eq", "left": _REF("input.flag"), "right": _REF("input.one")},
     {"flag": True, "one": 1}, "miss"),
    ("eq_no_string_coercion",
     {"op": "eq", "left": _REF("input.s"), "right": _REF("input.n")},
     {"s": "1", "n": 1}, "miss"),
    ("ne",
     {"op": "ne", "left": _REF("input.n"), "right": _REF("input.m")},
     {"n": 3, "m": 4}, "hit"),
    ("ne_missing_operand_is_false",
     {"op": "ne", "left": _REF("input.missing"), "right": 4}, {}, "miss"),
    ("gt_numbers",
     {"op": "gt", "left": _REF("input.n"), "right": _REF("input.m")},
     {"n": 3, "m": 2}, "hit"),
    ("lt_strings",
     {"op": "lt", "left": _REF("input.a"), "right": _REF("input.b")},
     {"a": "a", "b": "z"}, "hit"),
    ("gt_mixed_types_false",
     {"op": "gt", "left": _REF("input.n"), "right": _REF("input.s")},
     {"n": 3, "s": "2"}, "miss"),
    ("gt_boolean_is_not_a_number",
     {"op": "gt", "left": _REF("input.flag"), "right": _REF("input.zero")},
     {"flag": True, "zero": 0}, "miss"),
    ("contains_string",
     {"op": "contains", "left": _REF("input.q"), "right": "urgent"},
     {"q": "help urgent"}, "hit"),
    ("contains_array_element_one_and_one_point_zero",
     {"op": "contains", "left": _REF("input.tags"), "right": _REF("input.needle")},
     {"tags": ["a", 1], "needle": 1.0}, "hit"),
    ("contains_object_key",
     {"op": "contains", "left": _REF("input.obj"), "right": "items"},
     {"obj": {"items": 1}}, "hit"),
    ("contains_number_is_false",
     {"op": "contains", "left": _REF("input.n"), "right": 3},
     {"n": 3}, "miss"),
    ("exists", {"op": "exists", "left": _REF("input.q")}, {"q": "x"}, "hit"),
    ("exists_null_is_false", {"op": "exists", "left": _REF("input.none")}, {"none": None}, "miss"),
    ("exists_missing_is_false", {"op": "exists", "left": _REF("input.missing")}, {}, "miss"),
    ("eq_missing_left_takes_default",
     {"op": "eq", "left": _REF("input.missing"), "right": 1}, {}, "miss"),
    ("eq_missing_right_takes_default",
     {"op": "eq", "left": _REF("input.n"), "right": _REF("input.missing")},
     {"n": 1}, "miss"),
]


@pytest.mark.parametrize(
    ("when", "input_value", "taken"),
    [(when, value, taken) for _name, when, value, taken in _BRANCH_CASES],
    ids=[name for name, *_rest in _BRANCH_CASES],
)
def test_branch_condition_ops_missing_operands_and_default(
    worker, client, tenant, defs, when, input_value, taken
):
    spec = {
        "name": "Branch",
        "input_schema": {"type": "object"},
        "start": "route",
        "steps": [
            {"id": "route", "type": "branch", "cases": [{"when": when, "next": "hit"}], "default": "miss"},
            {"id": "hit", "type": "tool", "tool_id": "tickets.create", "input": {"title": "hit", "body": "taken"}},
            {"id": "miss", "type": "tool", "tool_id": "tickets.create", "input": {"title": "miss", "body": "default"}},
        ],
    }
    workflow_id, version_id = publish_workflow(worker.engine, tenant, spec)
    run_id = enqueue_workflow_run(
        client, worker.run_store, tenant_id=tenant, workflow_id=workflow_id, version_id=version_id,
        input_value=input_value, app_version=APP_VERSION,
    )
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "completed", run["error"]
    events = event_rows(worker.engine, run_id)
    _contiguous(events)
    keys = _keys(events)
    other = "miss" if taken == "hit" else "hit"
    assert f"step:{taken}:completed" in keys
    assert f"step:{other}:started" not in keys
    route = next(data for _s, key, _t, data in events if key == "step:route:completed")
    if taken == "hit":
        assert route["output"] == {"case": 0, "next": "hit"}
    else:
        assert route["output"] == {"case": None, "next": "miss"}
    stored = stored_ticket(worker.engine, tenant, f"{run_id}:wf_{taken}")
    assert stored["title"] == taken
    assert json.loads(run["output"]) == {"ticket_id": stored["ticket_id"], "created": True}
    assert worker.tickets.call_count(tenant, f"{run_id}:") == 1
    assert child_runs(worker.engine, run_id) == []


def test_step_output_too_large_fails_without_echoing_the_output(worker, client, tenant, defs, monkeypatch):
    # "Echo: " plus the secret, as canonical JSON, is over this limit. 32 was just above it.
    monkeypatch.setenv("EAP_WORKFLOW_MAX_STEP_OUTPUT_BYTES", "16")
    secret = "sk-step-output-secret"
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    spec = {
        "name": "Big",
        "input_schema": {"type": "object"},
        "start": "a",
        "steps": [{"id": "a", "type": "agent", "agent_id": agent_id, "input": {"p": secret}}],
    }
    workflow_id, version_id = publish_workflow(worker.engine, tenant, spec)
    run_id = enqueue_workflow_run(
        client, worker.run_store, tenant_id=tenant, workflow_id=workflow_id, version_id=version_id,
        input_value={}, app_version=APP_VERSION,
    )
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "failed"
    assert run["error"] == "WorkflowStepError: the step output exceeds the workflow output size limit"
    assert secret not in (run["error"] or "")
    events = event_rows(worker.engine, run_id)
    _contiguous(events)
    assert [(key, typ) for _s, key, typ, _d in events] == [
        ("start", "run_started"),
        ("step:a:started", "step_started"),
        ("step:a:failed", "step_failed"),
        ("end", "run_failed"),
    ]
    assert events[-2][3]["error_code"] == "step_output_too_large"
    assert events[-1][3]["error_code"] == "step_output_too_large" and events[-1][3]["step_id"] == "a"
    assert secret not in json.dumps(events[-2][3]) and secret not in json.dumps(events[-1][3])
    # The agent did run; the workflow rejected its text. The child row exists once.
    assert child_runs(worker.engine, run_id) == [(child_run_id(str(run_id), "a"), "a", "completed")]
    assert _keys(events).count("end") == 1


def test_pinned_agent_version_missing_is_agent_not_found(worker, client, tenant, defs):
    agent_id = publish_agent(worker.engine, tenant, agent_spec(), version=1)
    spec = {
        "name": "Pin",
        "input_schema": {"type": "object"},
        "start": "a",
        "steps": [{
            "id": "a", "type": "agent", "agent_id": agent_id, "version": 9, "input": {"p": "x"},
        }],
    }
    workflow_id, version_id = publish_workflow(worker.engine, tenant, spec)
    run_id = enqueue_workflow_run(
        client, worker.run_store, tenant_id=tenant, workflow_id=workflow_id, version_id=version_id,
        input_value={}, app_version=APP_VERSION,
    )
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "failed"
    assert run["error"] == "WorkflowStepError: the agent was not found or has no published version"
    events = event_rows(worker.engine, run_id)
    _contiguous(events)
    assert [(key, typ) for _s, key, typ, _d in events] == [
        ("start", "run_started"),
        ("step:a:started", "step_started"),
        ("step:a:failed", "step_failed"),
        ("end", "run_failed"),
    ]
    assert events[1][3]["agent_version"] == 9
    assert events[1][3]["child_run_id"] == child_run_id(str(run_id), "a")
    assert events[-2][3]["error_code"] == "agent_not_found"
    assert events[-1][3]["error_code"] == "agent_not_found" and events[-1][3]["step_id"] == "a"
    assert child_runs(worker.engine, run_id) == []


def test_failed_parallel_branch_leaves_the_run_running_until_the_other_settles(
    worker, worker_settings, client, tenant, defs, monkeypatch
):
    """One branch fails at once. The sibling is waiting on approval. The run stays running, with
    no fan failure, until that sibling settles; then the run fails exactly once."""
    monkeypatch.setenv("EAP_APPROVAL_POLL_SECONDS", "1")
    spec = {
        "name": "Fan fail",
        "input_schema": {"type": "object"},
        "start": "fan",
        "steps": [
            {"id": "fan", "type": "parallel", "branches": ["bad", "hold"], "join": "done"},
            {
                "id": "bad", "type": "tool", "tool_id": "tickets.create",
                "input": {"title": "T"}, "next": "done",
            },
            {
                "id": "hold", "type": "approval", "prompt": "Hold?", "timeout_hours": 24, "next": "done",
            },
            {"id": "done", "type": "tool", "tool_id": "clock.now", "input": {}},
        ],
    }
    workflow_id, version_id = publish_workflow(worker.engine, tenant, spec)
    run_id = enqueue_workflow_run(
        client, worker.run_store, tenant_id=tenant, workflow_id=workflow_id, version_id=version_id,
        input_value={}, app_version=APP_VERSION,
    )
    run_key = str(run_id)
    waiting = wait_event(worker.engine, run_key, "step:hold:waiting")
    assert waiting["data"]["workflow_id"] == f"{run_key}:fan:hold"
    wait_event(worker.engine, run_key, "step:bad:failed")
    time.sleep(2.0)
    assert worker.run_store.get_run(tenant, run_id)["status"] == "running"
    keys_while_waiting = _keys(event_rows(worker.engine, run_key))
    assert "step:fan:failed" not in keys_while_waiting
    assert "step:done:started" not in keys_while_waiting
    assert "end" not in keys_while_waiting
    assert worker.tickets.call_count(tenant, f"{run_key}:") == 0
    with workflow_api(worker_settings.dbos_system_schema, APP_VERSION) as api:
        approved = api.post(
            f"/api/v1/workflows/runs/{run_key}/steps/hold/approve",
            json={"comment": "continue"},
            headers=auth_header(tenant),
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["decision"] == "approved"
        assert approved.json()["decided_by"] == "alice"
    run = wait_terminal(worker.run_store, tenant, run_id, timeout=90)
    assert run["status"] == "failed"
    assert run["error"] == "ToolInputInvalid: the input does not match tool 'tickets.create'"
    events = event_rows(worker.engine, run_key)
    _contiguous(events)
    keys = _keys(events)
    assert keys.count("end") == 1
    assert "step:fan:completed" not in keys and "step:done:started" not in keys
    assert keys.index("step:bad:failed") < keys.index("step:fan:failed")
    assert keys.index("step:hold:completed") < keys.index("step:fan:failed") < keys.index("end")
    assert keys.index("step:fan:started") < keys.index("step:bad:started")
    assert keys.index("step:fan:started") < keys.index("step:hold:started")
    bad = next(data for _s, key, _t, data in events if key == "step:bad:failed")
    assert bad["error_code"] == "tool_input_invalid"
    assert events[-2][1] == "step:fan:failed" and events[-2][3]["error_code"] == "tool_input_invalid"
    assert events[-1][2] == "run_failed" and events[-1][3]["error_code"] == "tool_input_invalid"
    assert events[-1][3]["step_id"] == "fan"
    assert worker.tickets.call_count(tenant, f"{run_key}:") == 0
    assert child_runs(worker.engine, run_key) == []


def test_approval_inside_a_parallel_branch_completes_once_via_api(
    worker, worker_settings, client, tenant, defs, monkeypatch
):
    """Approve a step that waits inside a branch. The HTTP route wakes that branch workflow."""
    monkeypatch.setenv("EAP_APPROVAL_POLL_SECONDS", "1")
    agent_id = publish_agent(worker.engine, tenant, agent_spec())
    spec = {
        "name": "Fan approve",
        "input_schema": {"type": "object"},
        "start": "fan",
        "steps": [
            {"id": "fan", "type": "parallel", "branches": ["gate", "side"], "join": "done"},
            {
                "id": "gate", "type": "approval", "prompt": "Ship?", "timeout_hours": 24, "next": "done",
            },
            {
                "id": "side", "type": "tool", "tool_id": "tickets.create",
                "input": {"title": "S", "body": "B"}, "next": "done",
            },
            {
                "id": "done", "type": "agent", "agent_id": agent_id,
                "input": {
                    "left": {"$ref": "steps.gate.output"},
                    "right": {"$ref": "steps.side.output"},
                },
            },
        ],
    }
    workflow_id, version_id = publish_workflow(worker.engine, tenant, spec)
    run_id = enqueue_workflow_run(
        client, worker.run_store, tenant_id=tenant, workflow_id=workflow_id, version_id=version_id,
        input_value={}, app_version=APP_VERSION,
    )
    run_key = str(run_id)
    waiting = wait_event(worker.engine, run_key, "step:gate:waiting")
    assert waiting["data"]["workflow_id"] == f"{run_key}:fan:gate"
    assert waiting["data"]["prompt"] == "Ship?"
    assert worker.run_store.get_run(tenant, run_id)["status"] == "running"
    with workflow_api(worker_settings.dbos_system_schema, APP_VERSION) as api:
        approved = api.post(
            f"/api/v1/workflows/runs/{run_key}/steps/gate/approve",
            json={"comment": "yes"},
            headers=auth_header(tenant),
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["decided_by"] == "alice"
        assert approved.json()["comment"] == "yes"
        assert isinstance(approved.json()["notified"], bool)
        again = api.post(
            f"/api/v1/workflows/runs/{run_key}/steps/gate/approve",
            headers=auth_header(tenant),
        )
        assert again.status_code == 409
        assert again.json()["detail"] == "step is not waiting for approval"
    run = wait_terminal(worker.run_store, tenant, run_id, timeout=90)
    assert run["status"] == "completed", run["error"]
    events = event_rows(worker.engine, run_key)
    _contiguous(events)
    keys = _keys(events)
    by_key = {key: data for _seq, key, _typ, data in events}
    assert keys.count("end") == 1
    assert by_key["step:gate:completed"]["output"] == {
        "approved": True, "decided_by": "alice", "comment": "yes"
    }
    stored = stored_ticket(worker.engine, tenant, f"{run_key}:wf_side")
    assert stored["title"] == "S" and stored["body"] == "B"
    assert by_key["step:side:completed"]["output"] == {"ticket_id": stored["ticket_id"], "created": True}
    assert by_key["step:done:started"]["input"]["left"] == by_key["step:gate:completed"]["output"]
    assert by_key["step:done:started"]["input"]["right"] == by_key["step:side:completed"]["output"]
    assert keys.index("step:fan:started") < keys.index("step:gate:started")
    assert keys.index("step:fan:started") < keys.index("step:side:started")
    assert keys.index("step:gate:completed") < keys.index("step:fan:completed")
    assert keys.index("step:side:completed") < keys.index("step:fan:completed")
    assert keys.index("step:fan:completed") < keys.index("step:done:started")
    assert by_key["step:fan:completed"]["output"] is None
    assert child_runs(worker.engine, run_key) == [(child_run_id(run_key, "done"), "done", "completed")]
    assert worker.tickets.call_count(tenant, f"{run_key}:") == 1
    assert worker.tickets.ticket_count(tenant, f"{run_key}:") == 1
    echoed = json.loads(run["output"])
    assert echoed.startswith("Echo: ")
    payload = json.loads(echoed.removeprefix("Echo: "))
    assert payload["left"]["approved"] is True and payload["left"]["comment"] == "yes"
    assert payload["right"]["ticket_id"] == stored["ticket_id"]

