"""AC.10: the eval store (0015_evals): suites and cases with caps, snapshot, idempotent case results
with judge usage and event in one transaction, aggregates, compare, and tenant isolation.
Implementer's focused tests; the acceptance suite adds more."""

from __future__ import annotations

import pickle
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text

from eap_run_store import (
    CaseResultWrite,
    EvalCaseLimitExceeded,
    EvalCaseNameConflict,
    EvalCaseNotFound,
    EvalRunNotFound,
    EvalStore,
    EvalSuiteArchived,
    EvalSuiteNameConflict,
    EvalSuiteNotFound,
    EventLog,
    RunNotFound,
    RunStore,
    aggregate_results,
    judge_request_key,
)
from eap_schemas.evals import EvalSuiteInvalid, EvalSuiteSnapshot

import eap_run_store.evals as evals_mod


@pytest.fixture
def tenant(engine):
    tenant_id = f"test_tenant_{uuid4().hex[:8]}"
    yield tenant_id
    with engine.begin() as conn:
        for sql in (
            "DELETE FROM eval_case_results WHERE tenant_id = :t",
            "DELETE FROM run_model_requests WHERE tenant_id = :t",
            "DELETE FROM execution_events WHERE tenant_id = :t",
            "DELETE FROM runs WHERE tenant_id = :t AND parent_run_id IS NOT NULL",
            "DELETE FROM runs WHERE tenant_id = :t",
            "DELETE FROM eval_suites WHERE tenant_id = :t",
        ):
            conn.execute(text(sql), {"t": tenant_id})


@pytest.fixture
def other(engine):
    tenant_id = f"test_tenant_{uuid4().hex[:8]}"
    yield tenant_id
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM eval_suites WHERE tenant_id = :t"), {"t": tenant_id})


@pytest.fixture
def store(engine):
    return EvalStore(engine)


def _usage(key: str, cost: str | None, model: str = "test:echo") -> dict:
    return {
        "request_key": key, "model_id": model, "provider_model_name": None, "input_tokens": 10,
        "output_tokens": 5, "cache_read_tokens": 0, "cache_write_tokens": 0, "usage_measured": True,
        "cost_usd": None if cost is None else Decimal(cost), "price_source": None if cost is None else "test",
    }


def _suite_with_cases(store, tenant, agent="ag", n=2, evaluators=({"kind": "equals_expected"},)):
    suite = store.create_suite(tenant, agent, f"suite-{uuid4().hex[:6]}", evaluators=evaluators)
    cases = [store.add_case(tenant, suite.id, {"name": f"c{i}", "input": f"in {i}", "expected_output": i}) for i in range(n)]
    return suite, cases


def _eval_run(engine, store, tenant, suite, agent="ag", version=1):
    snap = store.snapshot(tenant, suite.id)
    run_id = uuid4()
    RunStore(engine).create_eval_run(tenant, agent, version, suite.id, snap.canonical_json(), run_id)
    return str(run_id), snap


def _child(engine, tenant, parent, cost: str | None):
    child = uuid4()
    rs = RunStore(engine)
    rs.create_child_agent_run(tenant, "ag", 1, "in", child, parent, "case")
    EventLog(engine).append(tenant, child, "model:0:final", "model_message", {}, usage=_usage("model:0", cost))
    return str(child)


# ---------------------------------------------------------------------------- suites and cases


def test_suite_crud_conflicts_archive_and_isolation(store, tenant, other):
    suite = store.create_suite(tenant, "ag", "smoke", description="d", evaluators=[{"kind": "contains", "value": "x"}])
    assert suite.case_count == 0 and suite.evaluators[0].kind == "contains" and not suite.archived
    with pytest.raises(EvalSuiteNameConflict):
        store.create_suite(tenant, "ag", "smoke")
    store.create_suite(tenant, "ag2", "smoke")  # another agent may reuse the name
    store.create_suite(other, "ag", "smoke")  # another tenant too
    with pytest.raises(EvalSuiteInvalid):
        store.create_suite(tenant, "ag", "bad", evaluators=[{"kind": "nope"}])
    with pytest.raises(EvalSuiteInvalid):
        store.create_suite(tenant, "ag", "  ")

    assert [s.name for s in store.list_suites(tenant, "ag")] == ["smoke"]
    with pytest.raises(EvalSuiteNotFound):
        store.get_suite(other, suite.id)
    with pytest.raises(EvalSuiteNotFound):
        store.get_suite(tenant, suite.id, agent_id="ag2")
    with pytest.raises(EvalSuiteNotFound):
        store.update_suite(other, suite.id, name="x")
    with pytest.raises(EvalSuiteNotFound):
        store.delete_suite(other, suite.id)

    renamed = store.update_suite(tenant, suite.id, name="renamed", description=None)
    assert renamed.name == "renamed" and renamed.description is None
    archived = store.update_suite(tenant, suite.id, archived=True)
    assert archived.archived and store.list_suites(tenant, "ag") == []
    assert [s.id for s in store.list_suites(tenant, "ag", include_archived=True)] == [suite.id]
    with pytest.raises(EvalSuiteArchived):
        store.update_suite(tenant, suite.id, name="y")
    with pytest.raises(EvalSuiteArchived):
        store.add_case(tenant, suite.id, {"name": "c", "input": "x"})
    restored = store.update_suite(tenant, suite.id, archived=False)
    assert not restored.archived
    store.delete_suite(tenant, suite.id)
    with pytest.raises(EvalSuiteNotFound):
        store.get_suite(tenant, suite.id)


def test_case_crud_positions_names_and_isolation(store, tenant, other):
    suite = store.create_suite(tenant, "ag", "s", evaluators=[{"kind": "regex", "pattern": "a", "name": "r"}])
    a = store.add_case(tenant, suite.id, {"name": "a", "input": "1"})
    b = store.add_case(tenant, suite.id, {"name": "b", "input": "2", "expected_output": {"k": [1, None]}})
    assert (a.position, b.position) == (0, 1) and b.expected_output == {"k": [1, None]} and a.expected_output is None
    assert store.get_suite(tenant, suite.id).case_count == 2
    with pytest.raises(EvalCaseNameConflict):
        store.add_case(tenant, suite.id, {"name": "a", "input": "x"})
    # A case evaluator may not reuse a suite evaluator's name.
    with pytest.raises(EvalSuiteInvalid):
        store.add_case(tenant, suite.id, {"name": "c", "input": "x", "evaluators": [{"kind": "equals_expected", "name": "r"}]})
    with pytest.raises(EvalSuiteInvalid):
        store.add_case(tenant, suite.id, {"name": "c", "input": "x" * (32 * 1024 + 1)})
    with pytest.raises(EvalSuiteNotFound):
        store.add_case(other, suite.id, {"name": "c", "input": "x"})
    with pytest.raises(EvalCaseNotFound):
        store.get_case(other, a.id)
    with pytest.raises(EvalCaseNotFound):
        store.update_case(other, a.id, input="z")
    with pytest.raises(EvalCaseNotFound):
        store.delete_case(other, a.id)
    with pytest.raises(EvalSuiteNotFound):
        store.list_cases(other, suite.id)

    moved = store.update_case(tenant, a.id, input="changed", position=5)
    assert moved.input == "changed" and moved.position == 5 and moved.name == "a"
    assert [c.name for c in store.list_cases(tenant, suite.id)] == ["b", "a"]
    with pytest.raises(EvalCaseNameConflict):
        store.update_case(tenant, a.id, name="b")
    # Suite evaluators that clash with a case's are refused.
    store.update_case(tenant, b.id, evaluators=[{"kind": "equals_expected", "name": "eq"}])
    with pytest.raises(EvalSuiteInvalid):
        store.update_suite(tenant, suite.id, evaluators=[{"kind": "equals_expected", "name": "eq"}])
    store.delete_case(tenant, a.id)
    assert [c.name for c in store.list_cases(tenant, suite.id)] == ["b"]


def test_case_cap_on_add_and_import(store, tenant, monkeypatch):
    monkeypatch.setattr(evals_mod, "MAX_SUITE_CASES", 3)
    suite, _ = _suite_with_cases(store, tenant, n=3)
    with pytest.raises(EvalCaseLimitExceeded) as err:
        store.add_case(tenant, suite.id, {"name": "x", "input": "x"})
    assert "at most 3" in str(err.value)
    with pytest.raises(EvalCaseLimitExceeded):
        store.upsert_cases(tenant, suite.id, [{"name": "c0", "input": "u"}, {"name": "new", "input": "n"}])
    # Updating existing cases only is within the cap.
    assert store.upsert_cases(tenant, suite.id, [{"name": "c0", "input": "u"}]).updated == 1


def test_upsert_cases_keeps_ids_and_positions_and_is_atomic(store, tenant):
    suite, cases = _suite_with_cases(store, tenant, n=2)
    result = store.upsert_cases(
        tenant, suite.id,
        [{"name": "c1", "input": "new 1"}, {"name": "z", "input": "z"}, {"name": "y", "input": "y"}],
        evaluators=[{"kind": "contains"}],
    )
    assert (result.created, result.updated) == (2, 1)
    listed = store.list_cases(tenant, suite.id)
    assert [(c.name, c.position) for c in listed] == [("c0", 0), ("c1", 1), ("z", 2), ("y", 3)]
    assert listed[1].id == cases[1].id and listed[1].input == "new 1"
    assert store.get_suite(tenant, suite.id).evaluators[0].kind == "contains"
    # A bad case anywhere writes nothing.
    with pytest.raises(EvalSuiteInvalid):
        store.upsert_cases(tenant, suite.id, [{"name": "c0", "input": "changed"}, {"name": "bad", "input": ""}])
    with pytest.raises(EvalSuiteInvalid):
        store.upsert_cases(tenant, suite.id, [{"name": "d", "input": "1"}, {"name": "d", "input": "2"}])
    assert store.get_case(tenant, cases[0].id).input == "in 0"


# ---------------------------------------------------------------------------- snapshot


def test_snapshot_hash_is_stable_and_position_sensitive(store, tenant):
    suite, cases = _suite_with_cases(store, tenant, n=3)
    snap = store.snapshot(tenant, suite.id, max_concurrency=2)
    assert isinstance(snap, EvalSuiteSnapshot) and snap.max_concurrency == 2
    assert [c.name for c in snap.ordered_cases()] == ["c0", "c1", "c2"]
    again = store.snapshot(tenant, suite.id)
    assert again.suite_hash() == snap.suite_hash()  # max_concurrency is not part of the hash
    assert EvalSuiteSnapshot.model_validate_json(snap.canonical_json()) == snap
    # Reordering changes the hash; restoring the order restores it.
    store.update_case(tenant, cases[0].id, position=9)
    moved = store.snapshot(tenant, suite.id)
    assert [c.name for c in moved.ordered_cases()] == ["c1", "c2", "c0"]
    assert moved.suite_hash() != snap.suite_hash()
    store.update_case(tenant, cases[0].id, position=0)
    assert store.snapshot(tenant, suite.id).suite_hash() == snap.suite_hash()
    # An edit changes it.
    store.update_case(tenant, cases[1].id, expected_output=42)
    assert store.snapshot(tenant, suite.id).suite_hash() != snap.suite_hash()


def test_snapshot_refuses_empty_archived_and_foreign(store, tenant, other):
    empty = store.create_suite(tenant, "ag", "empty")
    with pytest.raises(EvalSuiteInvalid):
        store.snapshot(tenant, empty.id)
    suite, _ = _suite_with_cases(store, tenant, n=1)
    with pytest.raises(EvalSuiteNotFound):
        store.snapshot(other, suite.id)
    with pytest.raises(EvalSuiteNotFound):
        store.snapshot(tenant, suite.id, agent_id="ag2")
    with pytest.raises(EvalSuiteInvalid):
        store.snapshot(tenant, suite.id, max_concurrency=9)
    store.update_suite(tenant, suite.id, archived=True)
    with pytest.raises(EvalSuiteArchived):
        store.snapshot(tenant, suite.id)


def test_create_eval_run_published_and_draft(engine, store, tenant):
    suite, _ = _suite_with_cases(store, tenant, n=1)
    snap = store.snapshot(tenant, suite.id)
    rs = RunStore(engine)
    rid = uuid4()
    assert rs.create_eval_run(tenant, "ag", 3, suite.id, snap.canonical_json(), rid) == (rid, True)
    assert rs.create_eval_run(tenant, "ag", 3, suite.id, snap.canonical_json(), rid) == (rid, False)
    run = rs.get_run(tenant, rid)
    assert run["kind"] == "eval" and run["eval_suite_id"] == suite.id and run["agent_version"] == 3
    draft = uuid4()
    rs.create_eval_run(tenant, "ag", None, suite.id, snap.canonical_json(), draft, draft_spec={"model": "test:echo"})
    assert rs.get_run(tenant, draft)["draft_spec"] == {"model": "test:echo"}
    assert [r["id"] for r in store.list_runs(tenant, "ag", suite_id=suite.id)] == [str(draft), str(rid)]


# ---------------------------------------------------------------------------- results


def test_record_case_result_is_idempotent_with_usage_event_and_cost(engine, store, tenant, other):
    suite, cases = _suite_with_cases(store, tenant, n=1)
    run_id, snap = _eval_run(engine, store, tenant, suite)
    case = snap.ordered_cases()[0]
    child = _child(engine, tenant, run_id, "0.001")
    write = CaseResultWrite(
        case_id=case.id, case_name=case.name, position=case.position, status="passed", child_run_id=child,
        output={"answer": "x"}, assertions={"eq": {"value": True, "reason": None}, "judge": {"value": True}},
        scores={"judge": {"value": 0.75, "reason": "ok"}}, labels={"tone": {"value": "calm"}}, duration_ms=120,
    )
    judge = [_usage(judge_request_key(case.id, 0), "0.0005", "test:judge"), _usage(judge_request_key(case.id, 1), "0.00025", "test:judge")]
    first, created = store.record_case_result(tenant, run_id, write, judge_usage=judge)
    assert created and first.status == "passed" and first.cost_usd == Decimal("0.00175")
    assert first.output == {"answer": "x"} and first.case_name == case.name

    # Replay: nothing new (a different payload never overwrites the first).
    again, created = store.record_case_result(
        tenant, run_id, CaseResultWrite(case_id=case.id, case_name=case.name, position=0, status="error", error_code="x"),
        judge_usage=[_usage(judge_request_key(case.id, 2), "1")],
    )
    assert not created and again == first
    with engine.connect() as conn:
        events = conn.execute(text(
            "SELECT seq, event_key, type, data FROM execution_events WHERE run_id = :r ORDER BY seq"), {"r": run_id}
        ).all()
        keys = conn.execute(text(
            "SELECT request_key FROM run_model_requests WHERE run_id = :r ORDER BY request_key"), {"r": run_id}
        ).scalars().all()
    assert [(e.seq, e.event_key, e.type) for e in events] == [(0, f"case:{case.id}", "eval_case_result")]
    data = events[0].data
    assert data["status"] == "passed" and data["assertions"] == {"passed": 2, "total": 2}
    assert data["cost_usd"] == "0.0017500000" and data["child_run_id"] == child
    assert "x" not in str(data.get("output", "")) and "answer" not in str(data)
    assert keys == [judge_request_key(case.id, 0), judge_request_key(case.id, 1)]

    # Another tenant cannot write or read it.
    with pytest.raises(RunNotFound):
        store.record_case_result(other, run_id, write)
    with pytest.raises(EvalRunNotFound):
        store.list_case_results(other, run_id)
    assert [r.case_id for r in store.list_case_results(tenant, run_id)] == [str(case.id)]


def test_record_case_result_validates(engine, store, tenant, run_store):
    suite, _ = _suite_with_cases(store, tenant, n=1)
    run_id, snap = _eval_run(engine, store, tenant, suite)
    case = snap.ordered_cases()[0]
    base = dict(case_id=case.id, case_name=case.name, position=0)
    bad = [
        CaseResultWrite(**base, status="error"),  # no error_code
        CaseResultWrite(**base, status="passed", error_code="x"),
        CaseResultWrite(**base, status="skipped"),
        CaseResultWrite(**base, status="passed", assertions={"a": {"value": 1}}),
        CaseResultWrite(**base, status="passed", scores={"s": {"value": float("nan")}}),
        CaseResultWrite(**base, status="passed", labels={"l": {"value": 3}}),
        CaseResultWrite(**base, status="passed", duration_ms=-1),
    ]
    for write in bad:
        with pytest.raises(ValueError):
            store.record_case_result(tenant, run_id, write)
    with pytest.raises(ValueError):
        store.record_case_result(tenant, run_id, CaseResultWrite(**base, status="passed"), judge_usage=[_usage("model:0", "1")])
    agent_run = uuid4()
    run_store.create_run(tenant, "ag", 1, "hi", agent_run)
    with pytest.raises(EvalRunNotFound):
        store.record_case_result(tenant, agent_run, CaseResultWrite(**base, status="passed"))
    assert store.list_case_results(tenant, run_id) == []


def test_unpriced_usage_makes_the_case_cost_unknown(engine, store, tenant):
    suite, _ = _suite_with_cases(store, tenant, n=2)
    run_id, snap = _eval_run(engine, store, tenant, suite)
    c0, c1 = snap.ordered_cases()
    r0, _ = store.record_case_result(
        tenant, run_id, CaseResultWrite(case_id=c0.id, case_name=c0.name, position=0, status="failed",
                                        child_run_id=_child(engine, tenant, run_id, None)))
    r1, _ = store.record_case_result(
        tenant, run_id, CaseResultWrite(case_id=c1.id, case_name=c1.name, position=1, status="error", error_code="output_invalid"))
    assert r0.cost_usd is None and r1.cost_usd == Decimal(0)
    assert store.aggregates(tenant, run_id)["cost_usd"] is None


# ---------------------------------------------------------------------------- aggregates and compare


def _record(store, tenant, run_id, case, status, *, score=None, duration=None, cost_child=None, engine=None, assertion=None):
    child = _child(engine, tenant, run_id, cost_child) if cost_child is not None else None
    return store.record_case_result(tenant, run_id, CaseResultWrite(
        case_id=case.id, case_name=case.name, position=case.position, status=status, child_run_id=child,
        error_code="agent_error" if status == "error" else None,
        assertions={} if assertion is None else {"eq": {"value": assertion}},
        scores={} if score is None else {"judge": {"value": score}}, duration_ms=duration,
    ))[0]


def test_aggregates_arithmetic(engine, store, tenant):
    suite, _ = _suite_with_cases(store, tenant, n=4)
    run_id, snap = _eval_run(engine, store, tenant, suite)
    c = snap.ordered_cases()
    _record(store, tenant, run_id, c[0], "passed", score=1.0, duration=100, cost_child="0.25", engine=engine)
    _record(store, tenant, run_id, c[1], "failed", score=0.5, duration=301, cost_child="0.5", engine=engine)
    _record(store, tenant, run_id, c[2], "passed", duration=200, cost_child="0.125", engine=engine)
    _record(store, tenant, run_id, c[3], "error", duration=401)
    agg = store.aggregates(tenant, run_id)
    assert agg == {
        "cases": 4, "passed": 2, "failed": 1, "errors": 1, "pass_rate": 0.5,
        "scores_avg": {"judge": 0.75}, "duration_ms_p50": 251, "cost_usd": "0.8750000000",
    }
    assert aggregate_results([]) == {
        "cases": 0, "passed": 0, "failed": 0, "errors": 0, "pass_rate": None, "scores_avg": {},
        "duration_ms_p50": None, "cost_usd": "0.0000000000",
    }


def test_compare_classifies_and_matches_by_id_then_name(engine, store, tenant, other):
    suite, cases = _suite_with_cases(store, tenant, n=4)
    base_id, base_snap = _eval_run(engine, store, tenant, suite, version=1)
    b = {c.name: c for c in base_snap.cases}
    _record(store, tenant, base_id, b["c0"], "failed", score=0.2, duration=100, assertion=False)   # -> fixed
    _record(store, tenant, base_id, b["c1"], "passed", score=0.9, duration=100, assertion=True)    # -> regressed
    _record(store, tenant, base_id, b["c2"], "passed", duration=100)                               # -> unchanged (renamed id)
    _record(store, tenant, base_id, b["c3"], "failed")                                             # -> removed

    # Target: c2 recreated under a new id (matched by name), c3 deleted, c4 added.
    store.delete_case(tenant, cases[2].id)
    store.add_case(tenant, suite.id, {"name": "c2", "input": "in 2", "expected_output": 2}, position=2)
    store.delete_case(tenant, cases[3].id)
    store.add_case(tenant, suite.id, {"name": "c4", "input": "new"})
    target_id, target_snap = _eval_run(engine, store, tenant, suite, version=2)
    t = {c.name: c for c in target_snap.cases}
    assert t["c2"].id != b["c2"].id
    _record(store, tenant, target_id, t["c0"], "passed", score=0.8, duration=150, assertion=True)
    _record(store, tenant, target_id, t["c1"], "error", duration=50)
    _record(store, tenant, target_id, t["c2"], "passed", duration=100)
    _record(store, tenant, target_id, t["c4"], "passed")

    cmp = store.compare(tenant, base_id, target_id, agent_id="ag")
    changes = {c["name"]: c["change"] for c in cmp["cases"]}
    assert changes == {"c0": "fixed", "c1": "regressed", "c2": "unchanged", "c3": "removed", "c4": "added"}
    assert [c["name"] for c in cmp["cases"]] == ["c0", "c1", "c2", "c4", "c3"]
    c0 = cmp["cases"][0]
    assert c0["assertions"] == {"eq": {"base": False, "target": True}}
    assert c0["score_deltas"] == {"judge": 0.6} and c0["duration_ms_delta"] == 50
    assert cmp["base"]["agent_version"] == 1 and cmp["target"]["agent_version"] == 2
    assert cmp["same_suite"] is False and cmp["warnings"]
    s = cmp["summary"]
    assert (s["fixed"], s["regressed"], s["unchanged"], s["added"], s["removed"]) == (1, 1, 1, 1, 1)
    assert s["pass_rate_delta"] == 0.25  # base 2/4, target 3/4

    # Same snapshot -> same_suite, no warning.
    again_id, _ = _eval_run(engine, store, tenant, suite, version=2)
    assert store.compare(tenant, target_id, again_id)["same_suite"] is True
    assert store.compare(tenant, target_id, again_id)["warnings"] == []

    # Another tenant, another agent, or a non-eval run: not found.
    with pytest.raises(EvalRunNotFound):
        store.compare(other, base_id, target_id)
    with pytest.raises(EvalRunNotFound):
        store.compare(tenant, base_id, target_id, agent_id="ag2")
    foreign_suite, _ = _suite_with_cases(store, tenant, agent="ag2", n=1)
    foreign_run, _ = _eval_run(engine, store, tenant, foreign_suite, agent="ag2")
    with pytest.raises(EvalRunNotFound):
        store.compare(tenant, base_id, foreign_run)


def test_errors_pickle():
    for exc in (
        EvalSuiteNotFound("t", "s"), EvalCaseNotFound("t", "c"), EvalRunNotFound("t", "r"),
        EvalSuiteNameConflict("a", "n"), EvalCaseNameConflict("s", "n"), EvalCaseLimitExceeded("s", 500),
        EvalSuiteArchived("s"),
    ):
        clone = pickle.loads(pickle.dumps(exc))
        assert type(clone) is type(exc) and str(clone) == str(exc)


# ---------------------------------------------------------------------------- review fixes


def test_suite_evaluator_cap_and_unique_names_on_every_write(store, tenant):
    eleven = [{"kind": "equals_expected", "name": f"e{i}"} for i in range(11)]
    ten = eleven[:10]
    dup = [{"kind": "equals_expected", "name": "x"}, {"kind": "contains", "name": "x"}]
    with pytest.raises(EvalSuiteInvalid, match="at most 10"):
        store.create_suite(tenant, "ag", "big", evaluators=eleven)
    with pytest.raises(EvalSuiteInvalid, match="duplicate"):
        store.create_suite(tenant, "ag", "dup", evaluators=dup)
    suite = store.create_suite(tenant, "ag", "ok", evaluators=ten)  # no cases yet
    with pytest.raises(EvalSuiteInvalid, match="at most 10"):
        store.update_suite(tenant, suite.id, evaluators=eleven)
    with pytest.raises(EvalSuiteInvalid, match="duplicate"):
        store.update_suite(tenant, suite.id, evaluators=dup)
    with pytest.raises(EvalSuiteInvalid, match="at most 10"):
        store.upsert_cases(tenant, suite.id, [], evaluators=eleven)
    with pytest.raises(EvalSuiteInvalid, match="duplicate"):
        store.upsert_cases(tenant, suite.id, [], evaluators=dup)
    assert len(store.get_suite(tenant, suite.id).evaluators) == 10
    store.add_case(tenant, suite.id, {"name": "c", "input": "x"})
    assert len(store.snapshot(tenant, suite.id).evaluators) == 10


def test_create_eval_child_run_version_and_draft(engine, store, tenant):
    suite, _ = _suite_with_cases(store, tenant, n=1)
    rs = RunStore(engine)
    run_id, snap = _eval_run(engine, store, tenant, suite)
    case = snap.ordered_cases()[0]
    child = uuid4()
    assert rs.create_eval_child_run(tenant, run_id, case.id, "ag", 1, case.input, child) == (child, True)
    assert rs.create_eval_child_run(tenant, run_id, case.id, "ag", 1, case.input, child) == (child, False)
    row = rs.get_run(tenant, child)
    assert (row["kind"], row["parent_run_id"], row["parent_step_id"], row["agent_version"], row["conversation_id"]) == (
        "agent", run_id, f"case:{case.id}", 1, None)

    draft_parent = uuid4()
    rs.create_eval_run(tenant, "ag", None, suite.id, snap.canonical_json(), draft_parent, draft_spec={"m": 1})
    draft_child = uuid4()
    rs.create_eval_child_run(tenant, draft_parent, case.id, "ag", None, case.input, draft_child, draft_spec={"m": 1})
    row = rs.get_run(tenant, draft_child)
    assert row["agent_version"] is None and row["draft_spec"] == {"m": 1}
    for version, draft in ((None, None), (1, {"m": 1})):
        with pytest.raises(ValueError):
            rs.create_eval_child_run(tenant, run_id, case.id, "ag", version, "x", uuid4(), draft_spec=draft)


def test_eval_run_records_the_suite_hash_and_rejects_bad_input(engine, store, tenant):
    suite, _ = _suite_with_cases(store, tenant, n=1)
    run_id, snap = _eval_run(engine, store, tenant, suite)
    assert RunStore(engine).get_run(tenant, run_id)["eval_suite_hash"] == snap.suite_hash()
    other_suite, _ = _suite_with_cases(store, tenant, n=1)
    with pytest.raises(ValueError):
        RunStore(engine).create_eval_run(tenant, "ag", 1, other_suite.id, snap.canonical_json(), uuid4())
    with pytest.raises(ValueError):
        RunStore(engine).create_eval_run(tenant, "ag", 1, suite.id, "{}", uuid4())


def test_cost_text_has_ten_places_everywhere(engine, store, tenant):
    suite, _ = _suite_with_cases(store, tenant, n=1)
    run_id, snap = _eval_run(engine, store, tenant, suite)
    case = snap.ordered_cases()[0]
    result, _ = store.record_case_result(tenant, run_id, CaseResultWrite(
        case_id=case.id, case_name=case.name, position=0, status="passed"), judge_usage=[_usage(judge_request_key(case.id, 0), "0.5")])
    with engine.connect() as conn:
        data = conn.execute(text("SELECT data FROM execution_events WHERE run_id = :r"), {"r": run_id}).scalar_one()
    assert data["cost_usd"] == "0.5000000000" == format(result.cost_usd, "f")
    assert store.aggregates(tenant, run_id)["cost_usd"] == "0.5000000000"


def test_empty_error_code_is_rejected(engine, store, tenant):
    suite, _ = _suite_with_cases(store, tenant, n=1)
    run_id, snap = _eval_run(engine, store, tenant, suite)
    case = snap.ordered_cases()[0]
    with pytest.raises(ValueError):
        store.record_case_result(tenant, run_id, CaseResultWrite(
            case_id=case.id, case_name=case.name, position=0, status="error", error_code=""))


def test_case_calls_check_the_agent(store, tenant):
    suite, cases = _suite_with_cases(store, tenant, n=2)
    assert store.get_case(tenant, cases[0].id, agent_id="ag").id == cases[0].id
    with pytest.raises(EvalCaseNotFound):
        store.get_case(tenant, cases[0].id, agent_id="ag2")
    with pytest.raises(EvalCaseNotFound):
        store.update_case(tenant, cases[0].id, agent_id="ag2", input="z")
    with pytest.raises(EvalCaseNotFound):
        store.delete_case(tenant, cases[0].id, agent_id="ag2")
    assert store.update_case(tenant, cases[0].id, agent_id="ag", input="z").input == "z"
    store.delete_case(tenant, cases[1].id, agent_id="ag")
    assert [c.name for c in store.list_cases(tenant, suite.id)] == ["c0"]


def test_update_case_after_a_concurrent_delete_is_not_found(engine, store, tenant, monkeypatch):
    suite, cases = _suite_with_cases(store, tenant, n=1)
    real = EvalStore._writable_suite

    def delete_then_lock(self, session, *args):
        # The case is deleted by another transaction after it was read, before it is locked.
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM eval_cases WHERE id = :c"), {"c": cases[0].id})
        return real(self, session, *args)

    monkeypatch.setattr(EvalStore, "_writable_suite", delete_then_lock)
    with pytest.raises(EvalCaseNotFound):
        store.update_case(tenant, cases[0].id, input="z")
