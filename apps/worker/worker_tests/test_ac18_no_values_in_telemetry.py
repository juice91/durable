"""AC.18: no structured output values, tool arguments, approval comments or eval outputs in spans or logs.

The in-process worker runs with telemetry switched on into in-memory exporters (as
worker_tests/test_telemetry.py does), Pydantic AI instrumented the way production instruments it
(`include_content=False`). Each run carries a distinct sentinel in exactly the value that must stay
out of telemetry:

- a structured output object (`test:structured` answers the prompt's `output` override), including a
  run whose first candidate fails validation (the `output_retry` path and its log line);
- the arguments of a gated tool call, and the comment of its denial;
- an eval case's input and the child's output, scored by an LLM judge.

Then every exported span (name, attributes, event names and attributes, status description) and
every exported log record (body and attributes), plus every Python log record of the test (caplog),
is searched for every sentinel, in plain and JSON-escaped form. The runs' own durable rows do hold the
values (the events, the output); only telemetry must not.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import pytest

from worker_tests.conftest import APP_VERSION, enqueue_run, event_rows, wait_terminal
from worker_tests.eval_run_helpers import JUDGE, cleanup_evals, create_suite, start_eval
from worker_tests.test_telemetry import telemetry  # noqa: F401 - fixture
from worker_tests.test_tool_approval import decide, gated_spec, wait_waiting
from worker_tests.workflow_run_helpers import agent_spec, cleanup_definitions, publish_agent

SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}, "score": {"type": "integer"}},
    "required": ["answer", "score"],
    "additionalProperties": False,
}


def sentinel(what: str) -> str:
    return f"SENTINEL{what}{uuid.uuid4().hex[:12]}"


def texts(spans: list[Any], logs: list[Any], records: list[logging.LogRecord]) -> list[str]:
    out: list[str] = []
    for span in spans:
        out += [span.name, json.dumps(dict(span.attributes or {}), default=str), str(span.status.description or "")]
        for event in span.events:
            out += [event.name, json.dumps(dict(event.attributes or {}), default=str)]
    for log in logs:
        out += [str(log.body), json.dumps(dict(log.attributes or {}), default=str)]
    for record in records:
        out += [record.getMessage(), json.dumps({k: v for k, v in record.__dict__.items() if k not in {"args", "msg"}},
                                                default=str)]
    return out


def assert_absent(haystack: list[str], sentinels: dict[str, str]) -> None:
    for what, value in sentinels.items():
        for form in {value, json.dumps(value)[1:-1]}:
            hits = [t for t in haystack if form in t]
            assert not hits, f"{what} leaked into telemetry: {hits[:3]}"


@pytest.fixture
def defs(worker, tenant):
    yield
    cleanup_evals(worker.engine, tenant)
    cleanup_definitions(worker.engine, tenant)


def test_no_output_args_comments_or_eval_values_in_spans_or_logs(
    worker, client, tenant, defs, telemetry, caplog  # noqa: F811
):
    _provider, finished = telemetry
    caplog.set_level(logging.DEBUG)
    values = {name: sentinel(name) for name in ("output", "retried", "args", "comment", "case", "judged")}

    def run(spec: dict[str, Any], prompt: str):
        run_id = enqueue_run(client, worker.run_store, tenant_id=tenant, spec=spec, input_text=prompt,
                             app_version=APP_VERSION)
        return run_id, wait_terminal(worker.run_store, tenant, run_id)

    structured = {**agent_spec("structured"), "output": {"json_schema": SCHEMA}}
    run_id, row = run(structured, json.dumps({"output": {"answer": values["output"], "score": 1}}))
    assert row["status"] == "completed" and values["output"] in row["output"], row
    run_id, row = run(structured, json.dumps({"bad_first": True, "output": {"answer": values["retried"], "score": 1}}))
    assert row["status"] == "completed" and values["retried"] in row["output"], row
    assert "output_retry" in [t for _s, _k, t, _d in event_rows(worker.engine, run_id)]

    prompt = json.dumps({"tool": "tickets.create", "args": {"title": values["args"], "body": values["args"]}})
    gated = enqueue_run(client, worker.run_store, tenant_id=tenant, spec=gated_spec(), input_text=prompt,
                        app_version=APP_VERSION)
    (waiting,) = wait_waiting(worker, gated)
    assert values["args"] in json.dumps(waiting["args"])  # the event holds them; telemetry must not
    assert decide(worker, client, tenant, gated, waiting["tool_call_id"], approve=False,
                  comment=values["comment"]) == "created"
    row = wait_terminal(worker.run_store, tenant, gated)
    assert row["status"] == "completed" and values["comment"] in row["output"], row

    agent_id = publish_agent(worker.engine, tenant, agent_spec("echo"))
    suite_id = create_suite(worker.engine, tenant, agent_id,
                            [{"name": "c0", "input": f"{values['case']} {values['judged']}"}],
                            [{"kind": "contains", "value": "Echo:"},
                             {"kind": "llm_judge", "rubric": "MUST_CONTAIN:Echo", "model": JUDGE,
                              "include_input": True}])
    eval_run = start_eval(worker.engine, worker.run_store, client, tenant_id=tenant, agent_id=agent_id,
                          suite_id=suite_id, app_version=APP_VERSION)
    row = wait_terminal(worker.run_store, tenant, eval_run, timeout=120)
    assert row["status"] == "completed", row

    spans, logs = finished()
    assert any(s.name == "eap.run" for s in spans) and logs, "telemetry was not captured"
    haystack = texts(spans, logs, caplog.records)
    # The search is not vacuous: each run's id is in its spans and logs.
    for rid in (run_id, gated, eval_run):
        assert any(str(rid) in t for t in texts(spans, [], [])), rid
        assert any(str(rid) in t for t in texts([], logs, [])), rid
    assert_absent(haystack, values)
