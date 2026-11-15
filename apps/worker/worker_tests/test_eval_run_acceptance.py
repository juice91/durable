"""AC.11 acceptance (tester): every evaluator with a pass, a fail and an error plus edge cases
(non-string and structured output, an invalid regex), the judge through eap-judge, child-run rules
(deny mode, no conversation, no memory, knowledge retrieval), batching and order, snapshot integrity,
cost, and an eval run created before AC.11. Complements test_evaluators / test_eval_run (not
duplicated). Crash recovery lives in test_eval_run_crash_acceptance.py."""

from __future__ import annotations

import asyncio
import json
import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text

from eap_run_store.evals import EvalStore
from eap_worker.evaluators import (
    EVALUATOR_RAISED,
    NO_COST,
    NO_DURATION,
    REGEX_INVALID,
    plan_evaluators,
    score_deterministic,
)
from worker_tests.conftest import APP_VERSION, event_rows, wait_terminal
from worker_tests.eval_run_helpers import JUDGE, child_runs, cleanup_evals, create_suite, results, start_eval, usage_keys
from worker_tests.test_conversation import step_names
from worker_tests.workflow_run_helpers import agent_spec, cleanup_definitions, publish_agent

SCHEMA = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}


def score(spec: dict[str, Any], output: Any, *, expected: Any = None, duration_s: float | None = 1.0,
          cost: str | None = "0.01", schema: dict[str, Any] | None = None):
    case = {"name": "c", "input": "i", "expected_output": expected, "metadata": {}}
    return asyncio.run(score_deterministic(plan_evaluators([spec], ()), case, output, duration_s=duration_s,
                                           cost_usd=cost, output_schema=schema))


# (spec, passing output, failing output, expected output)
KINDS = [
    ({"kind": "equals_expected"}, {"n": 1}, {"n": 2}, {"n": 1}),
    ({"kind": "contains", "value": "ok"}, "all ok", "nope", None),
    ({"kind": "regex", "pattern": r"^\d+$"}, "123", "12a", None),
    ({"kind": "schema_valid", "json_schema": SCHEMA}, {"n": 1}, {"n": "x"}, None),
    ({"kind": "field_equals", "path": "n", "value": 1}, {"n": 1}, {"n": 2}, None),
    ({"kind": "max_duration", "seconds": 5}, "x", "x", None),  # failing case uses a long duration below
    ({"kind": "max_cost", "usd": "0.05"}, "x", "x", None),  # failing case uses a high cost below
]


@pytest.mark.parametrize("spec,good,bad,expected", KINDS, ids=[k[0]["kind"] for k in KINDS])
def test_every_deterministic_evaluator_passes_and_fails(spec, good, bad, expected):
    slow, dear = (9.0, "0.50") if spec["kind"] in ("max_duration", "max_cost") else (1.0, "0.01")
    ok = score(spec, good, expected=expected)
    assert ok.evaluator_failures == [] and ok.passed() and ok.assertions[spec["kind"]]["value"] is True
    ko = score(spec, bad, expected=expected, duration_s=slow, cost=dear)
    assert ko.evaluator_failures == [] and not ko.passed() and ko.assertions[spec["kind"]]["value"] is False


def test_every_evaluator_error_is_a_failure_entry_with_a_fixed_message():
    cases = [
        (score({"kind": "max_duration", "seconds": 1}, "x", duration_s=None), "EvaluatorUnavailable", NO_DURATION),
        (score({"kind": "max_cost", "usd": "1"}, "x", cost=None), "EvaluatorUnavailable", NO_COST),
        # A pattern RE2 refuses (the API refuses it too; a stored one fails here, never echoed).
        (score({"kind": "regex", "pattern": "(unclosed"}, "x"), "EvaluatorUnavailable", REGEX_INVALID),
    ]
    for outcome, error_type, message in cases:
        [failure] = outcome.evaluator_failures
        assert outcome.assertions == {} and not outcome.passed()
        assert failure["error"] == message and failure["error_type"] == error_type
        assert "unclosed" not in json.dumps(failure)  # never exception text
    # Evaluators that compare with the expected output are skipped without one (pydantic_evals).
    for spec in ({"kind": "equals_expected"}, {"kind": "contains"}):
        skipped = score(spec, "x")
        assert skipped.assertions == {} and skipped.evaluator_failures == [] and skipped.passed()
    assert EVALUATOR_RAISED  # still the message of an evaluator that raises unexpectedly


def test_non_string_and_structured_outputs():
    assert score({"kind": "regex", "pattern": "42"}, 42).assertions["regex"]["value"] is True
    assert score({"kind": "regex", "pattern": r'"n":1'}, {"n": 1}).assertions["regex"]["value"] is True  # canonical JSON
    assert score({"kind": "regex", "pattern": r"^\[1,2\]$"}, [1, 2]).assertions["regex"]["value"] is True
    assert score({"kind": "equals_expected"}, 42, expected=42).passed()
    assert not score({"kind": "equals_expected"}, True, expected=1).passed()  # a boolean is not a number
    assert score({"kind": "contains", "value": 4}, [4, 5]).passed()
    assert score({"kind": "field_equals", "path": "a.0", "value": 1}, {"a": [1]}).passed()
    assert not score({"kind": "field_equals", "path": "a", "value": 1}, "not json").passed()
    assert not score({"kind": "field_equals", "path": "a", "value": 1}, 5).passed()
    assert score({"kind": "schema_valid"}, {"n": 3}, schema=SCHEMA).passed()  # default: the agent's schema
    assert score({"kind": "schema_valid"}, '{"n": 3}', schema=SCHEMA).passed()  # JSON text parsed first
    assert not score({"kind": "schema_valid", "json_schema": {"type": "object"}}, 5).passed()


# --------------------------------------------------------------------------- runtime


@pytest.fixture
def defs(worker, tenant):
    yield
    with worker.engine.begin() as conn:  # agents reference knowledge bases in specs only; clear KBs too
        conn.execute(text("DELETE FROM knowledge_bases WHERE tenant_id = :t"), {"t": tenant})
    cleanup_evals(worker.engine, tenant)
    cleanup_definitions(worker.engine, tenant)


def run_eval(worker, client, tenant, spec, cases, evaluators, **kwargs):
    agent_id = publish_agent(worker.engine, tenant, spec)
    suite_id = create_suite(worker.engine, tenant, agent_id, cases, evaluators)
    run_id = start_eval(worker.engine, worker.run_store, client, tenant_id=tenant, agent_id=agent_id,
                        suite_id=suite_id, app_version=APP_VERSION, **kwargs)
    return agent_id, suite_id, run_id, wait_terminal(worker.run_store, tenant, run_id, timeout=120)


def test_max_cost_reads_the_childs_priced_usage_and_excludes_the_judge(worker, client, tenant, defs):
    evaluators = [{"kind": "max_cost", "usd": "0.025", "name": "within"}, {"kind": "max_cost", "usd": "0.01", "name": "over"},
                  {"kind": "llm_judge", "rubric": "MUST_CONTAIN:Echo", "model": JUDGE}]
    _, _, run_id, run = run_eval(worker, client, tenant, agent_spec("priced"), [{"name": "p", "input": "hi"}], evaluators)
    assert run["status"] == "completed", run
    (row,) = results(worker.engine, tenant, run_id)
    child_cost = worker.run_store.get_run(tenant, row.child_run_id)["cost_usd"]
    assert child_cost == Decimal("0.02")  # test:priced: 1 request
    assert row.assertions["within"]["value"] is True and row.assertions["over"]["value"] is False
    assert row.status == "failed" and row.cost_usd == child_cost + Decimal("0.0004")  # judge counted in the total only
    assert run["cost_usd"] == row.cost_usd
    with worker.engine.connect() as conn:
        ops = dict(conn.execute(text("SELECT request_key, operation FROM run_model_requests WHERE run_id = :r"),
                                {"r": str(run_id)}).all())
    assert ops == {f"judge:{row.case_id}:2": "chat"}  # plan: column chat, `judge:` key prefix (metrics `judge` is AC.17)


def test_judge_pass_fail_threshold_and_invalid_verdict_failures_are_not_crashes(worker, client, tenant, defs):
    evaluators = [
        {"kind": "llm_judge", "rubric": "MUST_CONTAIN:Echo", "model": JUDGE, "name": "passes"},
        {"kind": "llm_judge", "rubric": "MUST_CONTAIN:zzz", "model": JUDGE, "name": "fails"},
        {"kind": "llm_judge", "rubric": "MUST_CONTAIN:Echo", "model": JUDGE, "name": "thresh", "pass_threshold": 0.5},
        {"kind": "llm_judge", "rubric": "no marker here", "model": JUDGE, "name": "nomarker"},
        {"kind": "llm_judge", "rubric": "MUST_CONTAIN:x", "model": {"provider": "openai", "name": "gpt-4o"}, "name": "denied"},
    ]
    _, _, run_id, run = run_eval(worker, client, tenant, agent_spec("echo"), [{"name": "j", "input": "hi"}], evaluators)
    assert run["status"] == "completed", run  # judge failures never fail the run
    (row,) = results(worker.engine, tenant, run_id)
    assert {k: v["value"] for k, v in row.assertions.items()} == {
        "passes": True, "fails": False, "thresh": True, "nomarker": False}
    assert [f["name"] for f in row.evaluator_failures] == ["denied"]
    assert row.evaluator_failures[0]["error"] == "the judge request failed"
    assert row.status == "failed"
    # Priced `judge:` usage rows for the four verdicts; none for the refused model.
    assert usage_keys(worker.engine, run_id) == sorted(f"judge:{row.case_id}:{i}" for i in range(4))
    with worker.engine.connect() as conn:
        rows = conn.execute(text("SELECT operation, cost_usd FROM run_model_requests WHERE run_id = :r"),
                            {"r": str(run_id)}).all()
    assert {r[0] for r in rows} == {"chat"} and all(r[1] == Decimal("0.0004") for r in rows)


def test_snapshot_hash_mismatch_fails_cleanly_with_no_children_or_usage(worker, client, tenant, defs):
    agent_id = publish_agent(worker.engine, tenant, agent_spec("echo"))
    suite_id = create_suite(worker.engine, tenant, agent_id, [{"name": "a", "input": "one"}],
                            [{"kind": "llm_judge", "rubric": "MUST_CONTAIN:one", "model": JUDGE}])
    snap = EvalStore(worker.engine).snapshot(tenant, suite_id, agent_id=agent_id)
    evaluators = list(snap.evaluators)
    tampered = snap.model_copy(update={"evaluators": tuple(e.model_copy(update={"rubric": "MUST_CONTAIN:no"}) for e in evaluators)})
    run_id = start_eval(worker.engine, worker.run_store, client, tenant_id=tenant, agent_id=agent_id, suite_id=suite_id,
                        app_version=APP_VERSION, input_json=tampered.canonical_json())
    run = wait_terminal(worker.run_store, tenant, run_id, timeout=60)
    assert run["status"] == "failed" and run["output"] is None
    end = event_rows(worker.engine, run_id)[-1]
    assert end[2] == "run_failed" and end[3]["error_code"] == "eval_suite_mismatch"
    assert results(worker.engine, tenant, run_id) == [] and child_runs(worker.engine, run_id) == []
    assert usage_keys(worker.engine, run_id) == []
    assert "rubric" not in json.dumps(end[3])  # no snapshot content in the failure


def test_garbage_input_json_fails_as_an_invalid_suite(worker, client, tenant, defs):
    agent_id = publish_agent(worker.engine, tenant, agent_spec("echo"))
    suite_id = create_suite(worker.engine, tenant, agent_id, [{"name": "a", "input": "one"}], [])
    run_id = start_eval(worker.engine, worker.run_store, client, tenant_id=tenant, agent_id=agent_id,
                        suite_id=suite_id, app_version=APP_VERSION, input_json='{"not": "a snapshot"}')
    assert wait_terminal(worker.run_store, tenant, run_id, timeout=60)["status"] == "failed"
    end = event_rows(worker.engine, run_id)[-1]
    assert end[2] == "run_failed" and end[3]["error_code"] == "eval_suite_invalid"
    assert child_runs(worker.engine, run_id) == []


def test_missing_agent_version_fails_eval_agent_not_found(worker, client, tenant, defs):
    agent_id = publish_agent(worker.engine, tenant, agent_spec("echo"))
    suite_id = create_suite(worker.engine, tenant, agent_id, [{"name": "a", "input": "one"}], [])
    run_id = start_eval(worker.engine, worker.run_store, client, tenant_id=tenant, agent_id=agent_id,
                        suite_id=suite_id, app_version=APP_VERSION, version=9)  # only version 1 exists
    assert wait_terminal(worker.run_store, tenant, run_id, timeout=60)["status"] == "failed"
    end = event_rows(worker.engine, run_id)[-1]
    assert end[2] == "run_failed" and end[3]["error_code"] == "eval_agent_not_found"
    assert child_runs(worker.engine, run_id) == [] and results(worker.engine, tenant, run_id) == []


MEMORY_STEPS = {"eap.load_memory", "eap.memory_recall", "eap.memory_index", "eap.memory_observe_run", "eap.load_history"}


def tenant_counts(engine, tenant: str) -> dict[str, int]:
    with engine.connect() as conn:
        return {t: conn.execute(text(f"SELECT count(*) FROM {t} WHERE tenant_id = :t"), {"t": tenant}).scalar_one()
                for t in ("working_memories", "message_embeddings", "memory_observations")}


def test_memory_agent_case_children_use_no_memory(worker, client, worker_settings, tenant, defs):
    from worker_tests.test_observational_memory import obs_spec

    spec = obs_spec(model="echo", working={"scope": "resource", "template": "Notes: {{x}}"})
    before = tenant_counts(worker.engine, tenant)
    cases = [{"name": f"m{i}", "input": f"hello {i}"} for i in range(2)]
    _, _, run_id, run = run_eval(worker, client, tenant, spec, cases, [{"kind": "contains", "value": "Echo:"}])
    assert run["status"] == "completed", run
    rows = results(worker.engine, tenant, run_id)
    assert [r.status for r in rows] == ["passed", "passed"]
    for r in rows:
        steps = step_names(worker_settings.dbos_system_schema, r.child_run_id)
        assert steps and not MEMORY_STEPS & set(steps), steps
    assert all(c.conversation_id is None for c in child_runs(worker.engine, run_id))
    assert tenant_counts(worker.engine, tenant) == before
    with worker.engine.connect() as conn:  # no observer child workflow was started for the eval or its children
        parents = conn.execute(text("SELECT count(*) FROM runs WHERE tenant_id = :t AND parent_run_id IS NOT NULL"),
                               {"t": tenant}).scalar_one()
    assert parents == 2


def test_knowledge_agent_case_retrieves_and_its_embedding_cost_is_in_the_case(worker, client, tenant, defs):
    from worker_tests.test_retrieve import PRINTER, ingest, make_kb, rag_spec

    kb = make_kb(worker, tenant)
    ingest(worker, tenant, kb, "printer.md", PRINTER)
    cases = [{"name": "jam", "input": "How do I fix paper jams in tray two?"}]
    _, _, run_id, run = run_eval(worker, client, tenant, rag_spec({"id": kb["id"]}), cases,
                                 [{"kind": "contains", "value": "Paper jams"}])
    assert run["status"] == "completed", run
    (row,) = results(worker.engine, tenant, run_id)
    assert row.status == "passed", (row.output, row.assertions)
    events = event_rows(worker.engine, row.child_run_id)
    assert [e for e in events if e[2] == "tool_call" and e[3]["tool_name"] == "retrieve"]
    [embedding] = [e[3] for e in events if e[2] == "embedding"]
    assert embedding["model_id"] == "test:hash"
    child = worker.run_store.get_run(tenant, row.child_run_id)
    with worker.engine.connect() as conn:
        emb = conn.execute(text("SELECT cost_usd FROM run_model_requests WHERE run_id = :r AND operation = 'embedding'"),
                           {"r": str(row.child_run_id)}).scalar_one()
    assert emb is not None and emb > 0 and child["cost_usd"] >= emb
    assert row.cost_usd == child["cost_usd"]  # no judge: the case cost is the child's, embeddings included


def test_gated_tool_in_a_child_is_denied_not_waited_on(worker, client, worker_settings, tenant, defs):
    from worker_tests.test_tool_approval import TICKET_ARGS, gated_spec

    cases = [{"name": "t", "input": json.dumps({"tool": "tickets.create", "args": TICKET_ARGS})}]
    _, _, run_id, run = run_eval(worker, client, tenant, gated_spec(), cases, [{"kind": "contains", "value": "outcome=denied"}])
    assert run["status"] == "completed", run
    (row,) = results(worker.engine, tenant, run_id)
    assert row.status == "passed"
    events = event_rows(worker.engine, row.child_run_id)
    types = [e[2] for e in events]
    assert "tool_approval_waiting" not in types  # nothing was waited on
    assert [e[3]["status"] for e in events if e[2] == "tool_result"] == ["denied"]
    steps = step_names(worker_settings.dbos_system_schema, row.child_run_id)
    assert not any("recv" in s for s in steps)


@pytest.mark.parametrize("size,batches", [(1, 5), (2, 3), (4, 2), (5, 1)])
def test_max_concurrency_batches_and_results_keep_case_order(worker, client, worker_settings, tenant, defs, size, batches):
    cases = [{"name": f"c{i}", "input": f"w{i}", "expected_output": f"Echo: w{i}"} for i in range(5)]
    _, _, run_id, run = run_eval(worker, client, tenant, agent_spec("echo"), cases, [{"kind": "equals_expected"}],
                                 max_concurrency=size)
    assert run["status"] == "completed", run
    steps = step_names(worker_settings.dbos_system_schema, run_id)
    assert steps.count("eap.eval_create_children") == batches  # a batch's children exist before the next batch's
    rows = results(worker.engine, tenant, run_id)
    assert [(r.position, r.case_name, r.status) for r in rows] == [(i, f"c{i}", "passed") for i in range(5)]
    case_events = [e for e in event_rows(worker.engine, run_id) if e[2] == "eval_case_result"]
    assert [e[1] for e in case_events] == [f"case:{r.case_id}" for r in rows]
    assert event_rows(worker.engine, run_id)[0][3]["max_concurrency"] == size
    assert len(child_runs(worker.engine, run_id)) == 5
    # At most `size` children at once: a batch's child rows are only created after every child of
    # the previous batch finished, and no more than `size` child runs ever overlap in time.
    timing = child_timing(worker.engine, run_id, rows)
    for b in range(1, batches):
        previous = [t for t in timing if t["position"] // size == b - 1]
        current = [t for t in timing if t["position"] // size == b]
        assert min(t["created_at"] for t in current) >= max(t["finished_at"] for t in previous)
    assert max_overlap(timing) <= size


def child_timing(engine, run_id, rows) -> list[dict]:
    position = {f"case:{r.case_id}": r.position for r in rows}
    with engine.connect() as conn:
        found = conn.execute(
            text("SELECT parent_step_id, created_at, started_at, finished_at FROM runs WHERE parent_run_id = :r"),
            {"r": str(run_id)},
        ).fetchall()
    return [{"position": position[f.parent_step_id], "created_at": f.created_at, "started_at": f.started_at,
             "finished_at": f.finished_at} for f in found]


def max_overlap(timing: list[dict]) -> int:
    """The largest number of child runs running at one instant ([started_at, finished_at])."""
    points = sorted([(t["started_at"], 1) for t in timing] + [(t["finished_at"], -1) for t in timing],
                    key=lambda p: (p[0], p[1]))  # an end before a start at the same instant
    running = peak = 0
    for _, delta in points:
        running += delta
        peak = max(peak, running)
    return peak


def test_six_case_suite_of_structured_and_echo_agents_with_aggregates(worker, client, tenant, defs):
    structured = {**agent_spec("structured"), "output": {"json_schema": {
        "type": "object", "properties": {"label": {"type": "string", "enum": ["bug", "question"]}},
        "required": ["label"], "additionalProperties": False}}}
    cases = [{"name": f"s{i}", "input": json.dumps({"output": {"label": "bug" if i % 3 else "question"}})} for i in range(6)]
    evaluators = [{"kind": "schema_valid"}, {"kind": "field_equals", "path": "label", "value": "bug"}]
    _, _, run_id, run = run_eval(worker, client, tenant, structured, cases, evaluators, max_concurrency=4)
    assert run["status"] == "completed", run
    rows = results(worker.engine, tenant, run_id)
    assert [r.status for r in rows] == ["failed", "passed", "passed"] * 2
    assert all(r.assertions["schema_valid"]["value"] is True for r in rows)
    agg = json.loads(run["output"])
    assert (agg["cases"], agg["passed"], agg["failed"], agg["errors"], agg["pass_rate"]) == (6, 4, 2, 0, pytest.approx(4 / 6))


def test_eval_run_created_before_ac11_now_runs(worker, client, tenant, defs):
    """AC.12 created the run row (input = the snapshot, eval_suite_hash) with no worker to run it; the
    stored row is then launched exactly as the API's launcher does: eap.eval_run(run_id, tenant, runs.input)."""
    agent_id = publish_agent(worker.engine, tenant, agent_spec("echo"))
    suite_id = create_suite(worker.engine, tenant, agent_id, [{"name": "old", "input": "x", "expected_output": "Echo: x"}],
                            [{"kind": "equals_expected"}])
    snapshot = EvalStore(worker.engine).snapshot(tenant, suite_id, agent_id=agent_id).canonical_json()
    run_id = uuid.uuid4()
    worker.run_store.create_eval_run(tenant, agent_id, 1, suite_id, snapshot, run_id)
    stored = worker.run_store.get_run(tenant, run_id)
    assert stored["status"] in {"queued", "pending", "running"} and results(worker.engine, tenant, run_id) == []
    client.enqueue({"queue_name": "eap-agent-runs", "workflow_name": "eap.eval_run", "workflow_id": str(run_id),
                    "app_version": APP_VERSION}, str(run_id), tenant, stored["input"])
    run = wait_terminal(worker.run_store, tenant, run_id, timeout=120)
    assert run["status"] == "completed", run
    (row,) = results(worker.engine, tenant, run_id)
    assert (row.case_name, row.status) == ("old", "passed")
    assert json.loads(run["output"])["passed"] == 1
