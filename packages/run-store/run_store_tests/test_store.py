"""Tests for the Postgres store."""

import threading
import time
from uuid import uuid4

import pytest

from eap_schemas import RunEvent, RunEventType
from eap_run_store import (
    EventLog,
    PostgresSideEffectStore,
    RunIdConflict,
    RunStore,
)

# Import cleanup functions from conftest
from .conftest import cleanup_side_effects_by_tenant


@pytest.fixture
def test_tenant():
    """Generate a unique tenant_id for each test."""
    return f"test_tenant_{uuid4().hex[:8]}"


@pytest.fixture
def run_store(engine, test_tenant):
    """Create a RunStore for testing."""
    return RunStore(engine)


@pytest.fixture
def event_log(engine):
    """Create an EventLog for testing."""
    return EventLog(engine)


@pytest.fixture
def side_effect_store(engine, test_tenant):
    """Create a PostgresSideEffectStore for testing."""
    return PostgresSideEffectStore(engine, test_tenant)


# ============ RunStore Tests ============

def test_create_run_idempotent_on_run_id(run_store, test_tenant):
    """Run create is idempotent: same run_id returns (id, True) then (id, False), one row."""
    agent_id = uuid4()
    run_id = uuid4()
    
    # First create
    result1, created1 = run_store.create_run(
        tenant_id=test_tenant,
        agent_id=agent_id,
        agent_version=1,
        input="test input",
        run_id=run_id
    )
    
    # Second create with same run_id
    result2, created2 = run_store.create_run(
        tenant_id=test_tenant,
        agent_id=agent_id,
        agent_version=1,
        input="test input",
        run_id=run_id
    )
    
    assert created1 is True
    assert created2 is False
    assert result1 == result2 == run_id
    
    # Verify only one row exists
    run = run_store.get_run(test_tenant, run_id)
    assert run is not None
    assert run["id"] == str(run_id)


def test_create_run_idempotent_different_tenant_raises(run_store, test_tenant):
    """Same run_id under another tenant -> RunIdConflict."""
    agent_id = uuid4()
    run_id = uuid4()
    
    # First create for first tenant
    run_store.create_run(
        tenant_id=test_tenant,
        agent_id=agent_id,
        agent_version=1,
        input="test input",
        run_id=run_id
    )
    
    # Second create for different tenant should raise
    other_tenant = test_tenant.replace("test_tenant", "other_tenant")
    
    with pytest.raises(RunIdConflict) as exc_info:
        run_store.create_run(
            tenant_id=other_tenant,
            agent_id=agent_id,
            agent_version=1,
            input="test input",
            run_id=run_id
        )
    
    assert exc_info.value.run_id == run_id
    
    # Verify only one row exists
    run1 = run_store.get_run(test_tenant, run_id)
    run2 = run_store.get_run(other_tenant, run_id)
    assert run1 is not None
    assert run2 is None


def test_set_status_round_trip(run_store, test_tenant):
    """set_status correctly updates run and get_run retrieves it."""
    agent_id = uuid4()
    run_id = uuid4()
    
    # First create
    run_store.create_run(
        tenant_id=test_tenant,
        agent_id=agent_id,
        agent_version=1,
        input="test input",
        run_id=run_id
    )
    
    # Update status to running
    run_store.set_status(test_tenant, run_id, "running")
    run = run_store.get_run(test_tenant, run_id)
    assert run["status"] == "running"
    
    # Update status to completed with output
    run_store.finish_with_end(test_tenant, run_id, "completed", output="test output")
    run = run_store.get_run(test_tenant, run_id)
    assert run["status"] == "completed"
    assert run["output"] == "test output"
    
    # A terminal run stays terminal (loops D12): its end fixes the status, so failed is refused
    assert run_store.finish_with_end(test_tenant, run_id, "failed", error="test error") is False
    run = run_store.get_run(test_tenant, run_id)
    assert run["status"] == "completed"
    assert run["error"] is None


def test_cross_tenant_get_run_returns_none(run_store, test_tenant):
    """Cross-tenant get_run returns None for another tenant's run."""
    agent_id = uuid4()
    other_tenant = test_tenant.replace("test_tenant", "other_tenant")
    run_id = uuid4()
    
    # Create run for first tenant
    run_store.create_run(
        tenant_id=test_tenant,
        agent_id=agent_id,
        agent_version=1,
        input="test input",
        run_id=run_id
    )
    
    # Try to get from another tenant
    result = run_store.get_run(other_tenant, run_id)
    assert result is None


def test_heartbeat_and_display_status(run_store, test_tenant):
    """heartbeat: running + heartbeat 30 s old -> display_status 'recovering'; fresh -> 'running'."""
    agent_id = uuid4()
    run_id = uuid4()
    
    # Create run and set to running
    run_store.create_run(
        tenant_id=test_tenant,
        agent_id=agent_id,
        agent_version=1,
        input="test input",
        run_id=run_id
    )
    run_store.set_status(test_tenant, run_id, "running")
    
    # Fresh heartbeat -> display_status should be 'running'
    run = run_store.get_run(test_tenant, run_id)
    assert run["status"] == "running"
    assert run["display_status"] == "running"
    
    # Set heartbeat_at to 30 seconds ago (simulate old heartbeat)
    from eap_run_store import RunModel
    from datetime import datetime, timedelta, timezone
    old_heartbeat = datetime.now(timezone.utc) - timedelta(seconds=30)
    with run_store.engine.begin() as conn:
        conn.execute(
            RunModel.__table__.update()
            .where(RunModel.id == str(run_id))
            .values(heartbeat_at=old_heartbeat)
        )
    
    # Now display_status should be 'recovering'
    run = run_store.get_run(test_tenant, run_id)
    assert run["status"] == "running"
    assert run["display_status"] == "recovering"
    
    # Now do a real heartbeat
    run_store.heartbeat(test_tenant, run_id)
    run = run_store.get_run(test_tenant, run_id)
    assert run["status"] == "running"
    assert run["display_status"] == "running"


# ============ EventLog Tests ============

def test_sequential_appends_get_seq_0_1_2(event_log, run_store, test_tenant):
    """3 sequential appends get seq 0, 1, 2."""
    agent_id = uuid4()
    run_id = uuid4()
    
    run_store.create_run(
        tenant_id=test_tenant,
        agent_id=agent_id,
        agent_version=1,
        input="test input",
        run_id=run_id
    )
    
    event1, created1 = event_log.append(test_tenant, run_id, "start", "run_started", {"step": 1})
    event2, created2 = event_log.append(test_tenant, run_id, "a", "text_delta", {"text": "hello"})
    event3, created3 = event_log.append(test_tenant, run_id, "b", "tool_call", {"tool": "test_tool"})
    
    assert created1 is True
    assert created2 is True
    assert created3 is True
    assert event1.seq == 0
    assert event2.seq == 1
    assert event3.seq == 2
    
    assert event1.run_id == run_id
    assert event2.run_id == run_id
    assert event3.run_id == run_id


def test_append_duplicate_event_key_no_seq_gap(event_log, run_store, test_tenant):
    """append 'a' again -> created=False, seq unchanged, and next new key gets next seq (no gap)."""
    agent_id = uuid4()
    run_id = uuid4()
    
    run_store.create_run(
        tenant_id=test_tenant,
        agent_id=agent_id,
        agent_version=1,
        input="test input",
        run_id=run_id
    )
    
    # Append 'start', 'a', 'b' -> seq 0, 1, 2
    event_log.append(test_tenant, run_id, "start", "run_started", {})
    event_a, created_a = event_log.append(test_tenant, run_id, "a", "text_delta", {"index": 0})
    event_b, created_b = event_log.append(test_tenant, run_id, "b", "tool_call", {})
    
    assert created_a is True
    assert created_b is True
    assert event_a.seq == 1  # 'a' is the second append
    assert event_b.seq == 2  # 'b' is the third append
    
    # Re-append 'a' - should not consume a seq
    event_a_again, created_a_again = event_log.append(test_tenant, run_id, "a", "text_delta", {"index": 0})
    assert created_a_again is False
    assert event_a_again.seq == 1  # seq unchanged
    
    # Append 'c' - should get seq 3 (not 4)
    event_c, created_c = event_log.append(test_tenant, run_id, "c", "tool_result", {})
    assert created_c is True
    assert event_c.seq == 3  # no gap


def test_concurrent_appends_distinct_keys_no_gaps(event_log, run_store, test_tenant):
    """20 threads appending distinct keys -> seq exactly 0..19."""
    agent_id = uuid4()
    run_id = uuid4()
    
    run_store.create_run(
        tenant_id=test_tenant,
        agent_id=agent_id,
        agent_version=1,
        input="test input",
        run_id=run_id
    )
    
    events = []
    lock = threading.Lock()
    
    def append_event(i):
        event_key = f"event_{i}"
        event, created = event_log.append(test_tenant, run_id, event_key, "text_delta", {"index": i})
        with lock:
            events.append(event)
    
    threads = [threading.Thread(target=append_event, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    
    seqs = sorted([e.seq for e in events])
    assert seqs == list(range(20)), f"Expected seqs 0..19, got {seqs}"


def test_concurrent_appends_same_key_one_created(event_log, run_store, test_tenant):
    """20 threads appending the SAME key -> exactly one created=True, one row."""
    agent_id = uuid4()
    run_id = uuid4()
    
    run_store.create_run(
        tenant_id=test_tenant,
        agent_id=agent_id,
        agent_version=1,
        input="test input",
        run_id=run_id
    )
    
    results = []
    lock = threading.Lock()
    
    def append_same_key():
        event, created = event_log.append(test_tenant, run_id, "same_key", "text_delta", {})
        with lock:
            results.append((event, created))
    
    threads = [threading.Thread(target=append_same_key) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    
    created_count = sum(1 for _, created in results if created)
    assert created_count == 1, f"Expected exactly 1 created=True, got {created_count}"
    
    # Verify only one row in database
    from eap_run_store import ExecutionEventModel
    with event_log.engine.begin() as conn:
        count = conn.execute(
            ExecutionEventModel.__table__.select().where(
                ExecutionEventModel.run_id == str(run_id),
                ExecutionEventModel.event_key == "same_key"
            )
        ).rowcount
    assert count == 1, f"Expected exactly 1 row in database, got {count}"


def test_list_events_after_seq(event_log, run_store, test_tenant):
    """list_events(after_seq=1) returns seq>=2."""
    agent_id = uuid4()
    run_id = uuid4()
    
    run_store.create_run(
        tenant_id=test_tenant,
        agent_id=agent_id,
        agent_version=1,
        input="test input",
        run_id=run_id
    )
    
    event_log.append(test_tenant, run_id, "start", "run_started", {})
    event_log.append(test_tenant, run_id, "a", "text_delta", {"index": 0})
    event_log.append(test_tenant, run_id, "b", "text_delta", {"index": 1})
    event_log.append(test_tenant, run_id, "c", "text_delta", {"index": 2})
    
    events = event_log.list_events(test_tenant, run_id, after_seq=1)
    
    assert len(events) == 2
    assert events[0].seq == 2
    assert events[1].seq == 3


def test_cross_tenant_list_events_is_empty(event_log, run_store, test_tenant):
    """Cross-tenant list_events is empty."""
    agent_id = uuid4()
    other_tenant = test_tenant.replace("test_tenant", "other_tenant")
    run_id = uuid4()
    
    run_store.create_run(
        tenant_id=test_tenant,
        agent_id=agent_id,
        agent_version=1,
        input="test input",
        run_id=run_id
    )
    
    event_log.append(test_tenant, run_id, "start", "run_started", {})
    
    events = event_log.list_events(other_tenant, run_id)
    assert events == []


# ============ Tenant ownership on mutations ============


def _snapshot(engine, run_id):
    from sqlalchemy import text

    with engine.connect() as conn:
        run = conn.execute(
            text("SELECT tenant_id, status, output, error, heartbeat_at, updated_at FROM runs WHERE id = :r"),
            {"r": str(run_id)},
        ).one()
        events = conn.execute(
            text("SELECT tenant_id, seq, event_key, type, data FROM execution_events WHERE run_id = :r ORDER BY seq"),
            {"r": str(run_id)},
        ).all()
    return tuple(run), [tuple(e) for e in events]


@pytest.fixture
def owned_run(run_store, event_log, test_tenant):
    run_id = uuid4()
    run_store.create_run(test_tenant, uuid4(), 1, "owned input", run_id)
    run_store.set_status(test_tenant, run_id, "running")
    run_store.heartbeat(test_tenant, run_id)
    event_log.append(test_tenant, run_id, "start", "run_started", {"input": "owned input"})
    return run_id


def test_set_status_other_tenant_raises_and_changes_nothing(run_store, test_tenant, owned_run):
    from eap_run_store import RunNotFound

    before = _snapshot(run_store.engine, owned_run)
    with pytest.raises(RunNotFound):
        run_store.set_status("intruder_" + test_tenant, owned_run, "completed", output="pwned", error="x")
    assert _snapshot(run_store.engine, owned_run) == before
    assert run_store.get_run(test_tenant, owned_run)["status"] == "running"


def test_heartbeat_other_tenant_raises_and_changes_nothing(run_store, test_tenant, owned_run):
    from eap_run_store import RunNotFound

    before = _snapshot(run_store.engine, owned_run)
    with pytest.raises(RunNotFound):
        run_store.heartbeat("intruder_" + test_tenant, owned_run)
    assert _snapshot(run_store.engine, owned_run) == before


def test_append_other_tenant_raises_and_writes_nothing(event_log, run_store, test_tenant, owned_run):
    from eap_run_store import RunNotFound

    before = _snapshot(run_store.engine, owned_run)
    with pytest.raises(RunNotFound):
        event_log.append("intruder_" + test_tenant, owned_run, "end", "run_completed", {"output": "pwned"})
    # Even a key that already exists under the owner is not readable/replayable by another tenant.
    with pytest.raises(RunNotFound):
        event_log.append("intruder_" + test_tenant, owned_run, "start", "run_started", {})
    assert _snapshot(run_store.engine, owned_run) == before


def test_mutations_on_missing_run_raise(run_store, event_log, test_tenant):
    from eap_run_store import RunNotFound

    missing = uuid4()
    with pytest.raises(RunNotFound):
        run_store.set_status(test_tenant, missing, "running")
    with pytest.raises(RunNotFound):
        run_store.heartbeat(test_tenant, missing)
    with pytest.raises(RunNotFound):
        event_log.append(test_tenant, missing, "start", "run_started", {})


def test_mutations_require_tenant(run_store, event_log, owned_run):
    with pytest.raises(ValueError):
        run_store.set_status("", owned_run, "completed")
    with pytest.raises(ValueError):
        run_store.heartbeat("", owned_run)
    with pytest.raises(ValueError):
        event_log.append("", owned_run, "x", "run_started", {})


def test_events_carry_the_runs_tenant(event_log, run_store, test_tenant, owned_run):
    event_log.append(test_tenant, owned_run, "a", "model_message", {"text": "hi"})
    _, events = _snapshot(run_store.engine, owned_run)
    assert [e[0] for e in events] == [test_tenant, test_tenant]


def test_event_tenant_must_match_run_tenant_at_db_level(run_store, test_tenant, owned_run):
    """The composite (tenant_id, run_id) FK rejects an event row whose tenant is not the run's."""
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        with run_store.engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO execution_events (tenant_id, run_id, seq, event_key, type, data, ts) "
                    "VALUES (:t, :r, 99, 'forged', 'run_completed', '{}'::jsonb, now())"
                ),
                {"t": "intruder_" + test_tenant, "r": str(owned_run)},
            )


# ============ PostgresSideEffectStore Tests ============

def test_side_effect_reserve_first_time_returns_new(event_log, side_effect_store, test_tenant):
    """reserve -> ('new', None) on first call."""
    key = "test_key"
    
    state, result = side_effect_store.reserve(key)
    
    assert state == "new"
    assert result is None


def test_side_effect_reserve_twice_returns_reserved(event_log, side_effect_store, test_tenant):
    """Second reserve -> ('reserved', None)."""
    key = "test_key"
    
    state1, result1 = side_effect_store.reserve(key)
    state2, result2 = side_effect_store.reserve(key)
    
    assert state1 == "new"
    assert result1 is None
    assert state2 == "reserved"
    assert result2 is None


def test_side_effect_complete(event_log, side_effect_store, test_tenant):
    """complete; reserve -> ('completed', result)."""
    key = "test_key"
    result_data = {"output": "success"}
    
    # First reserve
    state1, _ = side_effect_store.reserve(key)
    assert state1 == "new"
    
    # Complete
    side_effect_store.complete(key, result_data)
    
    # Reserve again
    state2, result2 = side_effect_store.reserve(key)
    assert state2 == "completed"
    assert result2 == result_data


def test_side_effect_complete_unknown_key_raises(event_log, side_effect_store, test_tenant):
    """complete on unknown key raises ValueError."""
    key = "unknown_key"
    
    with pytest.raises(ValueError) as exc_info:
        side_effect_store.complete(key, {"output": "test"})
    
    assert "not found" in str(exc_info.value).lower() or "not reserved" in str(exc_info.value).lower()


def test_side_effect_complete_non_reserved_key_raises(event_log, side_effect_store, test_tenant):
    """complete on already-completed key raises."""
    key = "test_key"
    
    # Reserve and complete
    side_effect_store.reserve(key)
    side_effect_store.complete(key, {"output": "test"})
    
    # Try to complete again
    with pytest.raises(ValueError) as exc_info:
        side_effect_store.complete(key, {"output": "test2"})
    
    assert "not reserved" in str(exc_info.value).lower()


def test_concurrent_side_effect_reserve_one_new(event_log, side_effect_store, test_tenant):
    """10 threads reserving one key -> exactly one 'new'."""
    key = "concurrent_key"
    
    results = []
    lock = threading.Lock()
    
    def reserve_side_effect():
        state, result = side_effect_store.reserve(key)
        with lock:
            results.append((state, result))
    
    threads = [threading.Thread(target=reserve_side_effect) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    
    new_count = sum(1 for state, _ in results if state == "new")
    assert new_count == 1, f"Expected exactly 1 'new', got {new_count}"
    
    reserved_count = sum(1 for state, _ in results if state == "reserved")
    assert reserved_count == 9, f"Expected 9 'reserved', got {reserved_count}"


def test_side_effect_different_tenants_independent(event_log, engine, run_store):
    """Same key under two tenants -> both 'new'."""
    tenant1 = f"test_tenant_{uuid4().hex[:8]}"
    tenant2 = f"test_tenant_{uuid4().hex[:8]}"
    key = "shared_key"
    
    store1 = PostgresSideEffectStore(engine, tenant1)
    store2 = PostgresSideEffectStore(engine, tenant2)
    
    state1, _ = store1.reserve(key)
    state2, _ = store2.reserve(key)
    
    assert state1 == "new"
    assert state2 == "new"
    
    # Clean up
    cleanup_side_effects_by_tenant(engine, tenant1, key)
    cleanup_side_effects_by_tenant(engine, tenant2, key)


def test_concurrent_side_effect_reserve_distinct_keys(event_log, side_effect_store, test_tenant):
    """10 threads reserving distinct keys -> all 10 'new'."""
    results = []
    lock = threading.Lock()
    
    def reserve_side_effect(i):
        key = f"key_{i}"
        state, result = side_effect_store.reserve(key)
        with lock:
            results.append((key, state, result))
    
    threads = [threading.Thread(target=reserve_side_effect, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    
    new_count = sum(1 for _, state, _ in results if state == "new")
    assert new_count == 10, f"Expected all 10 to be 'new', got {new_count}"


# ============ list_runs cursor ============

def test_list_runs_cursor_survives_tied_created_at(run_store, engine, test_tenant):
    """(created_at, id) keyset paging: 7 rows sharing one timestamp, pages of 3, each row once."""
    from datetime import datetime, timezone

    from sqlalchemy import text

    from eap_run_store import RunCursor

    agent_id = uuid4()
    ids = [str(run_store.create_run(test_tenant, agent_id, 1, "x", uuid4())[0]) for _ in range(7)]
    tied = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with engine.begin() as conn:
        conn.execute(text("UPDATE runs SET created_at = :t WHERE id = ANY(:r)"), {"t": tied, "r": ids})

    seen, before = [], None
    while True:
        page = run_store.list_runs(test_tenant, limit=3, before=before)
        seen += [str(r["id"]) for r in page]
        if len(page) < 3:
            break
        before = RunCursor.after(page[-1])
    assert seen == sorted(ids, reverse=True)
    # Another tenant's position cursor never yields this tenant's rows.
    assert run_store.list_runs("other_" + test_tenant, limit=50, before=RunCursor(tied, ids[0])) == []


def test_run_cursor_round_trips_and_rejects_garbage():
    from datetime import datetime, timezone

    from eap_run_store import InvalidRunCursor, RunCursor

    cursor = RunCursor(datetime(2026, 1, 1, 12, 30, 1, 123456, tzinfo=timezone.utc), str(uuid4()))
    token = cursor.encode()
    assert RunCursor.decode(token) == cursor
    assert all(c.isalnum() or c in "-_" for c in token)  # URL-safe, no padding
    for bad in ("", "!!!", "2026-01-01T00:00:00+00:00", token[:-4], token + "x" * 600, "e30"):
        with pytest.raises(InvalidRunCursor):
            RunCursor.decode(bad)


def test_list_runs_recovering_filter_matches_stale_running_runs(run_store, test_tenant):
    """`recovering` is a display status: the list filter maps it to running + heartbeat > 20 s old."""
    from datetime import datetime, timedelta, timezone

    from eap_run_store import RunModel

    agent_id = uuid4()
    stale, fresh, done = uuid4(), uuid4(), uuid4()
    for rid in (stale, fresh, done):
        run_store.create_run(tenant_id=test_tenant, agent_id=agent_id, agent_version=1, input="x", run_id=rid)
    run_store.set_status(test_tenant, stale, "running")
    run_store.set_status(test_tenant, fresh, "running")
    run_store.finish_with_end(test_tenant, done, "completed")
    run_store.heartbeat(test_tenant, fresh)
    with run_store.engine.begin() as conn:
        conn.execute(
            RunModel.__table__.update()
            .where(RunModel.id == str(stale))
            .values(heartbeat_at=datetime.now(timezone.utc) - timedelta(seconds=30))
        )

    def ids(status):
        return {r["id"] for r in run_store.list_runs(test_tenant, status=status)}

    assert ids("recovering") == {str(stale)}
    assert all(r["display_status"] == "recovering" for r in run_store.list_runs(test_tenant, status="recovering"))
    assert ids(["recovering", "completed"]) == {str(stale), str(done)}
    assert ids("running") == {str(stale), str(fresh)}
    assert ids("recovering") <= ids(None)
    assert run_store.list_runs("other_" + test_tenant, status="recovering") == []
