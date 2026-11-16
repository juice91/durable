"""Run options on the row (0010_run_options): absent JSONB options are SQL NULL, so
ck_runs_kind_target accepts published-version runs; a draft run has a snapshot and no version."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError


@pytest.fixture
def tenant(engine):
    tenant_id = f"test_tenant_{uuid4().hex[:8]}"
    yield tenant_id
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM execution_events WHERE tenant_id = :t"), {"t": tenant_id})
        conn.execute(text("DELETE FROM runs WHERE tenant_id = :t"), {"t": tenant_id})
        conn.execute(text("DELETE FROM conversations WHERE tenant_id = :t"), {"t": tenant_id})


def _nulls(engine, run_id) -> tuple:
    with engine.connect() as conn:
        return conn.execute(text(
            "SELECT request_context IS NULL, draft_spec IS NULL, agent_version, model_id, model_overridden"
            " FROM runs WHERE id = :r"
        ), {"r": str(run_id)}).one()


def test_published_run_without_options_stores_sql_null(run_store, engine, tenant):
    run_id = uuid4()
    _, created = run_store.create_run(tenant, uuid4(), 1, "hi", run_id)
    assert created
    assert tuple(_nulls(engine, run_id)) == (True, True, 1, None, False)


def test_empty_context_is_sql_null_and_values_are_stored(run_store, engine, tenant):
    empty, full = uuid4(), uuid4()
    run_store.create_run(tenant, uuid4(), 1, "hi", empty, request_context={}, model_id="test:echo")
    run_store.create_run(tenant, uuid4(), 2, "hi", full, request_context={"plan": "pro"},
                         model_id="test:history", model_overridden=True)
    assert tuple(_nulls(engine, empty)) == (True, True, 1, "test:echo", False)
    assert tuple(_nulls(engine, full)) == (False, True, 2, "test:history", True)
    run = run_store.get_run(tenant, full)
    assert run["request_context"] == {"plan": "pro"} and run["draft_spec"] is None


def test_draft_run_satisfies_check_and_needs_exactly_one_target(run_store, engine, tenant):
    draft = {"name": "A", "model": {"provider": "test", "name": "echo"}}
    run_id = uuid4()
    _, created = run_store.create_run(tenant, uuid4(), None, "hi", run_id, draft_spec=draft)
    assert created
    assert tuple(_nulls(engine, run_id))[:3] == (True, False, None)
    assert run_store.get_run(tenant, run_id)["draft_spec"] == draft
    with pytest.raises(IntegrityError, match="ck_runs_kind_target"):  # neither a version nor a draft
        run_store.create_run(tenant, uuid4(), None, "hi", uuid4())
    with pytest.raises(IntegrityError, match="ck_runs_kind_target"):  # both
        run_store.create_run(tenant, uuid4(), 3, "hi", uuid4(), draft_spec=draft)


def test_draft_turn_and_published_turn(run_store, engine, tenant):
    agent_id = uuid4()
    conv, _ = run_store.create_conversation(tenant, agent_id, None, None)
    first = uuid4()
    run_store.create_turn(tenant, conv["id"], 1, "one", first)
    assert tuple(_nulls(engine, first))[:3] == (True, True, 1)
    run_store.finish_with_end(tenant, first, "completed", output="ok")
    second = uuid4()
    run_store.create_turn(tenant, conv["id"], None, "two", second, draft_spec={"name": "A"},
                          request_context={"x": 1})
    assert tuple(_nulls(engine, second))[:3] == (False, False, None)
