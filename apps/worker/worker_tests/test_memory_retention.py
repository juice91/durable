"""RM.15 memory retention: the implementer's focused tests.

Unit: RetentionConfig from the environment and the schedule context; `sweep` (cutoffs from the
tick time, tenant paging, cursors, the per-run step bound, counts only). In-process worker: the
`eap.memory_retention` workflow deletes back-dated rows and keeps recent ones, a re-run of the same
tick is answered from DBOS, a fresh run after it is a no-op, the schedule is applied, left alone
without the env and removed on `off`; a swept conversation keeps its coverage, so the observer
plans nothing for its old turns.

The in-process tests date their old rows in 2001 and run a 2001 tick, so the sweep (which crosses
tenants) can only ever touch this file's rows, whatever else the session database holds.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import text

from eap_run_store import MemoryStore, RunStore, TurnText
from eap_schemas.memory import MemoryConfig
from eap_worker.observational import memory_observe_step
from eap_worker.retention import (
    DEFAULT_SCHEDULE,
    RETENTION_SCHEDULE_NAME,
    TENANT_PAGE,
    RetentionConfig,
    RetentionConfigError,
    apply_retention_schedule,
    memory_retention,
    sweep,
)

AT = datetime(2026, 11, 14, 3, 0, tzinfo=timezone.utc)
TICK = datetime(2001, 6, 2, tzinfo=timezone.utc)  # cutoff 2001-06-01 with one day of retention
OLD = "2001-01-01"


# ------------------------------------------------------------------------------ configuration


def test_config_unset_is_disabled():
    cfg = RetentionConfig.from_env({})
    assert not cfg.enabled and cfg.schedule == DEFAULT_SCHEDULE


def test_config_default_covers_the_plan_kinds_and_overrides():
    cfg = RetentionConfig.from_env({"EAP_MEMORY_RETENTION_DAYS": "30"})
    assert dict(cfg.days) == {"message_embeddings": 30, "observations": 30, "conversation_working_memory": 30}
    cfg = RetentionConfig.from_env({
        "EAP_MEMORY_RETENTION_DAYS": "30",
        "EAP_MEMORY_RETENTION_EMBEDDING_DAYS": "7",
        "EAP_MEMORY_RETENTION_OBSERVATION_DAYS": "keep",
        "EAP_MEMORY_RETENTION_RESOURCE_WORKING_MEMORY_DAYS": "365",
        "EAP_MEMORY_RETENTION_BATCH_SIZE": "50",
        "EAP_MEMORY_RETENTION_MAX_STEPS": "9",
        "EAP_MEMORY_RETENTION_SCHEDULE": "0 0 4 * * *",
    })
    assert dict(cfg.days) == {
        "message_embeddings": 7, "conversation_working_memory": 30, "resource_working_memory": 365,
    }
    assert (cfg.batch_size, cfg.max_steps, cfg.schedule) == (50, 9, "0 0 4 * * *")
    assert RetentionConfig.from_context(cfg.to_context()) == RetentionConfig(
        days=cfg.days, batch_size=50, max_steps=9
    )


@pytest.mark.parametrize("env", [
    {"EAP_MEMORY_RETENTION_DAYS": "0"},
    {"EAP_MEMORY_RETENTION_DAYS": "-1"},
    {"EAP_MEMORY_RETENTION_EMBEDDING_DAYS": "soon"},
    {"EAP_MEMORY_RETENTION_BATCH_SIZE": "0"},
    {"EAP_MEMORY_RETENTION_BATCH_SIZE": "10001"},
    {"EAP_MEMORY_RETENTION_MAX_STEPS": "x"},
    {"EAP_MEMORY_RETENTION_OBSERVATION_DAYS": "off"},  # `off` is only for EAP_MEMORY_RETENTION_DAYS
])
def test_config_rejects_bad_values(env):
    with pytest.raises(RetentionConfigError) as exc:
        RetentionConfig.from_env(env)
    assert next(iter(env)) in str(exc.value)


def test_config_off_is_explicit():
    cfg = RetentionConfig.from_env({"EAP_MEMORY_RETENTION_DAYS": "OFF", "EAP_MEMORY_RETENTION_EMBEDDING_DAYS": "3"})
    assert cfg.off and not cfg.enabled
    assert not RetentionConfig.from_env({}).off


def test_context_tolerates_garbage():
    assert not RetentionConfig.from_context(None).enabled
    assert not RetentionConfig.from_context({"days": {"kb_chunks": 1, "observations": 0}}).enabled


# ------------------------------------------------------------------------------ sweep


class Fake:
    """Step stand-ins: `rows[kind][tenant]` batches to report before done."""

    def __init__(self, tenants: dict[str, list[str]], batches: int = 1) -> None:
        self.tenants, self.batches, self.calls = tenants, batches, []
        self.left: dict[tuple[str, str], int] = {}

    def tenant_page(self, kind, cutoff, after, limit):
        self.calls.append(("tenants", kind, cutoff, after))
        ids = [t for t in self.tenants.get(kind, []) if after is None or t > after]
        return ids[:limit]

    def batch(self, kind, tenant, cutoff, limit, after):
        self.calls.append(("batch", kind, tenant, after))
        left = self.left.setdefault((kind, tenant), self.batches) - 1
        self.left[(kind, tenant)] = left
        cursor = None if left == 0 else f"c{left}"
        return {"deleted": 2, "skipped": 1, "done": left == 0, "next_after": cursor}


def test_sweep_cutoffs_come_from_the_tick_and_counts_add_up():
    cfg = RetentionConfig(days={"observations": 1, "message_embeddings": 10})
    fake = Fake({"message_embeddings": ["tenant-x1", "tenant-x2"], "observations": ["tenant-x1"]}, batches=2)
    out = sweep(cfg, AT, fake.tenant_page, fake.batch)
    assert out["complete"] is True and out["scheduled_at"] == AT.isoformat()
    emb, obs = out["kinds"]["message_embeddings"], out["kinds"]["observations"]
    assert emb == {"cutoff": (AT - timedelta(days=10)).isoformat(), "tenants": 2, "batches": 4, "deleted": 8,
                   "skipped": 4}
    assert obs["cutoff"] == (AT - timedelta(days=1)).isoformat() and obs["batches"] == 2
    assert ("batch", "observations", "tenant-x1", "c1") in fake.calls  # the cursor is passed on
    assert out["steps"] == 2 + 4 + 2  # two tenant pages and six batches
    assert list(out["kinds"]) == ["message_embeddings", "observations"]  # RETENTION_KINDS order
    assert "tenant-x" not in json.dumps(out)  # counts only, no tenant ids


def test_sweep_is_bounded_per_run_and_says_so():
    cfg = RetentionConfig(days={"message_embeddings": 1, "observations": 1}, max_steps=3)
    fake = Fake({"message_embeddings": ["a", "b"], "observations": ["a"]}, batches=5)
    out = sweep(cfg, AT, fake.tenant_page, fake.batch)
    assert out["complete"] is False and out["steps"] == 3 and len(fake.calls) == 3
    # round-robin: both kinds started (a page each), then the first kind's first batch
    assert [c[:2] for c in fake.calls] == [("tenants", "message_embeddings"), ("tenants", "observations"),
                                          ("batch", "message_embeddings")]


def test_sweep_pages_tenants():
    many = [f"t{i:04d}" for i in range(TENANT_PAGE + 3)]
    fake = Fake({"observations": many})
    out = sweep(RetentionConfig(days={"observations": 1}, max_steps=10_000), AT, fake.tenant_page, fake.batch)
    pages = [c for c in fake.calls if c[0] == "tenants"]
    assert [p[3] for p in pages] == [None, many[TENANT_PAGE - 1]]
    assert out["kinds"]["observations"]["tenants"] == len(many) and out["complete"] is True


def test_sweep_shares_the_budget_round_robin():
    """A large backlog of one kind cannot starve the others: each unfinished kind gets a step in
    turn (RETENTION_KINDS order)."""
    cfg = RetentionConfig(days={"message_embeddings": 1, "observations": 1, "conversation_working_memory": 1},
                          max_steps=9)
    fake = Fake({"message_embeddings": ["t1"], "observations": ["t1"], "conversation_working_memory": ["t1"]},
                batches=100)
    out = sweep(cfg, AT, fake.tenant_page, fake.batch)
    assert [c[1] for c in fake.calls] == ["message_embeddings", "observations", "conversation_working_memory"] * 3
    assert [out["kinds"][k]["batches"] for k in out["kinds"]] == [2, 2, 2] and out["complete"] is False


def test_sweep_round_robin_lets_a_finished_kind_drop_out():
    cfg = RetentionConfig(days={"message_embeddings": 1, "observations": 1}, max_steps=20)
    fake = Fake({"message_embeddings": ["t1"], "observations": ["t1"]}, batches=1)
    fake.left[("observations", "t1")] = 5
    out = sweep(cfg, AT, fake.tenant_page, fake.batch)
    assert out["complete"] is True and out["steps"] == 2 + 1 + 5
    assert out["kinds"]["observations"]["batches"] == 5


def test_sweep_naive_tick_is_utc_and_disabled_is_a_no_op():
    fake = Fake({})
    assert sweep(RetentionConfig(days={}), AT.replace(tzinfo=None), fake.tenant_page, fake.batch) == {
        "scheduled_at": AT.isoformat(), "complete": True, "steps": 0, "kinds": {},
    }
    assert fake.calls == []


# ------------------------------------------------------------------------------ in-process worker


def _add_turn(engine, tenant: str, agent: str, conv: str, n: int, status: str = "completed") -> str:
    run_id = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO runs (id, tenant_id, kind, agent_id, agent_version, status, input, output, messages,"
            " conversation_id, turn, created_at, updated_at)"
            " VALUES (:id, :t, 'agent', :a, 1, :s, 'in', 'out', '[]'::jsonb, :c, :n, now(), now())"),
            {"id": run_id, "t": tenant, "a": agent, "s": status, "c": conv, "n": n})
    return run_id


def _vec(i: int) -> list[float]:
    v = [0.0] * 1536
    v[i] = 1.0
    return v


def _plant(engine, tenant: str) -> str:
    """Turn 1's memory dated 2001 (OLD), turn 2's new, in one conversation of resource `ana`."""
    store, runs = MemoryStore(engine), RunStore(engine)
    agent = str(uuid.uuid4())
    conv = runs.create_conversation(tenant, agent, resource_id="ana")[0]["id"]
    for n in (1, 2):
        run_id = _add_turn(engine, tenant, agent, conv, n)
        store.index_turn(tenant, agent, conv, run_id, [TurnText("user", f"q{n}", _vec(n))], embedding_model="test:hash")
        store.insert_observation(tenant, agent, conv, covers_from_turn=n, covers_to_turn=n, content=f"o{n}")
        if n == 1:
            with engine.begin() as conn:
                for table in ("message_embeddings", "memory_observations"):
                    conn.execute(text(f"UPDATE {table} SET created_at = CAST(:old AS timestamptz)"
                                      " WHERE tenant_id = :t"), {"t": tenant, "old": OLD})
    store.update_working_memory(tenant, agent, scope="conversation", scope_key=conv, content="# c")
    with engine.begin() as conn:
        conn.execute(text("UPDATE working_memories SET updated_at = CAST(:old AS timestamptz) WHERE tenant_id = :t"),
                     {"t": tenant, "old": OLD})
    return conv


def _count(engine, table: str, tenant: str) -> int:
    with engine.connect() as conn:
        return conn.execute(text(f"SELECT count(*) FROM {table} WHERE tenant_id = :t"), {"t": tenant}).scalar_one()


def _obs_text(engine, tenant: str) -> int:
    """Observation rows that still hold text (a swept one leaves a text-free coverage marker)."""
    with engine.connect() as conn:
        return conn.execute(text("SELECT count(*) FROM memory_observations WHERE tenant_id = :t AND content <> ''"),
                            {"t": tenant}).scalar_one()


@pytest.fixture
def mem_tenant(tenant, worker):
    yield tenant
    with worker.engine.begin() as conn:
        conn.execute(text("DELETE FROM working_memories WHERE tenant_id = :t"), {"t": tenant})


def test_workflow_deletes_old_rows_and_keeps_new_ones(worker, mem_tenant):
    from dbos import DBOS, SetWorkflowID

    engine = worker.engine
    _plant(engine, mem_tenant)
    context = RetentionConfig(days={"message_embeddings": 1, "observations": 1,
                                    "conversation_working_memory": 1}, batch_size=1).to_context()
    now = TICK
    wf_id = f"test-retention-{uuid.uuid4().hex[:8]}"
    with SetWorkflowID(wf_id):
        out = DBOS.start_workflow(memory_retention, now, context).get_result()
    kinds = out["kinds"]
    # other tenants' old rows may exist in the shared schema: assert on this tenant's rows
    assert kinds["message_embeddings"]["deleted"] >= 1 and kinds["observations"]["deleted"] >= 1
    assert out["complete"] is True
    assert _count(engine, "message_embeddings", mem_tenant) == 1  # turn 2's
    assert _obs_text(engine, mem_tenant) == 1  # turn 1's text gone (its marker stays), turn 2's kept
    assert _count(engine, "working_memories", mem_tenant) == 0
    assert _count(engine, "conversations", mem_tenant) == 1 and _count(engine, "runs", mem_tenant) == 2
    assert mem_tenant not in json.dumps(out) and "ana" not in json.dumps(out)  # counts only

    # The same tick again (another worker firing it, or a recovery) is answered from DBOS.
    with SetWorkflowID(wf_id):
        assert DBOS.start_workflow(memory_retention, now, context).get_result() == out
    # A later tick finds nothing of this tenant to delete.
    again = DBOS.start_workflow(memory_retention, now + timedelta(seconds=1), context).get_result()
    assert again["complete"] is True
    assert _count(engine, "message_embeddings", mem_tenant) == 1


def test_running_conversation_is_left_for_a_later_run(worker, mem_tenant):
    from dbos import DBOS

    engine = worker.engine
    conv = _plant(engine, mem_tenant)
    with engine.connect() as conn:
        agent = conn.execute(text("SELECT agent_id FROM conversations WHERE id = :c"), {"c": conv}).scalar_one()
    _add_turn(engine, mem_tenant, agent, conv, 3, status="running")
    context = RetentionConfig(days={"message_embeddings": 1, "observations": 1,
                                    "conversation_working_memory": 1}).to_context()
    DBOS.start_workflow(memory_retention, TICK, context).get_result()
    assert _count(engine, "message_embeddings", mem_tenant) == 2
    assert _obs_text(engine, mem_tenant) == 2
    assert _count(engine, "working_memories", mem_tenant) == 1


def test_schedule_is_applied_left_and_removed(worker, caplog):
    from dbos import DBOS

    try:
        cfg = RetentionConfig.from_env({"EAP_MEMORY_RETENTION_DAYS": "30", "EAP_MEMORY_RETENTION_SCHEDULE": "0 0 4 * * *"})
        assert apply_retention_schedule(cfg) == "applied"
        assert apply_retention_schedule(cfg) == "applied"  # idempotent (every worker start)
        sched = DBOS.get_schedule(RETENTION_SCHEDULE_NAME)
        assert sched is not None and sched["schedule"] == "0 0 4 * * *"
        assert sched["workflow_name"] == "eap.memory_retention" and sched["context"] == cfg.to_context()
        # a worker without the env leaves the shared schedule alone
        assert apply_retention_schedule(RetentionConfig.from_env({})) == "left"
        assert DBOS.get_schedule(RETENTION_SCHEDULE_NAME) is not None
        # a worker configured differently replaces it, with a warning
        caplog.clear()
        other = RetentionConfig.from_env({"EAP_MEMORY_RETENTION_DAYS": "7", "EAP_MEMORY_RETENTION_SCHEDULE": "0 0 4 * * *"})
        assert apply_retention_schedule(other) == "applied"
        assert any(r.levelname == "WARNING" and "differs" in r.getMessage() for r in caplog.records)
        assert DBOS.get_schedule(RETENTION_SCHEDULE_NAME)["context"] == other.to_context()
        # only an explicit off removes it
        assert apply_retention_schedule(RetentionConfig.from_env({"EAP_MEMORY_RETENTION_DAYS": "off"})) == "removed"
        assert DBOS.get_schedule(RETENTION_SCHEDULE_NAME) is None
    finally:
        DBOS.delete_schedule(RETENTION_SCHEDULE_NAME)


def test_worker_start_without_retention_has_no_schedule(worker):
    from dbos import DBOS

    assert DBOS.get_schedule(RETENTION_SCHEDULE_NAME) is None


# ------------------------------------------------------------------------------ coverage after a sweep


def _observed_conversation(worker, tenant: str, turns: int, old_upto: int, chunks: list[tuple[int, int]]) -> tuple[str, str, str]:
    """A conversation of `turns` big completed turns, observed in `chunks`; the observations whose
    end is <= `old_upto` are dated 2001."""
    store, agent = MemoryStore(worker.engine), str(uuid.uuid4())
    conv = RunStore(worker.engine).create_conversation(tenant, agent, resource_id="ana")[0]["id"]
    run_id = ""
    for n in range(1, turns + 1):
        run_id = _add_turn(worker.engine, tenant, agent, conv, n)
        with worker.engine.begin() as conn:  # ~1000 tokens per turn: far above observe_after
            conn.execute(text("UPDATE runs SET input = :i WHERE id = :r"), {"i": "word " * 800, "r": run_id})
    for a, b in chunks:
        store.insert_observation(tenant, agent, conv, covers_from_turn=a, covers_to_turn=b, content=f"o{a}-{b}")
    with worker.engine.begin() as conn:
        conn.execute(text("UPDATE memory_observations SET created_at = CAST(:old AS timestamptz)"
                          " WHERE tenant_id = :t AND covers_to_turn <= :k"), {"t": tenant, "old": OLD, "k": old_upto})
    return conv, agent, run_id


def _plan(worker, tenant, agent, conv, run_id, turn) -> dict[str, Any]:
    memory_json = MemoryConfig.model_validate({"observational": {
        "model": {"provider": "test", "name": "observer"}, "observe_after_tokens": 1000, "keep_recent_turns": 0,
        "reflect_after_tokens": 200_000,
    }}).model_dump(mode="json")
    return asyncio.run(memory_observe_step(run_id, tenant, agent, conv, turn, memory_json))


@pytest.mark.parametrize("shape", ["all_old", "active"])
def test_swept_turns_stay_covered_and_are_not_observed_again(worker, mem_tenant, shape):
    """After the sweep the old turns are neither in the snapshot's history nor planned for
    observation: the marker keeps the coverage (formerly the observer re-observed them all)."""
    if shape == "all_old":
        conv, agent, run_id = _observed_conversation(worker, mem_tenant, 4, 4, [(1, 2), (3, 4)])
    else:  # the head is old, the newest observation is recent
        conv, agent, run_id = _observed_conversation(worker, mem_tenant, 4, 2, [(1, 2), (3, 4)])
    assert _plan(worker, mem_tenant, agent, conv, run_id, 4)["action"] == "none"
    out = memory_retention(TICK, RetentionConfig(days={"observations": 1}).to_context())
    assert out["kinds"]["observations"]["deleted"] == (2 if shape == "all_old" else 1)
    snap = MemoryStore(worker.engine).load_snapshot(mem_tenant, agent, conv, observations=True)
    assert snap.covered_to_turn == 4 and snap.turn_numbers == ()
    assert [o.content for o in snap.observations] == ([] if shape == "all_old" else ["o3-4"])
    plan = _plan(worker, mem_tenant, agent, conv, run_id, 4)
    assert plan == {"action": "none", "reason": "below_threshold"}
    # control: had the rows been deleted outright, every old turn would be observed (and paid) again
    MemoryStore(worker.engine).delete_observations(mem_tenant, conv)
    assert _plan(worker, mem_tenant, agent, conv, run_id, 4)["action"] == "observe"
