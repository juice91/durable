"""AGUI.8 / 0016_ag_ui: the per-root commit order of events (`execution_events.root_order`).

Every append locks the ROOT run's row (then the child's) and stamps `root_order` from the root's
counter, so commit order equals root_order, readers see a gapless prefix, and a late commit can
never land before an event a reader has already seen.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from eap_run_store import EventLog, RunStore, append_event_in_session
from eap_run_store.approvals import insert_waiting_in_session


@pytest.fixture
def tenant(engine):
    tenant_id = f"test_tenant_{uuid4().hex[:8]}"
    yield tenant_id
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM pending_approvals WHERE tenant_id = :t"), {"t": tenant_id})
        conn.execute(text("DELETE FROM execution_events WHERE tenant_id = :t"), {"t": tenant_id})
        conn.execute(text("DELETE FROM runs WHERE tenant_id = :t AND parent_run_id IS NOT NULL"), {"t": tenant_id})
        conn.execute(text("DELETE FROM runs WHERE tenant_id = :t"), {"t": tenant_id})


def _workflow(rs: RunStore, tenant: str, children: int):
    root = uuid4()
    rs.create_workflow_run(tenant, uuid4(), 1, "{}", root)
    kids = []
    for i in range(children):
        kid = uuid4()
        rs.create_child_agent_run(tenant, uuid4(), 1, "hi", kid, root, f"step_{i}")
        kids.append(kid)
    return root, kids


def _orders(engine, tenant: str) -> list[tuple[str, int, int]]:
    with engine.connect() as conn:
        return [tuple(r) for r in conn.execute(text(
            "SELECT run_id, seq, root_order FROM execution_events WHERE tenant_id = :t ORDER BY root_order"
        ), {"t": tenant})]


def test_root_and_children_share_one_gapless_counter(engine, tenant):
    rs, log = RunStore(engine), EventLog(engine)
    root, (a, b) = _workflow(rs, tenant, 2)
    log.append(tenant, root, "start", "run_started", {})
    log.append(tenant, a, "start", "run_started", {})
    log.append(tenant, b, "start", "run_started", {})
    log.append(tenant, a, "end", "run_completed", {})
    _, created = log.append(tenant, a, "end", "run_completed", {})  # a repeated key takes no number
    assert created is False
    log.append(tenant, root, "end", "run_completed", {})
    rows = _orders(engine, tenant)
    assert [r[2] for r in rows] == [0, 1, 2, 3, 4]
    assert [(r[0], r[1]) for r in rows] == [(str(root), 0), (str(a), 0), (str(b), 0), (str(a), 1), (str(root), 1)]
    ordered = log.list_root_events(tenant, root)
    assert [o.position for o in ordered] == [0, 1, 2, 3, 4]
    assert [o.position for o in log.list_root_events(tenant, root, 2)] == [3, 4]
    assert log.list_root_events("someone_else", root) == []
    # Another root has its own counter.
    other, _ = _workflow(rs, tenant, 0)
    log.append(tenant, other, "start", "run_started", {})
    assert [o.position for o in log.list_root_events(tenant, other)] == [0]


def test_decisions_take_the_root_first_and_are_ordered(engine, tenant):
    rs, log = RunStore(engine), EventLog(engine)
    root, (kid,) = _workflow(rs, tenant, 1)
    rs.mark_running(tenant, root)
    rs.mark_running(tenant, kid)
    deadline = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    log.append_approval_waiting(tenant, kid, "tool", "c1", {
        "tool_call_id": "c1", "tool_name": "t", "args": {}, "deadline": deadline, "workflow_id": str(kid)})
    outcome, event = log.append_tool_decision(tenant, kid, "c1", "tool_approved", {"tool_call_id": "c1"})
    assert outcome == "created"
    log.append_approval_waiting(tenant, root, "step", "s", {
        "step_id": "s", "prompt": "?", "timeout_hours": 1, "workflow_id": str(root), "deadline": deadline})
    assert [r[2] for r in _orders(engine, tenant)] == [0, 1, 2]
    assert [o.event.type for o in log.list_root_events(tenant, root)] == [
        "tool_approval_waiting", "tool_approved", "step_waiting"]


def test_a_late_commit_is_ordered_where_it_commits_never_before_a_seen_event(engine, tenant):
    """The reviewer's probe 3: branch A's gated call is raised first but commits 1.5 s late;
    branch B's is raised 0.15 s later. B waits on the root lock, so it is ordered after A, and a
    reader polling meanwhile never sees B before A."""
    rs, log = RunStore(engine), EventLog(engine)
    root, (a, b) = _workflow(rs, tenant, 2)
    log.append(tenant, root, "start", "run_started", {})
    deadline = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    errors: list[BaseException] = []

    def late_a() -> None:
        try:
            with Session(engine) as session:
                event, _ = append_event_in_session(session, tenant, a, "tool:ca:approval:waiting", "tool_approval_waiting",
                                                   {"tool_call_id": "ca", "tool_name": "t", "args": {}, "deadline": deadline})
                insert_waiting_in_session(session, tenant_id=tenant, run_id=str(a), kind="tool", approval_key="ca",
                                          data={"tool_call_id": "ca", "tool_name": "t", "args": {}, "deadline": deadline},
                                          requested_at=event.ts)
                time.sleep(1.5)
                session.commit()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def quick_b() -> None:
        try:
            time.sleep(0.15)
            log.append_approval_waiting(tenant, b, "tool", "cb", {
                "tool_call_id": "cb", "tool_name": "t", "args": {}, "deadline": deadline, "workflow_id": str(b)})
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    seen: list[list[str]] = []
    threads = [threading.Thread(target=late_a), threading.Thread(target=quick_b)]
    for t in threads:
        t.start()
    deadline_t = time.monotonic() + 10
    while any(t.is_alive() for t in threads) and time.monotonic() < deadline_t:
        seen.append([str((o.event.data or {}).get("tool_call_id")) for o in log.list_root_events(tenant, root, 0)])
        time.sleep(0.05)
    for t in threads:
        t.join(10)
    assert errors == []
    final = [str((o.event.data or {}).get("tool_call_id")) for o in log.list_root_events(tenant, root, 0)]
    assert final == ["ca", "cb"]
    # Every poll saw a prefix of the final order: never ["cb"] alone.
    assert all(view == final[: len(view)] for view in seen), seen


@pytest.mark.parametrize("children", [8])
def test_parallel_children_appending_at_once_never_deadlock_and_stay_gapless(engine, tenant, children):
    rs, log = RunStore(engine), EventLog(engine)
    root, kids = _workflow(rs, tenant, children)
    rs.mark_running(tenant, root)
    per_child = 15
    errors: list[BaseException] = []
    barrier = threading.Barrier(children + 1)
    deadline = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

    def child_writer(kid) -> None:
        try:
            barrier.wait(10)
            rs.mark_running(tenant, kid)
            for i in range(per_child):
                if i == per_child - 1:
                    log.append_approval_waiting(tenant, kid, "tool", "c", {
                        "tool_call_id": "c", "tool_name": "t", "args": {}, "deadline": deadline, "workflow_id": str(kid)})
                    log.append_tool_decision(tenant, kid, "c", "tool_approved", {"tool_call_id": "c"})
                else:
                    log.append(tenant, kid, f"e{i}", "model_message", {"model_request": i, "text": "x"})
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def root_writer() -> None:
        try:
            barrier.wait(10)
            for i in range(per_child):
                log.append(tenant, root, f"r{i}", "step_started", {"step_id": f"r{i}"})
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=child_writer, args=(k,)) for k in kids] + [threading.Thread(target=root_writer)]
    started = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    assert not any(t.is_alive() for t in threads), "a writer is stuck (deadlock)"
    assert errors == [], errors
    orders = [r[2] for r in _orders(engine, tenant)]
    expected = children * (per_child + 1) + per_child  # each child: 14 events + waiting + decision
    assert orders == list(range(expected))  # gapless, unique
    # Within every run, root_order follows seq.
    by_run: dict[str, list[tuple[int, int]]] = {}
    for run_id, seq, order in _orders(engine, tenant):
        by_run.setdefault(run_id, []).append((seq, order))
    for pairs in by_run.values():
        assert [o for _, o in sorted(pairs)] == sorted(o for _, o in pairs)
    assert time.monotonic() - started < 120


def test_decisions_racing_appends_on_the_same_child_never_deadlock(engine, tenant):
    """Review MINOR 4: the API's decision path and the worker's append path on the SAME child
    run, started together at a barrier, round after round. With a child-then-root order on either
    path, two of these transactions form a lock cycle (Postgres aborts one with a deadlock); with
    the root first on both, none does."""
    rs, log = RunStore(engine), EventLog(engine)
    root, (kid,) = _workflow(rs, tenant, 1)
    rs.mark_running(tenant, root)
    rs.mark_running(tenant, kid)
    deadline = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    rounds, per_round = 6, 4
    for r in range(rounds):
        for i in range(per_round):
            call = f"c{r}_{i}"
            log.append_approval_waiting(tenant, kid, "tool", call, {
                "tool_call_id": call, "tool_name": "t", "args": {}, "deadline": deadline, "workflow_id": str(kid)})
    errors: list[BaseException] = []

    def run_round(r: int) -> None:
        barrier = threading.Barrier(per_round * 2 + 1)

        def decide(i: int) -> None:
            try:
                barrier.wait(10)
                outcome, _ = log.append_tool_decision(tenant, kid, f"c{r}_{i}", "tool_approved", {"tool_call_id": f"c{r}_{i}"})
                assert outcome == "created"
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def append(i: int) -> None:
            try:
                barrier.wait(10)
                log.append(tenant, kid, f"m{r}_{i}", "model_message", {"model_request": i, "text": "x"})
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def append_root() -> None:
            try:
                barrier.wait(10)
                log.append(tenant, root, f"s{r}", "step_started", {"step_id": f"s{r}"})
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=decide, args=(i,)) for i in range(per_round)]
        threads += [threading.Thread(target=append, args=(i,)) for i in range(per_round)]
        threads.append(threading.Thread(target=append_root))
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        assert not any(t.is_alive() for t in threads), "a writer is stuck"

    for r in range(rounds):
        run_round(r)
    assert errors == [], errors
    orders = [r[2] for r in _orders(engine, tenant)]
    assert orders == list(range(len(orders)))
    assert len(orders) == rounds * per_round * 3 + rounds  # waiting + decision + message, per call; + root


def test_the_insert_trigger_stamps_a_row_written_without_root_order(engine, tenant):
    """An old-code writer (rolling deploy) inserts without root_order: 0016's trigger stamps it from
    the root's counter, so it takes its commit-order place and no unordered row appears."""
    rs, log = RunStore(engine), EventLog(engine)
    root, (kid,) = _workflow(rs, tenant, 1)
    log.append(tenant, root, "start", "run_started", {})
    with engine.begin() as conn:
        conn.execute(text("SELECT id FROM runs WHERE id = :r FOR UPDATE"), {"r": str(kid)})  # the old lock
        conn.execute(text(
            "INSERT INTO execution_events (tenant_id, run_id, seq, event_key, type, data, ts)"
            " VALUES (:t, :r, 0, 'old', 'run_started', '{}', now())"
        ), {"t": tenant, "r": str(kid)})
    log.append(tenant, root, "next", "step_started", {"step_id": "x"})
    assert [r[2] for r in _orders(engine, tenant)] == [0, 1, 2]
    assert [o.event.type for o in log.list_root_events(tenant, root)] == ["run_started", "run_started", "step_started"]
    assert [o.position for o in log.list_root_events(tenant, root, 0)] == [1, 2]
