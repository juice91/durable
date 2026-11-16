"""AC.3 acceptance: the approvals projection under races, tenant isolation, paging, display status,
the 8 KiB truncation boundary and the output_format check. Real concurrent connections; the
implementer's focused tests live in test_approvals_store.py."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from eap_db.testing import SCHEMA_ENV
from eap_run_store import ApprovalCursor, ApprovalStore, InvalidRunCursor, RunNotFound
from eap_run_store.approvals import APPROVAL_ARGS_MAX_BYTES, projection_args

from .test_knowledge_store_concurrency import interleave

pytestmark = pytest.mark.skipif(
    not os.environ.get(SCHEMA_ENV), reason="needs the eap_db pytest plugin's migrated schema (dev Postgres)"
)


@pytest.fixture
def tenant(engine):
    t = f"acc_t_{uuid4().hex[:8]}"
    yield t
    _cleanup(engine, t)


@pytest.fixture
def other(engine):
    t = f"acc_o_{uuid4().hex[:8]}"
    yield t
    _cleanup(engine, t)


def _cleanup(engine, t):
    with engine.begin() as conn:
        for sql in (
            "DELETE FROM pending_approvals WHERE tenant_id = :t",
            "DELETE FROM execution_events WHERE tenant_id = :t",
            "DELETE FROM runs WHERE tenant_id = :t AND parent_run_id IS NOT NULL",
            "DELETE FROM runs WHERE tenant_id = :t",
            "DELETE FROM conversations WHERE tenant_id = :t",
        ):
            conn.execute(text(sql), {"t": t})


@pytest.fixture
def approvals(engine):
    return ApprovalStore(engine)


def _dl(hours: float = 1) -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=hours)


def _agent_run(rs, tenant, agent=None):
    run_id = uuid4()
    rs.create_run(tenant, agent or uuid4(), 1, "hi", run_id)
    rs.set_status(tenant, run_id, "running")
    rs.heartbeat(tenant, run_id)
    return run_id


def _wf_run(rs, tenant, wf=None):
    run_id = uuid4()
    rs.create_workflow_run(tenant, wf or uuid4(), 1, "{}", run_id)
    rs.set_status(tenant, run_id, "running")
    rs.heartbeat(tenant, run_id)
    return run_id


def _child(rs, tenant, parent, agent=None, step="agent_step"):
    run_id = uuid4()
    rs.create_child_agent_run(tenant, agent or uuid4(), 1, "hi", run_id, parent, step)
    rs.set_status(tenant, run_id, "running")
    rs.heartbeat(tenant, run_id)
    return run_id


def _tool(el, tenant, run_id, call="c1", args=None, deadline=None, name="send"):
    return el.append_approval_waiting(
        tenant, run_id, "tool", call,
        {"tool_call_id": call, "tool_name": name, "args": {"to": "x"} if args is None else args,
         "deadline": deadline or _dl()},
    )


def _step(el, tenant, run_id, step="gate", deadline=None):
    return el.append_approval_waiting(
        tenant, run_id, "step", step, {"step_id": step, "prompt": "ok?", "deadline": deadline or _dl()}
    )


def _row(engine, tenant, run_id, kind, key):
    with engine.connect() as conn:
        return conn.execute(text(
            "SELECT * FROM pending_approvals WHERE tenant_id = :t AND run_id = :r AND kind = :k AND approval_key = :a"
        ), {"t": tenant, "r": str(run_id), "k": kind, "a": key}).mappings().one_or_none()


def _events(engine, run_id):
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(text(
            "SELECT seq, event_key, type FROM execution_events WHERE run_id = :r ORDER BY seq"), {"r": str(run_id)})]


# ---------------------------------------------------------------------------- races

DECISIONS = {"tool": ("tool_approved", "tool_denied", "tool_approval_timed_out"),
             "step": ("step_approved", "step_rejected", "step_timed_out")}
MAPPED = {"tool_approved": "approved", "tool_denied": "denied", "tool_approval_timed_out": "timed_out",
          "step_approved": "approved", "step_rejected": "denied", "step_timed_out": "timed_out"}


def _decide(el, kind):
    return el.append_tool_decision if kind == "tool" else el.append_step_decision


def _setup(rs, el, tenant, kind):
    if kind == "tool":
        run_id = _agent_run(rs, tenant)
        _tool(el, tenant, run_id)
        return run_id, "c1"
    run_id = _wf_run(rs, tenant)
    _step(el, tenant, run_id)
    return run_id, "gate"


def _assert_agrees(engine, tenant, run_id, kind, key, winner_type, who):
    row = _row(engine, tenant, run_id, kind, key)
    assert row["decision"] == MAPPED[winner_type]
    assert row["decided_by"] == who
    events = _events(engine, run_id)
    assert [e[2] for e in events][1:] == [winner_type] and [e[0] for e in events] == [0, 1]


@pytest.mark.parametrize("kind", ["tool", "step"])
@pytest.mark.parametrize("first", [0, 1])
def test_two_decisions_racing_on_the_row_lock_exactly_one_created(run_store, event_log, engine, tenant, kind, first):
    """The first decision is paused right after taking the run-row lock; the second must wait for it
    (not read a stale 'waiting'), then report `exists` with the first's event."""
    run_id, key = _setup(run_store, event_log, tenant, kind)
    approve, deny = DECISIONS[kind][0], DECISIONS[kind][1]
    calls = [
        ("approve", lambda: _decide(event_log, kind)(tenant, run_id, key, approve, {"decided_by": "alice"})),
        ("deny", lambda: _decide(event_log, kind)(tenant, run_id, key, deny, {"decided_by": "bob"})),
    ]
    a, b = interleave(engine, calls[first], ("FROM runs", "FOR UPDATE"), calls[1 - first], blocks=True)
    assert a[0] == "created" and b[0] == "exists"
    assert b[1].type == a[1].type and b[1].seq == a[1].seq == 1
    _assert_agrees(engine, tenant, run_id, kind, key, a[1].type, "alice" if first == 0 else "bob")


@pytest.mark.parametrize("kind", ["tool", "step"])
@pytest.mark.parametrize("timeout_first", [True, False])
def test_decision_racing_the_timeout(run_store, event_log, engine, tenant, kind, timeout_first):
    run_id, key = _setup(run_store, event_log, tenant, kind)
    approve, timed_out = DECISIONS[kind][0], DECISIONS[kind][2]
    human = ("human", lambda: _decide(event_log, kind)(tenant, run_id, key, approve, {"decided_by": "alice"}))
    clock = ("timeout", lambda: _decide(event_log, kind)(tenant, run_id, key, timed_out, {}))
    first, second = (clock, human) if timeout_first else (human, clock)
    a, b = interleave(engine, first, ("FROM runs", "FOR UPDATE"), second, blocks=True)
    assert (a[0], b[0]) == ("created", "exists")
    winner = timed_out if timeout_first else approve
    assert a[1].type == winner and b[1].type == winner
    _assert_agrees(engine, tenant, run_id, kind, key, winner, None if timeout_first else "alice")
    # A late human decision after the timeout stays `exists` and never flips the row.
    late = _decide(event_log, kind)(tenant, run_id, key, approve, {"decided_by": "carol"})
    assert late[0] == "exists" and _row(engine, tenant, run_id, kind, key)["decision"] == MAPPED[winner]


@pytest.mark.parametrize("kind", ["tool", "step"])
def test_many_threads_mixed_decisions_repeatedly(run_store, event_log, engine, tenant, kind):
    for _ in range(4):
        run_id, key = _setup(run_store, event_log, tenant, kind)
        barrier, results, errors = threading.Barrier(8), [], []

        def go(i, run_id=run_id, key=key, barrier=barrier, results=results):
            try:
                barrier.wait()
                typ = DECISIONS[kind][i % 3]
                results.append((typ, _decide(event_log, kind)(tenant, run_id, key, typ, {"decided_by": f"u{i}"})))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
        [t.start() for t in threads]
        [t.join(30) for t in threads]
        assert not errors and len(results) == 8
        created = [r for r in results if r[1][0] == "created"]
        assert len(created) == 1 and [r[1][0] for r in results].count("exists") == 7
        winner_type = created[0][0]
        assert {r[1][1].type for r in results} == {winner_type}
        assert len(_events(engine, run_id)) == 2
        row = _row(engine, tenant, run_id, kind, key)
        assert row["decision"] == MAPPED[winner_type] and row["decided_at"] == created[0][1][1].ts


def test_waiting_replays_race_keep_one_event_row_and_first_deadline(run_store, event_log, engine, tenant):
    run_id = _agent_run(run_store, tenant)
    barrier, results = threading.Barrier(6), []
    deadlines = [_dl(i + 1) for i in range(6)]

    def go(i):
        barrier.wait()
        results.append((i, _tool(event_log, tenant, run_id, deadline=deadlines[i])))

    threads = [threading.Thread(target=go, args=(i,)) for i in range(6)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert sorted(r[1][1] for r in results) == [False] * 5 + [True]
    winner = next(i for i, r in results if r[1])
    assert len(_events(engine, run_id)) == 1
    row = _row(engine, tenant, run_id, "tool", "c1")
    assert row["deadline"] == deadlines[winner]
    assert {r[1][0].data["deadline"] for r in results} == {deadlines[winner].isoformat()}


# ---------------------------------------------------------------------------- tenant isolation


def test_every_store_method_is_tenant_scoped(run_store, event_log, engine, approvals, tenant, other):
    wf = _wf_run(run_store, tenant)
    child = _child(run_store, tenant, wf)
    agent_run = _agent_run(run_store, tenant)
    _step(event_log, tenant, wf)
    _tool(event_log, tenant, child, "cc")
    _tool(event_log, tenant, agent_run, "ca")
    mine = _agent_run(run_store, other)
    _tool(event_log, other, mine, "ca")  # the other tenant's own, same call id
    before = {r: _events(engine, r) for r in (wf, child, agent_run)}

    # Writes: another tenant's run is unknown, and nothing is written.
    for call in (
        lambda: _tool(event_log, other, agent_run, "zz"),
        lambda: _step(event_log, other, wf, "zz"),
        lambda: event_log.append_approval_waiting(other, child, "tool", "cc", {"tool_name": "t", "deadline": _dl()}),
        lambda: event_log.append_tool_decision(other, agent_run, "ca", "tool_approved", {"decided_by": "x"}),
        lambda: event_log.append_tool_decision(other, child, "cc", "tool_denied", {}),
        lambda: event_log.append_step_decision(other, wf, "gate", "step_approved", {"decided_by": "x"}),
    ):
        with pytest.raises(RunNotFound):
            call()
    assert {r: _events(engine, r) for r in (wf, child, agent_run)} == before
    assert _row(engine, tenant, agent_run, "tool", "ca")["decided_at"] is None
    assert _row(engine, tenant, wf, "step", "gate")["decided_at"] is None

    # Reads.
    assert approvals.get(other, str(agent_run), "tool", "ca") is None
    assert approvals.get(other, str(wf), "step", "gate") is None
    assert approvals.list_for_run(other, str(wf)) == [] and approvals.list_for_run(other, str(child)) == []
    assert [r["run_id"] for r in approvals.list(other, "pending")[0]] == [str(mine)]
    assert approvals.list(other, "pending", kind="step")[0] == []
    assert approvals.list(other, "decided")[0] == []
    assert approvals.list(other, "pending", agent_id=str(uuid4()))[0] == []
    assert len(approvals.list(tenant, "pending")[0]) == 3
    # A cursor minted in one tenant never reveals rows of the other.
    page, cur = approvals.list(tenant, "pending", limit=1)
    assert cur is not None and approvals.list(other, "pending", cursor=cur)[0] == []
    assert run_store.get_run(other, wf) is None and run_store.get_run(other, child) is None
    assert run_store.list_runs(other, status="waiting_approval")[0]["id"] == str(mine)
    assert {r["id"] for r in run_store.list_runs(tenant, status="waiting_approval")} == {str(agent_run), str(wf)}
    assert run_store.list_runs(other, parent_run_id=wf) == []
    # The other tenant's own decision works on its own row only.
    assert event_log.append_tool_decision(other, mine, "ca", "tool_denied", {"decided_by": "o"})[0] == "created"
    assert _row(engine, tenant, agent_run, "tool", "ca")["decision"] is None
    assert approvals.get(other, str(mine), "tool", "ca")["decision"] == "denied"


def test_conversation_reads_are_tenant_scoped(run_store, event_log, tenant, other):
    conv, _ = run_store.create_conversation(tenant, uuid4())
    run_id = uuid4()
    run_store.create_turn(tenant, conv["id"], 1, "hi", run_id)
    run_store.set_status(tenant, run_id, "running")
    run_store.heartbeat(tenant, run_id)
    _tool(event_log, tenant, run_id)
    assert run_store.get_conversation(tenant, conv["id"])["last_turn_display_status"] == "waiting_approval"
    assert run_store.get_conversation(other, conv["id"]) is None
    assert run_store.list_turns(other, conv["id"]) == []


def test_waiting_and_deciding_validate_before_writing(run_store, event_log, engine, tenant):
    run_id = _agent_run(run_store, tenant)
    for kind, key, data in (
        ("other", "k", {"deadline": _dl()}),
        ("tool", "", {"tool_name": "t", "deadline": _dl()}),
        ("tool", "k", {"deadline": _dl()}),  # no tool_name
    ):
        with pytest.raises(ValueError):
            event_log.append_approval_waiting(tenant, run_id, kind, key, data)
    with pytest.raises(ValueError):
        event_log.append_approval_waiting("", run_id, "tool", "k", {"tool_name": "t", "deadline": _dl()})
    assert _events(engine, run_id) == []


# ---------------------------------------------------------------------------- inbox paging

BASE = datetime(2020, 1, 1, tzinfo=timezone.utc)  # in the past: rows written now sort ahead of it


def _build_inbox(rs, el, engine, tenant) -> dict:
    """120 open rows: 60 tool (30 agent runs x 2), 30 step (10 workflow runs x 3), 30 on 15 workflow
    child runs (a tool and a step each). Returns {(run, kind, key): agent_id}. requested_at is pinned
    to groups of 7 equal timestamps, so every page boundary can fall inside a tie."""
    agents = [str(uuid4()) for _ in range(3)]
    expect: dict = {}
    for i in range(30):
        run_id = _agent_run(rs, tenant, agents[i % 3])
        for call in ("a", "b"):
            _tool(el, tenant, run_id, call)
            expect[(str(run_id), "tool", call)] = agents[i % 3]
    for _ in range(10):
        wf = _wf_run(rs, tenant)
        for step in ("s1", "s2", "s3"):
            _step(el, tenant, wf, step)
            expect[(str(wf), "step", step)] = None
    for i in range(15):
        child = _child(rs, tenant, _wf_run(rs, tenant), agents[(i % 2) * 2])
        _tool(el, tenant, child, "t")
        _step(el, tenant, child, "s")
        expect[(str(child), "tool", "t")] = expect[(str(child), "step", "s")] = agents[(i % 2) * 2]
    assert len(expect) == 120
    with engine.begin() as conn:
        conn.execute(text(
            "WITH n AS (SELECT run_id, kind, approval_key, row_number() OVER (ORDER BY random()) AS rn"
            " FROM pending_approvals WHERE tenant_id = :t) UPDATE pending_approvals p"
            " SET requested_at = :base - (n.rn / 7) * interval '1 second' FROM n"
            " WHERE p.tenant_id = :t AND p.run_id = n.run_id AND p.kind = n.kind AND p.approval_key = n.approval_key"
        ), {"t": tenant, "base": BASE})
    return expect


def _all_pages(approvals, tenant, status="pending", limit=25, between=None, **filters):
    seen, cursor, n = [], None, 0
    while True:
        rows, cursor = approvals.list(tenant, status, cursor=cursor, limit=limit, **filters)
        seen += rows
        n += 1
        if between:
            between(n, rows)
        if cursor is None:
            return seen


def _full_order(engine, tenant, status="pending"):
    col = "requested_at" if status == "pending" else "decided_at"
    cond = "decided_at IS NULL" if status == "pending" else "decided_at IS NOT NULL"
    with engine.connect() as conn:
        return [(r[0], r[1], r[2]) for r in conn.execute(text(
            f"SELECT run_id, kind, approval_key FROM pending_approvals WHERE tenant_id = :t AND {cond}"
            f" ORDER BY {col} DESC, run_id DESC, kind DESC, approval_key DESC"), {"t": tenant})]


def _ids(rows):
    return [(r["run_id"], r["kind"], r["approval_key"]) for r in rows]


def test_inbox_120_rows_page_sizes_filters_and_clamping(run_store, event_log, engine, approvals, tenant):
    expect = _build_inbox(run_store, event_log, engine, tenant)
    order = _full_order(engine, tenant)
    for limit in (1, 7, 14, 25, 50, 119, 120, 200):
        got = _ids(_all_pages(approvals, tenant, limit=limit))
        assert got == order, limit  # every row once, in the one stable order
    rows = _all_pages(approvals, tenant, limit=13)
    stamps = [r["requested_at"] for r in rows]
    assert stamps == sorted(stamps, reverse=True) and len(set(stamps)) == 18  # ties of 7 straddle pages
    assert all(r["tenant_id"] == tenant and r["status"] == "pending" and r["run_status"] == "running" for r in rows)
    assert {(r["run_id"], r["kind"], r["approval_key"]): r["agent_id"] for r in rows} == expect
    page, cur = approvals.list(tenant, "pending", limit=1000)
    assert len(page) == 120 and cur is None  # clamped to 200
    page, cur = approvals.list(tenant, "pending", limit=0)
    assert len(page) == 1 and cur is not None  # clamped to 1
    for kind in ("tool", "step"):
        want = [i for i in order if i[1] == kind]
        assert _ids(_all_pages(approvals, tenant, limit=9, kind=kind)) == want and len(want) == 75 - 30 * (kind == "step")
    for agent in {a for a in expect.values() if a}:
        want = [i for i in order if expect[i] == agent]
        assert _ids(_all_pages(approvals, tenant, limit=6, agent_id=agent)) == want
        both = _ids(_all_pages(approvals, tenant, limit=6, agent_id=agent, kind="step"))
        assert both == [i for i in want if i[1] == "step"]
    assert approvals.list(tenant, "pending", agent_id=str(uuid4()))[0] == []


def test_cursors_are_stable_under_inserts_and_decisions_between_pages(run_store, event_log, engine, approvals, tenant):
    _build_inbox(run_store, event_log, engine, tenant)
    original = _full_order(engine, tenant)
    added_new, added_old, decided = [], [], []

    def between(n, rows):
        if n == 1:  # after page 1: newer rows, older rows, and decisions on seen and unseen originals
            for _ in range(3):
                run_id = _agent_run(run_store, tenant)
                _tool(event_log, tenant, run_id, "new")
                added_new.append((str(run_id), "tool", "new"))
            for _ in range(3):
                run_id = _agent_run(run_store, tenant)
                _tool(event_log, tenant, run_id, "old")
                with engine.begin() as conn:
                    conn.execute(text("UPDATE pending_approvals SET requested_at = :ts WHERE run_id = :r"),
                                 {"ts": BASE - timedelta(days=1), "r": str(run_id)})
                added_old.append((str(run_id), "tool", "old"))
            for run, kind, key in (_ids(rows)[:2] + original[60:62]):  # two seen, two not yet reached
                _decide_any(event_log, tenant, run, kind, key)
                decided.append((run, kind, key))

    seen = _all_pages(approvals, tenant, limit=10, between=between)
    ids = _ids(seen)
    assert len(ids) == len(set(ids))  # no row twice
    assert not set(added_new) & set(ids)  # newer than the cursor: never on a later page
    assert set(added_old) <= set(ids)  # older than the cursor: reached exactly once
    # Rows decided after the first page are gone from later pages, unless already returned on page 1.
    page1 = set(original[:10])
    assert set(original) - set(ids) == {d for d in decided if d not in page1}
    stamps = [r["requested_at"] for r in seen]
    assert stamps == sorted(stamps, reverse=True)


def _decide_any(event_log, tenant, run, kind, key, deny=False):
    if kind == "tool":
        typ = "tool_denied" if deny else "tool_approved"
        return event_log.append_tool_decision(tenant, run, key, typ, {"decided_by": "u"})
    typ = "step_rejected" if deny else "step_approved"
    return event_log.append_step_decision(tenant, run, key, typ, {"decided_by": "u"})


def test_cursor_stable_while_another_thread_inserts_continuously(run_store, event_log, engine, approvals, tenant):
    _build_inbox(run_store, event_log, engine, tenant)
    original = _full_order(engine, tenant)
    stop, inserted, errors = threading.Event(), [], []

    def writer():
        try:
            while not stop.is_set():
                run_id = _agent_run(run_store, tenant)
                _tool(event_log, tenant, run_id, "live")
                inserted.append((str(run_id), "tool", "live"))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        first, cursor = approvals.list(tenant, "pending", limit=5)  # fixes the position before inserts start
        rows = list(first)
        while cursor is not None:
            page, cursor = approvals.list(tenant, "pending", cursor=cursor, limit=5)
            rows += page
    finally:
        stop.set()
        thread.join(30)
    assert not errors and inserted
    ids = _ids(rows)
    assert len(ids) == len(set(ids))
    assert [i for i in ids if i in set(original)] == [i for i in original if i in set(ids)]
    # Everything inserted after the first page landed ahead of the cursor: none of it is returned,
    # and every original row from page 2 on was.
    assert set(original[5:]) <= set(ids)
    assert not set(inserted) & set(ids[5:])


def test_pending_hides_terminal_runs_and_decided_pages_stay(run_store, event_log, engine, approvals, tenant):
    _build_inbox(run_store, event_log, engine, tenant)
    with engine.connect() as conn:
        runs = [r[0] for r in conn.execute(text(
            "SELECT DISTINCT run_id FROM pending_approvals WHERE tenant_id = :t ORDER BY 1 LIMIT 12"), {"t": tenant})]
        gone = conn.execute(text(
            "SELECT count(*) FROM pending_approvals WHERE run_id = ANY(:r)"), {"r": runs}).scalar_one()
    for run_id, status in zip(runs, ["completed", "failed", "needs_attention"] * 4):
        run_store.finish_with_end(tenant, run_id, status, error="x" if status == "failed" else None)
    pending = _all_pages(approvals, tenant, limit=11)
    assert len(pending) == 120 - gone and not {r["run_id"] for r in pending} & set(runs)
    assert {r["run_status"] for r in pending} == {"running"}
    # The rows themselves stay open: a terminal run is hidden from the inbox, not rewritten.
    assert len(approvals.list_for_run(tenant, runs[0], open_only=True)) >= 1


def test_decided_inbox_pages_newest_decision_first_with_ties_and_concurrent_decisions(
    run_store, event_log, engine, approvals, tenant
):
    _build_inbox(run_store, event_log, engine, tenant)
    order = _full_order(engine, tenant)
    kinds = {"tool": "tool_denied", "step": "step_rejected"}
    for run, kind, key in order[:90]:
        _decide_any(event_log, tenant, run, kind, key, deny=True)
    with engine.begin() as conn:  # decided_at in ties of 6, in an order unrelated to the request order
        conn.execute(text(
            "WITH n AS (SELECT run_id, kind, approval_key, row_number() OVER (ORDER BY random()) AS rn"
            " FROM pending_approvals WHERE tenant_id = :t AND decided_at IS NOT NULL) UPDATE pending_approvals p"
            " SET decided_at = :base - (n.rn / 6) * interval '1 second' FROM n WHERE p.tenant_id = :t"
            " AND p.run_id = n.run_id AND p.kind = n.kind AND p.approval_key = n.approval_key"), {"t": tenant, "base": BASE})
    want = _full_order(engine, tenant, "decided")
    assert len(want) == 90
    for limit in (1, 6, 16, 90):
        assert _ids(_all_pages(approvals, tenant, "decided", limit=limit)) == want
    rows = _all_pages(approvals, tenant, "decided", limit=7)
    assert all(r["status"] == "decided" and r["decision"] == "denied" and r["decided_by"] == "u" for r in rows)
    assert [r["decided_at"] for r in rows] == sorted((r["decided_at"] for r in rows), reverse=True)
    assert {r["kind"]: None for r in rows}.keys() == kinds.keys()

    # Decisions landing between pages are newest: ahead of the cursor, never on a later page.
    late = []

    def between(n, page):
        if n == 1:
            for run, kind, key in order[90:95]:
                _decide_any(event_log, tenant, run, kind, key, deny=True)
                late.append((run, kind, key))

    paged = _ids(_all_pages(approvals, tenant, "decided", limit=10, between=between))
    assert paged == want and not set(late) & set(paged)
    assert len(_all_pages(approvals, tenant, "decided", limit=50)) == 95
    assert len(_all_pages(approvals, tenant, "pending", limit=50)) == 25


def test_invalid_cursor_tokens_are_rejected(approvals, tenant):
    import base64

    def tok(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    ts = datetime.now(timezone.utc).isoformat()
    bad = [
        "", "!!!", "bm90LWpzb24", tok([1]), tok({"t": ts}), tok({"t": ts, "r": "r", "k": "other", "a": "a"}),
        tok({"t": ts, "r": 1, "k": "tool", "a": "a"}), tok({"t": "yesterday", "r": "r", "k": "tool", "a": "a"}),
        tok({"t": "2030-01-01T00:00:00", "r": "r", "k": "tool", "a": "a"}),  # naive timestamp
        tok({"t": ts, "r": "r", "k": "tool", "a": "a", "x": 1}), "A" * 5000,
    ]
    for token in bad:
        with pytest.raises(InvalidRunCursor):
            approvals.list(tenant, "pending", cursor=token)
    good = ApprovalCursor(datetime.now(timezone.utc), "r", "step", "k:with:colons").encode()
    assert approvals.list(tenant, "pending", cursor=good) == ([], None)
    with pytest.raises(ValueError):
        approvals.list("", "pending")
    with pytest.raises(ValueError):
        approvals.list(tenant, "pending", kind="other")


# ---------------------------------------------------------------------------- display status


def _stale(engine, run_id):
    with engine.begin() as conn:
        conn.execute(text("UPDATE runs SET heartbeat_at = now() - interval '5 minutes' WHERE id = :r"), {"r": str(run_id)})


def _display(rs, tenant, run_id):
    return rs.get_run(tenant, run_id)["display_status"]


def test_workflow_parent_shows_waiting_approval_for_an_open_child_row(run_store, event_log, engine, tenant):
    wf = _wf_run(run_store, tenant)
    c1, c2, quiet = _child(run_store, tenant, wf, step="s1"), _child(run_store, tenant, wf, step="s2"), _wf_run(run_store, tenant)
    assert _display(run_store, tenant, wf) == "running"
    _tool(event_log, tenant, c1)
    _tool(event_log, tenant, c2)
    assert [_display(run_store, tenant, r) for r in (wf, c1, c2, quiet)] == ["waiting_approval"] * 3 + ["running"]
    # One child decided: the parent still waits on the other.
    _decide_any(event_log, tenant, c1, "tool", "c1")
    assert [_display(run_store, tenant, r) for r in (wf, c1, c2)] == ["waiting_approval", "running", "waiting_approval"]
    _decide_any(event_log, tenant, c2, "tool", "c1")
    assert [_display(run_store, tenant, r) for r in (wf, c1, c2)] == ["running"] * 3
    # The parent's own step wait counts too, and the child list keeps the per-child status.
    _step(event_log, tenant, wf)
    assert _display(run_store, tenant, wf) == "waiting_approval" and _display(run_store, tenant, c1) == "running"
    assert [r["display_status"] for r in run_store.list_runs(tenant, parent_run_id=wf)] == ["running", "running"]


def test_waiting_approval_filter_and_recovering_precedence(run_store, event_log, engine, tenant):
    wf, plain, agent = _wf_run(run_store, tenant), _wf_run(run_store, tenant), _agent_run(run_store, tenant)
    child = _child(run_store, tenant, wf)
    _tool(event_log, tenant, child)
    _tool(event_log, tenant, agent)

    def listed(status):
        return {r["id"] for r in run_store.list_runs(tenant, status=status)}

    # Child runs are not top-level, so the workflow parent and the agent run are listed.
    assert listed("waiting_approval") == {str(wf), str(agent)}
    assert listed("running") == {str(wf), str(plain), str(agent)}
    assert listed(["waiting_approval", "queued"]) == {str(wf), str(agent)}
    assert listed("recovering") == set()
    # A stale heartbeat on the parent: recovering wins over its open child row (and the filters agree).
    _stale(engine, wf)
    assert _display(run_store, tenant, wf) == "recovering" and _display(run_store, tenant, child) == "waiting_approval"
    assert listed("waiting_approval") == {str(agent)} and listed("recovering") == {str(wf)}
    assert listed(["recovering", "waiting_approval"]) == {str(wf), str(agent)}
    assert listed("running") == {str(wf), str(plain), str(agent)}
    # Stale on the child: it recovers, its parent still waits for the child's open row.
    run_store.heartbeat(tenant, wf)
    _stale(engine, child)
    assert _display(run_store, tenant, child) == "recovering" and _display(run_store, tenant, wf) == "waiting_approval"
    run_store.heartbeat(tenant, child)
    # A running run that never heartbeated (NULL) with an open row waits; terminal and queued runs keep their status.
    with engine.begin() as conn:
        conn.execute(text("UPDATE runs SET heartbeat_at = NULL WHERE id = :r"), {"r": str(agent)})
    assert _display(run_store, tenant, agent) == "waiting_approval"
    run_store.finish_with_end(tenant, agent, "completed", output="x")
    assert _display(run_store, tenant, agent) == "completed" and str(agent) not in listed("waiting_approval")
    run_store.finish_with_end(tenant, wf, "failed", error="boom")
    assert _display(run_store, tenant, wf) == "failed" and listed("waiting_approval") == set()
    queued = uuid4()
    run_store.create_run(tenant, uuid4(), 1, "hi", queued)
    _tool(event_log, tenant, queued)
    assert _display(run_store, tenant, queued) == "queued" and listed("waiting_approval") == set()


def test_conversation_display_status_recovering_precedence(run_store, event_log, engine, tenant):
    conv, _ = run_store.create_conversation(tenant, uuid4())
    run_id = uuid4()
    run_store.create_turn(tenant, conv["id"], 1, "hi", run_id)
    run_store.set_status(tenant, run_id, "running")
    run_store.heartbeat(tenant, run_id)
    _tool(event_log, tenant, run_id)
    _stale(engine, run_id)
    assert run_store.get_conversation(tenant, conv["id"])["last_turn_display_status"] == "recovering"
    assert [r["display_status"] for r in run_store.list_turns(tenant, conv["id"])] == ["recovering"]
    run_store.heartbeat(tenant, run_id)
    assert run_store.get_conversation(tenant, conv["id"])["last_turn_display_status"] == "waiting_approval"


# ---------------------------------------------------------------------------- truncation boundary


def _canon_bytes(args) -> int:
    return len(json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())


def _padded(total_bytes: int, char: str = "x") -> dict:
    """{"k": <char * n>} whose canonical JSON is exactly total_bytes bytes (n counts characters)."""
    width = len(char.encode())
    n, rest = divmod(total_bytes - 8, width)
    assert rest == 0
    args = {"k": char * n}
    assert _canon_bytes(args) == total_bytes
    return args


@pytest.mark.parametrize(
    "total, char, kept",
    [
        (8191, "x", True), (8192, "x", True), (8193, "x", False), (8194, "x", False),
        (8192, "é", True), (8194, "é", False),  # 2-byte characters: bytes count, not characters
        (8192, "€", True), (8195, "€", False),  # 3-byte
        (8192, "😀", True), (8196, "😀", False),  # 4-byte
    ],
)
def test_args_truncation_boundary_is_exactly_8_kib_of_canonical_json(
    run_store, event_log, engine, tenant, total, char, kept
):
    assert APPROVAL_ARGS_MAX_BYTES == 8192
    if (total - 8) % len(char.encode()):
        pytest.fail(f"bad parameter {total} {char}")
    args = _padded(total, char)
    run_id = _agent_run(run_store, tenant)
    event, created = _tool(event_log, tenant, run_id, "big", args=args)
    assert created and event.data["args"] == args  # the event always keeps the full arguments
    row = _row(engine, tenant, run_id, "tool", "big")
    assert row["args"] == (args if kept else {"truncated": True})
    assert projection_args(args) == (args if kept else {"truncated": True})


def test_truncation_counts_canonical_not_input_ordering_and_nested_structures(run_store, event_log, engine, tenant):
    run_id = _agent_run(run_store, tenant)
    nested = {"b": [{"x": "y" * 100}] * 40, "a": {"deep": {"er": list(range(500))}}, "c": None, "d": True, "e": 1.5}
    size = _canon_bytes(nested)
    assert size < 8192
    _tool(event_log, tenant, run_id, "n", args=nested)
    assert _row(engine, tenant, run_id, "tool", "n")["args"] == nested
    # Grow one value until the canonical size is exactly the limit, then one byte over.
    nested["pad"] = "p" * (8192 - _canon_bytes(nested) - len(',"pad":""'))
    assert _canon_bytes(nested) == 8192
    _tool(event_log, tenant, run_id, "edge", args=nested)
    assert _row(engine, tenant, run_id, "tool", "edge")["args"] == nested
    nested["pad"] += "p"
    _tool(event_log, tenant, run_id, "over", args=nested)
    assert _row(engine, tenant, run_id, "tool", "over")["args"] == {"truncated": True}


def test_args_shapes_and_step_rows_never_store_args(run_store, event_log, engine, tenant):
    run_id = _agent_run(run_store, tenant)
    event_log.append_approval_waiting(tenant, run_id, "tool", "none", {"tool_name": "t", "deadline": _dl()})
    _tool(event_log, tenant, run_id, "empty", args={})
    assert _row(engine, tenant, run_id, "tool", "none")["args"] is None
    assert _row(engine, tenant, run_id, "tool", "empty")["args"] == {}
    wf = _wf_run(run_store, tenant)
    event_log.append_approval_waiting(tenant, wf, "step", "s", {"prompt": "p", "args": {"a": 1}, "tool_name": "x", "deadline": _dl()})
    row = _row(engine, tenant, wf, "step", "s")
    assert row["args"] is None and row["tool_name"] is None and row["prompt"] == "p"
    # A replay with different (larger) arguments changes nothing.
    first, _ = _tool(event_log, tenant, run_id, "re", args={"v": 1})
    again, created = _tool(event_log, tenant, run_id, "re", args=_padded(9000))
    assert not created and again.seq == first.seq and _row(engine, tenant, run_id, "tool", "re")["args"] == {"v": 1}


# ---------------------------------------------------------------------------- output_format


@pytest.mark.parametrize("value", ["text", "json", None])
def test_output_format_accepts_text_json_and_null(run_store, engine, tenant, value):
    run_id = _agent_run(run_store, tenant)
    with engine.begin() as conn:
        conn.execute(text("UPDATE runs SET output_format = :v WHERE id = :r"), {"v": value, "r": str(run_id)})
    assert run_store.get_run(tenant, run_id)["output_format"] == value
    assert run_store.list_runs(tenant)[0]["output_format"] == value


@pytest.mark.parametrize("value", ["", "JSON", "Text", " json", "json ", "xml", "jsonl", "yaml", "null"])
def test_output_format_check_rejects_everything_else(run_store, engine, tenant, value):
    run_id = _agent_run(run_store, tenant)
    with engine.begin() as conn:
        conn.execute(text("UPDATE runs SET output_format = 'json' WHERE id = :r"), {"r": str(run_id)})
    with pytest.raises(IntegrityError, match="ck_runs_output_format"):
        with engine.begin() as conn:
            conn.execute(text("UPDATE runs SET output_format = :v WHERE id = :r"), {"v": value, "r": str(run_id)})
    assert run_store.get_run(tenant, run_id)["output_format"] == "json"  # the failed update changed nothing
    with pytest.raises(IntegrityError, match="ck_runs_output_format"):  # and an INSERT is checked too
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO runs (id, tenant_id, kind, agent_id, agent_version, status, input, output_format,"
                " created_at, updated_at) VALUES (:id, :t, 'agent', 'a', 1, 'queued', 'i', :v, now(), now())"
            ), {"id": str(uuid4()), "t": tenant, "v": value})


def test_output_format_in_every_run_read_and_untouched_by_status_changes(run_store, engine, tenant):
    conv, _ = run_store.create_conversation(tenant, uuid4())
    turn = uuid4()
    run_store.create_turn(tenant, conv["id"], 1, "hi", turn)
    wf = _wf_run(run_store, tenant)
    child = _child(run_store, tenant, wf)
    for run_id in (turn, child, wf):
        assert run_store.get_run(tenant, run_id)["output_format"] is None
    with engine.begin() as conn:
        conn.execute(text("UPDATE runs SET output_format = 'json' WHERE id = ANY(:r)"), {"r": [str(turn), str(child)]})
    run_store.finish_with_end(tenant, turn, "completed", output='{"a":1}')
    assert run_store.get_run(tenant, turn)["output_format"] == "json"
    assert [r["output_format"] for r in run_store.list_turns(tenant, conv["id"])] == ["json"]
    assert [r["output_format"] for r in run_store.list_runs(tenant, parent_run_id=wf)] == ["json"]
    assert run_store.get_run(tenant, wf)["output_format"] is None


# ---------------------------------------------------------------------------- events and rows agree


def test_bad_deadlines_write_nothing(run_store, event_log, engine, tenant):
    run_id = _agent_run(run_store, tenant)
    for deadline in (None, "tomorrow", 12345, datetime(2030, 1, 1), "2030-01-01T00:00:00"):  # missing/unparsable/naive
        with pytest.raises(ValueError):
            event_log.append_approval_waiting(tenant, run_id, "tool", "k", {"tool_name": "t", "deadline": deadline})
    assert _events(engine, run_id) == [] and _row(engine, tenant, run_id, "tool", "k") is None
    # ISO text with an offset is stored as given and parsed to the same instant.
    event, _ = event_log.append_approval_waiting(
        tenant, run_id, "step", "s", {"prompt": "p", "deadline": "2030-01-01T10:00:00+02:00"})
    assert _row(engine, tenant, run_id, "step", "s")["deadline"] == datetime(2030, 1, 1, 8, tzinfo=timezone.utc)
    assert event.data["deadline"] == "2030-01-01T10:00:00+02:00"


@pytest.fixture
def failing_trigger(engine):
    """A real database failure inside the projection write: a trigger that rejects rows whose
    approval_key is 'boom' (insert) or whose decision is set on them (update)."""
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE FUNCTION ac3_reject() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN"
            " IF NEW.approval_key = 'boom' AND (TG_OP = 'INSERT' OR NEW.decision IS NOT NULL) THEN"
            " RAISE EXCEPTION 'ac3 forced projection failure'; END IF; RETURN NEW; END $$"))
        conn.execute(text(
            "CREATE TRIGGER ac3_reject BEFORE INSERT OR UPDATE ON pending_approvals"
            " FOR EACH ROW EXECUTE FUNCTION ac3_reject()"))
    yield
    with engine.begin() as conn:
        conn.execute(text("DROP TRIGGER IF EXISTS ac3_reject ON pending_approvals"))
        conn.execute(text("DROP FUNCTION IF EXISTS ac3_reject()"))


@pytest.mark.parametrize("kind", ["tool", "step"])
def test_real_projection_failure_rolls_back_the_event_and_a_retry_succeeds(
    run_store, event_log, engine, tenant, failing_trigger, kind
):
    from sqlalchemy.exc import DBAPIError

    run_id = _agent_run(run_store, tenant) if kind == "tool" else _wf_run(run_store, tenant)
    wait = (lambda key: _tool(event_log, tenant, run_id, key)) if kind == "tool" else (
        lambda key: _step(event_log, tenant, run_id, key))
    with pytest.raises(DBAPIError, match="ac3 forced projection failure"):
        wait("boom")
    assert _events(engine, run_id) == [] and _row(engine, tenant, run_id, kind, "boom") is None
    # The run is usable afterwards: the next write takes seq 0 (no gap from the rolled-back one).
    wait("fine")
    assert [e[0] for e in _events(engine, run_id)] == [0]


@pytest.mark.parametrize("kind", ["tool", "step"])
def test_real_failure_in_the_decision_update_rolls_back_the_decision_event(
    run_store, event_log, engine, tenant, kind
):
    from sqlalchemy.exc import DBAPIError

    run_id = _agent_run(run_store, tenant) if kind == "tool" else _wf_run(run_store, tenant)
    (_tool(event_log, tenant, run_id, "boom") if kind == "tool" else _step(event_log, tenant, run_id, "boom"))
    with engine.begin() as conn:  # armed after the wait is written: only the decision update fails
        conn.execute(text(
            "CREATE FUNCTION ac3_reject() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN"
            " IF NEW.decision IS NOT NULL THEN RAISE EXCEPTION 'ac3 forced decision failure'; END IF;"
            " RETURN NEW; END $$"))
        conn.execute(text("CREATE TRIGGER ac3_reject BEFORE UPDATE ON pending_approvals"
                          " FOR EACH ROW EXECUTE FUNCTION ac3_reject()"))
    try:
        with pytest.raises(DBAPIError, match="ac3 forced decision failure"):
            _decide_any(event_log, tenant, str(run_id), kind, "boom")
        assert [e[1] for e in _events(engine, run_id)] == [
            "tool:boom:approval:waiting" if kind == "tool" else "step:boom:waiting"]
        assert _row(engine, tenant, run_id, kind, "boom")["decided_at"] is None
    finally:
        with engine.begin() as conn:
            conn.execute(text("DROP TRIGGER IF EXISTS ac3_reject ON pending_approvals"))
            conn.execute(text("DROP FUNCTION IF EXISTS ac3_reject()"))
    # Nothing is stuck: the same decision now lands, event and row together.
    assert _decide_any(event_log, tenant, str(run_id), kind, "boom")[0] == "created"
    assert _row(engine, tenant, run_id, kind, "boom")["decision"] == "approved"


def test_kind_namespaces_are_separate_and_decision_types_map(run_store, event_log, engine, tenant):
    run_id = _wf_run(run_store, tenant)
    _step(event_log, tenant, run_id, "x")
    # A tool decision never closes a step wait with the same id, nor the reverse.
    assert event_log.append_tool_decision(tenant, run_id, "x", "tool_approved", {}) == ("not_waiting", None)
    _tool(event_log, tenant, run_id, "x")
    assert event_log.append_tool_decision(tenant, run_id, "x", "tool_denied", {"decided_by": "t"})[0] == "created"
    assert _row(engine, tenant, run_id, "step", "x")["decided_at"] is None
    assert _row(engine, tenant, run_id, "tool", "x")["decision"] == "denied"
    assert event_log.append_step_decision(tenant, run_id, "x", "step_timed_out", {})[0] == "created"
    assert _row(engine, tenant, run_id, "step", "x")["decision"] == "timed_out"
    for kind, types in DECISIONS.items():
        for typ in types:
            run = _wf_run(run_store, tenant) if kind == "step" else _agent_run(run_store, tenant)
            (_step if kind == "step" else _tool)(event_log, tenant, run, "k")
            assert _decide(event_log, kind)(tenant, run, "k", typ, {"decided_by": "d"})[0] == "created"
            assert _row(engine, tenant, run, kind, "k")["decision"] == MAPPED[typ]
    with pytest.raises(ValueError):
        event_log.append_step_decision(tenant, run_id, "x", "tool_approved", {})
    with pytest.raises(ValueError):
        event_log.append_tool_decision(tenant, run_id, "x", "step_approved", {})


def test_rows_and_events_always_agree_after_a_mixed_workload(run_store, event_log, engine, approvals, tenant):
    runs = []
    for i in range(12):
        run_id = _agent_run(run_store, tenant) if i % 2 else _wf_run(run_store, tenant)
        kind = "tool" if i % 2 else "step"
        for key in ("a", "b", "c"):
            (_tool if kind == "tool" else _step)(event_log, tenant, run_id, key)
            (_tool if kind == "tool" else _step)(event_log, tenant, run_id, key)  # a replay
        typ = DECISIONS[kind]
        _decide(event_log, kind)(tenant, run_id, "a", typ[i % 3], {"decided_by": f"u{i}"})
        _decide(event_log, kind)(tenant, run_id, "a", typ[(i + 1) % 3], {"decided_by": "late"})  # exists
        _decide(event_log, kind)(tenant, run_id, "zzz", typ[0], {})  # not_waiting
        if i % 4 == 0:  # the run ends while "c" is still open: decisions are refused from then on
            run_store.finish_with_end(tenant, run_id, "failed", error="x")
            assert _decide(event_log, kind)(tenant, run_id, "b", typ[0], {})[0] == "not_waiting"
        else:
            _decide(event_log, kind)(tenant, run_id, "b", typ[2], {})
        runs.append((run_id, kind))
    with engine.connect() as conn:
        for run_id, kind in runs:
            events = {e["event_key"]: e for e in conn.execute(text(
                "SELECT event_key, type, ts, data FROM execution_events WHERE run_id = :r"), {"r": str(run_id)}).mappings()}
            rows = conn.execute(text(
                "SELECT * FROM pending_approvals WHERE run_id = :r ORDER BY approval_key"), {"r": str(run_id)}).mappings().all()
            assert [r["approval_key"] for r in rows] == ["a", "b", "c"] and {r["kind"] for r in rows} == {kind}
            for row in rows:
                wkey = f"tool:{row['approval_key']}:approval:waiting" if kind == "tool" else f"step:{row['approval_key']}:waiting"
                dkey = wkey.replace("waiting", "decision")
                assert row["requested_at"] == events[wkey]["ts"]
                assert row["deadline"] == datetime.fromisoformat(events[wkey]["data"]["deadline"])
                if dkey in events:
                    assert row["decided_at"] == events[dkey]["ts"]
                    assert row["decision"] == MAPPED[events[dkey]["type"]]
                    assert row["decided_by"] == events[dkey]["data"].get("decided_by")
                else:
                    assert (row["decided_at"], row["decision"], row["decided_by"]) == (None, None, None)
            assert sum(1 for k in events if k.endswith("decision")) == sum(1 for r in rows if r["decided_at"])
    # The decided inbox lists exactly the rows that have a decision event: "a" everywhere, "b" on
    # the 9 runs that did not end.
    assert len(_all_pages(approvals, tenant, "decided", limit=50)) == 12 + 9
    # The pending inbox: "c" of the 9 live runs (the 3 ended runs are hidden).
    assert len(_all_pages(approvals, tenant, "pending", limit=50)) == 9
