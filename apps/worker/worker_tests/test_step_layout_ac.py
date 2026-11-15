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
