"""AC.6 implementer tests: the tool approval runtime (SpecCapability.prepare_tools and
handle_deferred_tool_calls, the durable batch wait, denial and timeout texts, eval deny mode).

The model is `test:approval`: it calls the tool named in its prompt JSON, then answers one line per
return, `Tool <name> outcome=<outcome> content=<content>`. A decision is recorded like the API does
(EventLog.append_tool_decision, then the wake-up on the shared topic), or through the real API
routes (POST /runs/{id}/tool-approvals/{call}/approve|deny) in the end-to-end test."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from sqlalchemy import text

from eap_run_store import ApprovalStore
from eap_run_store.approvals import tool_approval_topic
from eap_schemas import AgentSpec
from eap_schemas.approvals import EVAL_DENIED_MESSAGE, TIMED_OUT_MESSAGE, denial_message
from eap_worker.capabilities import (
    INVALID_ID_MESSAGE,
    ApprovalSettings,
    SpecCapability,
    _result_for,
    approvable_tool_call_id,
)
from worker_tests.conftest import APP_VERSION, enqueue_run, event_rows, ticket_spec, wait_for, wait_terminal
from worker_tests.test_conversation import step_names

TICKET_ARGS = {"title": "Laptop broken", "body": "It will not boot."}


def gated_spec(*, tools: tuple[str, ...] = ("tickets.create",), gate: tuple[str, ...] = ("tickets.create",),
               timeout_hours: int | None = None, **extra: Any) -> dict[str, Any]:
    approvals: dict[str, Any] = {"tools": list(gate)}
    if timeout_hours is not None:
        approvals["timeout_hours"] = timeout_hours
    return {**ticket_spec(tools=tools, model="approval"), "approvals": approvals, **extra}


def prompt(tool: str = "tickets.create", args: dict[str, Any] | None = None) -> str:
    return json.dumps({"tool": tool, "args": TICKET_ARGS if args is None else args})


def start(worker, client, tenant, spec, input_text, **kw):
    return enqueue_run(client, worker.run_store, tenant_id=tenant, spec=spec, input_text=input_text,
                       app_version=APP_VERSION, **kw)


def events(worker, run_id) -> dict[str, tuple[str, dict]]:
    return {key: (type_, data) for _seq, key, type_, data in event_rows(worker.engine, run_id)}


def wait_waiting(worker, run_id, count: int = 1, timeout: float = 60.0) -> list[dict[str, Any]]:
    def found():
        rows = [data for _s, key, type_, data in event_rows(worker.engine, run_id) if type_ == "tool_approval_waiting"]
        return rows if len(rows) >= count else None

    return wait_for(found, timeout=timeout, what=f"{count} tool_approval_waiting event(s) of {run_id}")


def decide(worker, client, tenant, run_id, call_id, *, approve: bool = True, comment: str | None = None,
           send: bool = True) -> str:
    """Like eap_api.approvals.decide: the decision event (+ inbox row), then the wake-up."""
    outcome, _ = worker.event_log.append_tool_decision(
        tenant, run_id, call_id, "tool_approved" if approve else "tool_denied",
        {"tool_call_id": call_id, "decided_by": "alice", "comment": comment},
    )
    if outcome == "created" and send:
        client.send(str(run_id), {"tool_call_id": call_id, "decision": "approved" if approve else "denied"},
                    tool_approval_topic(str(run_id)))
    return outcome


def side_effects(worker, tenant) -> list[tuple[str, str]]:
    with worker.engine.connect() as conn:
        return [tuple(r) for r in conn.execute(
            text("SELECT key, state FROM side_effects WHERE tenant_id = :t ORDER BY key"),
            {"t": tenant},
        )]


def contiguous(worker, run_id) -> None:
    seqs = [e[0] for e in event_rows(worker.engine, run_id)]
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs))), seqs


# --------------------------------------------------------------------------- unit


def test_approvable_tool_call_ids_follow_the_decide_route_rule():
    assert approvable_tool_call_id("call_1") and approvable_tool_call_id("x" * 256)
    assert approvable_tool_call_id("toolu_01A~!")
    for bad in ("", "x" * 257, "a b", "tab\tid", "é", None, 7):
        assert not approvable_tool_call_id(bad), bad


def test_results_for_each_decision():
    assert type(_result_for({"type": "tool_approved", "data": {}})).__name__ == "ToolApproved"
    assert _result_for({"type": "tool_denied", "data": {"comment": "no"}}).message == denial_message("no")
    assert _result_for({"type": "tool_denied", "data": {}}).message == "The user denied this tool call."
    assert _result_for({"type": "tool_denied", "data": {"decided_by": "eval"}}).message == EVAL_DENIED_MESSAGE
    assert _result_for({"type": "tool_denied", "data": {"reason": "invalid_tool_call_id"}}).message == INVALID_ID_MESSAGE
    assert _result_for({"type": "tool_approval_timed_out", "data": {}}).message == TIMED_OUT_MESSAGE
    assert _result_for(None).message == TIMED_OUT_MESSAGE


def test_settings_from_an_old_checkpoint_are_interactive(monkeypatch):
    # Workflow code never reads the env: an old checkpoint gets the production defaults.
    monkeypatch.setenv("EAP_WORKFLOW_HOUR_SECONDS", "2")
    monkeypatch.setenv("EAP_APPROVAL_POLL_SECONDS", "2")
    s = ApprovalSettings.from_link({})
    assert (s.mode, s.hour_seconds, s.poll_seconds) == ("interactive", 3600.0, 3600.0)
    s = ApprovalSettings.from_link({"approval_mode": "deny", "approval_timing": {"hour_seconds": 5, "poll_seconds": 1}})
    assert (s.mode, s.hour_seconds, s.poll_seconds) == ("deny", 5.0, 1.0)
    assert ApprovalSettings.from_link({"approval_mode": "bogus"}).mode == "interactive"


def test_spec_capability_keeps_every_hook_as_a_method():
    # Regression (AC.6 review): a module-level function inserted mid-class once swallowed
    # after_output_validate, so the output schema was silently no longer enforced.
    for name in ("prepare_tools", "handle_deferred_tool_calls", "after_output_validate", "get_serialization_name"):
        assert name in SpecCapability.__dict__, name


def test_prepare_tools_marks_only_gated_function_tools():
    import asyncio
    from types import SimpleNamespace

    from pydantic_ai.tools import ToolDefinition

    spec = AgentSpec.model_validate(gated_spec(tools=("tickets.create", "clock.now")))
    defs = [ToolDefinition(name="tickets__create"), ToolDefinition(name="clock__now")]
    out = asyncio.run(SpecCapability().prepare_tools(SimpleNamespace(deps=SimpleNamespace(spec=spec)), defs))
    assert [(d.name, d.kind) for d in out] == [("tickets__create", "unapproved"), ("clock__now", "function")]
    plain = AgentSpec.model_validate(ticket_spec())
    assert asyncio.run(SpecCapability().prepare_tools(SimpleNamespace(deps=SimpleNamespace(spec=plain)), defs)) is defs


# --------------------------------------------------------------------------- the wait


def test_approved_call_runs_once_after_the_decision(worker, client, tenant):
    run_id = start(worker, client, tenant, gated_spec(), prompt())
    (waiting,) = wait_waiting(worker, run_id)
    call_id = waiting["tool_call_id"]
    assert waiting["tool_name"] == "tickets__create" and waiting["args"] == TICKET_ARGS
    assert waiting["workflow_id"] == str(run_id) and waiting["batch"] == [call_id] and waiting["deadline"]
    # Waiting: running, displayed waiting_approval, in the inbox, nothing reserved yet.
    run = worker.run_store.get_run(tenant, run_id)
    assert (run["status"], run["display_status"]) == ("running", "waiting_approval")
    (row,), _ = ApprovalStore(worker.engine).list(tenant, "pending")
    assert (row["kind"], row["approval_key"], row["tool_name"], row["root_run_id"]) == (
        "tool", call_id, "tickets__create", str(run_id))
    assert side_effects(worker, tenant) == []
    keys = [k for _s, k, _t, _d in event_rows(worker.engine, run_id)]
    assert keys.index(f"tool:{call_id}:call") < keys.index(f"tool:{call_id}:approval:waiting")

    assert decide(worker, client, tenant, run_id, call_id) == "created"
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "completed", run["error"]
    assert "outcome=success" in run["output"]
    assert side_effects(worker, tenant) == [(f"{run_id}:{call_id}", "completed")]
    ev = events(worker, run_id)
    assert ev[f"tool:{call_id}:approval:decision"][0] == "tool_approved"
    assert ev[f"tool:{call_id}:result"][1]["status"] == "ok"
    contiguous(worker, run_id)
    (decided,), _ = ApprovalStore(worker.engine).list(tenant, "decided")
    assert (decided["decision"], decided["decided_by"]) == ("approved", "alice")


def test_denied_call_never_runs_and_the_model_reads_the_comment(worker, client, tenant):
    run_id = start(worker, client, tenant, gated_spec(), prompt())
    (waiting,) = wait_waiting(worker, run_id)
    call_id = waiting["tool_call_id"]
    assert decide(worker, client, tenant, run_id, call_id, approve=False, comment="not today") == "created"
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "completed", run["error"]
    message = denial_message("not today")
    assert run["output"] == f"Tool tickets__create outcome=denied content={message}"
    result = events(worker, run_id)[f"tool:{call_id}:result"][1]
    assert (result["status"], result["content"]) == ("denied", message)
    assert side_effects(worker, tenant) == []
    # The completed turn's messages hold the call and its denied return.
    returns = [p for m in run_messages(worker, tenant, run_id) for p in m.get("parts", [])
               if p.get("part_kind") == "tool-return"]
    assert [(p["tool_call_id"], p["outcome"]) for p in returns] == [(call_id, "denied")]


def run_messages(worker, tenant, run_id) -> list[dict[str, Any]]:
    with worker.engine.connect() as conn:
        return conn.execute(text("SELECT messages FROM runs WHERE id = :r AND tenant_id = :t"),
                            {"r": str(run_id), "t": tenant}).scalar_one() or []


def test_timeout_denies_and_the_run_continues(worker, client, tenant, monkeypatch):
    monkeypatch.setenv("EAP_WORKFLOW_HOUR_SECONDS", "1")
    monkeypatch.setenv("EAP_APPROVAL_POLL_SECONDS", "0.3")
    run_id = start(worker, client, tenant, gated_spec(timeout_hours=1), prompt())
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "completed", run["error"]
    assert run["output"] == f"Tool tickets__create outcome=denied content={TIMED_OUT_MESSAGE}"
    ev = events(worker, run_id)
    (call_id,) = [d["tool_call_id"] for t, d in ev.values() if t == "tool_approval_waiting"]
    decision_type, decision = ev[f"tool:{call_id}:approval:decision"]
    assert decision_type == "tool_approval_timed_out" and decision.get("decided_by") is None
    assert ev[f"tool:{call_id}:result"][1]["status"] == "denied"
    row = ApprovalStore(worker.engine).get(tenant, str(run_id), "tool", call_id)
    assert row["decision"] == "timed_out" and row["decided_by"] is None
    # A late person's decision is "exists" (the API answers 409).
    assert decide(worker, client, tenant, run_id, call_id) == "exists"
    assert side_effects(worker, tenant) == []


def test_two_gated_calls_decided_in_reverse_order(worker, client, tenant):
    spec = gated_spec(tools=("tickets.create", "clock.now"), gate=("tickets.create", "clock.now"))
    body = json.dumps({"calls": [{"tool": "tickets.create", "args": TICKET_ARGS}, {"tool": "clock.now", "args": {}}]})
    run_id = start(worker, client, tenant, spec, body)
    first, second = wait_waiting(worker, run_id, count=2)
    assert first["batch"] == second["batch"] == [first["tool_call_id"], second["tool_call_id"]]
    assert first["deadline"] == second["deadline"]  # one deadline per batch
    # Decide the second first: the wait must not end after one decision.
    assert decide(worker, client, tenant, run_id, second["tool_call_id"], approve=False) == "created"
    wait_for(lambda: step_names(_schema(), run_id).count("eap.tool_approval_decisions") >= 2 or None,
             timeout=30, what="the wait to read the decisions again")
    assert worker.run_store.get_run(tenant, run_id)["status"] == "running"
    assert decide(worker, client, tenant, run_id, first["tool_call_id"]) == "created"
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "completed", run["error"]
    lines = run["output"].splitlines()
    assert lines[0].startswith("Tool tickets__create outcome=success")
    assert lines[1] == "Tool clock__now outcome=denied content=The user denied this tool call."
    assert side_effects(worker, tenant) == [(f"{run_id}:{first['tool_call_id']}", "completed")]


_SCHEMA: list[str] = []


def _schema() -> str:
    return _SCHEMA[0]


@pytest.fixture(autouse=True)
def _remember_schema(worker_settings):
    _SCHEMA[:] = [worker_settings.dbos_system_schema]


def test_lost_wake_up_is_picked_up_at_the_next_poll(worker, client, tenant, monkeypatch):
    monkeypatch.setenv("EAP_APPROVAL_POLL_SECONDS", "0.5")
    run_id = start(worker, client, tenant, gated_spec(), prompt())
    (waiting,) = wait_waiting(worker, run_id)
    assert decide(worker, client, tenant, run_id, waiting["tool_call_id"], send=False) == "created"
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "completed" and "outcome=success" in run["output"], run["error"]


def test_ungated_tool_of_a_gated_spec_runs_without_a_wait(worker, client, tenant):
    spec = gated_spec(tools=("tickets.create", "clock.now"), gate=("tickets.create",))
    run_id = start(worker, client, tenant, spec, prompt("clock.now", {}))
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "completed" and "outcome=success" in run["output"], run["error"]
    assert not [k for k in events(worker, run_id) if ":approval:" in k]
    names = step_names(_schema(), run_id)
    assert "eap.tool_approval_begin" not in names and "DBOS.recv" not in names


def test_eval_child_runs_deny_at_once(worker, client, tenant):
    from eap_run_store import EvalStore

    agent_id = str(uuid.uuid4())
    store = EvalStore(worker.engine)
    suite = store.create_suite(tenant, agent_id, "smoke", evaluators=[{"kind": "contains", "value": "x"}])
    store.add_case(tenant, suite.id, {"name": "c0", "input": "hi"})
    parent = uuid.uuid4()
    worker.run_store.create_eval_run(tenant, agent_id, 1, suite.id, store.snapshot(tenant, suite.id).canonical_json(), parent)
    child = uuid.uuid4()
    worker.run_store.create_child_agent_run(tenant, agent_id, 1, prompt(), child, parent, "case")
    start(worker, client, tenant, gated_spec(), prompt(), run_id=child, agent_id=uuid.UUID(agent_id), create_row=False)
    run = wait_terminal(worker.run_store, tenant, child)
    assert run["status"] == "completed", run["error"]
    assert run["output"] == f"Tool tickets__create outcome=denied content={EVAL_DENIED_MESSAGE}"
    ev = events(worker, child)
    assert not [t for t, _ in ev.values() if t == "tool_approval_waiting"]
    (decision,) = [d for t, d in ev.values() if t == "tool_denied"]
    assert decision["decided_by"] == "eval"
    assert ApprovalStore(worker.engine).list_for_run(tenant, str(child)) == []
    names = step_names(_schema(), child)
    assert "eap.tool_approval_deny" in names and "DBOS.recv" not in names
    assert side_effects(worker, tenant) == []


# --------------------------------------------------------------------------- through the API routes


def test_decide_routes_wake_the_waiting_run_end_to_end(worker, client, tenant, worker_settings):
    """POST /runs/{id}/tool-approvals/{call}/approve and /deny: the API's decision event and its
    DBOS wake-up (launcher.notify_tool_approval, topic tool_approval_topic) reach the worker's recv.
    The poll stays at its default (1 h), so only the wake-up can end the wait in time."""
    from worker_tests.test_workflow_run_acceptance import auth_header, workflow_api

    with workflow_api(worker_settings.dbos_system_schema, APP_VERSION) as api:
        run_id = start(worker, client, tenant, gated_spec(), prompt())
        (waiting,) = wait_waiting(worker, run_id)
        call_id = waiting["tool_call_id"]
        url = f"/api/v1/runs/{run_id}/tool-approvals/{call_id}"
        inbox = api.get("/api/v1/approvals?status=pending", headers=auth_header(tenant))
        assert inbox.status_code == 200, inbox.text
        response = api.post(f"{url}/approve", json={"comment": "ok"}, headers=auth_header(tenant))
        assert response.status_code == 200, response.text
        run = wait_terminal(worker.run_store, tenant, run_id, timeout=30)
        assert run["status"] == "completed" and "outcome=success" in run["output"], run["error"]
        again = api.post(f"{url}/deny", json={}, headers=auth_header(tenant))
        assert again.status_code == 409, again.text

        run_id = start(worker, client, tenant, gated_spec(), prompt())
        (waiting,) = wait_waiting(worker, run_id)
        url = f"/api/v1/runs/{run_id}/tool-approvals/{waiting['tool_call_id']}"
        response = api.post(f"{url}/deny", json={"comment": "use the portal"}, headers=auth_header(tenant))
        assert response.status_code == 200, response.text
        run = wait_terminal(worker.run_store, tenant, run_id, timeout=30)
        assert run["output"] == f"Tool tickets__create outcome=denied content={denial_message('use the portal')}"


# --------------------------------------------------------------------------- the tool-call limit


def test_denied_calls_do_not_count_against_the_tool_call_limit(worker, client, tenant):
    spec = gated_spec(tools=("tickets.create", "clock.now"), gate=("tickets.create", "clock.now"),
                      limits={"max_tool_calls": 1})
    body = json.dumps({"calls": [{"tool": "tickets.create", "args": TICKET_ARGS}, {"tool": "clock.now", "args": {}}]})
    run_id = start(worker, client, tenant, spec, body)
    first, second = wait_waiting(worker, run_id, count=2)
    decide(worker, client, tenant, run_id, first["tool_call_id"], approve=False)
    decide(worker, client, tenant, run_id, second["tool_call_id"])
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "completed", run["error"]
    assert run["output"].splitlines()[1].startswith("Tool clock__now outcome=success")


def test_ungated_and_approved_calls_together_count_against_the_limit(worker, client, tenant):
    # clock.now (ungated) runs first and uses the one allowed call; the approved ticket would be the
    # second: the run fails before it runs, like an ungated batch of two.
    spec = gated_spec(tools=("tickets.create", "clock.now"), gate=("tickets.create",), limits={"max_tool_calls": 1})
    body = json.dumps({"calls": [{"tool": "clock.now", "args": {}}, {"tool": "tickets.create", "args": TICKET_ARGS}]})
    run_id = start(worker, client, tenant, spec, body)
    (waiting,) = wait_waiting(worker, run_id)
    decide(worker, client, tenant, run_id, waiting["tool_call_id"])
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "failed", run
    end = events(worker, run_id)["end"][1]
    assert (end["error_code"], end["limit"], end["value"]) == ("usage_limit_exceeded", "max_tool_calls", 1)
    assert side_effects(worker, tenant) == []
    contiguous(worker, run_id)


# --------------------------------------------------------------------------- reused tool call ids


def _reusing_model():
    """A model that numbers its calls per response, like some OpenAI-compatible backends: every
    response calls tickets__create with tool_call_id `call_0`, twice, then answers with the outcomes."""
    from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
    from pydantic_ai.models.function import DeltaToolCall, FunctionModel

    def plan(messages):
        returns = [p for m in messages if isinstance(m, ModelRequest) for p in m.parts if isinstance(p, ToolReturnPart)]
        if len(returns) < 2:
            args = {"title": f"Ticket {len(returns)}", "body": "Second call, different args." if returns else "First."}
            return None, args
        return "\n".join(f"outcome={r.outcome} content={r.content}" for r in returns), None

    def respond(messages, info):
        text_out, args = plan(messages)
        if text_out is None:
            return ModelResponse(parts=[ToolCallPart("tickets__create", args, tool_call_id="call_0")])
        return ModelResponse(parts=[TextPart(text_out)])

    async def stream(messages, info):
        text_out, args = plan(messages)
        if text_out is None:
            yield {0: DeltaToolCall(name="tickets__create", json_args=json.dumps(args), tool_call_id="call_0")}
        else:
            yield text_out

    return FunctionModel(respond, stream_function=stream, model_name="scripted-reuse")


def test_a_reused_tool_call_id_never_inherits_an_earlier_approval(worker, client, tenant, monkeypatch):
    """Security: the second call reuses `call_0` (other args, a later model request). The first
    call's approval must not authorise it: it is denied at once (duplicate_tool_call_id), with no
    new wait, no inbox row and no second ticket."""
    from eap_worker import models
    from eap_worker.capabilities import DUPLICATE_ID_MESSAGE

    monkeypatch.setattr(models, "scripted_approval_model", _reusing_model)
    run_id = start(worker, client, tenant, gated_spec(), "hi")
    (waiting,) = wait_waiting(worker, run_id)
    assert (waiting["tool_call_id"], waiting["model_request"]) == ("call_0", 0)
    assert decide(worker, client, tenant, run_id, "call_0") == "created"
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "completed", run["error"]
    first, second = run["output"].splitlines()
    assert first.startswith("outcome=success")
    assert second == f"outcome=denied content={DUPLICATE_ID_MESSAGE}"
    ev = events(worker, run_id)
    assert [k for k in ev if k.startswith("tool:call_0:approval:")] == [
        "tool:call_0:approval:waiting", "tool:call_0:approval:decision", "tool:call_0:approval:duplicate:1"]
    denied = ev["tool:call_0:approval:duplicate:1"]
    assert denied[0] == "tool_denied"
    assert (denied[1]["decided_by"], denied[1]["reason"], denied[1]["model_request"]) == (
        "system", "duplicate_tool_call_id", 1)
    # One wait in the whole run, one inbox row, one ticket.
    names = step_names(_schema(), run_id)
    assert names.count("eap.tool_approval_begin") == 2 and names.count("eap.tool_approval_deny") == 1
    assert len(ApprovalStore(worker.engine).list_for_run(tenant, str(run_id))) == 1
    assert worker.tickets.ticket_count(tenant, f"{run_id}:") == 1
    contiguous(worker, run_id)
