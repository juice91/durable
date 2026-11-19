"""Optimistic concurrency (0033_revisions): every definition-class store bumps `revision` on a write and
refuses a stale `expected_revision` without writing anything."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text

from eap_run_store import RevisionMismatch, ScheduleStore
from eap_run_store.definitions import (
    publish_agent,
    publish_workflow,
    restore_agent_version,
    set_agent_archived,
    set_workflow_archived,
)
from eap_run_store.evals import EvalStore
from eap_run_store.kb_sources import ConnectionStore, SourceStore
from eap_run_store.revisions import bump_revision, check_expected
from eap_run_store.scorers import ScorerModel, ScorerStore
from eap_schemas import AgentSpec

T0 = datetime(1970, 1, 1, tzinfo=timezone.utc)
SPEC = AgentSpec.model_validate(
    {"name": "a", "instructions": "hi", "model": {"provider": "test", "name": "echo"}}
).model_dump(mode="json")
EVALUATOR = {"kind": "equals_expected"}
SCORER = {"kind": "contains", "value": "x"}


@pytest.fixture
def tenant(engine):
    t = f"test_tenant_{uuid4().hex[:8]}"
    yield t
    with engine.begin() as conn:
        for table in ("workflow_schedules", "eval_suites", "scorers", "kb_sources", "settings_connections",
                      "agent_versions", "workflow_versions", "agents", "workflows"):
            conn.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": t})


def test_check_expected_and_mismatch_carries_the_current_revision():
    check_expected(3, None)
    check_expected(3, 3)
    with pytest.raises(RevisionMismatch) as info:
        check_expected(3, 2)
    assert info.value.current_revision == 3 and info.value.expected == 2


def test_scorer_update_bumps_and_refuses_stale(engine, tenant):
    store = ScorerStore(engine)
    made = store.create(tenant, "s1", SCORER)
    assert made.revision == 1
    out = store.update(tenant, made.id, description="d", set_description=True, expected_revision=1)
    assert out.revision == 2 and out.description == "d"
    with pytest.raises(RevisionMismatch) as info:
        store.update(tenant, made.id, description="stale", set_description=True, expected_revision=1)
    assert info.value.current_revision == 2
    assert store.get(tenant, made.id).description == "d"  # nothing written
    assert store.update(tenant, made.id, description="blind", set_description=True).revision == 3  # no precondition


def test_schedule_update_stop_and_fire(engine, tenant):
    store = ScheduleStore(engine)
    row = store.create(tenant, "wf", name="n", cron="* * * * *", timezone="UTC", input={}, next_fire_at=T0)
    assert row["revision"] == 1
    out = store.update(tenant, row["id"], expected_revision=1, active=False)
    assert out["revision"] == 2
    with pytest.raises(RevisionMismatch):
        store.update(tenant, row["id"], expected_revision=1, name="x")
    assert store.get(tenant, row["id"])["name"] == "n"
    store.mark_fired(row["id"], T0, T0)  # a runtime tick is not an edit: the editor stays current
    assert store.get(tenant, row["id"])["revision"] == 2
    store.stop(row["id"], "bad input")  # a stop changes what the editor shows
    assert store.get(tenant, row["id"])["revision"] == 3
    assert store.update(tenant, "00000000-0000-0000-0000-000000000000", expected_revision=1, name="x") is None


def test_eval_suite_and_case_revisions(engine, tenant):
    store = EvalStore(engine)
    suite = store.create_suite(tenant, "ag", "suite", evaluators=[EVALUATOR])
    case = store.add_case(tenant, suite.id, {"name": "c", "input": "i", "expected_output": 1})
    assert suite.revision == 1 and case.revision == 1
    assert store.update_suite(tenant, suite.id, description="d", expected_revision=1).revision == 2
    with pytest.raises(RevisionMismatch) as info:
        store.update_suite(tenant, suite.id, description="stale", expected_revision=1)
    assert info.value.current_revision == 2
    assert store.get_suite(tenant, suite.id).description == "d"
    # a case edit does not move the suite's revision, and has its own
    assert store.update_case(tenant, case.id, input="j", expected_revision=1).revision == 2
    assert store.get_suite(tenant, suite.id).revision == 2
    with pytest.raises(RevisionMismatch):
        store.update_case(tenant, case.id, input="k", expected_revision=1)
    assert store.get_case(tenant, case.id).input == "j"


def test_connection_and_source_revisions(engine, tenant):
    conns = ConnectionStore(engine)
    made = conns.create(tenant, name="c", kind="api_token", base_url=None, config={"provider": "github"},
                        credential_refs={}, enabled=True)
    assert made.revision == 1
    assert conns.update(tenant, made.id, enabled=False, expected_revision=1).revision == 2
    with pytest.raises(RevisionMismatch) as info:
        conns.update(tenant, made.id, enabled=True, expected_revision=1)
    assert info.value.current_revision == 2
    assert conns.get(tenant, made.id).enabled is False
    conns.record_check(tenant, made.id, "ok", None)  # a check result is not an edit
    assert conns.get(tenant, made.id).revision == 2
    assert conns.update(tenant, str(uuid4()), enabled=True, expected_revision=1) is None
    assert SourceStore(engine).update(tenant, str(uuid4()), name="x", expected_revision=1) is None


def _agent(engine, tenant) -> str:
    agent_id = str(uuid4())
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO agents (id, tenant_id, name, draft_spec, archived, builtin, created_at, updated_at)"
                 " VALUES (:i, :t, 'a', CAST(:s AS jsonb), false, false, now(), now())"),
            {"i": agent_id, "t": tenant, "s": json.dumps(SPEC)},
        )
    return agent_id


def test_publish_restore_and_archive_bump_the_agent(engine, tenant):
    agent_id = _agent(engine, tenant)
    with engine.begin() as conn:
        assert publish_agent(conn, tenant, agent_id, created_by="u").revision == 2
    with engine.begin() as conn:
        conn.execute(text("UPDATE agents SET draft_spec = jsonb_set(draft_spec, '{instructions}', '\"v2\"') WHERE id = :i"),
                     {"i": agent_id})
        assert publish_agent(conn, tenant, agent_id, created_by="u").revision == 3
    with engine.begin() as conn:
        assert restore_agent_version(conn, tenant, agent_id, 1, created_by="u", discard_draft=True).revision == 4
    with engine.begin() as conn:
        archived = set_agent_archived(conn, tenant, agent_id, True)
        assert archived.changed and archived.revision == 5
    with engine.begin() as conn:
        again = set_agent_archived(conn, tenant, agent_id, True)  # idempotent repeat: nothing written
        assert not again.changed and again.revision == 5


def test_workflow_archive_bumps_the_workflow_and_pauses_schedules(engine, tenant):
    wid = str(uuid4())
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO workflows (id, tenant_id, name, draft_spec, created_at, updated_at)"
                          " VALUES (:i, :t, 'w', NULL, now(), now())"),
                     {"i": wid, "t": tenant})
    schedules = ScheduleStore(engine)
    sched = schedules.create(tenant, wid, name="n", cron="* * * * *", timezone="UTC", input={}, next_fire_at=T0)
    with engine.begin() as conn:
        result = set_workflow_archived(conn, tenant, wid, True, force=True)
    assert result.revision == 2 and result.paused_schedules == (sched["id"],)
    assert schedules.get(tenant, sched["id"])["revision"] == 2  # paused by the archive: that is a write


def test_bump_revision_helper_on_a_core_table(engine, tenant):
    store = ScorerStore(engine)
    made = store.create(tenant, "s1", SCORER)
    table = ScorerModel.__table__
    with engine.begin() as conn:
        assert bump_revision(conn, table, tenant, made.id) == 2
        assert bump_revision(conn, table, tenant, made.id, expected=2) == 3
        assert bump_revision(conn, table, tenant, str(uuid4())) is None
        with pytest.raises(RevisionMismatch):
            bump_revision(conn, table, tenant, made.id, expected=2)
