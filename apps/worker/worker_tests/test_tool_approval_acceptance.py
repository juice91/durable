"""AC.6 acceptance (tester): batches, timeouts, crashes at every approval point, the wake-up through
the real API, eval mode, invalid ids, the AG-UI `batch` field and the combined features (structured
output, limits/fallback, memory, workflows, MCP). Builds on the implementer's helpers in
test_tool_approval.py; their tests are not repeated here."""

from __future__ import annotations

import itertools
import json
import uuid
from typing import Any

import pytest
from sqlalchemy import text

from eap_run_store import ApprovalStore
from eap_run_store.approvals import tool_approval_topic
from eap_schemas.approvals import TIMED_OUT_MESSAGE, denial_message
from worker_tests.conftest import APP_VERSION, enqueue_run, event_rows, wait_for, wait_terminal
from worker_tests.test_conversation import start_turn, step_names
from worker_tests.test_tool_approval import (
    TICKET_ARGS,
    contiguous,
    decide,
    events,
    gated_spec,
    prompt,
    side_effects,
    start,
    wait_waiting,
)

DENIED = "The user denied this tool call."


@pytest.fixture
def schema(worker_settings) -> str:
    return worker_settings.dbos_system_schema


def args(n: int) -> dict[str, Any]:
    return {"title": f"Ticket {n}", "body": f"Body {n}"}


def batch_prompt(*calls: tuple[str, dict[str, Any]]) -> str:
    return json.dumps({"calls": [{"tool": t, "args": a} for t, a in calls]})


def lines(run: dict[str, Any]) -> list[str]:
    return run["output"].splitlines()


# --------------------------------------------------------------------------- batches


@pytest.mark.parametrize("approved", list(itertools.product([True, False], repeat=3)),
                         ids=lambda c: "".join("A" if x else "D" for x in c))
def test_three_gated_calls_mixed_with_an_ungated_one_every_combination(worker, client, tenant, approved):
    """3 gated tickets.create calls + 1 ungated clock.now, decided in reverse order."""
    spec = gated_spec(tools=("tickets.create", "clock.now"), gate=("tickets.create",))
    body = batch_prompt(("tickets.create", args(0)), ("clock.now", {}), ("tickets.create", args(1)),
                        ("tickets.create", args(2)))
    run_id = start(worker, client, tenant, spec, body)
    waiting = wait_waiting(worker, run_id, count=3)
    ids = [w["tool_call_id"] for w in waiting]
    assert all(w["batch"] == ids for w in waiting)  # only the waiting calls, in response order
    for i in reversed(range(3)):
        assert decide(worker, client, tenant, run_id, ids[i], approve=approved[i]) == "created"
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "completed", run["error"]
    out = lines(run)
    assert len(out) == 4
    assert out[1].startswith("Tool clock__now outcome=success")
    for i, line_no in zip(range(3), (0, 2, 3)):
        want = "outcome=success" if approved[i] else f"outcome=denied content={DENIED}"
        assert want in out[line_no], (i, out)
    assert side_effects(worker, tenant) == sorted(
        (f"{run_id}:{ids[i]}", "completed") for i in range(3) if approved[i])
    assert worker.tickets.ticket_count(tenant, f"{run_id}:") == sum(approved)
    ev = events(worker, run_id)
    results = [d for k, (t, d) in ev.items() if t == "tool_result"]
    assert sorted(r["status"] for r in results) == sorted(
        ["ok"] + ["ok" if a else "denied" for a in approved])
    contiguous(worker, run_id)
    assert not ApprovalStore(worker.engine).list(tenant, "pending")[0]


def test_decisions_arriving_before_the_wait_starts(worker, client, tenant, monkeypatch):
    """Decisions recorded with no wake-up while the run is not yet in recv: the first
    eap.tool_approval_decisions read finds them, so the wait ends without a recv round."""
    monkeypatch.setenv("EAP_APPROVAL_POLL_SECONDS", "3600")
    spec = gated_spec(tools=("tickets.create", "clock.now"), gate=("tickets.create", "clock.now"))
    run_id = start(worker, client, tenant, spec, batch_prompt(("tickets.create", args(0)), ("clock.now", {})))
    first, second = wait_waiting(worker, run_id, count=2)
    # The run may already sleep in recv: send=False keeps the wake-up out, so only a poll (1 h)
    # could end the wait, unless the decisions were read before it. Race it from the begin steps.
    for w, ok in ((second, True), (first, False)):
        decide(worker, client, tenant, run_id, w["tool_call_id"], approve=ok, send=False)
    client.send(str(run_id), {"nudge": True}, tool_approval_topic(str(run_id)))
    run = wait_terminal(worker.run_store, tenant, run_id, timeout=30)
    assert run["status"] == "completed", run["error"]
    out = lines(run)
    assert out[0] == f"Tool tickets__create outcome=denied content={DENIED}"
    assert out[1].startswith("Tool clock__now outcome=success")


def test_only_one_nudge_for_two_decisions_still_ends_the_wait(worker, client, tenant, monkeypatch):
    """The message body is only a nudge: one send after both decisions reads both events."""
    monkeypatch.setenv("EAP_APPROVAL_POLL_SECONDS", "3600")
    spec = gated_spec(tools=("tickets.create", "clock.now"), gate=("tickets.create", "clock.now"))
    run_id = start(worker, client, tenant, spec, batch_prompt(("tickets.create", args(0)), ("clock.now", {})))
    first, second = wait_waiting(worker, run_id, count=2)
    decide(worker, client, tenant, run_id, first["tool_call_id"], send=False)
    decide(worker, client, tenant, run_id, second["tool_call_id"], approve=False, send=False)
    # A body naming an unrelated id must not matter.
    client.send(str(run_id), {"tool_call_id": "unrelated"}, tool_approval_topic(str(run_id)))
    run = wait_terminal(worker.run_store, tenant, run_id, timeout=30)
    assert run["status"] == "completed", run["error"]
    assert lines(run)[1] == f"Tool clock__now outcome=denied content={DENIED}"


# --------------------------------------------------------------------------- invalid tool call ids


def _bad_ids(monkeypatch, ids: list[str]) -> None:
    """The scripted model's streamed calls carry these tool call ids (the worker runs in-process)."""
    from eap_worker import models

    real = models._stream_items

    def patched(kind, value):
        items = real(kind, value)
        if kind != "tools":
            return items
        for delta in items:
            for i, call in delta.items():
                call.tool_call_id = ids[i]
        return items

    monkeypatch.setattr(models, "_stream_items", patched)


@pytest.mark.parametrize("bad", ["call with space", "x" * 257, "tab\tid", "é-id"])
def test_invalid_tool_call_id_is_denied_at_once_beside_a_waiting_call(worker, client, tenant, monkeypatch, bad):
    _bad_ids(monkeypatch, [bad, "good-call-1"])
    spec = gated_spec(tools=("tickets.create", "clock.now"), gate=("tickets.create", "clock.now"))
    run_id = start(worker, client, tenant, spec,
                   batch_prompt(("tickets.create", args(0)), ("clock.now", {})))
    (waiting,) = wait_waiting(worker, run_id)
    # Only the addressable call waits, is in the inbox and in `batch`.
    assert waiting["tool_call_id"] == "good-call-1" and waiting["batch"] == ["good-call-1"]
    rows, _ = ApprovalStore(worker.engine).list(tenant, "pending")
    assert [r["approval_key"] for r in rows] == ["good-call-1"]
    ev = events(worker, run_id)
    denied = ev[f"tool:{bad}:approval:decision"]
    assert denied[0] == "tool_denied"
    assert (denied[1]["decided_by"], denied[1]["reason"]) == ("system", "invalid_tool_call_id")
    assert f"tool:{bad}:approval:waiting" not in ev
    assert decide(worker, client, tenant, run_id, "good-call-1") == "created"
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "completed", run["error"]
    first, second = lines(run)
    assert first.startswith("Tool tickets__create outcome=denied content=This tool call could not be offered")
    assert second.startswith("Tool clock__now outcome=success")
    assert events(worker, run_id)[f"tool:{bad}:result"][1]["status"] == "denied"
    assert side_effects(worker, tenant) == []  # the denied ticket call never reserved; clock.now has none
    contiguous(worker, run_id)


def test_only_invalid_ids_never_wait(worker, client, tenant, monkeypatch, schema):
    _bad_ids(monkeypatch, ["bad id"])
    run_id = start(worker, client, tenant, gated_spec(), prompt())
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "completed", run["error"]
    assert "outcome=denied" in run["output"]
    assert not [1 for _s, _k, t, _d in event_rows(worker.engine, run_id) if t == "tool_approval_waiting"]
    assert ApprovalStore(worker.engine).list_for_run(tenant, str(run_id)) == []
    assert "DBOS.recv" not in step_names(schema, run_id)
    assert side_effects(worker, tenant) == []


# --------------------------------------------------------------------------- AG-UI batch field


def test_batch_field_lists_every_waiting_call_of_one_deferred_batch(worker, client, tenant):
    spec = gated_spec(tools=("tickets.create", "clock.now"), gate=("tickets.create", "clock.now"))
    run_id = start(worker, client, tenant, spec, batch_prompt(
        ("tickets.create", args(0)), ("clock.now", {}), ("tickets.create", args(1))))
    waiting = wait_waiting(worker, run_id, count=3)
    ids = [w["tool_call_id"] for w in waiting]
    assert len(set(ids)) == 3
    assert [w["batch"] for w in waiting] == [ids] * 3
    for i in ids:
        decide(worker, client, tenant, run_id, i, approve=False)
    assert wait_terminal(worker.run_store, tenant, run_id)["status"] == "completed"


# --------------------------------------------------------------------------- timeouts and the API


def _api():
    from worker_tests.test_workflow_run_acceptance import auth_header, workflow_api

    return workflow_api, auth_header


def test_timeout_fires_at_the_deadline_and_the_api_and_inbox_reflect_it(worker, client, tenant, schema, monkeypatch):
    from datetime import datetime

    monkeypatch.setenv("EAP_WORKFLOW_HOUR_SECONDS", "3")
    monkeypatch.setenv("EAP_APPROVAL_POLL_SECONDS", "3600")  # only the deadline can end the wait
    workflow_api, auth_header = _api()
    with workflow_api(schema, APP_VERSION) as api:
        run_id = start(worker, client, tenant, gated_spec(timeout_hours=1), prompt())
        (waiting,) = wait_waiting(worker, run_id)
        call_id = waiting["tool_call_id"]
        deadline = datetime.fromisoformat(waiting["deadline"])
        pending = api.get("/api/v1/approvals?status=pending", headers=auth_header(tenant)).json()
        assert [r["approval_key"] for r in pending["items"]] == [call_id]
        run = wait_terminal(worker.run_store, tenant, run_id, timeout=30)
        assert run["status"] == "completed", run["error"]
        assert run["output"] == f"Tool tickets__create outcome=denied content={TIMED_OUT_MESSAGE}"
        with worker.engine.connect() as conn:
            ts = conn.execute(text("SELECT ts FROM execution_events WHERE run_id = :r AND event_key = :k"),
                              {"r": str(run_id), "k": f"tool:{call_id}:approval:decision"}).scalar_one()
        late = (ts - deadline).total_seconds()
        assert -0.5 <= late <= 3.0, late  # at the deadline, not a poll (1 h) later
        decision_type, decision = events(worker, run_id)[f"tool:{call_id}:approval:decision"]
        assert decision_type == "tool_approval_timed_out" and decision.get("decided_by") is None
        url = f"/api/v1/runs/{run_id}/tool-approvals/{call_id}"
        for verb in ("approve", "deny"):
            late_call = api.post(f"{url}/{verb}", json={}, headers=auth_header(tenant))
            assert late_call.status_code == 409, late_call.text
            body = late_call.json()
            assert body["decision"] == "timed_out" and body["decided_by"] is None
        decided = api.get("/api/v1/approvals?status=decided", headers=auth_header(tenant)).json()
        (row,) = [r for r in decided["items"] if r["approval_key"] == call_id]
        assert (row["decision"], row["decided_by"]) == ("timed_out", None)
        assert api.get("/api/v1/approvals?status=pending", headers=auth_header(tenant)).json()["items"] == []
    assert side_effects(worker, tenant) == []


def test_a_decision_just_before_the_deadline_beats_the_timeout(worker, client, tenant, monkeypatch):
    monkeypatch.setenv("EAP_WORKFLOW_HOUR_SECONDS", "4")
    monkeypatch.setenv("EAP_APPROVAL_POLL_SECONDS", "0.3")
    run_id = start(worker, client, tenant, gated_spec(timeout_hours=1), prompt())
    (waiting,) = wait_waiting(worker, run_id)
    assert decide(worker, client, tenant, run_id, waiting["tool_call_id"]) == "created"
    run = wait_terminal(worker.run_store, tenant, run_id, timeout=30)
    assert run["status"] == "completed" and "outcome=success" in run["output"], run["error"]
    assert events(worker, run_id)[f"tool:{waiting['tool_call_id']}:approval:decision"][0] == "tool_approved"


def test_timeout_of_a_batch_times_out_only_the_undecided_calls(worker, client, tenant, monkeypatch):
    monkeypatch.setenv("EAP_WORKFLOW_HOUR_SECONDS", "3")
    monkeypatch.setenv("EAP_APPROVAL_POLL_SECONDS", "0.3")
    spec = gated_spec(tools=("tickets.create", "clock.now"), gate=("tickets.create", "clock.now"), timeout_hours=1)
    run_id = start(worker, client, tenant, spec, batch_prompt(("tickets.create", args(0)), ("clock.now", {})))
    first, second = wait_waiting(worker, run_id, count=2)
    assert decide(worker, client, tenant, run_id, second["tool_call_id"]) == "created"
    run = wait_terminal(worker.run_store, tenant, run_id, timeout=30)
    assert run["status"] == "completed", run["error"]
    assert lines(run)[0] == f"Tool tickets__create outcome=denied content={TIMED_OUT_MESSAGE}"
    assert lines(run)[1].startswith("Tool clock__now outcome=success")
    ev = events(worker, run_id)
    assert ev[f"tool:{first['tool_call_id']}:approval:decision"][0] == "tool_approval_timed_out"
    assert ev[f"tool:{second['tool_call_id']}:approval:decision"][0] == "tool_approved"
    assert side_effects(worker, tenant) == []


# --------------------------------------------------------------------------- the wake-up through the API


def test_batch_decided_through_the_api_wakes_the_wait_with_no_poll(worker, client, tenant, schema, monkeypatch):
    monkeypatch.setenv("EAP_APPROVAL_POLL_SECONDS", "3600")  # only the DBOS.send wake-up can end a round
    workflow_api, auth_header = _api()
    spec = gated_spec(tools=("tickets.create", "clock.now"), gate=("tickets.create", "clock.now"))
    with workflow_api(schema, APP_VERSION) as api:
        run_id = start(worker, client, tenant, spec, batch_prompt(("tickets.create", args(0)), ("clock.now", {})))
        first, second = wait_waiting(worker, run_id, count=2)
        base = f"/api/v1/runs/{run_id}/tool-approvals"
        unknown = api.post(f"{base}/nope/approve", json={}, headers=auth_header(tenant))
        assert unknown.status_code == 404
        r = api.post(f"{base}/{second['tool_call_id']}/deny", json={"comment": "no clock"}, headers=auth_header(tenant))
        assert r.status_code == 200, r.text
        wait_for(lambda: step_names(schema, run_id).count("eap.tool_approval_decisions") >= 2 or None,
                 timeout=30, what="the wake-up to reach recv")  # woken by the send, not by a poll
        assert worker.run_store.get_run(tenant, run_id)["status"] == "running"
        r = api.post(f"{base}/{first['tool_call_id']}/approve", json={}, headers=auth_header(tenant))
        assert r.status_code == 200, r.text
        run = wait_terminal(worker.run_store, tenant, run_id, timeout=30)
        assert run["status"] == "completed", run["error"]
        assert lines(run)[0].startswith("Tool tickets__create outcome=success")
        assert lines(run)[1] == f"Tool clock__now outcome=denied content={denial_message('no clock')}"
        # Both calls shared one wait: exactly one begin per call, no extra decision-less rounds beyond recv wakes.
        assert step_names(schema, run_id).count("eap.tool_approval_begin") == 2


def test_missed_wake_up_is_recovered_by_the_poll(worker, client, tenant, schema, monkeypatch):
    monkeypatch.setenv("EAP_APPROVAL_POLL_SECONDS", "0.5")
    workflow_api, auth_header = _api()
    from eap_api.launcher import DBOSRunLauncher

    def boom(self, **_kw):
        raise RuntimeError("send failed")

    monkeypatch.setattr(DBOSRunLauncher, "notify_tool_approval", boom)
    with workflow_api(schema, APP_VERSION) as api:
        run_id = start(worker, client, tenant, gated_spec(), prompt())
        (waiting,) = wait_waiting(worker, run_id)
        url = f"/api/v1/runs/{run_id}/tool-approvals/{waiting['tool_call_id']}/approve"
        r = api.post(url, json={}, headers=auth_header(tenant))
        assert r.status_code == 200, r.text  # the decision stands though the send failed
        run = wait_terminal(worker.run_store, tenant, run_id, timeout=30)
        assert run["status"] == "completed" and "outcome=success" in run["output"], run["error"]
    assert events(worker, run_id)[f"tool:{waiting['tool_call_id']}:approval:decision"][0] == "tool_approved"


def test_a_wake_up_without_a_decision_does_not_end_the_wait(worker, client, tenant, schema):
    """A stray message on the topic (no decision event) is only a nudge: the loop re-reads and waits on."""
    run_id = start(worker, client, tenant, gated_spec(), prompt())
    (waiting,) = wait_waiting(worker, run_id)
    client.send(str(run_id), {"tool_call_id": waiting["tool_call_id"], "decision": "approved"},
                tool_approval_topic(str(run_id)))
    wait_for(lambda: step_names(schema, run_id).count("eap.tool_approval_decisions") >= 2 or None,
             timeout=30, what="the nudge to be read")
    assert worker.run_store.get_run(tenant, run_id)["status"] == "running"
    assert side_effects(worker, tenant) == []
    decide(worker, client, tenant, run_id, waiting["tool_call_id"], approve=False)
    assert wait_terminal(worker.run_store, tenant, run_id)["status"] == "completed"
    assert side_effects(worker, tenant) == []


# --------------------------------------------------------------------------- structured output (AC.4)


def _structured_plan(monkeypatch, *, together: bool = False) -> None:
    """test:approval calls the gated tool; once it has this turn's returns it calls the output tool with
    a schema instance (or, `together`, calls both in one response)."""
    from eap_worker import models

    real = models._approval_plan

    def plan(messages, info):
        out = info.output_tools[0] if info.output_tools else None
        parts = models._this_turn(messages)
        has_return = any(isinstance(p, models.ToolReturnPart) for p in parts)
        kind, value = real(messages, info)
        if out is None:
            return kind, value
        answer = (out.name, models.schema_instance(out.parameters_json_schema))
        if together and kind == "tools":
            return "tools", [*value, answer]
        return ("tools", [answer]) if has_return else (kind, value)

    monkeypatch.setattr(models, "_approval_plan", plan)


def structured_gated() -> dict[str, Any]:
    from worker_tests.test_step_layout_ac import with_output

    return with_output(gated_spec())


def test_approval_before_final_result_then_structured_output_completes(worker, client, tenant, monkeypatch):
    _structured_plan(monkeypatch)
    for approve in (True, False):
        run_id = start(worker, client, tenant, structured_gated(), prompt())
        (waiting,) = wait_waiting(worker, run_id)
        assert not [k for k, _ in side_effects(worker, tenant) if k.startswith(f"{run_id}:")]
        decide(worker, client, tenant, run_id, waiting["tool_call_id"], approve=approve)
        run = wait_terminal(worker.run_store, tenant, run_id)
        assert run["status"] == "completed", run["error"]
        assert json.loads(run["output"]) == {"answer": "x", "score": 1}
        ev = events(worker, run_id)
        assert ev[f"tool:{waiting['tool_call_id']}:result"][1]["status"] == ("ok" if approve else "denied")
        keys = [k for _s, k, _t, _d in event_rows(worker.engine, run_id)]
        assert keys.index(f"tool:{waiting['tool_call_id']}:result") < keys.index("end")
        contiguous(worker, run_id)
    assert len(side_effects(worker, tenant)) == 1  # only the approved run reserved a key


def test_final_result_in_the_same_response_as_a_gated_call_skips_the_wait(worker, client, tenant, monkeypatch):
    _structured_plan(monkeypatch, together=True)
    run_id = start(worker, client, tenant, structured_gated(), prompt())
    run = wait_terminal(worker.run_store, tenant, run_id, timeout=30)
    assert run["status"] == "completed", run["error"]
    assert json.loads(run["output"]) == {"answer": "x", "score": 1}
    assert not [1 for _s, _k, t, _d in event_rows(worker.engine, run_id) if t == "tool_approval_waiting"]
    assert side_effects(worker, tenant) == [] and ApprovalStore(worker.engine).list_for_run(tenant, str(run_id)) == []


# --------------------------------------------------------------------------- limits and fallback (AC.5)


def _end(worker, run_id) -> dict[str, Any]:
    return events(worker, run_id)["end"][1]


def test_gated_tool_waits_on_the_fallback_model_after_the_primary_fails(worker, client, tenant):
    spec = gated_spec()
    spec["model"] = {"provider": "test", "name": "fail-503"}
    spec["fallback_models"] = [{"provider": "test", "name": "approval"}]
    run_id = start(worker, client, tenant, spec, prompt())
    (waiting,) = wait_waiting(worker, run_id)
    decide(worker, client, tenant, run_id, waiting["tool_call_id"])
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "completed" and "outcome=success" in run["output"], run["error"]
    assert side_effects(worker, tenant) == [(f"{run_id}:{waiting['tool_call_id']}", "completed")]
    contiguous(worker, run_id)


@pytest.mark.parametrize(("limit", "ok"), [(2, True), (1, False)])
def test_request_limit_counts_the_requests_around_an_approval(worker, client, tenant, limit, ok):
    """One tool round = 2 model requests: max_requests=2 passes, 1 fails at the second request, which
    comes after the approval (the wait itself costs no request)."""
    run_id = start(worker, client, tenant, gated_spec(limits={"max_requests": limit}), prompt())
    (waiting,) = wait_waiting(worker, run_id, timeout=30)
    decide(worker, client, tenant, run_id, waiting["tool_call_id"])
    run = wait_terminal(worker.run_store, tenant, run_id, timeout=30)
    if ok:
        assert run["status"] == "completed", run["error"]
    else:
        assert run["status"] == "failed", run
        assert _end(worker, run_id)["error_code"] == "usage_limit_exceeded"
    contiguous(worker, run_id)


def test_tool_call_limit_with_two_approved_calls(worker, client, tenant):
    spec = gated_spec(tools=("tickets.create", "clock.now"), gate=("tickets.create", "clock.now"),
                      limits={"max_tool_calls": 1})
    run_id = start(worker, client, tenant, spec, batch_prompt(("tickets.create", args(0)), ("clock.now", {})))
    for w in wait_waiting(worker, run_id, count=2):
        decide(worker, client, tenant, run_id, w["tool_call_id"])
    run = wait_terminal(worker.run_store, tenant, run_id, timeout=30)
    assert run["status"] == "failed", run
    end = _end(worker, run_id)
    assert (end["error_code"], end["limit"]) == ("usage_limit_exceeded", "max_tool_calls")
    contiguous(worker, run_id)


# --------------------------------------------------------------------------- memory


def _memory_specs():
    from worker_tests.test_observational_memory import obs_spec
    from worker_tests.test_semantic_recall import recall_spec
    from worker_tests.test_working_memory import memory_spec

    return {
        "history": gated_spec_from(memory_spec(model="approval", history={"last_turns": 5})),
        "recall": gated_spec_from(recall_spec(model="approval")),
        "observational": gated_spec_from(obs_spec(model="approval")),
    }


def gated_spec_from(base: dict[str, Any]) -> dict[str, Any]:
    return {**base, "tools": ["tickets.create"], "approvals": {"tools": ["tickets.create"]}}


@pytest.fixture
def mem_tenant(worker, tenant):
    yield tenant
    with worker.engine.begin() as conn:
        for table in ("memory_observations", "message_embeddings", "working_memories"):
            conn.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tenant})


@pytest.mark.parametrize("shape", ["history", "recall", "observational"])
def test_waiting_turn_blocks_the_next_turn_and_delete_until_it_ends(worker, client, mem_tenant, schema, shape):
    from worker_tests.workflow_run_helpers import cleanup_definitions, publish_agent
    from worker_tests.test_working_memory import conversation

    workflow_api, auth_header = _api()
    spec = _memory_specs()[shape]
    agent_id = publish_agent(worker.engine, mem_tenant, {**spec, "name": "mem"})
    conv = conversation(worker, mem_tenant, uuid.UUID(agent_id))
    try:
        with workflow_api(schema, APP_VERSION) as api:
            run_id = start_turn(client, worker.run_store, mem_tenant, conv["id"], prompt(), spec=spec)
            (waiting,) = wait_waiting(worker, run_id)
            names = step_names(schema, run_id)
            assert not set(names) & {"eap.finish_run", "eap.memory_index", "eap.memory_observe_run"}
            h = auth_header(mem_tenant)
            busy = api.post(f"/api/v1/conversations/{conv['id']}/runs", json={"input": "too soon", "run_id": str(uuid.uuid4())}, headers=h)
            assert busy.status_code == 409, busy.text
            gone = api.delete(f"/api/v1/conversations/{conv['id']}", headers=h)
            assert gone.status_code == 409, gone.text
            assert api.get(f"/api/v1/conversations/{conv['id']}", headers=h).status_code == 200
            decide(worker, client, mem_tenant, run_id, waiting["tool_call_id"])
            assert wait_terminal(worker.run_store, mem_tenant, run_id)["status"] == "completed"
            from worker_tests.test_observational_memory import wait_workflow

            wait_workflow(schema, run_id)
            assert api.delete(f"/api/v1/conversations/{conv['id']}", headers=h).status_code == 204
    finally:
        cleanup_definitions(worker.engine, mem_tenant)


@pytest.mark.parametrize("shape", ["recall", "observational"])
def test_denied_turn_still_completes_and_runs_the_post_turn_steps(worker, client, mem_tenant, schema, shape):
    from worker_tests.test_observational_memory import wait_workflow
    from worker_tests.test_semantic_recall import embeddings
    from worker_tests.test_working_memory import conversation

    spec = _memory_specs()[shape]
    conv = conversation(worker, mem_tenant)
    run_id = start_turn(client, worker.run_store, mem_tenant, conv["id"], prompt(), spec=spec)
    (waiting,) = wait_waiting(worker, run_id)
    decide(worker, client, mem_tenant, run_id, waiting["tool_call_id"], approve=False)
    assert wait_terminal(worker.run_store, mem_tenant, run_id)["status"] == "completed"
    wait_workflow(schema, run_id)
    names = step_names(schema, run_id)
    post = names[names.index("eap.finish_run") + 1:]
    assert post == (["eap.memory_index"] if shape == "recall" else ["eap.memory_observe_run", "DBOS.getResult"])
    if shape == "recall":
        assert len(embeddings(worker.engine, conv["id"])) == 2  # the turn's user and assistant messages
    assert side_effects(worker, mem_tenant) == []


# --------------------------------------------------------------------------- MCP and workflows


def test_gated_mcp_tool_approved_makes_exactly_one_tools_call(worker, client, tenant, mcp_env, local_mcp_server):
    from worker_tests.mcp_helpers import mcp_spec, register_mcp_server
    from worker_tests.workflow_run_helpers import cleanup_definitions, publish_agent

    register_mcp_server(worker.engine, tenant, "orders", local_mcp_server.url)
    spec = {**mcp_spec(("orders", "cancel_order"), model="approval"), "approvals": {"tools": ["orders/cancel_order"]}}
    agent_id = publish_agent(worker.engine, tenant, spec)
    try:
        body = json.dumps({"tool": "mcp__orders__cancel_order", "args": {"order_id": "42"}})
        run_id = enqueue_run(client, worker.run_store, tenant_id=tenant, spec=spec, input_text=body,
                             app_version=APP_VERSION, agent_id=uuid.UUID(agent_id))
        (waiting,) = wait_waiting(worker, run_id)
        assert waiting["tool_name"] == "mcp__orders__cancel_order"
        assert local_mcp_server.call_count("cancel_order") == 0  # nothing before approval
        decide(worker, client, tenant, run_id, waiting["tool_call_id"])
        run = wait_terminal(worker.run_store, tenant, run_id)
        assert run["status"] == "completed", run["error"]
        assert "outcome=success" in run["output"]
        assert local_mcp_server.call_count("cancel_order") == 1
        contiguous(worker, run_id)
        # A denied MCP call is never sent.
        run_id = enqueue_run(client, worker.run_store, tenant_id=tenant, spec=spec, input_text=body,
                             app_version=APP_VERSION, agent_id=uuid.UUID(agent_id))
        (waiting,) = wait_waiting(worker, run_id)
        decide(worker, client, tenant, run_id, waiting["tool_call_id"], approve=False)
        assert wait_terminal(worker.run_store, tenant, run_id)["status"] == "completed"
        assert local_mcp_server.call_count("cancel_order") == 1
    finally:
        cleanup_definitions(worker.engine, tenant)


@pytest.mark.parametrize("how", ["approve_via_api", "timeout"])
def test_workflow_agent_step_whose_tool_waits(worker, client, tenant, schema, monkeypatch, how):
    from worker_tests.workflow_run_helpers import (
        cleanup_definitions, enqueue_workflow_run, publish_agent, publish_workflow)

    if how == "timeout":
        monkeypatch.setenv("EAP_WORKFLOW_HOUR_SECONDS", "2")
        monkeypatch.setenv("EAP_APPROVAL_POLL_SECONDS", "0.3")
    workflow_api, auth_header = _api()
    agent = gated_spec(timeout_hours=1)
    agent_id = publish_agent(worker.engine, tenant, {**agent, "name": "Gated"})
    wf = {"name": "Gated flow", "input_schema": {"type": "object", "properties": {"q": {"type": "string"}},
          "required": ["q"]}, "start": "ask",
          "steps": [{"id": "ask", "type": "agent", "agent_id": agent_id, "input": {"prompt": {"$ref": "input.q"}}}]}
    try:
        with workflow_api(schema, APP_VERSION) as api:
            workflow_id, version_id = publish_workflow(worker.engine, tenant, wf)
            root = enqueue_workflow_run(client, worker.run_store, tenant_id=tenant, workflow_id=workflow_id,
                                        version_id=version_id, input_value={"q": prompt()}, app_version=APP_VERSION)
            h = auth_header(tenant)
            row = wait_for(lambda: next(iter(ApprovalStore(worker.engine).list(tenant, "pending")[0]), None),
                           timeout=60, what="the child's inbox row")
            assert row["kind"] == "tool" and row["root_run_id"] == str(root) and row["run_id"] != str(root)
            listed = api.get("/api/v1/approvals?status=pending", headers=h).json()["items"]
            assert [(r["run_id"], r["root_run_id"]) for r in listed] == [(row["run_id"], str(root))]
            if how == "approve_via_api":
                url = f"/api/v1/runs/{row['run_id']}/tool-approvals/{row['approval_key']}/approve"
                assert api.post(url, json={}, headers=h).status_code == 200
            run = wait_terminal(worker.run_store, tenant, root, timeout=60)
            assert run["status"] == "completed", run["error"]
            child = worker.run_store.get_run(tenant, uuid.UUID(row["run_id"]))
            assert child["status"] == "completed"
            assert ("outcome=success" in child["output"]) == (how == "approve_via_api")
            assert side_effects(worker, tenant) == (
                [(f"{row['run_id']}:{row['approval_key']}", "completed")] if how == "approve_via_api" else [])
    finally:
        cleanup_definitions(worker.engine, tenant)


# --------------------------------------------------------------------------- crashes at every approval point

CRASHES = {
    # id: (crash env, when the decision is recorded: "after_restart" | "live")
    "before_begin": ({"EAP_CRASH_AT": "after_event_write", "EAP_CRASH_EVENT_KEY": "tool:*:call"}, "after_restart"),
    "mid_wait": ({"EAP_CRASH_AT": "tool_approval_waiting"}, "after_restart"),
    "after_decision_in_tool": ({"EAP_CRASH_AT": "after_tool_call"}, "live"),
    "after_tool_result": ({"EAP_CRASH_AT": "after_event_write", "EAP_CRASH_EVENT_KEY": "tool:*:result"}, "live"),
}


def _await_waiting(worker, run_id, count: int) -> list[str]:
    return [w["tool_call_id"] for w in wait_waiting(worker, run_id, count=count, timeout=90)]


# (point, approve): a denied call never reaches the in-tool crash point, so that pair is not a case.
CRASH_CASES = [(p, a) for p in CRASHES for a in (True, False) if not (p == "after_decision_in_tool" and not a)]


@pytest.mark.parametrize(("point", "approve"), CRASH_CASES, ids=[f"{p}-{'approve' if a else 'deny'}" for p, a in CRASH_CASES])
def test_crash_at_each_approval_point_runs_an_approved_tool_once_and_a_denied_never(
        worker, tenant, tmp_path, point, approve):
    from dbos import DBOSClient

    from worker_tests.conftest import DBOS_DB_URL, drop_schema, new_schema
    from worker_tests.test_crash_recovery import APP_VERSION as CRASH_VERSION, WorkerProcess, _env

    env, when = CRASHES[point]
    schema = new_schema("eap_test_crash")
    executor_id = f"ta-acc-{uuid.uuid4().hex[:8]}"
    client = DBOSClient(system_database_url=DBOS_DB_URL, dbos_system_schema=schema)
    procs: list[WorkerProcess] = []
    try:
        first = WorkerProcess(_env(schema, executor_id, EAP_CRASH_MARKER=str(tmp_path / "marker"), **env),
                              tmp_path / "w1.log")
        procs.append(first)
        first.wait_ready()
        run_id = enqueue_run(client, worker.run_store, tenant_id=tenant, spec=gated_spec(), input_text=prompt(),
                             app_version=CRASH_VERSION)
        if when == "live":
            (call_id,) = _await_waiting(worker, run_id, 1)
            assert decide(worker, client, tenant, run_id, call_id, approve=approve) == "created"
        assert first.wait_exit() == 137, (tmp_path / "w1.log").read_text()[-2000:]
        assert worker.run_store.get_run(tenant, run_id)["status"] == "running"
        second = WorkerProcess(_env(schema, executor_id), tmp_path / "w2.log")
        procs.append(second)
        if when == "after_restart":
            (call_id,) = _await_waiting(worker, run_id, 1)
            assert decide(worker, client, tenant, run_id, call_id, approve=approve) == "created"
        row = wait_terminal(worker.run_store, tenant, run_id, timeout=90)
        assert row["status"] == "completed", (row["error"], (tmp_path / "w2.log").read_text()[-3000:])
        _assert_exactly_once(worker, tenant, run_id, [call_id], [approve])
    finally:
        for proc in procs:
            proc.kill()
        client.destroy()
        drop_schema(schema)


def _assert_exactly_once(worker, tenant, run_id, call_ids: list[str], approved: list[bool]) -> None:
    rows = event_rows(worker.engine, run_id)
    seqs = [r[0] for r in rows]
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs))), seqs
    keys = [r[1] for r in rows]
    assert len(set(keys)) == len(keys)
    assert sum(1 for r in rows if r[2] == "run_completed") == 1 and keys.count("end") == 1
    for cid, ok in zip(call_ids, approved):
        assert keys.count(f"tool:{cid}:approval:waiting") == 1 and keys.count(f"tool:{cid}:result") == 1
        assert dict((r[1], r[3]) for r in rows)[f"tool:{cid}:result"]["status"] == ("ok" if ok else "denied")
    want = sorted((f"{run_id}:{cid}", "completed") for cid, ok in zip(call_ids, approved) if ok)
    assert side_effects(worker, tenant) == want
    assert worker.tickets.ticket_count(tenant, f"{run_id}:") == len(want)
    assert not ApprovalStore(worker.engine).list(tenant, "pending")[0]


def test_crash_mid_wait_of_a_batch_with_one_decision_made_before_the_crash(worker, tenant, tmp_path):
    from dbos import DBOSClient

    from worker_tests.conftest import DBOS_DB_URL, drop_schema, new_schema
    from worker_tests.test_crash_recovery import APP_VERSION as CRASH_VERSION, WorkerProcess, _env

    schema, executor_id = new_schema("eap_test_crash"), f"ta-acc-{uuid.uuid4().hex[:8]}"
    client = DBOSClient(system_database_url=DBOS_DB_URL, dbos_system_schema=schema)
    procs: list[WorkerProcess] = []
    spec = gated_spec(tools=("tickets.create", "clock.now"), gate=("tickets.create", "clock.now"))
    try:
        # The crash point fires once, right after the FIRST call's begin: the second call has no waiting event yet.
        crash = {"EAP_CRASH_AT": "tool_approval_waiting", "EAP_CRASH_MARKER": str(tmp_path / "m")}
        first = WorkerProcess(_env(schema, executor_id, **crash), tmp_path / "w1.log")
        procs.append(first)
        first.wait_ready()
        run_id = enqueue_run(client, worker.run_store, tenant_id=tenant, spec=spec, app_version=CRASH_VERSION,
                             input_text=batch_prompt(("tickets.create", args(0)), ("clock.now", {})))
        assert first.wait_exit() == 137
        (open_call,) = _await_waiting(worker, run_id, 1)  # the crash came right after the first begin
        assert decide(worker, client, tenant, run_id, open_call) == "created"  # decided while down
        second = WorkerProcess(_env(schema, executor_id), tmp_path / "w2.log")
        procs.append(second)
        ids = _await_waiting(worker, run_id, 2)
        assert ids[0] == open_call
        assert decide(worker, client, tenant, run_id, ids[1], approve=False) == "created"
        row = wait_terminal(worker.run_store, tenant, run_id, timeout=90)
        assert row["status"] == "completed", (row["error"], (tmp_path / "w2.log").read_text()[-3000:])
        _assert_exactly_once(worker, tenant, run_id, ids, [True, False])
    finally:
        for proc in procs:
            proc.kill()
        client.destroy()
        drop_schema(schema)


@pytest.mark.parametrize("shape", ["recall", "observational"])
def test_crash_while_waiting_with_memory_gives_one_index_row_and_one_observer_child(
        worker, mem_tenant, tmp_path, shape):
    from dbos import DBOSClient

    from worker_tests.conftest import DBOS_DB_URL, drop_schema, new_schema
    from worker_tests.test_crash_recovery import APP_VERSION as CRASH_VERSION, WorkerProcess, _env
    from worker_tests.test_semantic_recall import embeddings
    from worker_tests.test_working_memory import conversation

    tenant = mem_tenant
    schema, executor_id = new_schema("eap_test_crash"), f"ta-acc-{uuid.uuid4().hex[:8]}"
    client = DBOSClient(system_database_url=DBOS_DB_URL, dbos_system_schema=schema)
    procs: list[WorkerProcess] = []
    spec = _memory_specs()[shape]
    conv = conversation(worker, tenant)
    try:
        crash = {"EAP_CRASH_AT": "tool_approval_waiting", "EAP_CRASH_MARKER": str(tmp_path / "m")}
        first = WorkerProcess(_env(schema, executor_id, **crash), tmp_path / "w1.log")
        procs.append(first)
        first.wait_ready()
        run_id, _ = worker.run_store.create_turn(tenant, conv["id"], 1, prompt(), uuid.uuid4())
        enqueue_run(client, worker.run_store, tenant_id=tenant, spec=spec, input_text=prompt(), app_version=CRASH_VERSION,
                    run_id=run_id, agent_id=uuid.UUID(conv["agent_id"]), create_row=False)
        assert first.wait_exit() == 137
        procs.append(WorkerProcess(_env(schema, executor_id), tmp_path / "w2.log"))
        (call_id,) = _await_waiting(worker, run_id, 1)
        assert decide(worker, client, tenant, run_id, call_id) == "created"
        row = wait_terminal(worker.run_store, tenant, run_id, timeout=90)
        assert row["status"] == "completed", (row["error"], (tmp_path / "w2.log").read_text()[-3000:])
        wait_for(lambda: step_names(schema, run_id)[-1:] in (["eap.memory_index"], ["DBOS.getResult"]) or None,
                 timeout=60, what="the post-turn steps")
        names = step_names(schema, run_id)
        assert names.count("eap.finish_run") == 1
        if shape == "recall":
            assert names.count("eap.memory_index") == 1 and len(embeddings(worker.engine, conv["id"])) == 2
        else:
            assert names.count("eap.memory_observe_run") == 1
            from worker_tests.test_observational_memory import observer_workflow_id, workflow_status

            assert workflow_status(schema, observer_workflow_id(str(run_id))) == "SUCCESS"
        _assert_exactly_once(worker, tenant, run_id, [call_id], [True])
    finally:
        for proc in procs:
            proc.kill()
        client.destroy()
        drop_schema(schema)


# --------------------------------------------------------------------------- eval mode


def test_eval_child_with_two_gated_calls_denies_both_at_once_and_the_inbox_stays_empty(worker, client, tenant, schema):
    from eap_run_store import EvalStore
    from eap_schemas.approvals import EVAL_DENIED_MESSAGE

    workflow_api, auth_header = _api()
    spec = gated_spec(tools=("tickets.create", "clock.now"), gate=("tickets.create", "clock.now"))
    body = batch_prompt(("tickets.create", args(0)), ("clock.now", {}))
    agent_id, parent, child = str(uuid.uuid4()), uuid.uuid4(), uuid.uuid4()
    evals = EvalStore(worker.engine)
    suite = evals.create_suite(tenant, agent_id, "acc", evaluators=[{"kind": "contains", "value": "x"}])
    evals.add_case(tenant, suite.id, {"name": "c0", "input": "hi"})
    worker.run_store.create_eval_run(tenant, agent_id, 1, suite.id, evals.snapshot(tenant, suite.id).canonical_json(), parent)
    worker.run_store.create_child_agent_run(tenant, agent_id, 1, body, child, parent, "case")
    with workflow_api(schema, APP_VERSION) as api:
        start(worker, client, tenant, spec, body, run_id=child, agent_id=uuid.UUID(agent_id), create_row=False)
        run = wait_terminal(worker.run_store, tenant, child, timeout=30)
        assert run["status"] == "completed", run["error"]
        assert lines(run) == [f"Tool tickets__create outcome=denied content={EVAL_DENIED_MESSAGE}",
                              f"Tool clock__now outcome=denied content={EVAL_DENIED_MESSAGE}"]
        for status in ("pending", "decided"):
            assert api.get(f"/api/v1/approvals?status={status}", headers=auth_header(tenant)).json()["items"] == []
    ev = events(worker, child)
    assert not [1 for t, _ in ev.values() if t == "tool_approval_waiting"]
    assert sum(1 for t, d in ev.values() if t == "tool_denied" and d["decided_by"] == "eval") == 2
    names = step_names(schema, child)
    assert "DBOS.recv" not in names and "eap.tool_approval_begin" not in names
    assert side_effects(worker, tenant) == []
    contiguous(worker, child)


# --------------------------------------------------------------------------- old specs


def test_specs_without_approvals_keep_the_golden_step_lists(worker, client, tenant, schema):
    from worker_tests.test_step_layout_ac import GOLDEN

    for model, body, shape in (("ticket", "Laptop broken", "tool"), ("echo", "hi", "plain")):
        from worker_tests.conftest import ticket_spec

        spec = ticket_spec(tools=("tickets.create",) if shape == "tool" else (), model=model)
        assert "approvals" not in spec
        run_id = start(worker, client, tenant, spec, body)
        assert wait_terminal(worker.run_store, tenant, run_id)["status"] == "completed"
        assert step_names(schema, run_id) == GOLDEN[shape]
        assert not [1 for _s, k, _t, _d in event_rows(worker.engine, run_id) if ":approval:" in k]
