"""Schedule tick (parity round 2, Task 5): the scheduled DBOS workflow `eap.schedule_tick` creates a
run for each due workflow schedule (in-process worker; the API is not involved)."""

from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

# Imported at collection, so the workflow is registered before the session worker launches DBOS.
from eap_worker.schedule_tick import (
    SCHEDULE_TICK_SCHEDULE_NAME,
    ScheduleTickConfigError,
    apply_schedule_tick,
    schedule_tick,
)
from worker_tests.conftest import wait_for
from worker_tests.workflow_run_helpers import cleanup_definitions, publish_workflow

INPUT_SCHEMA = {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}


def _spec(name: str = "Tick") -> dict:
    return {
        "name": name,
        "input_schema": INPUT_SCHEMA,
        "start": "now",
        "steps": [{"id": "now", "type": "tool", "tool_id": "clock.now", "input": {}}],
    }


_LOOP = asyncio.new_event_loop()
threading.Thread(target=_LOOP.run_forever, daemon=True).start()  # like DBOS's: started workflows live on it


def run_tick() -> dict:
    """One tick, as DBOS would start it (the workflow function called directly, on a long-lived loop
    so the workflows it starts are not cancelled when the tick returns)."""
    return asyncio.run_coroutine_threadsafe(schedule_tick(datetime.now(timezone.utc), {}), _LOOP).result(60)


def _schedules(worker):
    from eap_run_store import ScheduleStore

    return ScheduleStore(worker.engine)


def _runs(worker, schedule_id: str) -> list[dict]:
    with worker.engine.connect() as conn:
        rows = conn.execute(
            text("SELECT id, tenant_id, workflow_id, workflow_version, status FROM runs WHERE schedule_id = :s"),
            {"s": schedule_id},
        ).mappings().all()
    return [dict(r) for r in rows]


@pytest.fixture
def make_schedule(worker, tenant):
    store = _schedules(worker)

    def make(*, next_fire_at: datetime, input=None, cron: str = "*/5 * * * *", spec=None) -> tuple[dict, str]:
        workflow_id, _ = publish_workflow(worker.engine, tenant, spec or _spec())
        schedule = store.create(
            tenant, workflow_id, name=f"s-{uuid.uuid4().hex[:6]}", cron=cron, timezone="UTC",
            input={"q": "hi"} if input is None else input, next_fire_at=next_fire_at,
        )
        return schedule, workflow_id

    yield make
    # Let the started runs finish before the tenant fixture deletes their rows.
    with worker.engine.connect() as conn:
        ids = [r[0] for r in conn.execute(text("SELECT id FROM runs WHERE tenant_id = :t"), {"t": tenant})]
    for run_id in ids:
        try:
            wait_for(lambda: (worker.run_store.get_run(tenant, uuid.UUID(run_id)) or {}).get("status")
                     in ("succeeded", "failed", "cancelled"), timeout=30, what="scheduled run end")
        except AssertionError:
            pass
    with worker.engine.begin() as conn:
        conn.execute(text("DELETE FROM workflow_schedules WHERE tenant_id = :t"), {"t": tenant})
    cleanup_definitions(worker.engine, tenant)


def _past(**kw) -> datetime:
    return datetime.now(timezone.utc) - timedelta(**kw)


def test_tick_creates_one_run_per_due_schedule_and_advances_next_fire(worker, tenant, make_schedule):
    due, workflow_id = make_schedule(next_fire_at=_past(minutes=1))
    future, _ = make_schedule(next_fire_at=datetime.now(timezone.utc) + timedelta(hours=1))
    run_tick()
    runs = _runs(worker, due["id"])
    assert len(runs) == 1 and runs[0]["tenant_id"] == tenant and runs[0]["workflow_id"] == workflow_id
    assert runs[0]["workflow_version"] == 1
    assert runs[0]["id"] == str(uuid.uuid5(uuid.UUID(due["id"]), due["next_fire_at"].astimezone(timezone.utc).isoformat()))
    assert _runs(worker, future["id"]) == []
    row = _schedules(worker).get(tenant, due["id"])
    assert row["next_fire_at"] > datetime.now(timezone.utc) and row["last_fired_at"] is not None
    assert row["active"] is True and row["last_error"] is None
    # The run really started on the worker.
    wait_for(lambda: (worker.run_store.get_run(tenant, uuid.UUID(runs[0]["id"])) or {}).get("status") == "completed",
             timeout=30, what="the scheduled run to complete")
    assert worker.run_store.get_run(tenant, uuid.UUID(runs[0]["id"]))["schedule_id"] == due["id"]


def test_tick_is_idempotent_for_the_same_fire_time(worker, tenant, make_schedule):
    schedule, _ = make_schedule(next_fire_at=_past(minutes=1))
    run_tick()
    first = [r["id"] for r in _runs(worker, schedule["id"])]
    # Second tick before next_fire_at: nothing due.
    run_tick()
    assert [r["id"] for r in _runs(worker, schedule["id"])] == first and len(first) == 1
    # A crash after the run was created but before mark_fired: the same fire time again, same run id.
    _schedules(worker).update(tenant, schedule["id"], next_fire_at=schedule["next_fire_at"])
    run_tick()
    again = _runs(worker, schedule["id"])
    assert [r["id"] for r in again] == first


def test_missed_window_fires_once(worker, tenant, make_schedule):
    schedule, _ = make_schedule(next_fire_at=_past(hours=3), cron="*/5 * * * *")
    run_tick()
    assert len(_runs(worker, schedule["id"])) == 1
    row = _schedules(worker).get(tenant, schedule["id"])
    assert row["next_fire_at"] > datetime.now(timezone.utc)
    run_tick()
    assert len(_runs(worker, schedule["id"])) == 1


def test_tick_deactivates_schedule_when_input_no_longer_validates(worker, tenant, make_schedule):
    schedule, _ = make_schedule(next_fire_at=_past(minutes=1), input={"q": 5})
    run_tick()
    row = _schedules(worker).get(tenant, schedule["id"])
    assert row["active"] is False and row["last_error"] == "input_schema_errors: q: type"  # never quotes the value
    assert _runs(worker, schedule["id"]) == []
    run_tick()
    assert _runs(worker, schedule["id"]) == []


def test_tick_deactivates_when_workflow_archived(worker, tenant, make_schedule):
    schedule, workflow_id = make_schedule(next_fire_at=_past(minutes=1))
    with worker.engine.begin() as conn:
        conn.execute(text("UPDATE workflows SET archived = true WHERE id = :w"), {"w": workflow_id})
    run_tick()
    row = _schedules(worker).get(tenant, schedule["id"])
    assert row["active"] is False and row["last_error"] == "workflow_archived"
    assert _runs(worker, schedule["id"]) == []


def test_tick_deactivates_when_no_published_version(worker, tenant, make_schedule):
    schedule, workflow_id = make_schedule(next_fire_at=_past(minutes=1))
    with worker.engine.begin() as conn:
        conn.execute(text("DELETE FROM workflow_versions WHERE workflow_id = :w"), {"w": workflow_id})
    run_tick()
    assert _schedules(worker).get(tenant, schedule["id"])["last_error"] == "no_published_version"


def test_tick_off_applies_no_schedule_and_config_from_env(worker, monkeypatch):
    from dbos import DBOS

    monkeypatch.setenv("EAP_SCHEDULE_TICK", "off")
    try:
        assert apply_schedule_tick() == "removed"
        assert DBOS.get_schedule(SCHEDULE_TICK_SCHEDULE_NAME) is None
        monkeypatch.delenv("EAP_SCHEDULE_TICK")
        assert apply_schedule_tick() == "applied"
        assert DBOS.get_schedule(SCHEDULE_TICK_SCHEDULE_NAME)["schedule"] == "0 * * * * *"
        monkeypatch.setenv("EAP_SCHEDULE_TICK", "not a cron")
        with pytest.raises(ScheduleTickConfigError):
            apply_schedule_tick()
    finally:
        DBOS.delete_schedule(SCHEDULE_TICK_SCHEDULE_NAME)


def test_two_concurrent_ticks_create_one_run(worker, tenant, make_schedule, monkeypatch):
    """The second tick overlaps the first MID-transaction (the first is held inside its fire step,
    holding the row locks): FOR UPDATE SKIP LOCKED makes the second claim zero rows, and next_fire_at
    advances exactly once."""
    import eap_worker.schedule_tick as st
    from eap_run_store import ScheduleStore

    schedule, _ = make_schedule(next_fire_at=_past(minutes=1))
    inside, release = threading.Event(), threading.Event()
    real_fire = st._fire
    marks: list[str] = []
    real_mark = ScheduleStore.mark_fired

    def held_fire(*args, **kwargs):
        inside.set()
        assert release.wait(30)
        return real_fire(*args, **kwargs)

    def counting_mark(self, schedule_id, *args, **kwargs):
        marks.append(schedule_id)
        return real_mark(self, schedule_id, *args, **kwargs)

    monkeypatch.setattr(st, "_fire", held_fire)
    monkeypatch.setattr(ScheduleStore, "mark_fired", counting_mark)
    results: list[dict] = []
    first = threading.Thread(target=lambda: results.append(run_tick()))
    first.start()
    try:
        assert inside.wait(30), "the first tick never reached the fire step"
        second = run_tick()  # overlaps: the first still holds the row lock
        assert second["due"] == 0 and second["fired"] == 0
    finally:
        release.set()
        first.join(60)
    assert results and results[0]["due"] == 1 and results[0]["fired"] == 1
    assert marks == [schedule["id"]]
    assert len(_runs(worker, schedule["id"])) == 1
    assert _schedules(worker).get(tenant, schedule["id"])["next_fire_at"] > datetime.now(timezone.utc)


def test_transient_db_error_leaves_schedule_active_and_next_tick_fires(worker, tenant, make_schedule, monkeypatch):
    import eap_worker.schedule_tick as st
    from sqlalchemy.exc import OperationalError

    monkeypatch.setattr(st, "_failures", {})
    schedule, _ = make_schedule(next_fire_at=_past(minutes=1))
    real_fire = st._fire
    calls = []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise OperationalError("SELECT 1", {}, Exception("connection reset"))
        return real_fire(*args, **kwargs)

    monkeypatch.setattr(st, "_fire", flaky)
    assert run_tick()["failed"] == 1
    row = _schedules(worker).get(tenant, schedule["id"])
    assert row["active"] is True and row["last_error"] is None and _runs(worker, schedule["id"]) == []
    assert run_tick()["fired"] == 1
    assert len(_runs(worker, schedule["id"])) == 1


def test_permanent_fire_failure_stops_schedule_after_five_ticks(worker, tenant, make_schedule, monkeypatch):
    import eap_worker.schedule_tick as st

    class RunIdConflict(Exception):
        pass

    monkeypatch.setattr(st, "_failures", {})

    def boom(*args, **kwargs):
        raise RunIdConflict("secret-value-123")

    monkeypatch.setattr(st, "_fire", boom)
    schedule, _ = make_schedule(next_fire_at=_past(minutes=1))
    for _ in range(4):
        run_tick()
        assert _schedules(worker).get(tenant, schedule["id"])["active"] is True
    run_tick()
    row = _schedules(worker).get(tenant, schedule["id"])
    assert row["active"] is False and row["last_error"] == "fire_failed: RunIdConflict"
    assert _runs(worker, schedule["id"]) == []


def test_late_tick_bursts_fire_once_and_next_fire_uses_the_real_clock(worker, tenant, make_schedule):
    schedule, _ = make_schedule(next_fire_at=_past(minutes=10), cron="*/5 * * * *")
    late = datetime.now(timezone.utc) - timedelta(minutes=10)  # DBOS recovered an old tick
    asyncio.run_coroutine_threadsafe(schedule_tick(late, {}), _LOOP).result(60)
    assert len(_runs(worker, schedule["id"])) == 1
    assert _schedules(worker).get(tenant, schedule["id"])["next_fire_at"] > datetime.now(timezone.utc)
    run_tick()
    assert len(_runs(worker, schedule["id"])) == 1


def test_six_transient_db_failures_never_stop_the_schedule(worker, tenant, make_schedule, monkeypatch):
    import eap_worker.schedule_tick as st
    from sqlalchemy.exc import InterfaceError, OperationalError

    monkeypatch.setattr(st, "_failures", {})
    schedule, _ = make_schedule(next_fire_at=_past(minutes=1))
    real_fire = st._fire
    calls = []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) <= 6:
            raise (OperationalError("SELECT 1", {}, Exception("down")) if len(calls) % 2
                   else InterfaceError("SELECT 1", {}, Exception("closed")))
        return real_fire(*args, **kwargs)

    monkeypatch.setattr(st, "_fire", flaky)
    for _ in range(6):
        assert run_tick()["failed"] == 1
        assert _schedules(worker).get(tenant, schedule["id"])["active"] is True
    assert st._failures == {}
    assert run_tick()["fired"] == 1


def test_stop_failure_does_not_block_other_schedules(worker, tenant, make_schedule, monkeypatch):
    import eap_worker.schedule_tick as st
    from eap_run_store import ScheduleStore

    bad, _ = make_schedule(next_fire_at=_past(minutes=2))
    good, _ = make_schedule(next_fire_at=_past(minutes=1))
    monkeypatch.setattr(st, "_failures", {bad["id"]: st.MAX_FIRE_FAILURES - 1})
    real_fire, real_stop = st._fire, ScheduleStore.stop

    def selective(session, store, schedule, now):
        if schedule["id"] == bad["id"]:
            raise ValueError("permanent")
        return real_fire(session, store, schedule, now)

    def broken_stop(self, schedule_id, *args, **kwargs):
        if schedule_id == bad["id"]:
            raise RuntimeError("stop failed")
        return real_stop(self, schedule_id, *args, **kwargs)

    monkeypatch.setattr(st, "_fire", selective)
    monkeypatch.setattr(ScheduleStore, "stop", broken_stop)
    result = run_tick()
    assert result["fired"] == 1 and result["failed"] == 1
    assert len(_runs(worker, good["id"])) == 1
    assert _schedules(worker).get(tenant, bad["id"])["active"] is True
    assert st._failures[bad["id"]] == st.MAX_FIRE_FAILURES - 1  # the streak stays


def test_failure_streak_entries_of_schedules_no_longer_due_are_dropped(worker, tenant, make_schedule, monkeypatch):
    import eap_worker.schedule_tick as st

    monkeypatch.setattr(st, "_failures", {"deleted-or-paused": 3})
    run_tick()
    assert "deleted-or-paused" not in st._failures


def test_integrity_error_is_permanent_and_stops_the_schedule(worker, tenant, make_schedule, monkeypatch):
    import eap_worker.schedule_tick as st
    from sqlalchemy.exc import IntegrityError

    monkeypatch.setattr(st, "_failures", {})

    def boom(*args, **kwargs):
        raise IntegrityError("INSERT", {}, Exception("duplicate"))

    monkeypatch.setattr(st, "_fire", boom)
    schedule, _ = make_schedule(next_fire_at=_past(minutes=1))
    for _ in range(5):
        run_tick()
    row = _schedules(worker).get(tenant, schedule["id"])
    assert row["active"] is False and row["last_error"] == "fire_failed: IntegrityError"


def test_wrapped_transient_error_is_not_counted(worker, tenant, make_schedule, monkeypatch):
    import eap_worker.schedule_tick as st
    from sqlalchemy.exc import OperationalError

    monkeypatch.setattr(st, "_failures", {})

    def wrapped(*args, **kwargs):
        try:
            raise OperationalError("SELECT 1", {}, Exception("down"))
        except OperationalError as exc:
            raise RuntimeError("fire failed") from exc

    monkeypatch.setattr(st, "_fire", wrapped)
    schedule, _ = make_schedule(next_fire_at=_past(minutes=1))
    for _ in range(6):
        run_tick()
    assert st._failures == {}
    assert _schedules(worker).get(tenant, schedule["id"])["active"] is True


def test_db_error_inside_stop_rolls_back_only_its_savepoint(worker, tenant, make_schedule, monkeypatch):
    import eap_worker.schedule_tick as st
    from eap_run_store import ScheduleStore

    bad, _ = make_schedule(next_fire_at=_past(minutes=2))
    good, _ = make_schedule(next_fire_at=_past(minutes=1))
    monkeypatch.setattr(st, "_failures", {bad["id"]: st.MAX_FIRE_FAILURES - 1})
    real_fire, real_stop = st._fire, ScheduleStore.stop

    def selective(session, store, schedule, now):
        if schedule["id"] == bad["id"]:
            raise ValueError("permanent")
        return real_fire(session, store, schedule, now)

    def aborting_stop(self, schedule_id, last_error, *, session=None):
        session.execute(text("SELECT 1/0"))  # aborts the transaction unless a savepoint isolates it

    monkeypatch.setattr(st, "_fire", selective)
    monkeypatch.setattr(ScheduleStore, "stop", aborting_stop)
    result = run_tick()
    assert result["fired"] == 1
    row = _schedules(worker).get(tenant, good["id"])
    assert row["last_fired_at"] is not None and row["next_fire_at"] > datetime.now(timezone.utc)
    assert len(_runs(worker, good["id"])) == 1


def test_streaks_survive_a_backlog_larger_than_the_claim_page(worker, tenant, make_schedule, monkeypatch):
    import eap_worker.schedule_tick as st

    first, _ = make_schedule(next_fire_at=_past(minutes=3))
    second, _ = make_schedule(next_fire_at=_past(minutes=2))
    monkeypatch.setattr(st, "_failures", {})
    monkeypatch.setattr(st, "CLAIM_LIMIT", 1)

    def boom(*args, **kwargs):
        raise ValueError("permanent")

    monkeypatch.setattr(st, "_fire", boom)
    run_tick()  # claims only `first`
    assert st._failures == {first["id"]: 1}
    st._failures[second["id"]] = 2  # tracked, still due, but off this tick's page
    run_tick()
    assert st._failures[second["id"]] == 2 and st._failures[first["id"]] == 2


# --------------------------------------------------------------------------- orphan sweep


def _queued_run(worker, tenant, schedule, workflow_id, *, scheduled: bool, start: bool) -> str:
    """A `queued` run created `ORPHAN` minutes ago. `start`: DBOS also knows the workflow (it has a
    workflow_status row), as for a legitimate backlog."""
    import json

    from dbos import DBOS, SetWorkflowID

    from eap_worker.workflow_run import workflow_run

    run_id = uuid.uuid4()
    worker.run_store.create_workflow_run(
        tenant, workflow_id, 1, json.dumps({"q": "hi"}), run_id, schedule_id=schedule["id"] if scheduled else None
    )
    if start:
        with SetWorkflowID(str(run_id)):
            asyncio.run_coroutine_threadsafe(
                DBOS.start_workflow_async(workflow_run, str(run_id), tenant, _version_id(worker, workflow_id),
                                          json.dumps({"q": "hi"})), _LOOP).result(30)
        wait_for(lambda: (worker.run_store.get_run(tenant, run_id) or {}).get("status") in ("completed", "failed"),
                 timeout=30, what="the started run to end")
    with worker.engine.begin() as conn:
        conn.execute(
            text("UPDATE runs SET status = 'queued', created_at = :c WHERE id = :i"),
            {"c": datetime.now(timezone.utc) - timedelta(minutes=10), "i": str(run_id)},
        )
    return str(run_id)


def _version_id(worker, workflow_id) -> str:
    with worker.engine.connect() as conn:
        return str(conn.execute(
            text("SELECT id FROM workflow_versions WHERE workflow_id = :w ORDER BY version DESC LIMIT 1"),
            {"w": workflow_id}).scalar_one())


def test_orphan_sweep_skips_known_workflows_and_non_scheduled_runs(worker, tenant, make_schedule):
    from eap_worker.schedule_tick import _orphans

    schedule, workflow_id = make_schedule(next_fire_at=datetime.now(timezone.utc) + timedelta(hours=1))
    known = _queued_run(worker, tenant, schedule, workflow_id, scheduled=True, start=True)
    orphan = _queued_run(worker, tenant, schedule, workflow_id, scheduled=True, start=False)
    plain = _queued_run(worker, tenant, schedule, workflow_id, scheduled=False, start=False)
    ids = {o["run_id"] for o in _orphans(datetime.now(timezone.utc))}
    assert orphan in ids
    assert known not in ids  # a workflow_status row exists: a legitimate backlog, not an orphan
    assert plain not in ids  # not a scheduled run


def test_orphan_sweep_is_not_starved_by_a_backlog_of_known_runs(worker, tenant, make_schedule, monkeypatch):
    import eap_worker.schedule_tick as st

    schedule, workflow_id = make_schedule(next_fire_at=datetime.now(timezone.utc) + timedelta(hours=1))
    orphan = _queued_run(worker, tenant, schedule, workflow_id, scheduled=True, start=False)
    known = _queued_run(worker, tenant, schedule, workflow_id, scheduled=True, start=True)
    # Make the known run older than the orphan and shrink the page so it alone would fill a page.
    with worker.engine.begin() as conn:
        conn.execute(text("UPDATE runs SET created_at = created_at - interval '1 hour' WHERE id = :i"), {"i": known})
    monkeypatch.setattr(st, "ORPHAN_PAGE", 1)
    ids = {o["run_id"] for o in st._orphans(datetime.now(timezone.utc))}
    assert ids == {orphan}


def test_tick_starts_an_orphan_but_not_a_known_queued_run(worker, tenant, make_schedule):
    schedule, workflow_id = make_schedule(next_fire_at=datetime.now(timezone.utc) + timedelta(hours=1))
    orphan = _queued_run(worker, tenant, schedule, workflow_id, scheduled=True, start=False)
    result = run_tick()
    assert result is not None
    wait_for(lambda: (worker.run_store.get_run(tenant, uuid.UUID(orphan)) or {}).get("status") in ("completed", "failed"),
             timeout=30, what="the orphan run to start and end")


def test_scheduled_run_that_returns_skipped_ends_skipped(worker, tenant, make_schedule, monkeypatch):
    monkeypatch.setitem(worker.__dict__, "allowed_tools", frozenset(worker.registry.tool_ids()))
    spec = _spec("Skip tick")
    spec["steps"] = [{"id": "now", "type": "tool", "tool_id": "workflow.return",
                      "input": {"value": {"status": "outside_slot"}, "outcome": "skipped"}}]
    schedule, _ = make_schedule(next_fire_at=datetime.now(timezone.utc) - timedelta(minutes=1), spec=spec)
    run_tick()
    (run,) = _runs(worker, schedule["id"])
    ended = wait_for(
        lambda: (r if (r := worker.run_store.get_run(tenant, uuid.UUID(run["id"])))
                 and r["status"] in ("completed", "skipped", "failed") else None),
        timeout=60, what="scheduled run end",
    )
    assert ended["status"] == "skipped" and "outside_slot" in ended["output"]
    last = _schedules(worker).last_runs(tenant, [schedule["id"]])[schedule["id"]]
    assert last["status"] == "skipped" and last["detail"] == "outside_slot"
