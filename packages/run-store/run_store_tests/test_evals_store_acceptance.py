"""AC.10 acceptance for the eval store: tenant isolation of every method, name conflicts (archived
suites too), all-or-nothing upsert, the case cap, snapshot hash stability and order, a concurrent
record_case_result, cost (None unpriced, 0 without rows), aggregates, compare, and event data
without text. Complements the implementer's test_evals_store.py."""

from __future__ import annotations

import json
import threading
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text

import eap_run_store.evals as evals_mod
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
    RunNotFound,
    judge_request_key,
)
from eap_schemas.evals import EvalSuiteInvalid

from .test_evals_store import _child, _eval_run, _record, _suite_with_cases, _usage


def _clean(engine, tenant_id):
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
def tenant(engine):
    t = f"test_tenant_{uuid4().hex[:8]}"
    yield t
    _clean(engine, t)


@pytest.fixture
def other(engine):
    t = f"test_tenant_{uuid4().hex[:8]}"
    yield t
    _clean(engine, t)


@pytest.fixture
def store(engine):
    return EvalStore(engine)


def _counts(engine, tenant_id):
    with engine.connect() as conn:
        return tuple(conn.execute(text(f"SELECT count(*) FROM {t} WHERE tenant_id = :t"), {"t": tenant_id}).scalar_one()
                     for t in ("eval_suites", "eval_cases", "eval_case_results", "execution_events", "run_model_requests"))


# ---------------------------------------------------------------------------- tenant isolation


def test_every_method_is_tenant_scoped_and_leaves_the_owner_untouched(engine, store, tenant, other):
    suite, cases = _suite_with_cases(store, tenant, n=2)
    run_id, snap = _eval_run(engine, store, tenant, suite)
    c = snap.ordered_cases()[0]
    write = CaseResultWrite(case_id=c.id, case_name=c.name, position=0, status="passed")
    store.record_case_result(tenant, run_id, write)
    run2, _ = _eval_run(engine, store, tenant, suite)
    before = _counts(engine, tenant)
    snap_before = store.snapshot(tenant, suite.id).canonical_json()

    not_found = [
        (EvalSuiteNotFound, lambda: store.get_suite(other, suite.id)),
        (EvalSuiteNotFound, lambda: store.update_suite(other, suite.id, name="hijack")),
        (EvalSuiteNotFound, lambda: store.update_suite(other, suite.id, archived=True)),
        (EvalSuiteNotFound, lambda: store.delete_suite(other, suite.id)),
        (EvalSuiteNotFound, lambda: store.add_case(other, suite.id, {"name": "x", "input": "x"})),
        (EvalCaseNotFound, lambda: store.get_case(other, cases[0].id)),
        (EvalSuiteNotFound, lambda: store.list_cases(other, suite.id)),
        (EvalCaseNotFound, lambda: store.update_case(other, cases[0].id, input="hijack")),
        (EvalCaseNotFound, lambda: store.delete_case(other, cases[0].id)),
        (EvalSuiteNotFound, lambda: store.upsert_cases(other, suite.id, [{"name": "c0", "input": "hijack"}])),
        (EvalSuiteNotFound, lambda: store.snapshot(other, suite.id)),
        (RunNotFound, lambda: store.record_case_result(other, run_id, write)),
        (EvalRunNotFound, lambda: store.list_case_results(other, run_id)),
        (EvalRunNotFound, lambda: store.aggregates(other, run_id)),
        (EvalRunNotFound, lambda: store.compare(other, run_id, run2)),
    ]
    for exc, call in not_found:
        with pytest.raises(exc):
            call()
    assert store.list_suites(other, "ag") == [] and store.list_suites(other, "ag", include_archived=True) == []
    assert store.list_runs(other, "ag") == [] and store.list_runs(other, "ag", suite_id=suite.id) == []
    assert _counts(engine, tenant) == before and store.snapshot(tenant, suite.id).canonical_json() == snap_before
    assert store.get_case(tenant, cases[0].id).input == "in 0"
    # Mixed ids: the other tenant's run with this tenant's case ids reads nothing.
    with pytest.raises(EvalCaseNotFound):
        store.delete_case(other, cases[1].id, suite_id=suite.id)
    # An empty tenant id can never write, and reads nothing.
    with pytest.raises(ValueError):
        store.create_suite("", "ag", "x")
    with pytest.raises(ValueError):
        store.record_case_result("", run_id, write)
    assert store.list_suites("", "ag") == [] and store.list_runs("", "ag") == []
    with pytest.raises(EvalSuiteNotFound):
        store.get_suite("", suite.id)


# ---------------------------------------------------------------------------- name conflicts


def test_suite_names_conflict_with_archived_suites_and_free_on_delete(store, tenant):
    keep = store.create_suite(tenant, "ag", "regression")
    other_suite = store.create_suite(tenant, "ag", "smoke")
    store.update_suite(tenant, keep.id, archived=True)
    with pytest.raises(EvalSuiteNameConflict):
        store.create_suite(tenant, "ag", "regression")  # an archived suite still owns its name
    with pytest.raises(EvalSuiteNameConflict):
        store.update_suite(tenant, other_suite.id, name="regression")
    assert store.get_suite(tenant, other_suite.id).name == "smoke"
    store.update_suite(tenant, keep.id, archived=False)  # restoring keeps working
    store.delete_suite(tenant, keep.id)
    assert store.create_suite(tenant, "ag", "regression").name == "regression"
    # Renaming to its own name is not a conflict.
    assert store.update_suite(tenant, other_suite.id, name="smoke").name == "smoke"


def test_case_names_conflict_within_a_suite_only(store, tenant):
    a, _ = _suite_with_cases(store, tenant, n=2)
    b, _ = _suite_with_cases(store, tenant, n=0)
    store.add_case(tenant, b.id, {"name": "c0", "input": "same name, other suite"})
    with pytest.raises(EvalCaseNameConflict):
        store.add_case(tenant, a.id, {"name": "c0", "input": "dup"})
    c1 = store.list_cases(tenant, a.id)[1]
    with pytest.raises(EvalCaseNameConflict):
        store.update_case(tenant, c1.id, name="c0")
    assert [c.name for c in store.list_cases(tenant, a.id)] == ["c0", "c1"]
    # A deleted case frees its name.
    store.delete_case(tenant, c1.id)
    assert store.add_case(tenant, a.id, {"name": "c1", "input": "again"}).name == "c1"


# ---------------------------------------------------------------------------- all-or-nothing upsert


def _state(store, tenant, suite_id):
    s = store.get_suite(tenant, suite_id)
    return (s.updated_at, [e.model_dump_json() for e in s.evaluators],
            [(c.id, c.name, c.input, c.position, c.expected_output) for c in store.list_cases(tenant, suite_id)])


@pytest.mark.parametrize("bad_batch, error", [
    ([{"name": "c0", "input": "NEW"}, {"name": "n1", "input": "ok"}, {"name": "bad", "input": ""}], EvalSuiteInvalid),
    ([{"name": "c1", "input": "NEW"}, {"name": "n1", "input": "ok"}, {"name": "n1", "input": "dup"}], EvalSuiteInvalid),
    ([{"name": "c0", "input": "NEW"}, {"name": "n1", "input": "ok"},
      {"name": "n2", "input": "x", "evaluators": [{"kind": "equals_expected", "name": "suite-eval"}]}], EvalSuiteInvalid),
    ([{"name": "c0", "input": "NEW"}, {"name": "n1", "input": "ok"}, {"name": "n2", "input": "x" * (32 * 1024 + 1)}], EvalSuiteInvalid),
])
def test_upsert_with_a_mid_batch_error_changes_nothing(store, tenant, bad_batch, error):
    suite, _ = _suite_with_cases(store, tenant, n=2)
    store.update_suite(tenant, suite.id, evaluators=[{"kind": "contains", "value": "x", "name": "suite-eval"}])
    before = _state(store, tenant, suite.id)
    with pytest.raises(error):
        store.upsert_cases(tenant, suite.id, bad_batch, evaluators=[{"kind": "equals_expected", "name": "suite-eval"}])
    assert _state(store, tenant, suite.id) == before  # no created, no updated, evaluators not replaced


def test_upsert_over_the_cap_and_on_an_archived_suite_changes_nothing(store, tenant, monkeypatch):
    monkeypatch.setattr(evals_mod, "MAX_SUITE_CASES", 3)
    suite, _ = _suite_with_cases(store, tenant, n=2)
    before = _state(store, tenant, suite.id)
    with pytest.raises(EvalCaseLimitExceeded):
        store.upsert_cases(tenant, suite.id, [{"name": "c0", "input": "NEW"}, {"name": "x", "input": "1"}, {"name": "y", "input": "2"}])
    assert _state(store, tenant, suite.id) == before
    store.update_suite(tenant, suite.id, archived=True)
    before = _state(store, tenant, suite.id)
    with pytest.raises(EvalSuiteArchived):
        store.upsert_cases(tenant, suite.id, [{"name": "c0", "input": "NEW"}])
    assert _state(store, tenant, suite.id) == before


# ---------------------------------------------------------------------------- case cap


def test_cap_is_exact_for_add_upsert_and_frees_on_delete(store, tenant, monkeypatch):
    monkeypatch.setattr(evals_mod, "MAX_SUITE_CASES", 3)
    suite = store.create_suite(tenant, "ag", "cap")
    assert store.upsert_cases(tenant, suite.id, [{"name": f"n{i}", "input": "x"} for i in range(3)]).created == 3
    with pytest.raises(EvalCaseLimitExceeded):
        store.add_case(tenant, suite.id, {"name": "over", "input": "x"})
    store.delete_case(tenant, store.list_cases(tenant, suite.id)[0].id)
    assert store.add_case(tenant, suite.id, {"name": "again", "input": "x"}).name == "again"
    assert store.get_suite(tenant, suite.id).case_count == 3
    # The cap is per suite.
    other_suite = store.create_suite(tenant, "ag", "cap2")
    assert store.upsert_cases(tenant, other_suite.id, [{"name": f"m{i}", "input": "x"} for i in range(3)]).created == 3
    with pytest.raises(EvalCaseLimitExceeded):
        store.upsert_cases(tenant, other_suite.id, [{"name": f"m{i}", "input": "x"} for i in range(4)])


def test_concurrent_adds_never_exceed_the_cap(store, tenant, monkeypatch):
    monkeypatch.setattr(evals_mod, "MAX_SUITE_CASES", 3)
    suite = store.create_suite(tenant, "ag", "race")
    barrier, results = threading.Barrier(8), []

    def add(i):
        barrier.wait()
        try:
            store.add_case(tenant, suite.id, {"name": f"r{i}", "input": "x"})
            results.append("ok")
        except EvalCaseLimitExceeded:
            results.append("limit")

    threads = [threading.Thread(target=add, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert sorted(results) == ["limit"] * 5 + ["ok"] * 3
    positions = [c.position for c in store.list_cases(tenant, suite.id)]
    assert sorted(positions) == sorted(set(positions)) and len(positions) == 3


# ---------------------------------------------------------------------------- snapshot


def test_snapshot_hash_is_stable_across_calls_order_sensitive_and_frozen_in_the_run(engine, store, tenant):
    suite, cases = _suite_with_cases(store, tenant, n=3)
    hashes = {store.snapshot(tenant, suite.id).suite_hash() for _ in range(3)}
    assert len(hashes) == 1
    base = store.snapshot(tenant, suite.id)
    # Swap two positions: the order (and the hash) change; swapping back restores both.
    store.update_case(tenant, cases[0].id, position=7)
    store.update_case(tenant, cases[2].id, position=0)
    store.update_case(tenant, cases[0].id, position=2)
    swapped = store.snapshot(tenant, suite.id)
    assert [c.name for c in swapped.ordered_cases()] == ["c2", "c1", "c0"]
    assert swapped.suite_hash() != base.suite_hash()
    store.update_case(tenant, cases[0].id, position=7)
    store.update_case(tenant, cases[2].id, position=2)
    store.update_case(tenant, cases[0].id, position=0)
    restored = store.snapshot(tenant, suite.id)
    assert [c.name for c in restored.ordered_cases()] == ["c0", "c1", "c2"]
    assert restored.suite_hash() == base.suite_hash()
    # A suite evaluator change changes the hash too.
    store.update_suite(tenant, suite.id, evaluators=[{"kind": "contains", "value": "z"}])
    assert store.snapshot(tenant, suite.id).suite_hash() != base.suite_hash()
    # The run input is frozen: later edits and a deleted case do not touch it.
    run_id, snap = _eval_run(engine, store, tenant, suite)
    stored = snap.canonical_json()
    store.update_case(tenant, cases[1].id, input="edited later")
    store.delete_case(tenant, cases[2].id)
    from eap_run_store import RunStore
    assert RunStore(engine).get_run(tenant, run_id)["input"] == stored


# ---------------------------------------------------------------------------- concurrent record


def test_concurrent_record_case_result_writes_exactly_once(engine, store, tenant):
    suite, _ = _suite_with_cases(store, tenant, n=1)
    run_id, snap = _eval_run(engine, store, tenant, suite)
    case = snap.ordered_cases()[0]
    n, barrier, out, errors = 8, threading.Barrier(8), [], []

    def record(i):
        write = CaseResultWrite(case_id=case.id, case_name=case.name, position=0, status="passed", output={"i": i},
                                assertions={"eq": {"value": True}}, duration_ms=10 + i)
        # Each thread brings a DIFFERENT set of judge rows: only the winner's may land.
        usage = [_usage(judge_request_key(case.id, j), "0.001", "test:judge") for j in range(i % 3 + 1)]
        barrier.wait()
        try:
            out.append(store.record_case_result(tenant, run_id, write, judge_usage=usage))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=record, args=(i,)) for i in range(n)]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    assert errors == [] and len(out) == n
    assert sum(1 for _, created in out if created) == 1
    winner = next(r for r, created in out if created)
    assert all(r.output == winner.output and r.duration_ms == winner.duration_ms for r, _ in out)
    with engine.connect() as conn:
        params = {"r": run_id}
        assert conn.execute(text("SELECT count(*) FROM eval_case_results WHERE run_id = :r"), params).scalar_one() == 1
        events = conn.execute(text("SELECT seq, event_key FROM execution_events WHERE run_id = :r"), params).all()
        usage_keys = conn.execute(text("SELECT request_key FROM run_model_requests WHERE run_id = :r"), params).scalars().all()
    assert [tuple(e) for e in events] == [(0, f"case:{case.id}")]
    expected = winner.output["i"] % 3 + 1
    assert sorted(usage_keys) == [judge_request_key(case.id, j) for j in range(expected)]
    assert winner.cost_usd == Decimal("0.001") * expected


# ---------------------------------------------------------------------------- cost


def _result(store, engine, tenant, run_id, case, child_cost=None, judge_costs=(), child=True):
    cid = _child(engine, tenant, run_id, child_cost) if child else None
    usage = [_usage(judge_request_key(case.id, i), c, "test:judge") for i, c in enumerate(judge_costs)]
    return store.record_case_result(
        tenant, run_id, CaseResultWrite(case_id=case.id, case_name=case.name, position=case.position, status="passed",
                                        child_run_id=cid), judge_usage=usage)[0]


def test_case_cost_is_none_when_any_row_is_unpriced_and_zero_without_rows(engine, store, tenant):
    suite, _ = _suite_with_cases(store, tenant, n=5)
    run_id, snap = _eval_run(engine, store, tenant, suite)
    c = snap.ordered_cases()
    assert _result(store, engine, tenant, run_id, c[0], "0.5", ["0.25", "0.125"]).cost_usd == Decimal("0.875")
    assert _result(store, engine, tenant, run_id, c[1], "0.5", ["0.25", None]).cost_usd is None  # one unpriced judge row
    assert _result(store, engine, tenant, run_id, c[2], None, ["0.25"]).cost_usd is None  # unpriced child
    assert _result(store, engine, tenant, run_id, c[3], None, [None], child=False).cost_usd is None  # judge only
    assert _result(store, engine, tenant, run_id, c[4], child=False).cost_usd == Decimal(0)  # no rows at all
    agg = store.aggregates(tenant, run_id)
    assert agg["cost_usd"] is None
    only_priced = [r for r in store.list_case_results(tenant, run_id) if r.cost_usd is not None]
    assert sorted(r.cost_usd for r in only_priced) == [Decimal(0), Decimal("0.875")]


def test_a_child_with_no_usage_rows_costs_zero_and_a_priced_run_sums_exactly(engine, store, tenant):
    suite, _ = _suite_with_cases(store, tenant, n=2)
    run_id, snap = _eval_run(engine, store, tenant, suite)
    c0, c1 = snap.ordered_cases()
    from eap_run_store import RunStore
    bare = uuid4()
    RunStore(engine).create_child_agent_run(tenant, "ag", 1, "in", bare, run_id, "case")
    r0 = store.record_case_result(tenant, run_id, CaseResultWrite(
        case_id=c0.id, case_name=c0.name, position=0, status="passed", child_run_id=bare))[0]
    assert r0.cost_usd == Decimal(0)
    _result(store, engine, tenant, run_id, c1, "0.0000000001", ["0.0000000002"])
    assert store.aggregates(tenant, run_id)["cost_usd"] == "0.0000000003"


# ---------------------------------------------------------------------------- aggregates


def test_aggregates_pass_rate_p50_and_score_averages(engine, store, tenant):
    suite, _ = _suite_with_cases(store, tenant, n=5)
    run_id, snap = _eval_run(engine, store, tenant, suite)
    c = snap.ordered_cases()
    assert store.aggregates(tenant, run_id)["pass_rate"] is None  # no results yet
    rows = [("passed", 0.9, 30), ("passed", 0.6, 10), ("failed", None, 20), ("failed", 0.3, None), ("error", None, None)]
    for case, (status, score, dur) in zip(c, rows):
        store.record_case_result(tenant, run_id, CaseResultWrite(
            case_id=case.id, case_name=case.name, position=case.position, status=status, duration_ms=dur,
            error_code="agent_error" if status == "error" else None,
            scores={} if score is None else {"judge": {"value": score}, "len": {"value": 2}}))
    agg = store.aggregates(tenant, run_id)
    assert (agg["cases"], agg["passed"], agg["failed"], agg["errors"]) == (5, 2, 2, 1)
    assert agg["pass_rate"] == 0.4  # 2 of 5, errors count as not passed
    assert agg["duration_ms_p50"] == 20  # median of 30, 10, 20 (cases without a duration are ignored)
    assert agg["scores_avg"] == {"judge": 0.6, "len": 2.0}  # mean over the 3 cases that have it
    assert Decimal(agg["cost_usd"]) == 0  # no usage rows anywhere


def test_aggregates_on_all_errors_a_single_result_and_a_half_up_median(engine, store, tenant):
    suite, _ = _suite_with_cases(store, tenant, n=2)
    run_id, snap = _eval_run(engine, store, tenant, suite)
    c0, c1 = snap.ordered_cases()
    store.record_case_result(tenant, run_id, CaseResultWrite(
        case_id=c0.id, case_name="c0", position=0, status="error", error_code="boom", duration_ms=5))
    one = store.aggregates(tenant, run_id)
    assert (one["pass_rate"], one["passed"], one["errors"], one["duration_ms_p50"], one["scores_avg"]) == (0.0, 0, 1, 5, {})
    store.record_case_result(tenant, run_id, CaseResultWrite(
        case_id=c1.id, case_name="c1", position=1, status="failed", duration_ms=8))
    assert store.aggregates(tenant, run_id)["duration_ms_p50"] == 7  # (5 + 8) / 2 = 6.5 -> 7


def test_aggregates_refuse_a_non_eval_run_and_a_wrong_agent(engine, store, run_store, tenant):
    suite, _ = _suite_with_cases(store, tenant, n=1)
    run_id, _snap = _eval_run(engine, store, tenant, suite)
    plain = uuid4()
    run_store.create_run(tenant, "ag", 1, "hi", plain)
    with pytest.raises(EvalRunNotFound):
        store.aggregates(tenant, plain)
    with pytest.raises(EvalRunNotFound):
        store.aggregates(tenant, run_id, agent_id="other-agent")
    with pytest.raises(EvalRunNotFound):
        store.aggregates(tenant, uuid4())
    assert store.list_runs(tenant, "other-agent") == []
    assert [r["id"] for r in store.list_runs(tenant, "ag")] == [run_id]


# ---------------------------------------------------------------------------- compare


def _two_runs(engine, store, tenant, statuses_base, statuses_target, mutate=None):
    suite, cases = _suite_with_cases(store, tenant, n=len(statuses_base))
    base_id, bs = _eval_run(engine, store, tenant, suite, version=1)
    for case, status in zip(bs.ordered_cases(), statuses_base):
        _record(store, tenant, base_id, case, status, engine=engine)
    if mutate:
        mutate(suite, cases)
    target_id, ts = _eval_run(engine, store, tenant, suite, version=2)
    for case, status in zip(ts.ordered_cases(), statuses_target):
        _record(store, tenant, target_id, case, status, engine=engine)
    return base_id, target_id


def test_compare_classifies_every_status_transition(engine, store, tenant):
    base, target = _two_runs(engine, store, tenant,
                             ["failed", "error", "passed", "passed", "failed", "error"],
                             ["passed", "passed", "failed", "error", "error", "failed"])
    cmp = store.compare(tenant, base, target)
    assert [c["change"] for c in cmp["cases"]] == ["fixed", "fixed", "regressed", "regressed", "unchanged", "unchanged"]
    assert cmp["same_suite"] is True and cmp["warnings"] == []
    assert cmp["summary"]["pass_rate_delta"] == 0.0  # 2 passed on each side
    assert cmp["base"]["aggregates"]["passed"] == 2 and cmp["target"]["aggregates"]["passed"] == 2


def test_compare_with_different_suites_added_removed_and_renamed_cases(engine, store, tenant):
    def mutate(suite, cases):
        store.update_case(tenant, cases[0].id, name="renamed")  # same id: matched by id
        store.delete_case(tenant, cases[2].id)  # removed
        store.add_case(tenant, suite.id, {"name": "fresh", "input": "x"})  # added
    base, target = _two_runs(engine, store, tenant, ["failed", "passed", "passed"], ["passed", "passed", "passed"], mutate)
    cmp = store.compare(tenant, base, target)
    by_name = {c["name"]: c for c in cmp["cases"]}
    assert set(by_name) == {"renamed", "c1", "c2", "fresh"}
    assert by_name["renamed"]["change"] == "fixed" and by_name["renamed"]["base_case_id"] == by_name["renamed"]["target_case_id"]
    assert by_name["c1"]["change"] == "unchanged"
    assert by_name["c2"]["change"] == "removed" and by_name["c2"]["target_case_id"] is None
    assert by_name["fresh"]["change"] == "added" and by_name["fresh"]["base_case_id"] is None
    assert cmp["cases"][-1]["name"] == "c2"  # removed cases come last
    assert cmp["same_suite"] is False and len(cmp["warnings"]) == 1
    assert cmp["base"]["suite_hash"] != cmp["target"]["suite_hash"]
    s = cmp["summary"]
    assert (s["fixed"], s["regressed"], s["unchanged"], s["added"], s["removed"]) == (1, 0, 1, 1, 1)


def test_compare_with_itself_empty_runs_and_cost_and_score_deltas(engine, store, tenant):
    suite, _ = _suite_with_cases(store, tenant, n=1)
    a, snap = _eval_run(engine, store, tenant, suite)
    b, _ = _eval_run(engine, store, tenant, suite)
    own = store.compare(tenant, a, a)
    assert own["same_suite"] is True and own["cases"] == [] and own["summary"]["pass_rate_delta"] is None
    c = snap.ordered_cases()[0]
    for rid, cost, score in ((a, "0.1", 0.25), (b, "0.35", 0.75)):
        child = _child(engine, tenant, rid, cost)
        store.record_case_result(tenant, rid, CaseResultWrite(
            case_id=c.id, case_name=c.name, position=0, status="passed", child_run_id=child,
            scores={"judge": {"value": score}}, duration_ms=100))
    s = store.compare(tenant, a, b)["summary"]
    assert Decimal(s["cost_usd_delta"]) == Decimal("0.25") and s["scores_avg_delta"] == {"judge": 0.5}
    assert s["pass_rate_delta"] == 0.0 and s["duration_ms_p50_delta"] == 0


# ---------------------------------------------------------------------------- event data has no text

SECRETS = ("SECRET-INPUT-TEXT", "SECRET-OUTPUT-TEXT", "SECRET-REASON", "SECRET-ERROR", "SECRET-LABEL", "SECRET-EXPECTED")
EVENT_KEYS = {"case_id", "position", "status", "assertions", "scores", "labels", "evaluator_failures", "duration_ms",
              "cost_usd", "error_code", "child_run_id"}


def test_the_case_event_carries_counts_only_never_input_or_output_text(engine, store, tenant):
    suite = store.create_suite(tenant, "ag", "secrets", evaluators=[{"kind": "equals_expected"}])
    store.add_case(tenant, suite.id, {"name": "c", "input": "SECRET-INPUT-TEXT", "expected_output": "SECRET-EXPECTED"})
    store.add_case(tenant, suite.id, {"name": "e", "input": "SECRET-INPUT-TEXT"})
    run_id, snap = _eval_run(engine, store, tenant, suite)
    ok, err = snap.ordered_cases()
    store.record_case_result(tenant, run_id, CaseResultWrite(
        case_id=ok.id, case_name=ok.name, position=0, status="failed", output={"text": "SECRET-OUTPUT-TEXT"},
        assertions={"eq": {"value": False, "reason": "SECRET-REASON"}, "ok": {"value": True, "reason": None}},
        scores={"j": {"value": 0.5, "reason": "SECRET-REASON"}}, labels={"tone": {"value": "SECRET-LABEL", "reason": None}},
        evaluator_failures=[{"name": "x", "error": "SECRET-ERROR"}], duration_ms=3, child_run_id=_child(engine, tenant, run_id, "0.01")))
    store.record_case_result(tenant, run_id, CaseResultWrite(
        case_id=err.id, case_name=err.name, position=1, status="error", error_code="agent_error", output="SECRET-OUTPUT-TEXT"))
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT event_key, type, data FROM execution_events WHERE run_id = :r ORDER BY seq"),
                            {"r": run_id}).all()
        usage = conn.execute(text("SELECT * FROM run_model_requests WHERE run_id = :r"), {"r": run_id}).all()
    assert [(r.event_key, r.type) for r in rows] == [(f"case:{ok.id}", "eval_case_result"), (f"case:{err.id}", "eval_case_result")]
    for row in rows:
        dumped = json.dumps(row.data)
        assert not any(s in dumped for s in SECRETS), dumped
        assert set(row.data) == EVENT_KEYS
        # Only ids, numbers, short codes and None: no free-text field.
        assert all(v is None or isinstance(v, (int, str, dict)) for v in row.data.values())
        assert all(isinstance(v, int) for v in row.data["assertions"].values())
    first = rows[0].data
    assert first["assertions"] == {"passed": 1, "total": 2} and first["scores"] == 1 and first["labels"] == 1
    assert first["evaluator_failures"] == 1 and first["status"] == "failed"
    assert usage == []
    # The text lives only in the result row.
    got = store.list_case_results(tenant, run_id)[0]
    assert got.output == {"text": "SECRET-OUTPUT-TEXT"} and got.assertions["eq"]["reason"] == "SECRET-REASON"
