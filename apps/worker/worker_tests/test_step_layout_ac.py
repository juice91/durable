"""AC.4 step-layout regression: golden DBOS step lists (agent capabilities milestone).

A spec WITHOUT `output` (and the other AC fields) must keep exactly the step names and order recorded
before AC.4, for every shape the worker runs: plain, a built-in tool, processors, MCP, knowledge
(`retrieve` inside the call_tool step, no extra step), and the memory shapes (refreshed 2026-11-14):
history only (`eap.load_memory` in place of `eap.load_history`), semantic recall (`eap.memory_recall`
before and `eap.memory_index` after `eap.finish_run`) and observational (the `eap.memory_observe_run`
enqueue and result after `eap.finish_run`, plus the observer workflow's own steps).

The `GOLDEN` lists below were recorded on the commit before AC.4 (8cadcdd) and must never change for
these specs. The `GOLDEN_OUTPUT` lists are the same shapes with `output` added: every structured-output
step sits inside `_drive_agent` (between the pre-turn memory steps and the `end` event) and never
after `eap.finish_run`.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from worker_tests.conftest import APP_VERSION, enqueue_run, ticket_spec, wait_terminal
from worker_tests.mcp_helpers import mcp_spec, register_mcp_server
from worker_tests.test_conversation import step_names
from worker_tests.test_observational_memory import OBSERVE_RUN, obs_spec, observer_workflow_id, wait_workflow
from worker_tests.test_processors_runtime import BASELINE, PII, echo_spec, moderation
from worker_tests.test_retrieve import PRINTER, ingest, kb_tenant, make_kb, rag_spec  # noqa: F401 - fixture
from worker_tests.test_semantic_recall import recall_spec
from worker_tests.test_working_memory import MEMORY_TURN, conversation, memory_spec, turn

GET_TOOLS = "eap-agent__dynamic_toolset__eap-spec-tools.get_tools"
CALL_TOOL = "eap-agent__dynamic_toolset__eap-spec-tools.call_tool"
MODEL = "eap-agent__model.request_stream"
HANDLER = "eap-agent__event_stream_handler"
EVENT = "eap.append_event"

# Recorded before AC.4 (8cadcdd). Never edit these for a spec without the new AC fields.
GOLDEN: dict[str, list[str]] = {
    "plain": BASELINE["echo"],
    "tool": BASELINE["ticket"],
    "processors": [
        "eap.start_run", "eap.input_processors", EVENT, EVENT, GET_TOOLS, MODEL, "eap.output_processors",
        "eap-moderator__model.request", EVENT, EVENT, EVENT, EVENT, "eap.finish_run",
    ],
    # MCP arguments are validated in their own step (milestone 3), before the call.
    "mcp": BASELINE["ticket"][:6] + ["eap-agent__dynamic_toolset__eap-spec-tools.validate_args"] + BASELINE["ticket"][6:],
    "knowledge": BASELINE["ticket"],
    "memory_history": MEMORY_TURN,
    "memory_recall": MEMORY_TURN[:3] + ["eap.memory_recall"] + MEMORY_TURN[3:] + ["eap.memory_index"],
    "memory_observational": MEMORY_TURN + OBSERVE_RUN,
}
OBSERVER_STEPS_BELOW_THRESHOLD = ["eap.memory_observe"]

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"answer": {"type": "string"}, "score": {"type": "integer"}},
    "required": ["answer", "score"],
    "additionalProperties": False,
}

# With `output` (tool mode, one valid candidate): the output tool's call and result go through the
# DBOSDurability event-handler steps (workflow-side events, after model:{n}:final), inside
# _drive_agent, before `end`. Recorded with AC.4.
OUTPUT_TURN = [GET_TOOLS, MODEL, EVENT, HANDLER, HANDLER]  # get_tools .. the output tool's events
STRUCTURED = "eap.output_processors_structured"
GOLDEN_OUTPUT: dict[str, list[str]] = {
    "plain": ["eap.start_run", EVENT, *OUTPUT_TURN, EVENT, "eap.finish_run"],
    # Candidate: string leaves processed before model:0:final; final pass (deterministic again, then
    # moderation once on the canonical JSON) and processor:output:1:final before `end`.
    "processors": [
        "eap.start_run", "eap.input_processors", EVENT, EVENT, GET_TOOLS, MODEL, STRUCTURED, EVENT, EVENT,
        HANDLER, HANDLER, STRUCTURED, "eap-moderator__model.request", EVENT, EVENT, "eap.finish_run",
    ],
    # retrieve inside call_tool exactly as without `output`, then the structured answer.
    "knowledge": BASELINE["ticket"][:10] + OUTPUT_TURN + [EVENT, "eap.finish_run"],
    "memory_history": MEMORY_TURN[:3] + OUTPUT_TURN + [EVENT, "eap.finish_run"],
    "memory_recall": MEMORY_TURN[:3] + ["eap.memory_recall"] + OUTPUT_TURN + [EVENT, "eap.finish_run", "eap.memory_index"],
    "memory_observational": MEMORY_TURN[:3] + OUTPUT_TURN + [EVENT, "eap.finish_run"] + OBSERVE_RUN,
}


def with_output(spec: dict[str, Any], **output: Any) -> dict[str, Any]:
    return {**spec, "output": {"json_schema": SCHEMA, **output}}


def run(worker, client, tenant, spec, input_text, **kw):
    run_id = enqueue_run(client, worker.run_store, tenant_id=tenant, spec=spec, input_text=input_text,
                         app_version=APP_VERSION, **kw)
    return run_id, wait_terminal(worker.run_store, tenant, run_id)


@pytest.fixture
def schema(worker_settings) -> str:
    return worker_settings.dbos_system_schema


@pytest.fixture
def mem_tenant(worker, tenant):
    yield tenant
    from sqlalchemy import text

    with worker.engine.begin() as conn:
        conn.execute(text("DELETE FROM memory_observations WHERE tenant_id = :t"), {"t": tenant})
        conn.execute(text("DELETE FROM message_embeddings WHERE tenant_id = :t"), {"t": tenant})
        conn.execute(text("DELETE FROM working_memories WHERE tenant_id = :t"), {"t": tenant})


# --------------------------------------------------------------------------- without `output`


def test_plain_tool_and_processor_specs_keep_their_steps(worker, client, schema, tenant):
    run_id, row = run(worker, client, tenant, ticket_spec(tools=(), model="echo"), "hi")
    assert row["status"] == "completed" and step_names(schema, run_id) == GOLDEN["plain"]
    run_id, row = run(worker, client, tenant, ticket_spec(), "Laptop broken")
    assert row["status"] == "completed" and step_names(schema, run_id) == GOLDEN["tool"]
    spec = echo_spec(input_processors=[PII], output_processors=[PII, moderation()])
    run_id, row = run(worker, client, tenant, spec, "mail ada@example.com")
    assert row["status"] == "completed", row
    assert step_names(schema, run_id) == GOLDEN["processors"]


def test_mcp_spec_keeps_its_steps(worker, client, schema, tenant, mcp_env, local_mcp_server):
    register_mcp_server(worker.engine, tenant, "orders", local_mcp_server.url)
    spec = mcp_spec(("orders", "lookup_order"))
    prompt = json.dumps({"tool": "mcp__orders__lookup_order", "args": {"order_id": "123"}})
    run_id, row = run(worker, client, tenant, spec, prompt)
    assert row["status"] == "completed", row
    assert step_names(schema, run_id) == GOLDEN["mcp"]


def test_knowledge_spec_keeps_its_steps(worker, client, schema, kb_tenant):
    kb = make_kb(worker, kb_tenant)
    ingest(worker, kb_tenant, kb, "printer.md", PRINTER)
    run_id, row = run(worker, client, kb_tenant, rag_spec({"id": kb["id"]}), "toner light")
    assert row["status"] == "completed", row
    assert step_names(schema, run_id) == GOLDEN["knowledge"]


def test_memory_specs_keep_their_steps(worker, client, schema, mem_tenant):
    conv = conversation(worker, mem_tenant)
    run_id, row = turn(worker, client, mem_tenant, conv, "hi", memory_spec(model="echo", history={"last_turns": 5}))
    assert row["status"] == "completed" and step_names(schema, run_id) == GOLDEN["memory_history"]

    conv = conversation(worker, mem_tenant)
    run_id, row = turn(worker, client, mem_tenant, conv, "hi", recall_spec(model="echo"))
    wait_workflow(schema, run_id)
    assert row["status"] == "completed" and step_names(schema, run_id) == GOLDEN["memory_recall"]

    conv = conversation(worker, mem_tenant)
    run_id, row = turn(worker, client, mem_tenant, conv, "hi", obs_spec())
    wait_workflow(schema, run_id)
    assert row["status"] == "completed" and step_names(schema, run_id) == GOLDEN["memory_observational"]
    assert step_names(schema, observer_workflow_id(str(run_id))) == OBSERVER_STEPS_BELOW_THRESHOLD


# --------------------------------------------------------------------------- with `output`


def test_structured_specs_add_steps_only_inside_the_agent_loop(worker, client, schema, kb_tenant):
    tenant = kb_tenant
    run_id, row = run(worker, client, tenant, with_output(ticket_spec(tools=(), model="structured")), "hi")
    assert row["status"] == "completed" and step_names(schema, run_id) == GOLDEN_OUTPUT["plain"]
    spec = with_output(echo_spec(input_processors=[PII], output_processors=[PII, moderation()], model="structured"))
    run_id, row = run(worker, client, tenant, spec, "mail ada@example.com")
    assert row["status"] == "completed", row
    assert step_names(schema, run_id) == GOLDEN_OUTPUT["processors"]
    kb = make_kb(worker, tenant)
    ingest(worker, tenant, kb, "printer.md", PRINTER)
    run_id, row = run(worker, client, tenant, with_output(rag_spec({"id": kb["id"]})), "toner light")
    assert row["status"] == "completed" and step_names(schema, run_id) == GOLDEN_OUTPUT["knowledge"]


def test_structured_memory_turns_keep_the_memory_step_slots(worker, client, schema, mem_tenant):
    conv = conversation(worker, mem_tenant)
    spec = with_output(memory_spec(model="structured", history={"last_turns": 5}))
    run_id, row = turn(worker, client, mem_tenant, conv, "hi", spec)
    assert row["status"] == "completed" and step_names(schema, run_id) == GOLDEN_OUTPUT["memory_history"]

    conv = conversation(worker, mem_tenant)
    run_id, row = turn(worker, client, mem_tenant, conv, "hi", with_output(recall_spec(model="structured")))
    wait_workflow(schema, run_id)
    assert row["status"] == "completed" and step_names(schema, run_id) == GOLDEN_OUTPUT["memory_recall"]

    conv = conversation(worker, mem_tenant)
    run_id, row = turn(worker, client, mem_tenant, conv, "hi", with_output(obs_spec(model="structured")))
    wait_workflow(schema, run_id)
    assert row["status"] == "completed" and step_names(schema, run_id) == GOLDEN_OUTPUT["memory_observational"]
    # The observer child workflow's own steps are unchanged by structured output.
    assert step_names(schema, observer_workflow_id(str(run_id))) == OBSERVER_STEPS_BELOW_THRESHOLD


# --------------------------------------------------------------------------- AC.5: limits and fallback

# Native limits and fallback models add NO step (usage_limits is an argument; the fallback chain lives
# inside the model step), so those shapes keep their GOLDEN lists exactly. Only `limits.max_cost_usd`
# adds `eap.check_budget`, before every model request n >= 1 (after the previous response's tool
# results are recorded), inside _drive_agent. Recorded with AC.5.
CHECK_BUDGET = "eap.check_budget"
GOLDEN_COST_CAP: dict[str, list[str]] = {
    "plain": GOLDEN["plain"],  # one model request: never checked
    "tool": BASELINE["ticket"][:10] + [CHECK_BUDGET] + BASELINE["ticket"][10:],
    "knowledge": BASELINE["ticket"][:10] + [CHECK_BUDGET] + BASELINE["ticket"][10:],
}


def with_limits_and_fallback(spec: dict[str, Any], **limits: Any) -> dict[str, Any]:
    """Native limits (generous: never reached) plus a fallback model that is never needed."""
    primary = spec["model"]["name"]
    fallback = "history" if primary != "history" else "echo"
    return {
        **spec,
        "limits": limits or {"max_requests": 20, "max_tool_calls": 20, "max_total_tokens": 1_000_000},
        "fallback_models": [{"provider": "test", "name": fallback}],
    }


def test_limits_and_fallback_keep_the_golden_steps(worker, client, schema, kb_tenant):
    tenant = kb_tenant
    run_id, row = run(worker, client, tenant, with_limits_and_fallback(ticket_spec(tools=(), model="echo")), "hi")
    assert row["status"] == "completed" and step_names(schema, run_id) == GOLDEN["plain"]
    run_id, row = run(worker, client, tenant, with_limits_and_fallback(ticket_spec()), "Laptop broken")
    assert row["status"] == "completed" and step_names(schema, run_id) == GOLDEN["tool"]
    spec = with_limits_and_fallback(echo_spec(input_processors=[PII], output_processors=[PII, moderation()]))
    run_id, row = run(worker, client, tenant, spec, "mail ada@example.com")
    assert row["status"] == "completed", row
    assert step_names(schema, run_id) == GOLDEN["processors"]
    kb = make_kb(worker, tenant)
    ingest(worker, tenant, kb, "printer.md", PRINTER)
    run_id, row = run(worker, client, tenant, with_limits_and_fallback(rag_spec({"id": kb["id"]})), "toner light")
    assert row["status"] == "completed" and step_names(schema, run_id) == GOLDEN["knowledge"]


def test_limits_and_fallback_keep_the_memory_step_slots(worker, client, schema, mem_tenant):
    conv = conversation(worker, mem_tenant)
    spec = with_limits_and_fallback(memory_spec(model="echo", history={"last_turns": 5}))
    run_id, row = turn(worker, client, mem_tenant, conv, "hi", spec)
    assert row["status"] == "completed" and step_names(schema, run_id) == GOLDEN["memory_history"]

    conv = conversation(worker, mem_tenant)
    run_id, row = turn(worker, client, mem_tenant, conv, "hi", with_limits_and_fallback(recall_spec(model="echo")))
    wait_workflow(schema, run_id)
    assert row["status"] == "completed" and step_names(schema, run_id) == GOLDEN["memory_recall"]

    conv = conversation(worker, mem_tenant)
    run_id, row = turn(worker, client, mem_tenant, conv, "hi", with_limits_and_fallback(obs_spec()))
    wait_workflow(schema, run_id)
    assert row["status"] == "completed" and step_names(schema, run_id) == GOLDEN["memory_observational"]
    assert step_names(schema, observer_workflow_id(str(run_id))) == OBSERVER_STEPS_BELOW_THRESHOLD


def test_cost_cap_adds_check_budget_inside_the_agent_loop(worker, client, schema, kb_tenant):
    tenant = kb_tenant
    cap = {"max_cost_usd": "100"}
    run_id, row = run(worker, client, tenant, with_limits_and_fallback(ticket_spec(tools=(), model="echo"), **cap), "hi")
    assert row["status"] == "completed" and step_names(schema, run_id) == GOLDEN_COST_CAP["plain"]
    run_id, row = run(worker, client, tenant, with_limits_and_fallback(ticket_spec(), **cap), "Laptop broken")
    assert row["status"] == "completed" and step_names(schema, run_id) == GOLDEN_COST_CAP["tool"]
    kb = make_kb(worker, tenant)
    ingest(worker, tenant, kb, "printer.md", PRINTER)
    run_id, row = run(worker, client, tenant, with_limits_and_fallback(rag_spec({"id": kb["id"]}), **cap), "toner light")
    assert row["status"] == "completed" and step_names(schema, run_id) == GOLDEN_COST_CAP["knowledge"]


# --------------------------------------------------------------------------- AC.6: tool approval

# The approval wait sits inside _drive_agent: after the response's model_message and tool_call
# events and the two event-handler steps of the gated call (its FunctionToolCallEvent and the
# deferred batch), before the deferred results' handler step and any tool of the response. One
# wait round here: the decision is recorded while the loop sleeps in its first `DBOS.recv` (whose
# timeout is the durable `DBOS.sleep`), so the second `eap.tool_approval_decisions` finds it.
# Recorded with AC.6.
APPROVAL_WAIT = ["eap.tool_approval_begin", "eap.tool_approval_decisions", "eap.workflow_clock", "DBOS.recv",
                 "DBOS.sleep", "eap.tool_approval_decisions"]
TOOL_HEAD = BASELINE["ticket"][:7]  # start_run .. tool_call event, first handler step
TOOL_TAIL = BASELINE["ticket"][9:]  # tool_result event .. finish_run
GOLDEN_APPROVALS: dict[str, list[str]] = {
    # approved: deferred-results handler, then call_tool and its handler step as without approvals.
    "approve": TOOL_HEAD + [HANDLER, *APPROVAL_WAIT, HANDLER, CALL_TOOL, HANDLER] + TOOL_TAIL,
    # denied: no call_tool; the denied return's result event comes through a handler step instead.
    "deny": TOOL_HEAD + [HANDLER, *APPROVAL_WAIT, HANDLER, HANDLER] + TOOL_TAIL,
    # an eval child (approval_mode deny): one deny step, no wait.
    "eval_deny": TOOL_HEAD + [HANDLER, "eap.tool_approval_deny", HANDLER, HANDLER] + TOOL_TAIL,
}
MEMORY_TOOL_HEAD = MEMORY_TURN[:3] + TOOL_HEAD[2:]  # start_run, start, load_memory, get_tools ...


def gated(spec: dict[str, Any], *gate: str, tools: tuple[str, ...] = ("tickets.create",)) -> dict[str, Any]:
    return {**spec, "tools": list(tools), "approvals": {"tools": list(gate or tools)}}


APPROVAL_PROMPT = json.dumps({"tool": "tickets.create", "args": {"title": "Laptop", "body": "Broken."}})


def _waiting_call(worker, schema, run_id) -> str:
    """The gated call's id, once the wait sleeps in its first DBOS.recv."""
    from worker_tests.conftest import event_rows, wait_for

    wait_for(lambda: "DBOS.sleep" in step_names(schema, run_id) or None, timeout=60, what="the approval wait")
    (call_id,) = [d["tool_call_id"] for _s, _k, t, d in event_rows(worker.engine, run_id) if t == "tool_approval_waiting"]
    return call_id


def _decide(worker, client, tenant, run_id, call_id, approve: bool) -> None:
    from eap_run_store.approvals import tool_approval_topic

    outcome, _ = worker.event_log.append_tool_decision(
        tenant, run_id, call_id, "tool_approved" if approve else "tool_denied",
        {"tool_call_id": call_id, "decided_by": "alice", "comment": None},
    )
    assert outcome == "created"
    client.send(str(run_id), {"tool_call_id": call_id}, tool_approval_topic(str(run_id)))


def test_approval_specs_wait_inside_the_agent_loop(worker, client, schema, tenant):
    spec = gated(ticket_spec(model="approval"))
    for approve, shape in ((True, "approve"), (False, "deny")):
        run_id = enqueue_run(client, worker.run_store, tenant_id=tenant, spec=spec, input_text=APPROVAL_PROMPT,
                             app_version=APP_VERSION)
        _decide(worker, client, tenant, run_id, _waiting_call(worker, schema, run_id), approve)
        row = wait_terminal(worker.run_store, tenant, run_id)
        assert row["status"] == "completed", row["error"]
        assert step_names(schema, run_id) == GOLDEN_APPROVALS[shape], shape


def test_eval_child_denies_without_a_wait(worker, client, schema, tenant):
    from eap_run_store import EvalStore

    agent_id = str(uuid.uuid4())
    evals = EvalStore(worker.engine)
    suite = evals.create_suite(tenant, agent_id, "layout", evaluators=[{"kind": "contains", "value": "x"}])
    evals.add_case(tenant, suite.id, {"name": "c0", "input": "hi"})
    parent, child = uuid.uuid4(), uuid.uuid4()
    worker.run_store.create_eval_run(tenant, agent_id, 1, suite.id, evals.snapshot(tenant, suite.id).canonical_json(), parent)
    worker.run_store.create_child_agent_run(tenant, agent_id, 1, APPROVAL_PROMPT, child, parent, "case")
    enqueue_run(client, worker.run_store, tenant_id=tenant, spec=gated(ticket_spec(model="approval")),
                input_text=APPROVAL_PROMPT, app_version=APP_VERSION, run_id=child, agent_id=uuid.UUID(agent_id),
                create_row=False)
    row = wait_terminal(worker.run_store, tenant, child)
    assert row["status"] == "completed", row["error"]
    assert step_names(schema, child) == GOLDEN_APPROVALS["eval_deny"]


def test_approvals_without_a_gated_call_keep_the_golden_steps(worker, client, schema, tenant):
    # The model calls only tickets.create; only clock.now is gated: nothing waits, no step is added.
    spec = gated(ticket_spec(), "clock.now", tools=("tickets.create", "clock.now"))
    run_id, row = run(worker, client, tenant, spec, "Laptop broken")
    assert row["status"] == "completed", row["error"]
    assert step_names(schema, run_id) == GOLDEN["tool"]
    run_id, row = run(worker, client, tenant, gated(ticket_spec(tools=(), model="echo")), "hi")
    assert row["status"] == "completed" and step_names(schema, run_id) == GOLDEN["plain"]


def test_approval_memory_turns_run_post_turn_steps_after_the_wait(worker, client, schema, mem_tenant):
    from worker_tests.test_conversation import start_turn

    shapes = (
        (memory_spec(model="approval", history={"last_turns": 5}), [], []),
        (recall_spec(model="approval"), ["eap.memory_recall"], ["eap.memory_index"]),
        (obs_spec(model="approval"), [], OBSERVE_RUN),
    )
    for base, pre, post in shapes:
        conv = conversation(worker, mem_tenant)
        run_id = start_turn(client, worker.run_store, mem_tenant, conv["id"], APPROVAL_PROMPT, spec=gated(base))
        call_id = _waiting_call(worker, schema, run_id)
        # While waiting: none of the post-turn steps.
        assert not set(step_names(schema, run_id)) & {"eap.finish_run", "eap.memory_index", "eap.memory_observe_run"}
        _decide(worker, client, mem_tenant, run_id, call_id, True)
        row = wait_terminal(worker.run_store, mem_tenant, run_id)
        assert row["status"] == "completed", row["error"]
        if post:
            wait_workflow(schema, run_id)
        expected = (MEMORY_TOOL_HEAD[:3] + pre + MEMORY_TOOL_HEAD[3:]
                    + [HANDLER, *APPROVAL_WAIT, HANDLER, CALL_TOOL, HANDLER] + TOOL_TAIL + post)
        assert step_names(schema, run_id) == expected, base["memory"]
