"""RunStore.mark_running for conversation turns (input hardening review, finding 1): under the
conversation lock, a turn overtaken by a later turn is left failed and False is returned; any
other run is set running (clearing ENQUEUE_FAILED_ERROR) and True is returned."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text

from eap_run_store import ENQUEUE_FAILED_ERROR, RunNotFound, TurnInProgress


@pytest.fixture
def cleanup(engine, test_tenant):
    yield
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM runs WHERE tenant_id = :t"), {"t": test_tenant})
        conn.execute(text("DELETE FROM conversations WHERE tenant_id = :t"), {"t": test_tenant})


def _status(store, tenant, run_id):
    run = store.get_run(tenant, run_id)
    return run["status"], run["error"]


def test_overtaken_turn_stays_failed(run_store, test_tenant, cleanup):
    conv, _ = run_store.create_conversation(test_tenant, uuid4(), "c")
    first, _ = run_store.create_turn(test_tenant, conv["id"], 1, "a", uuid4())
    assert run_store.mark_enqueue_failed(test_tenant, first)
    second, _ = run_store.create_turn(test_tenant, conv["id"], 1, "b", uuid4())
    assert run_store.mark_running(test_tenant, first) is False
    assert _status(run_store, test_tenant, first) == ("failed", ENQUEUE_FAILED_ERROR)
    assert _status(run_store, test_tenant, second) == ("queued", None)


def test_latest_failed_turn_is_revived_and_then_blocks_the_next(run_store, test_tenant, cleanup):
    conv, _ = run_store.create_conversation(test_tenant, uuid4(), "c")
    first, _ = run_store.create_turn(test_tenant, conv["id"], 1, "a", uuid4())
    assert run_store.mark_enqueue_failed(test_tenant, first)
    assert run_store.mark_running(test_tenant, first) is True
    assert _status(run_store, test_tenant, first) == ("running", None)
    with pytest.raises(TurnInProgress):
        run_store.create_turn(test_tenant, conv["id"], 1, "b", uuid4())


def test_runs_without_a_conversation_are_unaffected(run_store, test_tenant, cleanup):
    run_id, _ = run_store.create_run(test_tenant, uuid4(), 1, "x", uuid4())
    assert run_store.mark_enqueue_failed(test_tenant, run_id)
    assert run_store.mark_running(test_tenant, run_id) is True
    assert _status(run_store, test_tenant, run_id) == ("running", None)
    tool_run, _ = run_store.create_tool_run(test_tenant, "clock.now", "{}", uuid4())
    assert run_store.mark_running(test_tenant, tool_run) is True


def test_other_tenant_or_unknown_run_is_not_found(run_store, test_tenant, cleanup):
    run_id, _ = run_store.create_run(test_tenant, uuid4(), 1, "x", uuid4())
    with pytest.raises(RunNotFound):
        run_store.mark_running("someone-else", run_id)
    with pytest.raises(RunNotFound):
        run_store.mark_running(test_tenant, uuid4())
