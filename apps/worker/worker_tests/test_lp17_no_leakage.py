"""LP.17: a loop body's item, state and output, and a judge's prompt/rubric, stay out of telemetry.

Telemetry on in-memory (shared `telemetry` fixture: an OTEL span exporter and log exporter). A
foreach iteration (whose item carries SENTINEL_ITEM), a loop whose state carries SENTINEL_STATE and
whose body emits SENTINEL_OUTPUT, and a judge configured with SENTINEL_RUBRIC are exercised. Every
exported span's name, attributes, event names and attributes, and status description; every log
record's body and attributes; and every Python log record of the test (caplog) is searched for
each sentinel in both plain and JSON-escaped form. The durable rows (events, run output) legitimately
hold the values; only telemetry must not.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import pytest

from worker_tests.conftest import APP_VERSION, event_rows, wait_terminal
from worker_tests.test_telemetry import telemetry  # noqa: F401 - fixture
from worker_tests.workflow_run_helpers import (
    agent_spec,
    cleanup_definitions,
    enqueue_workflow_run,
    publish_agent,
    publish_workflow,
)

SENTINEL_ITEM = f"SENTINELITEM{uuid.uuid4().hex[:12]}"
SENTINEL_STATE = f"SENTINELSTATE{uuid.uuid4().hex[:12]}"
SENTINEL_OUTPUT = f"SENTINELOUTPUT{uuid.uuid4().hex[:12]}"
SENTINEL_RUBRIC = f"SENTINELRUBRIC{uuid.uuid4().hex[:12]}"
SENTINELS = {
    "foreach_item": SENTINEL_ITEM,
    "loop_state": SENTINEL_STATE,
    "body_output": SENTINEL_OUTPUT,
    "judge_rubric": SENTINEL_RUBRIC,
}


def _texts(spans, logs, records) -> list[str]:
    """Every span's name/attributes/events, every log record's body/attributes, every caplog record."""
    out: list[str] = []
    for span in spans:
        out += [span.name, json.dumps(dict(span.attributes or {}), default=str), str(span.status.description or "")]
        for event in span.events:
            out += [event.name, json.dumps(dict(event.attributes or {}), default=str)]
    for log in logs:
        out += [str(log.body), json.dumps(dict(log.attributes or {}), default=str)]
    for record in records:
        out += [record.getMessage(),
                json.dumps({k: v for k, v in record.__dict__.items() if k not in {"args", "msg"}}, default=str)]
    return out


def _absent(haystack: list[str], values: dict[str, str]) -> list[str]:
    hits: list[str] = []
    for name, value in values.items():
        for form in {value, json.dumps(value)[1:-1]}:
            if any(form in t for t in haystack):
                hits.append(name)
                break
    return hits


@pytest.fixture
def defs(worker, tenant):
    yield
    cleanup_definitions(worker.engine, tenant)


def _foreach_spec() -> dict:
    """A foreach of 3 items, each item a tool call that references `loop.each.item`. The item is not
    the body output (tool's clock.now returns a timestamp), so if the item leaks into telemetry it
    came from the per-iteration span wiring, not the body's input."""
    return {
        "name": "Foreach leakage",
        "input_schema": {"type": "object", "properties": {"items": {"type": "array"}}, "required": ["items"]},
        "start": "each",
        "steps": [{
            "id": "each", "type": "foreach", "items": {"$ref": "input.items"}, "max_items": 5,
            "body": {"start": "x", "steps": [
                {"id": "x", "type": "tool", "tool_id": "clock.now", "input": {}}
            ]},
        }],
    }


def _loop_state_spec(agent_id: str) -> dict:
    """A loop whose `state` carries a sentinel through `carry`. The body emits SENTINEL_OUTPUT as
    the step's output via the agent prompt (`test:echo` echoes `Echo: <prompt>`)."""
    body = [{
        "id": "say", "type": "agent", "agent_id": agent_id,
        "input": {"prompt": SENTINEL_OUTPUT, "state": {"$ref": "loop.lp.state.marker"}},
    }]
    return {
        "name": "Loop state leakage",
        "input_schema": {"type": "object"},
        "start": "lp",
        "steps": [{
            "id": "lp", "type": "loop", "max_iterations": 2, "on_max_iterations": "exit",
            "until": {"op": "eq", "left": {"$ref": "loop.lp.index"}, "right": 9},
            "state": {"marker": SENTINEL_STATE},
            "body": {"start": "say", "steps": body},
        }],
    }


def _judge_spec() -> dict:
    """A top-level evaluate step with one llm_judge whose rubric is SENTINEL_RUBRIC."""
    return {
        "name": "Judge rubric leakage",
        "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        "start": "ev",
        "steps": [{
            "id": "ev", "type": "evaluate", "value": {"$ref": "input.text"},
            "evaluators": [
                {"kind": "contains", "value": "x"},
                {"kind": "llm_judge", "rubric": SENTINEL_RUBRIC,
                 "model": {"provider": "test", "name": "judge"}},
            ],
        }],
    }


def _run(worker, client, tenant, spec: dict, input_value: dict) -> uuid.UUID:
    workflow_id, version_id = publish_workflow(worker.engine, tenant, spec)
    return enqueue_workflow_run(
        client, worker.run_store, tenant_id=tenant, workflow_id=workflow_id, version_id=version_id,
        input_value=input_value, app_version=APP_VERSION,
    )


def test_a_loops_item_state_output_and_a_judges_rubric_stay_out_of_telemetry(
    worker, client, tenant, defs, telemetry, caplog  # noqa: F811
):
    _provider, finished = telemetry
    caplog.set_level(logging.DEBUG)

    # 1. A foreach over an item carrying SENTINEL_ITEM.
    foreach_id = _run(worker, client, tenant, _foreach_spec(),
                      {"items": [{"v": SENTINEL_ITEM}, {"v": 2}, {"v": 3}]})
    row = wait_terminal(worker.run_store, tenant, foreach_id)
    assert row["status"] == "completed", row

    # 2. A loop whose state carries SENTINEL_STATE and whose body output is SENTINEL_OUTPUT.
    agent_id = publish_agent(worker.engine, tenant, agent_spec("echo"))
    loop_id = _run(worker, client, tenant, _loop_state_spec(agent_id), {})
    row = wait_terminal(worker.run_store, tenant, loop_id)
    assert row["status"] == "completed", row

    # 3. A top-level evaluate step whose judge's rubric is SENTINEL_RUBRIC.
    judge_id = _run(worker, client, tenant, _judge_spec(), {"text": "x"})
    row = wait_terminal(worker.run_store, tenant, judge_id)
    assert row["status"] == "completed", row

    spans, logs = finished()
    assert any(s.name == "eap.loop_iteration" for s in spans), "no loop_iteration spans captured"
    assert logs, "no log records captured"
    haystack = _texts(spans, logs, caplog.records)

    # The search is not vacuous: each run id appears somewhere in telemetry.
    for rid in (foreach_id, loop_id, judge_id):
        assert any(str(rid) in t for t in _texts(spans, [], [])), f"missing spans for {rid}"

    leaks = _absent(haystack, SENTINELS)
    assert leaks == [], f"sentinels leaked into telemetry: {leaks}"

    # But the durable rows DO hold the sentinels (the runs' events and output). These are the only
    # places the values are allowed to appear.
    rows = event_rows(worker.engine, loop_id)
    dump = json.dumps([d for _, _, _, d in rows], default=str)
    assert SENTINEL_STATE in dump, "SENTINEL_STATE must be recorded in the durable events"
    assert SENTINEL_OUTPUT in dump, "SENTINEL_OUTPUT must be recorded in the durable events"
