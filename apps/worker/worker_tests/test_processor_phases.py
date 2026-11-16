"""Step and stream processor phases in the worker (parity round 2, Task 10; in-process worker,
real Postgres and DBOS). Step processors guard tool-call arguments and results; stream processors
filter live deltas and, implicitly, the final answer (the output pass)."""

from __future__ import annotations

import json

from worker_tests.conftest import APP_VERSION, enqueue_run, event_rows, ticket_spec, wait_terminal
from worker_tests.test_agent_run import DeltaListener
from worker_tests.test_processors_runtime import echo_spec, keys_types, moderation, run

SECRET = "SECRET"
PII = {"type": "pii_redact", "config": {"kinds": ["email"], "action": "redact"}}
BLOCK_SECRET = {"type": "regex_blocklist", "config": {"patterns": [SECRET], "action": "block"}}
REDACT_SECRET = {"type": "regex_blocklist", "config": {"patterns": [SECRET], "action": "redact"}}
REDACT_TICKET_KEY = {"type": "regex_blocklist", "config": {"patterns": ["ticket_id"], "action": "redact"}}


def stream_spec(stream, *, output=()) -> dict:
    spec = echo_spec(output_processors=output)
    spec["stream_processors"] = list(stream)
    return spec


def step_spec(step) -> dict:
    return ticket_spec() | {"step_processors": list(step)}


def event(events, key):
    return next(e for e in events if e[1] == key)


# --------------------------------------------------------------------------- step phase


def test_step_processor_blocks_tool_args_and_run_continues(worker, client, tenant):
    run_id, row = run(worker, client, tenant, step_spec([BLOCK_SECRET]), f"{SECRET} printer on fire")
    assert row["status"] == "completed", row["error"]
    assert worker.tickets.call_count(tenant, f"{run_id}:") == 0  # the tool never ran
    events = event_rows(worker.engine, run_id)
    block = event(events, "processor:step:0:tool:0:args")
    assert block[2] == "processor_result"
    assert block[3]["phase"] == "step" and block[3]["action"] == "block"
    result = next(e for e in events if e[2] == "tool_result")
    assert "Blocked by processor regex_blocklist" in json.dumps(result[3])
    assert SECRET not in json.dumps(result[3])
    assert events[-1][2] == "run_completed"  # the model got the message and answered


def test_step_processor_redacts_tool_result(worker, client, tenant):
    run_id, row = run(worker, client, tenant, step_spec([REDACT_TICKET_KEY]), "printer on fire")
    assert row["status"] == "completed", row["error"]
    assert worker.tickets.call_count(tenant, f"{run_id}:") == 1  # args were fine, the tool ran
    events = event_rows(worker.engine, run_id)
    assert event(events, "processor:step:0:tool:0:args")[3]["action"] == "allow"
    redacted = event(events, "processor:step:0:tool:0:result")
    assert redacted[3]["action"] == "redact"
    result = next(e for e in events if e[2] == "tool_result")
    assert "ticket_id" not in json.dumps(result[3])


def test_step_processor_allows_clean_calls(worker, client, tenant):
    run_id, row = run(worker, client, tenant, step_spec([BLOCK_SECRET]), "printer on fire")
    assert row["status"] == "completed", row["error"]
    assert worker.tickets.call_count(tenant, f"{run_id}:") == 1
    events = event_rows(worker.engine, run_id)
    assert event(events, "processor:step:0:tool:0:args")[3]["action"] == "allow"
    assert event(events, "processor:step:0:tool:0:result")[3]["action"] == "allow"


# --------------------------------------------------------------------------- stream phase

PROMPT = f"xx {SECRET} yy"  # "Echo: xx" / " SECRET " / "yy": no delta of the scripted model splits the word


def test_stream_processor_redacts_live_deltas(worker, client, tenant):
    listener = DeltaListener()
    try:
        run_id, row = run(worker, client, tenant, stream_spec([REDACT_SECRET]), PROMPT)
        assert row["status"] == "completed", row["error"]
    finally:
        listener.close()
    deltas = listener.for_run(run_id)
    assert deltas, "live deltas stay on with only stream processors"
    shown = "".join(d["delta"] for d in deltas)
    assert SECRET not in shown and "Echo: xx" in shown


def test_stream_processor_result_matches_stored_output(worker, client, tenant):
    listener = DeltaListener()
    try:
        run_id, row = run(worker, client, tenant, stream_spec([REDACT_SECRET]), PROMPT)
    finally:
        listener.close()
    assert row["status"] == "completed", row["error"]
    assert SECRET not in row["output"]
    events = event_rows(worker.engine, run_id)
    assert ("processor:stream:0:model:0", "processor_result") in keys_types(events)
    assert event(events, "processor:stream:0:model:0")[3]["phase"] == "stream"
    stored = event(events, "model:0:final")[3]
    assert SECRET not in json.dumps(stored)
    shown = "".join(d["delta"] for d in listener.for_run(run_id))
    assert shown == row["output"]  # the viewer saw what a reload shows


def test_stream_pattern_split_across_deltas_is_caught_by_the_stored_pass(worker, client, tenant):
    listener = DeltaListener()
    try:
        run_id, row = run(worker, client, tenant, stream_spec([REDACT_SECRET]), f"{SECRET} at the start")
    finally:
        listener.close()
    assert row["status"] == "completed", row["error"]
    assert SECRET not in row["output"]  # "Echo: SE" + "CRET ..." slipped past the live filter only


def test_stream_block_stops_deltas_and_blocks_output(worker, client, tenant):
    listener = DeltaListener()
    try:
        run_id, row = run(worker, client, tenant, stream_spec([BLOCK_SECRET]), PROMPT)
    finally:
        listener.close()
    assert row["status"] == "failed" and row["output"] is None
    shown = "".join(d["delta"] for d in listener.for_run(run_id))
    assert SECRET not in shown and "yy" not in shown  # publishing stopped at the blocked delta
    events = event_rows(worker.engine, run_id)
    assert event(events, "processor:stream:0:model:0")[3]["action"] == "block"
    assert event(events, "model:0:final")[3]["blocked"] is True
    assert events[-1][3]["error_code"] == "output_blocked"


def test_live_deltas_stay_on_with_only_stream_processors_and_off_with_output(worker, client, tenant):
    listener = DeltaListener()
    try:
        on_id, on = run(worker, client, tenant, stream_spec([REDACT_SECRET]), "hello")
        off_id, off = run(worker, client, tenant, stream_spec([REDACT_SECRET], output=[moderation()]), "hello")
    finally:
        listener.close()
    assert on["status"] == off["status"] == "completed"
    assert listener.for_run(on_id) and listener.for_run(off_id) == []


def test_stream_and_output_phases_both_recorded(worker, client, tenant):
    spec = stream_spec([REDACT_SECRET], output=[{"type": "max_length", "config": {"max_chars": 500}}])
    run_id, row = run(worker, client, tenant, spec, PROMPT)
    assert row["status"] == "completed", row["error"]
    keys = [k for _, k, _, _ in event_rows(worker.engine, run_id) if k.startswith("processor:")]
    assert keys == ["processor:stream:0:model:0", "processor:output:0:model:0"]


# --------------------------------------------------------------------------- follow-ups (10b)

SCHEMA = {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"], "additionalProperties": False}


def test_logged_tool_call_args_are_redacted_not_raw(worker, client, tenant):
    run_id, row = run(worker, client, tenant, step_spec([REDACT_SECRET]), f"{SECRET} printer on fire")
    assert row["status"] == "completed", row["error"]
    events = event_rows(worker.engine, run_id)
    call = next(e for e in events if e[2] == "tool_call")
    assert SECRET not in json.dumps(call[3]["args"])
    assert SECRET not in json.dumps(event(events, "model:0:final")[3]["message"])
    assert worker.tickets.call_count(tenant, f"{run_id}:") == 1


def test_blocked_tool_call_args_are_withheld_in_the_log(worker, client, tenant):
    run_id, row = run(worker, client, tenant, step_spec([BLOCK_SECRET]), f"{SECRET} printer on fire")
    assert row["status"] == "completed", row["error"]
    events = event_rows(worker.engine, run_id)
    call = next(e for e in events if e[2] == "tool_call")
    assert SECRET not in json.dumps(call[3])
    assert SECRET not in json.dumps(event(events, "model:0:final")[3]["message"])


def test_stream_max_length_counts_across_deltas(worker, client, tenant):
    cap = {"type": "max_length", "config": {"max_chars": 14, "action": "truncate"}}
    listener = DeltaListener()
    try:
        run_id, row = run(worker, client, tenant, stream_spec([cap]), "hello world, this is long")
    finally:
        listener.close()
    assert row["status"] == "completed", row["error"]
    shown = "".join(d["delta"] for d in listener.for_run(run_id))
    assert row["output"] == "Echo: hello wo" and shown == row["output"]


def structured_spec(stream) -> dict:
    return ticket_spec(tools=(), model="structured") | {"output": {"json_schema": SCHEMA}, "stream_processors": list(stream)}


def test_structured_stream_processor_is_recorded_as_stream_phase(worker, client, tenant):
    prompt = json.dumps({"output": {"note": f"xx {SECRET} yy"}})
    run_id, row = run(worker, client, tenant, structured_spec([REDACT_SECRET]), prompt)
    assert row["status"] == "completed", row["error"]
    keys = [k for _, k, _, _ in event_rows(worker.engine, run_id) if k.startswith("processor:")]
    assert keys and all(k.startswith("processor:stream:0") for k in keys), keys
    assert SECRET not in row["output"]


def test_structured_stream_block_reports_its_own_index(worker, client, tenant):
    prompt = json.dumps({"output": {"note": f"xx {SECRET} yy"}})
    run_id, row = run(worker, client, tenant, structured_spec([PII, BLOCK_SECRET]), prompt)
    assert row["status"] == "failed"
    events = event_rows(worker.engine, run_id)
    assert event(events, "processor:stream:1:model:0")[3]["action"] == "block"
    assert not any(k.startswith("processor:output:") for _, k, _, _ in events)
