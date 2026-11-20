"""The Architect's test-suite tools: create a suite, add cases, attach scorers, start a run, read it.

The logic is eap_run_store's (EvalStore, RunStore); these tests check the wiring over a real Postgres, the
refusals the model can fix, the gating and the approval wording. The enqueue is replaced (no DBOS here).
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from sqlalchemy import create_engine, text

from eap_runtime import ToolRegistry
from eap_schemas.architect import ArchitectWorkflowSpec
from eap_worker import architect_evals
from eap_worker.architect_tools import (
    GATED_TOOL_IDS,
    TOOL_IDS,
    approval_summary_text,
    publish_workflow,
    register_architect_tools,
    save_workflow,
)
from worker_tests.conftest import APP_DB_URL
from worker_tests.test_architect_tools import allowlist

EVAL_GATED = (
    "architect.create_eval_suite", "architect.add_eval_case", "architect.attach_scorer", "architect.start_eval_run",
)
EVAL_READS = ("architect.list_eval_suites", "architect.get_eval_run")


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def engine():
    eng = create_engine(APP_DB_URL)
    yield eng
    eng.dispose()


@pytest.fixture
def tenant(engine):
    tenant_id = f"t_{uuid.uuid4().hex[:10]}"
    yield tenant_id
    with engine.begin() as conn:
        for table in ("eval_case_results", "runs", "eval_cases", "eval_suites", "workflow_versions", "workflows"):
            conn.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tenant_id})


@pytest.fixture
def enqueued(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(architect_evals, "_enqueue", lambda *a: calls.append(a))
    return calls


def published_workflow(engine, tenant, slug="daily", name="Daily note"):
    spec = ArchitectWorkflowSpec(
        name=name, id=slug, input_schema={"type": "object", "properties": {"dry": {"type": "boolean"}}, "required": ["dry"]},
        steps=[{"kind": "tool", "id": "stamp", "tool": "clock.now"}],
    )
    assert run(save_workflow(engine, tenant, spec, allowlist=allowlist()))["status"] == "draft"
    assert run(publish_workflow(engine, tenant, slug))["status"] == "published"


def make_suite(engine, tenant, name="Dry runs"):
    out = architect_evals.create_eval_suite_sync(engine, tenant, "workflow", "daily", name)
    assert out["status"] == "created", out
    return out["id"]


# ------------------------------------------------------------------------------------ gating


def test_the_four_writes_are_gated_and_the_two_reads_are_not():
    registry = ToolRegistry()
    register_architect_tools(registry)
    assert set(TOOL_IDS) == registry.tool_ids() and set(EVAL_GATED) <= set(GATED_TOOL_IDS)
    for tool_id in EVAL_GATED:
        assert registry.summary_for(tool_id, {"target": "workflow", "id": "x", "name": "n", "suite_id": "s", "scorer": {}})
        assert registry.get_metadata(tool_id)["side_effect"] is True
    for tool_id in EVAL_READS:
        assert tool_id in TOOL_IDS and tool_id not in GATED_TOOL_IDS
        assert registry.summary_for(tool_id, {}) is None
        assert registry.get_metadata(tool_id)["side_effect"] is False


# ------------------------------------------------------------------------------------- suites


def test_create_and_list_a_suite(engine, tenant):
    published_workflow(engine, tenant)
    suite_id = make_suite(engine, tenant)
    listed = architect_evals.list_eval_suites_sync(engine, tenant, "workflow", "daily")
    assert listed["name"] == "Daily note" and [s["id"] for s in listed["suites"]] == [suite_id]
    assert listed["suites"][0]["case_count"] == 0 and listed["suites"][0]["scorers"] == []
    taken = architect_evals.create_eval_suite_sync(engine, tenant, "workflow", "daily", "Dry runs")
    assert taken["error"]["code"] == "suite_name_taken"


def test_suite_refusals_unknown_wrong_kind_and_other_tenant(engine, tenant):
    published_workflow(engine, tenant)
    gone = architect_evals.create_eval_suite_sync(engine, tenant, "workflow", "nope", "x")
    assert gone["error"]["code"] == "not_found"
    wrong = architect_evals.create_eval_suite_sync(engine, tenant, "agent", "daily", "x")
    assert wrong["error"]["code"] == "not_found" and "target 'workflow'" in wrong["error"]["message"]
    assert architect_evals.create_eval_suite_sync(engine, tenant, "tool", "daily", "x")["error"]["code"] == "invalid_target"
    assert architect_evals.create_eval_suite_sync(engine, tenant, "workflow", "daily", " ")["error"]["code"] == "missing_field"
    other = architect_evals.list_eval_suites_sync(engine, f"t_{uuid.uuid4().hex[:8]}", "workflow", "daily")
    assert other["error"]["code"] == "not_found"


# -------------------------------------------------------------------------------------- cases


def test_add_cases_in_a_batch_and_refuse_bad_ones(engine, tenant):
    published_workflow(engine, tenant)
    sid = make_suite(engine, tenant)
    one = architect_evals.add_eval_case_sync(engine, tenant, sid, name="dry", inputs={"dry": True}, metadata={"slot": "am"})
    assert one["added"] == ["dry"] and one["case_count"] == 1
    batch = architect_evals.add_eval_case_sync(
        engine, tenant, sid, cases=[{"name": "a", "inputs": {"dry": True}}, {"name": "b", "inputs": {"dry": False}, "expected": {"x": 1}}])
    assert batch["added"] == ["a", "b"] and batch["case_count"] == 3
    dup = architect_evals.add_eval_case_sync(engine, tenant, sid, cases=[{"name": "c", "inputs": {"dry": True}}, {"name": "a", "inputs": {"dry": True}}])
    assert dup["error"]["code"] == "case_name_taken"
    bad = architect_evals.add_eval_case_sync(engine, tenant, sid, cases=[{"name": "c", "inputs": {"dry": True}}, {"name": "d", "inputs": {}}])
    assert bad["error"]["code"] == "input_schema_errors" and bad["error"]["case"] == "d"
    assert architect_evals.add_eval_case_sync(engine, tenant, sid, name="e", inputs="text")["error"]["code"] == "invalid_case"
    assert architect_evals.add_eval_case_sync(engine, tenant, sid, cases=[])["error"]["code"] == "invalid_case"
    assert architect_evals.add_eval_case_sync(engine, tenant, str(uuid.uuid4()), name="e", inputs={"dry": True})["error"]["code"] == "not_found"
    assert architect_evals.add_eval_case_sync(engine, tenant, "not-a-uuid", name="e", inputs={"dry": True})["error"]["code"] == "not_found"
    # a refused batch adds nothing ("c" was valid but "d" was not)
    assert architect_evals.list_eval_suites_sync(engine, tenant, "workflow", "daily")["suites"][0]["case_count"] == 3


# ------------------------------------------------------------------------------------ scorers


def test_attach_scorers_validated_with_the_shared_schemas(engine, tenant):
    published_workflow(engine, tenant)
    sid = make_suite(engine, tenant)
    for scorer in (
        {"kind": "schema_valid", "json_schema": {"type": "object", "properties": {"status": {"enum": ["posted", "dry_run"]}}}},
        {"kind": "regex", "pattern": "https?://", "negate": True, "field": "text", "name": "no-urls"},
        {"kind": "max_duration", "seconds": 120},
    ):
        out = architect_evals.attach_scorer_sync(engine, tenant, sid, scorer)
        assert out["status"] == "attached", out
    assert [s["kind"] for s in out["scorers"]] == ["schema_valid", "regex", "max_duration"]
    as_text = architect_evals.attach_scorer_sync(engine, tenant, sid, json.dumps({"kind": "contains", "value": "ok"}))
    assert as_text["status"] == "attached"
    for bad in ({"kind": "sentiment"}, {"kind": "regex"}, {"kind": "regex", "pattern": "(?=x)"},
                {"kind": "max_duration", "seconds": -1}, {"kind": "regex", "pattern": "a", "extra": 1}, "nonsense", 5):
        refused = architect_evals.attach_scorer_sync(engine, tenant, sid, bad)
        assert refused["error"]["code"].startswith("invalid_scorer"), bad
    assert architect_evals.attach_scorer_sync(engine, tenant, str(uuid.uuid4()), {"kind": "equals_expected"})["error"]["code"] == "not_found"
    assert len(architect_evals.list_eval_suites_sync(engine, tenant, "workflow", "daily")["suites"][0]["scorers"]) == 4


# ---------------------------------------------------------------------------------------- runs


def test_start_and_read_a_run(engine, tenant, enqueued):
    published_workflow(engine, tenant)
    sid = make_suite(engine, tenant)
    empty = architect_evals.start_eval_run_sync(engine, tenant, sid)
    assert empty["error"]["code"] == "invalid_suite" and "add_eval_case" in empty["error"]["message"]
    architect_evals.add_eval_case_sync(engine, tenant, sid, cases=[{"name": "a", "inputs": {"dry": True}}, {"name": "b", "inputs": {"dry": True}}])
    assert architect_evals.start_eval_run_sync(engine, tenant, sid, version=9)["error"]["code"] == "not_published"
    assert architect_evals.start_eval_run_sync(engine, tenant, sid, version=0)["error"]["code"] == "invalid_version"
    started = architect_evals.start_eval_run_sync(engine, tenant, sid, run_id="architect-run")
    assert started["status"] == "queued" and started["cases"] == 2 and started["version"] == 1
    (call,) = enqueued
    assert str(call[0]) == started["run_id"] and call[1] == tenant and json.loads(call[2])["suite_id"] == sid
    got = architect_evals.get_eval_run_sync(engine, tenant, started["run_id"])
    assert got["status"] == "queued" and got["suite_id"] == sid and got["version"] == 1
    assert got["aggregates"]["cases"] == 0
    assert got["results"] == []
    assert architect_evals.get_eval_run_sync(engine, tenant, str(uuid.uuid4()))["error"]["code"] == "not_found"
    assert architect_evals.get_eval_run_sync(engine, f"t_{uuid.uuid4().hex[:8]}", started["run_id"])["error"]["code"] == "not_found"


def test_a_failed_enqueue_is_an_error_not_a_crash(engine, tenant, monkeypatch):
    published_workflow(engine, tenant)
    sid = make_suite(engine, tenant)
    architect_evals.add_eval_case_sync(engine, tenant, sid, name="a", inputs={"dry": True})

    def boom(*_a):
        raise RuntimeError("queue down")

    monkeypatch.setattr(architect_evals, "_enqueue", boom)
    out = architect_evals.start_eval_run_sync(engine, tenant, sid)
    assert out["error"]["code"] == "enqueue_failed"


def test_an_unpublished_workflow_cannot_be_run(engine, tenant, enqueued):
    spec = ArchitectWorkflowSpec(name="Draft", id="daily", steps=[{"kind": "tool", "id": "s", "tool": "clock.now"}])
    run(save_workflow(engine, tenant, spec, allowlist=allowlist()))
    sid = make_suite(engine, tenant)
    architect_evals.add_eval_case_sync(engine, tenant, sid, name="a", inputs={"input": "x"})
    assert architect_evals.start_eval_run_sync(engine, tenant, sid)["error"]["code"] == "not_published"
    assert enqueued == []


# ----------------------------------------------------------------------------- approval summaries


def test_approval_summaries_read_in_the_users_words(engine, tenant):
    published_workflow(engine, tenant)
    sid = make_suite(engine, tenant)
    create = approval_summary_text(engine, tenant, "architect.create_eval_suite", {"target": "workflow", "id": "daily", "name": "Dry runs 2"})
    assert create == "Create test suite 'Dry runs 2' for workflow 'Daily note'."
    cases = approval_summary_text(
        engine, tenant, "architect.add_eval_case", {"suite_id": sid, "cases": [{"name": "a", "inputs": {}}, {"name": "b", "inputs": {}}]})
    assert cases == "Add 2 test cases to suite 'Dry runs' (workflow 'Daily note'): 'a', 'b'."
    scorer = approval_summary_text(
        engine, tenant, "architect.attach_scorer", {"suite_id": sid, "scorer": {"kind": "regex", "pattern": "https?://", "negate": True, "field": "text"}})
    assert scorer == (
        "Add a regex check (forbids /https?:/// in text) to suite 'Dry runs' (workflow 'Daily note'): "
        "it scores every case's output.")
    architect_evals.add_eval_case_sync(engine, tenant, sid, name="a", inputs={"dry": True})
    start = approval_summary_text(engine, tenant, "architect.start_eval_run", {"suite_id": sid})
    assert "'Dry runs'" in start and "latest published version" in start and "1 case, 0 scorers" in start and "model calls" in start
    missing = approval_summary_text(engine, tenant, "architect.start_eval_run", {"suite_id": str(uuid.uuid4())})
    assert "It would be refused" in missing
    refused = approval_summary_text(engine, tenant, "architect.create_eval_suite", {"target": "workflow", "id": "nope", "name": "x"})
    assert "It would be refused" in refused


def test_an_agent_suite_runs_a_published_agent_version(engine, tenant, enqueued):
    from eap_worker.architect_tools import publish_agent, save_agent
    from worker_tests.test_architect_tools import agent_spec

    run(save_agent(engine, tenant, agent_spec(id="helper"), allowlist=allowlist()))
    try:
        made = architect_evals.create_eval_suite_sync(engine, tenant, "agent", "helper", "Smoke")
        assert made["status"] == "created" and made["target"] == "agent"
        sid = made["id"]
        assert architect_evals.add_eval_case_sync(engine, tenant, sid, name="x", inputs={"dry": 1})["error"]["code"] == "invalid_case"
        architect_evals.add_eval_case_sync(engine, tenant, sid, name="hi", inputs={"input": "hello"})
        with engine.connect() as conn:  # an agent case is sent as plain text
            assert conn.execute(text("SELECT input FROM eval_cases WHERE suite_id = :s"), {"s": sid}).scalar() == "hello"
        assert architect_evals.start_eval_run_sync(engine, tenant, sid)["error"]["code"] == "not_published"
        assert run(publish_agent(engine, tenant, "helper"))["status"] == "published"
        started = architect_evals.start_eval_run_sync(engine, tenant, sid)
        assert started["status"] == "queued" and started["version"] == 1 and len(enqueued) == 1
    finally:
        with engine.begin() as conn:
            for table in ("eval_case_results", "runs", "eval_cases", "eval_suites", "agent_versions", "agents"):
                conn.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tenant})
