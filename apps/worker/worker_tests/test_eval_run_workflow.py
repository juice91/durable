"""Workflow eval suites on the in-process worker (parity round 2, D5): `eap.eval_run` starts a
workflow child run per case, scores its final output and records the results."""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from eap_run_store.evals import EvalStore
from eap_worker.eval_run import eval_child_run_id
from worker_tests.conftest import APP_VERSION, event_rows, wait_terminal
from worker_tests.eval_run_helpers import cleanup_evals, results
from worker_tests.workflow_run_helpers import agent_spec, cleanup_definitions, publish_agent, publish_workflow

SCHEMA = {"type": "object", "properties": {"label": {"type": "string", "enum": ["bug", "question"]}},
          "required": ["label"], "additionalProperties": False}


@pytest.fixture
def defs(worker, tenant):
    yield
    cleanup_evals(worker.engine, tenant)
    cleanup_definitions(worker.engine, tenant)


def forced(label: str) -> str:
    """A case input whose workflow ends with the structured agent answering {"label": label}."""
    return json.dumps({"q": json.dumps({"output": {"label": label}})})


def publish_label_workflow(worker, tenant) -> tuple[str, str]:
    agent_id = publish_agent(worker.engine, tenant, {**agent_spec("structured"), "output": {"json_schema": SCHEMA}})
    spec = {
        "name": "Label",
        "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
        "start": "ask",
        "steps": [{"id": "ask", "type": "agent", "agent_id": agent_id, "input": {"prompt": {"$ref": "input.q"}}}],
    }
    return publish_workflow(worker.engine, tenant, spec)


def start_workflow_eval(worker, client, tenant, workflow_id, cases, evaluators, *, max_concurrency=4):
    store = EvalStore(worker.engine)
    suite = store.create_workflow_suite(tenant, workflow_id, "wf suite", evaluators=evaluators)
    for case in cases:
        store.add_case(tenant, suite.id, case)
    snapshot = store.snapshot(tenant, suite.id, workflow_id=workflow_id, max_concurrency=max_concurrency)
    run_id = uuid.uuid4()
    stored = snapshot.canonical_json()
    worker.run_store.create_workflow_eval_run(tenant, workflow_id, 1, suite.id, stored, run_id)
    client.enqueue(
        {"queue_name": "eap-agent-runs", "workflow_name": "eap.eval_run", "workflow_id": str(run_id),
         "app_version": APP_VERSION},
        str(run_id), tenant, stored,
    )
    return suite.id, run_id


def child_runs(engine, run_id):
    """The workflow runs of the eval run's cases (linked by the results' child_run_id)."""
    with engine.connect() as conn:
        return list(conn.execute(
            text("SELECT r.id, r.kind, r.workflow_id, r.workflow_version, r.status FROM runs r"
                 " JOIN eval_case_results c ON c.child_run_id = r.id::text WHERE c.run_id = :r ORDER BY r.id"),
            {"r": str(run_id)},
        ))


def test_workflow_suite_runs_each_case_as_a_workflow_child(worker, client, tenant, defs):
    workflow_id, _ = publish_label_workflow(worker, tenant)
    cases = [{"name": "one", "input": forced("question")}, {"name": "two", "input": forced("bug")}]
    _, run_id = start_workflow_eval(worker, client, tenant, workflow_id, cases, [{"kind": "equals_expected"}])
    run = wait_terminal(worker.run_store, tenant, run_id, timeout=120)
    assert run["status"] == "completed", run

    rows = results(worker.engine, tenant, run_id)
    assert [r.case_name for r in rows] == ["one", "two"]
    assert [r.output for r in rows] == [{"label": "question"}, {"label": "bug"}]
    for r in rows:
        assert r.child_run_id == eval_child_run_id(str(run_id), r.case_id)
    kids = child_runs(worker.engine, run_id)
    assert {k.kind for k in kids} == {"workflow"} and len(kids) == 2
    assert {(k.workflow_id, k.workflow_version, k.status) for k in kids} == {(workflow_id, 1, "completed")}
    # Linked to the eval run as an agent case run is: parent_run_id and parent_step_id.
    with worker.engine.connect() as conn:
        links = {tuple(r) for r in conn.execute(
            text("SELECT id, parent_run_id, parent_step_id FROM runs WHERE parent_run_id = :r"), {"r": str(run_id)})}
    assert links == {(r.child_run_id, str(run_id), f"case:{r.case_id}") for r in rows}
    # The eval run's cost covers the case runs, and their agent steps (three levels deep).
    assert run["cost_usd"] is not None and run["cost_usd"] > 0
    assert run["cost_usd"] == sum((r.cost_usd for r in rows), type(run["cost_usd"])(0))
    agg = json.loads(run["output"])
    assert agg["cases"] == 2
    events = event_rows(worker.engine, run_id)
    assert events[0][3]["kind"] == "eval" and events[0][3]["workflow_id"] == workflow_id
    assert events[0][3]["workflow_version"] == 1 and "agent_id" not in events[0][3]


def test_equals_expected_compares_json_values_for_workflow_output(worker, client, tenant, defs):
    workflow_id, _ = publish_label_workflow(worker, tenant)
    cases = [
        {"name": "match", "input": forced("question"), "expected_output": {"label": "question"}},
        {"name": "differs", "input": forced("question"), "expected_output": {"label": "bug"}},
        {"name": "as text", "input": forced("bug"), "expected_output": '{"label":"bug"}'},
    ]
    evaluators = [
        {"kind": "equals_expected"},
        {"kind": "contains", "value": '"label":"question"', "name": "has_text"},
        {"kind": "regex", "pattern": 'label":"(bug|question)"', "name": "re"},
    ]
    _, run_id = start_workflow_eval(worker, client, tenant, workflow_id, cases, evaluators)
    assert wait_terminal(worker.run_store, tenant, run_id, timeout=120)["status"] == "completed"
    match, differs, as_text = results(worker.engine, tenant, run_id)
    assert match.assertions["equals_expected"]["value"] is True
    assert differs.assertions["equals_expected"]["value"] is False
    # contains matches the canonical JSON text (not an object's keys); regex too.
    assert match.assertions["has_text"]["value"] is True and match.assertions["re"]["value"] is True
    assert differs.assertions["has_text"]["value"] is True
    assert as_text.assertions["has_text"]["value"] is False and as_text.assertions["re"]["value"] is True
    assert [r.status for r in (match, differs)] == ["passed", "failed"]


def test_field_equals_reads_top_level_output_field(worker, client, tenant, defs):
    workflow_id, _ = publish_label_workflow(worker, tenant)
    cases = [{"name": "q", "input": forced("question")}, {"name": "b", "input": forced("bug")}]
    evaluators = [
        {"kind": "field_equals", "path": "label", "value": "bug"},
        {"kind": "schema_valid"},  # the workflow has no output_schema: the assertion fails, not the run
    ]
    _, run_id = start_workflow_eval(worker, client, tenant, workflow_id, cases, evaluators)
    assert wait_terminal(worker.run_store, tenant, run_id, timeout=120)["status"] == "completed"
    question, bug = results(worker.engine, tenant, run_id)
    assert question.assertions["field_equals"]["value"] is False
    assert bug.assertions["field_equals"]["value"] is True
    assert bug.assertions["schema_valid"]["value"] is False


def test_workflow_output_schema_feeds_schema_valid(worker, client, tenant, defs):
    agent_id = publish_agent(worker.engine, tenant, {**agent_spec("structured"), "output": {"json_schema": SCHEMA}})
    spec = {
        "name": "Label",
        "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
        "output_schema": SCHEMA,
        "start": "ask",
        "steps": [{"id": "ask", "type": "agent", "agent_id": agent_id, "input": {"prompt": {"$ref": "input.q"}}}],
    }
    workflow_id, _ = publish_workflow(worker.engine, tenant, spec)
    _, run_id = start_workflow_eval(
        worker, client, tenant, workflow_id, [{"name": "c", "input": forced("bug")}], [{"kind": "schema_valid"}]
    )
    assert wait_terminal(worker.run_store, tenant, run_id, timeout=120)["status"] == "completed"
    (row,) = results(worker.engine, tenant, run_id)
    assert row.assertions["schema_valid"]["value"] is True and row.status == "passed"


def test_a_failing_child_is_an_error_case_not_a_failed_eval(worker, client, tenant, defs):
    workflow_id, _ = publish_label_workflow(worker, tenant)
    # The structured test model answers outside the enum: the agent step fails the child workflow.
    bad = json.dumps({"q": json.dumps({"output": {"label": "nope"}})})
    cases = [{"name": "bad", "input": bad}, {"name": "good", "input": forced("bug")}]
    _, run_id = start_workflow_eval(worker, client, tenant, workflow_id, cases, [{"kind": "field_equals", "path": "label", "value": "bug"}])
    run = wait_terminal(worker.run_store, tenant, run_id, timeout=120)
    assert run["status"] == "completed", run
    bad_row, good_row = results(worker.engine, tenant, run_id)
    assert (bad_row.status, good_row.status) == ("error", "passed")
    assert bad_row.error_code and bad_row.output is None


def test_archived_workflow_fails_the_eval_run(worker, client, tenant, defs):
    workflow_id, _ = publish_label_workflow(worker, tenant)
    with worker.engine.begin() as conn:
        conn.execute(text("UPDATE workflows SET archived = true WHERE id = :w"), {"w": workflow_id})
    cases = [{"name": "one", "input": forced("bug")}, {"name": "two", "input": forced("bug")}]
    _, run_id = start_workflow_eval(worker, client, tenant, workflow_id, cases, [{"kind": "equals_expected"}])
    run = wait_terminal(worker.run_store, tenant, run_id, timeout=120)
    assert run["status"] == "failed" and "archived" in run["error"]
    events = event_rows(worker.engine, run_id)
    assert events[-1][2] == "run_failed" and events[-1][3]["error_code"] == "eval_workflow_archived"
    rows = results(worker.engine, tenant, run_id)
    assert [(r.status, r.error_code, r.child_run_id) for r in rows] == [("error", "workflow_archived", None)] * 2
    assert child_runs(worker.engine, run_id) == []


def test_a_missing_workflow_version_fails_the_eval_run(worker, client, tenant, defs):
    workflow_id, _ = publish_label_workflow(worker, tenant)
    store = EvalStore(worker.engine)
    suite = store.create_workflow_suite(tenant, workflow_id, "s")
    store.add_case(tenant, suite.id, {"name": "c", "input": forced("bug")})
    stored = store.snapshot(tenant, suite.id, workflow_id=workflow_id).canonical_json()
    run_id = uuid.uuid4()
    worker.run_store.create_workflow_eval_run(tenant, workflow_id, 7, suite.id, stored, run_id)  # no version 7
    client.enqueue(
        {"queue_name": "eap-agent-runs", "workflow_name": "eap.eval_run", "workflow_id": str(run_id),
         "app_version": APP_VERSION},
        str(run_id), tenant, stored,
    )
    run = wait_terminal(worker.run_store, tenant, run_id, timeout=60)
    assert run["status"] == "failed"
    assert event_rows(worker.engine, run_id)[-1][3]["error_code"] == "eval_workflow_not_found"
