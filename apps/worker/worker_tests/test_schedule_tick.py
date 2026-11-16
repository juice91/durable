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


def run_tick() -> None:
    """One tick, as DBOS would start it (the workflow function called directly, on a long-lived loop
    so the workflows it starts are not cancelled when the tick returns)."""
    asyncio.run_coroutine_threadsafe(schedule_tick(datetime.now(timezone.utc), {}), _LOOP).result(60)


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
    assert row["active"] is False and row["last_error"].startswith("input_schema_errors")
    assert "5" not in row["last_error"].split(":", 1)[1]  # never quotes the value
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


def test_two_concurrent_ticks_create_one_run(worker, tenant, make_schedule):
    """claim and mark_fired share one transaction: the second tick skips the locked row (or finds it
    advanced), so one run exists even when two ticks overlap."""
    schedules = [make_schedule(next_fire_at=_past(minutes=1))[0] for _ in range(3)]
    errors: list[BaseException] = []

    def tick() -> None:
        try:
            run_tick()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=tick) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors
    for schedule in schedules:
        assert len(_runs(worker, schedule["id"])) == 1
