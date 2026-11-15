"""AC.18 forged rows in the worker's queries (agent capabilities milestone).

Rows written straight to the database, bypassing the API's tenant checks, must never make the worker
act on another tenant's data:

1. an eval run row of tenant A that names tenant B's agent (and B's published version): the eval
   workflow's `eap.start_eval_run` reads the version with A's tenant, finds nothing, and fails the
   run `eval_agent_not_found` with no child run, no judge call and no usage row;
2. a tool decision recorded by tenant B for tenant A's waiting call: refused (`not_waiting`, no
   event, no inbox change), and even a wake-up message sent for it carries no decision: A's run
   keeps waiting until A's own decision, then runs the tool once;
3. the DB itself refuses every cross-tenant attachment the milestone's tables could carry: an inbox
   row, an event, a usage row, a case result or an eval child run of tenant B on tenant A's run,
   and a case of B in A's suite (composite `(tenant_id, ...)` foreign keys).
"""

from __future__ import annotations

import json
import time
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from eap_run_store import RunNotFound
from eap_run_store.approvals import ApprovalStore, tool_approval_topic
from eap_run_store.evals import EvalStore
from worker_tests.conftest import APP_VERSION, cleanup_tenant, event_rows, wait_terminal
from worker_tests.eval_run_helpers import JUDGE, child_runs, cleanup_evals, create_suite, usage_keys
from worker_tests.test_tool_approval import TICKET_ARGS, decide, gated_spec, side_effects, start, wait_waiting
from worker_tests.workflow_run_helpers import agent_spec, cleanup_definitions, publish_agent


@pytest.fixture
def other(worker):
    tenant_id = f"t_{uuid.uuid4().hex[:10]}"
    yield tenant_id
    cleanup_evals(worker.engine, tenant_id)
    cleanup_tenant(worker.engine, tenant_id)
    cleanup_definitions(worker.engine, tenant_id)


@pytest.fixture
def defs(worker, tenant):
    yield
    cleanup_evals(worker.engine, tenant)
    cleanup_definitions(worker.engine, tenant)


def test_an_eval_run_naming_another_tenants_agent_runs_nothing(worker, client, tenant, other, defs):
    mine = publish_agent(worker.engine, tenant, agent_spec("echo"))
    theirs = publish_agent(worker.engine, other, agent_spec("ticket", tools=("tickets.create",), name="Theirs"))
    suite_id = create_suite(worker.engine, tenant, mine, [{"name": "c0", "input": "hi"}],
                            [{"kind": "llm_judge", "rubric": "MUST_CONTAIN:hi", "model": JUDGE}])
    snapshot = EvalStore(worker.engine).snapshot(tenant, suite_id, agent_id=mine, max_concurrency=1)
    run_id = uuid.uuid4()
    worker.run_store.create_eval_run(tenant, mine, 1, suite_id, snapshot.canonical_json(), run_id)
    with worker.engine.begin() as conn:  # the forged row: tenant A's eval run pointing at B's agent
        conn.execute(text("UPDATE runs SET agent_id = :a WHERE id = :r AND tenant_id = :t"),
                     {"a": theirs, "r": str(run_id), "t": tenant})
    client.enqueue({"queue_name": "eap-agent-runs", "workflow_name": "eap.eval_run", "workflow_id": str(run_id),
                    "app_version": APP_VERSION}, str(run_id), tenant, snapshot.canonical_json())
    run = wait_terminal(worker.run_store, tenant, run_id, timeout=60)
    assert run["status"] == "failed", run
    end = {k: d for _s, k, _t, d in event_rows(worker.engine, run_id)}["end"]
    assert end["error_code"] == "eval_agent_not_found", end
    assert child_runs(worker.engine, run_id) == [] and usage_keys(worker.engine, run_id) == []
    with worker.engine.connect() as conn:
        for t in (tenant, other):
            assert conn.execute(text("SELECT count(*) FROM fake_external_ticket_calls WHERE tenant_id = :t"),
                                {"t": t}).scalar() == 0
        assert conn.execute(text("SELECT count(*) FROM runs WHERE tenant_id = :t"), {"t": other}).scalar() == 0


def test_another_tenants_decision_never_reaches_a_waiting_call(worker, client, tenant, other):
    run_id = start(worker, client, tenant, gated_spec(), json.dumps({"tool": "tickets.create", "args": TICKET_ARGS}))
    (waiting,) = wait_waiting(worker, run_id)
    call_id = waiting["tool_call_id"]
    events_before = event_rows(worker.engine, run_id)

    with pytest.raises(RunNotFound):
        worker.event_log.append_tool_decision(
            other, run_id, call_id, "tool_approved", {"tool_call_id": call_id, "decided_by": "mallory", "comment": None})
    # A wake-up for the forged decision: the wait re-reads its OWN tenant's decision events.
    client.send(str(run_id), {"tool_call_id": call_id, "decision": "approved"}, tool_approval_topic(str(run_id)))
    time.sleep(2.5)  # several poll rounds
    assert worker.run_store.get_run(tenant, run_id)["status"] == "running"
    assert event_rows(worker.engine, run_id) == events_before
    assert side_effects(worker, tenant) == []
    approvals = ApprovalStore(worker.engine)
    assert approvals.list(other, "pending")[0] == [] and approvals.get(other, str(run_id), "tool", call_id) is None
    assert approvals.get(tenant, str(run_id), "tool", call_id)["decision"] is None

    assert decide(worker, client, tenant, run_id, call_id) == "created"
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "completed" and "outcome=success" in run["output"], run
    assert worker.tickets.ticket_count(tenant, f"{run_id}:") == 1
    assert approvals.get(tenant, str(run_id), "tool", call_id)["decided_by"] == "alice"


# --------------------------------------------------------------------------- the schema refuses them

# Every foreign key of the milestone's tables (and of the rows its worker writes) names tenant_id with
# the referenced id, so a row of tenant B can never hang off tenant A's run or suite.
TENANT_SCOPED_FKS = {
    "pending_approvals": {("tenant_id", "run_id"), ("tenant_id", "root_run_id")},
    "eval_cases": {("tenant_id", "suite_id")},
    "eval_case_results": {("tenant_id", "run_id")},
}


def foreign_keys(engine, table: str) -> set[tuple[str, ...]]:
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT c.conname, array_agg(a.attname ORDER BY k.ord) FROM pg_constraint c"
            " CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord)"
            " JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum"
            " WHERE c.contype = 'f' AND c.conrelid = to_regclass(:t) GROUP BY c.conname"), {"t": table}).all()
    return {tuple(cols) for _name, cols in rows}


@pytest.mark.parametrize("table", sorted(TENANT_SCOPED_FKS))
def test_every_foreign_key_of_the_new_tables_is_tenant_scoped(worker, table):
    fks = foreign_keys(worker.engine, table)
    assert fks, f"{table} has no foreign key"
    assert all(cols[0] == "tenant_id" for cols in fks), fks
    assert TENANT_SCOPED_FKS[table] <= fks, fks


def test_rows_of_another_tenant_on_my_run_or_suite_are_refused(worker, client, tenant, other, defs):
    agent = publish_agent(worker.engine, tenant, agent_spec("echo"))
    suite_id = create_suite(worker.engine, tenant, agent, [{"name": "c0", "input": "hi"}], [])
    snapshot = EvalStore(worker.engine).snapshot(tenant, suite_id, agent_id=agent, max_concurrency=1)
    run_id = uuid.uuid4()
    worker.run_store.create_eval_run(tenant, agent, 1, suite_id, snapshot.canonical_json(), run_id)
    case_id = str(snapshot.cases[0].id)
    forged = {
        "inbox row": ("INSERT INTO pending_approvals (tenant_id, run_id, kind, approval_key, root_run_id, tool_name,"
                      " deadline) VALUES (:o, :r, 'tool', 'call_1', :r, 'tickets__create', now())"),
        "event": ("INSERT INTO execution_events (run_id, tenant_id, seq, event_key, type, data, ts)"
                  " VALUES (:r, :o, 99, 'forged', 'run_started', '{}', now())"),
        "usage row": ("INSERT INTO run_model_requests (run_id, tenant_id, request_key, model_id, input_tokens,"
                      " output_tokens, cache_read_tokens, cache_write_tokens, usage_measured, created_at)"
                      " VALUES (:r, :o, 'judge:x:1', 'test:judge', 1, 1, 0, 0, true, now())"),
        "case result": ("INSERT INTO eval_case_results (tenant_id, run_id, case_id, case_name, position, status)"
                        " VALUES (:o, :r, :c2, 'x', 9, 'passed')"),
        "case": "INSERT INTO eval_cases (id, tenant_id, suite_id, name, input, position) VALUES (:c2, :o, :s, 'x', 'x', 9)",
        "child run": ("INSERT INTO runs (id, tenant_id, kind, agent_id, agent_version, status, input, parent_run_id,"
                      " parent_step_id, created_at, updated_at)"
                      " VALUES (:c2, :o, 'agent', :a, 1, 'queued', 'x', :r, :step, now(), now())"),
    }
    from psycopg.errors import ForeignKeyViolation

    for what, sql in forged.items():
        params = {"r": str(run_id), "s": suite_id, "a": agent, "step": f"case:{case_id}"}
        # Control: the same row for the run's own tenant is valid (rolled back), so the refusal
        # below is the tenant-scoped foreign key, not a missing column or check.
        with worker.engine.connect() as conn:
            conn.execute(text(sql), {**params, "o": tenant, "c2": str(uuid.uuid4())})
            conn.rollback()
        with pytest.raises(IntegrityError) as caught, worker.engine.begin() as conn:
            conn.execute(text(sql), {**params, "o": other, "c2": str(uuid.uuid4())})
        assert isinstance(caught.value.orig, ForeignKeyViolation), (what, caught.value)
