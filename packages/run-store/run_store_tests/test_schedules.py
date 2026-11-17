"""ScheduleStore (0022): CRUD with tenant scoping, name uniqueness, paging, and claim_due under
two concurrent sessions (FOR UPDATE SKIP LOCKED: each due row is claimed once)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text

from eap_run_store import RunCursor, ScheduleNameConflict, ScheduleStore

T0 = datetime(1970, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def tenants(engine):
    made = [f"test_tenant_{uuid4().hex[:8]}" for _ in range(2)]
    yield made
    with engine.begin() as conn:
        for t in made:
            conn.execute(text("DELETE FROM workflow_schedules WHERE tenant_id = :t"), {"t": t})


@pytest.fixture
def store(engine):
    return ScheduleStore(engine)


def make(store, tenant, wf="wf1", name="n", at=T0):
    return store.create(tenant, wf, name=name, cron="* * * * *", timezone="UTC", input={"a": 1}, next_fire_at=at)


def test_create_get_update_delete_are_tenant_scoped(store, tenants):
    a, b = tenants
    row = make(store, a)
    assert row["active"] is True and row["last_error"] is None and row["input"] == {"a": 1}
    assert store.get(b, row["id"]) is None
    assert store.update(b, row["id"], active=False) is None
    assert store.delete(b, row["id"]) is False
    out = store.update(a, row["id"], active=False, last_error="x")
    assert out["active"] is False and out["last_error"] == "x" and out["updated_at"] >= row["updated_at"]
    assert store.get(a, "not-a-uuid") is None
    assert store.delete(a, row["id"]) is True and store.get(a, row["id"]) is None


def test_name_is_unique_per_workflow(store, tenants):
    a, b = tenants
    make(store, a)
    with pytest.raises(ScheduleNameConflict):
        make(store, a)
    make(store, a, wf="wf2")
    make(store, b)  # another tenant may reuse it
    other = make(store, a, name="m")
    with pytest.raises(ScheduleNameConflict):
        store.update(a, other["id"], name="n")


def test_list_filters_by_workflow_and_pages_newest_first(store, tenants):
    a, b = tenants
    ids = [make(store, a, wf="w1" if i % 2 else "w2", name=f"s{i}")["id"] for i in range(5)]
    make(store, b, name="foreign")
    page, nxt = store.list(a, None, 2, None)
    assert [r["id"] for r in page] == ids[::-1][:2] and nxt
    page2, nxt2 = store.list(a, None, 2, RunCursor.decode(nxt))
    page3, nxt3 = store.list(a, None, 2, RunCursor.decode(nxt2))
    assert [r["id"] for r in page + page2 + page3] == ids[::-1] and nxt3 is None
    only, _ = store.list(a, "w1", 50, None)
    assert {r["name"] for r in only} == {"s1", "s3"}


def test_claim_due_takes_only_active_due_rows_and_mark_fired_advances(store, tenants):
    a, _ = tenants
    due = make(store, a, name="due")
    make(store, a, name="later", at=T0 + timedelta(days=30))
    paused = make(store, a, name="paused")
    store.update(a, paused["id"], active=False)
    now = T0 + timedelta(days=1)
    # a shared dev DB may hold other tenants' due rows: only this test's tenant is compared
    assert [r["id"] for r in store.claim_due(now) if r["tenant_id"] == a] == [due["id"]]
    store.mark_fired(due["id"], now, now + timedelta(days=2))
    assert [r for r in store.claim_due(now) if r["tenant_id"] == a] == []
    assert store.get(a, due["id"])["last_fired_at"] == now
    store.stop(due["id"], "gone")
    row = store.get(a, due["id"])
    assert row["active"] is False and row["last_error"] == "gone"


def test_two_concurrent_sessions_claim_each_due_row_once(store, tenants):
    a, _ = tenants
    ids = {make(store, a, name=f"c{i}", at=T0 + timedelta(seconds=i))["id"] for i in range(2)}
    now = T0 + timedelta(days=1)
    s1, s2 = store.Session(), store.Session()
    try:
        first = store.claim_due(now, 1, session=s1)  # s1 keeps its row lock
        second = store.claim_due(now, 1, session=s2)  # skips the locked row
        third = store.claim_due(now, 5, session=s2)  # nothing left that is unlocked
        assert len(first) == 1 and len(second) == 1
        assert {first[0]["id"], second[0]["id"]} == ids
        assert [r["id"] for r in third if r["id"] in ids] == [second[0]["id"]]
    finally:
        s1.rollback(); s2.rollback(); s1.close(); s2.close()
