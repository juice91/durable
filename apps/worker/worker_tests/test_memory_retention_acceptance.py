"""RM.15 acceptance for the retention workflow (on top of test_memory_retention.py).

In-process worker (real DBOS): the workflow called with a fake schedule time. Every row planted
here is dated in 2001 and the fake tick is mid-2001, so the sweep (which crosses tenants) can only
ever touch this test's rows, whatever else the shared database holds. Subprocess workers: a crash
between batches, several workers applying one schedule, bad, unset and `off` configuration.

Decided changes (RM.15 review, 2026-11-14): an old observation's text is removed but a text-free
marker keeps its turns covered, so observation counts here count rows that still hold text; and a
worker without the retention env leaves the shared schedule alone (only
`EAP_MEMORY_RETENTION_DAYS=off` removes it).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import hashlib

import pytest
from dbos import DBOS, DBOSClient, SetWorkflowID
from sqlalchemy import text

from eap_run_store import ChunkInput, KnowledgeStore, MemoryStore, RunStore, TurnText
from eap_worker.retention import RETENTION_SCHEDULE_NAME, RetentionConfig, memory_retention
from worker_tests.conftest import DBOS_DB_URL, cleanup_tenant, wait_for
from worker_tests.test_crash_recovery import WorkerProcess, _env, crash_schema  # noqa: F401 - fixture
from worker_tests.test_memory_retention import _add_turn, _count, _vec

TICK = datetime(2001, 6, 2, tzinfo=timezone.utc)  # cutoff 2001-06-01 with one day of retention
OLD, NEW = "2001-01-01", "2001-12-01"
TABLES = ("message_embeddings", "memory_observations", "working_memories")
SECRET = "ana-secret-resource"
TEXT = "secret observation text"
ALL_DAYS = {"message_embeddings": 1, "observations": 1, "conversation_working_memory": 1,
            "resource_working_memory": 1}


@pytest.fixture
def mem(tenant, worker):
    yield tenant
    with worker.engine.begin() as conn:
        for table in TABLES:
            conn.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tenant})


def plant(engine, tenant: str, convs: int = 1, resource: str = SECRET) -> list[str]:
    """`convs` conversations, each with 2 turns: turn 1's memory is OLD, turn 2's is NEW; plus an
    OLD conversation note and an OLD resource note (one per conversation / agent)."""
    store, runs, ids = MemoryStore(engine), RunStore(engine), []
    for i in range(convs):
        agent = str(uuid.uuid4())
        conv = runs.create_conversation(tenant, agent, resource_id=resource)[0]["id"]
        ids.append(conv)
        for n in (1, 2):
            run_id = _add_turn(engine, tenant, agent, conv, n)
            store.index_turn(tenant, agent, conv, run_id, [TurnText("user", f"q{n}", _vec(n))],
                             embedding_model="test:hash")
            store.insert_observation(tenant, agent, conv, covers_from_turn=n, covers_to_turn=n, content=TEXT)
        store.update_working_memory(tenant, agent, scope="conversation", scope_key=conv, content=TEXT)
        store.update_working_memory(tenant, agent, scope="resource", scope_key=resource, content=TEXT)
    with engine.begin() as conn:
        p = {"t": tenant, "old": OLD, "new": NEW}
        conn.execute(text("UPDATE message_embeddings SET created_at = CAST(:old AS timestamptz) WHERE tenant_id = :t"
                          " AND turn = 1"), p)
        conn.execute(text("UPDATE message_embeddings SET created_at = CAST(:new AS timestamptz) WHERE tenant_id = :t"
                          " AND turn = 2"), p)
        conn.execute(text("UPDATE memory_observations SET created_at = CAST(:old AS timestamptz) WHERE tenant_id = :t"
                          " AND covers_to_turn = 1"), p)
        conn.execute(text("UPDATE memory_observations SET created_at = CAST(:new AS timestamptz) WHERE tenant_id = :t"
                          " AND covers_to_turn = 2"), p)
        conn.execute(text("UPDATE working_memories SET updated_at = CAST(:old AS timestamptz) WHERE tenant_id = :t"), p)
    return ids


def state(engine, tenant: str) -> dict[str, int]:
    return {
        "emb": _count(engine, "message_embeddings", tenant),
        # rows that still hold text (a swept observation leaves a text-free coverage marker)
        "obs": _count_obs_text(engine, tenant),
        "conv_wm": _count_where(engine, tenant, "scope = 'conversation'"),
        "res_wm": _count_where(engine, tenant, "scope = 'resource'"),
    }


def _count_obs_text(engine, tenant: str) -> int:
    with engine.connect() as conn:
        return conn.execute(text("SELECT count(*) FROM memory_observations WHERE tenant_id = :t AND content <> ''"),
                            {"t": tenant}).scalar_one()


def _count_where(engine, tenant: str, where: str) -> int:
    with engine.connect() as conn:
        return conn.execute(text(f"SELECT count(*) FROM working_memories WHERE tenant_id = :t AND {where}"),
                            {"t": tenant}).scalar_one()


def run(days: dict[str, int], at: datetime = TICK, **extra) -> dict:
    context = RetentionConfig(days=days, **extra).to_context()
    wf_id = f"acc-retention-{uuid.uuid4().hex[:10]}"
    with SetWorkflowID(wf_id):
        out = memory_retention(at, context)  # the workflow function itself, with a fake schedule time
    assert DBOS.retrieve_workflow(wf_id).get_status().status == "SUCCESS"
    return out


# ------------------------------------------------------------------------- per kind, in the workflow

START = {"emb": 2, "obs": 2, "conv_wm": 1, "res_wm": 1}


def test_workflow_deletes_exactly_the_old_rows_of_each_configured_kind(worker, mem):
    plant(worker.engine, mem)
    other = f"{mem}_o"
    try:
        plant(worker.engine, other)
        assert state(worker.engine, mem) == START
        out = run({k: v for k, v in ALL_DAYS.items() if k != "resource_working_memory"})
        assert state(worker.engine, mem) == {"emb": 1, "obs": 1, "conv_wm": 0, "res_wm": 1}  # note: opt-in
        assert state(worker.engine, other) == {"emb": 1, "obs": 1, "conv_wm": 0, "res_wm": 1}  # all tenants
        assert [k for k in out["kinds"]] == ["message_embeddings", "observations", "conversation_working_memory"]
        assert [out["kinds"][k]["deleted"] for k in out["kinds"]] == [2, 2, 2]  # this tenant's and the other's
        out = run(ALL_DAYS)  # opted in: the resource notes go as well
        assert out["kinds"]["resource_working_memory"]["deleted"] == 2
        assert state(worker.engine, mem) == {"emb": 1, "obs": 1, "conv_wm": 0, "res_wm": 0}
    finally:
        with worker.engine.begin() as conn:
            for table in TABLES:
                conn.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": other})
        cleanup_tenant(worker.engine, other)


def test_env_keep_leaves_a_kind_alone_and_opt_in_adds_the_resource_note(worker, mem):
    plant(worker.engine, mem)
    cfg = RetentionConfig.from_env({"EAP_MEMORY_RETENTION_DAYS": "1", "EAP_MEMORY_RETENTION_OBSERVATION_DAYS": "keep"})
    out = memory_retention(TICK, cfg.to_context())
    assert "observations" not in out["kinds"] and out["complete"] is True
    assert state(worker.engine, mem) == {"emb": 1, "obs": 2, "conv_wm": 0, "res_wm": 1}
    cfg = RetentionConfig.from_env({"EAP_MEMORY_RETENTION_DAYS": "1",
                                    "EAP_MEMORY_RETENTION_RESOURCE_WORKING_MEMORY_DAYS": "1"})
    memory_retention(TICK, cfg.to_context())
    assert state(worker.engine, mem) == {"emb": 1, "obs": 1, "conv_wm": 0, "res_wm": 0}


KB_TABLES = ("knowledge_bases", "kb_documents", "kb_chunks")


def plant_kb(engine, tenant: str) -> None:
    """A knowledge base with one document and two chunks, all dated OLD (far past the cutoff)."""
    kb_store = KnowledgeStore(engine)
    kb = kb_store.create_knowledge_base(tenant, name="Docs", embedding_model="test:hash", dimensions=1536,
                                        description="d")
    put = kb_store.put_document(tenant, kb["id"], document_key="d", title="D", content="one\n\ntwo",
                                mime_type="text/markdown")
    gen = kb_store.get_knowledge_base(tenant, kb["id"])["active_generation"]
    chunks = []
    for i, body in enumerate(("one", "two")):
        embed = f"title: S{i}\n\n{body}"
        chunks.append(ChunkInput(ordinal=i, title=f"S{i}", text=body, embed_text=embed,
                                 embed_text_hash=hashlib.sha256(embed.encode()).hexdigest(), approx_tokens=4,
                                 heading_path=("D", f"S{i}"), anchor=None))
    kb_store.upsert_chunks(tenant, put.document["id"], put.document["latest_version"], gen, chunks, metadata={})
    with engine.begin() as conn:
        for table in KB_TABLES:
            cols = "created_at = CAST(:old AS timestamptz)"
            if table != "kb_chunks":
                cols += ", updated_at = CAST(:old AS timestamptz)"
            conn.execute(text(f"UPDATE {table} SET {cols} WHERE tenant_id = :t"), {"t": tenant, "old": OLD})


def kb_counts(engine, tenant: str) -> dict[str, int]:
    with engine.connect() as conn:
        return {t: conn.execute(text(f"SELECT count(*) FROM {t} WHERE tenant_id = :t"), {"t": tenant}).scalar_one()
                for t in KB_TABLES}


def test_knowledge_base_data_is_untouched(worker, mem):
    """Back-dated knowledge base rows (as old as the swept memory) survive every kind's sweep."""
    plant(worker.engine, mem)
    try:
        plant_kb(worker.engine, mem)
        before = kb_counts(worker.engine, mem)
        assert before == {"knowledge_bases": 1, "kb_documents": 1, "kb_chunks": 2}
        out = run(ALL_DAYS)
        assert out["kinds"]["observations"]["deleted"] >= 1  # the sweep did run over this tenant
        assert kb_counts(worker.engine, mem) == before
    finally:
        with worker.engine.begin() as conn:
            conn.execute(text("DELETE FROM knowledge_bases WHERE tenant_id = :t"), {"t": mem})


# ------------------------------------------------------------------------- bounded runs


def test_max_steps_bounds_a_run_and_the_next_run_continues(worker, mem):
    plant(worker.engine, mem, convs=5)
    days = {"observations": 1}
    first = run(days, batch_size=1, max_steps=3)  # 1 tenant page + 2 batches
    assert first["complete"] is False and first["steps"] == 3
    assert first["kinds"]["observations"]["deleted"] == 2 and state(worker.engine, mem)["obs"] == 8
    second = run(days, batch_size=1, max_steps=3, at=TICK.replace(minute=1))
    assert second["complete"] is False and second["kinds"]["observations"]["deleted"] == 2
    third = run(days, batch_size=1, max_steps=50, at=TICK.replace(minute=2))
    assert third["complete"] is True and third["kinds"]["observations"]["deleted"] == 1
    assert state(worker.engine, mem)["obs"] == 5  # every old row went once, the 5 new ones stayed
    assert run(days)["kinds"].get("observations", {"deleted": 0})["deleted"] == 0


def test_a_conversation_with_a_running_turn_is_swept_by_a_later_run(worker, mem):
    conv = plant(worker.engine, mem)[0]
    with worker.engine.connect() as conn:
        agent = conn.execute(text("SELECT agent_id FROM conversations WHERE id = :c"), {"c": conv}).scalar_one()
    running = _add_turn(worker.engine, mem, agent, conv, 3, status="running")
    held = run(ALL_DAYS)
    assert state(worker.engine, mem) == START
    assert held["kinds"]["observations"]["skipped"] == 1 and held["complete"] is True
    with worker.engine.begin() as conn:
        conn.execute(text("UPDATE runs SET status = 'completed' WHERE id = :r"), {"r": running})
    run(ALL_DAYS, at=TICK.replace(minute=1))
    assert state(worker.engine, mem) == {"emb": 1, "obs": 1, "conv_wm": 0, "res_wm": 0}


# ------------------------------------------------------------------------- privacy and replay


def test_result_logs_and_checkpoints_carry_counts_only(worker, mem, caplog):
    convs = plant(worker.engine, mem)
    caplog.set_level(logging.DEBUG)
    wf_id = f"acc-privacy-{uuid.uuid4().hex[:8]}"
    with SetWorkflowID(wf_id):
        out = memory_retention(TICK, RetentionConfig(days=ALL_DAYS, batch_size=1).to_context())
    assert out["kinds"]["observations"]["deleted"] >= 1
    steps = DBOS.list_workflow_steps(wf_id)
    assert {s["function_name"] for s in steps} == {"eap.memory_retention.tenants", "eap.memory_retention.batch"}
    logged = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("eap_worker"))
    assert "memory retention swept kind=observations" in logged  # the sweep did log
    persisted = json.dumps([out, [(s["output"], s["error"]) for s in steps]], default=str)
    for secret in (SECRET, TEXT, "ana-secret", *convs):
        assert secret not in logged and secret not in json.dumps(out)
    # DBOS checkpoints (operator-only, documented in retention.py) hold tenant ids and conversation
    # id cursors, but never a resource id or any text
    assert SECRET not in persisted and TEXT not in persisted and "ana-secret" not in persisted
    assert mem not in logged and mem not in json.dumps(out)  # tenant ids: neither result nor logs
    assert all(set(v) == {"cutoff", "tenants", "batches", "deleted", "skipped"} for v in out["kinds"].values())


def test_rerunning_a_finished_tick_replays_the_result_without_deleting_again(worker, mem):
    plant(worker.engine, mem)
    wf_id = f"acc-replay-{uuid.uuid4().hex[:8]}"
    ctx = RetentionConfig(days=ALL_DAYS).to_context()
    with SetWorkflowID(wf_id):
        first = memory_retention(TICK, ctx)
    after_first = state(worker.engine, mem)
    plant(worker.engine, mem)  # more old rows arrive; the same tick (a duplicate fire) must not take them
    with SetWorkflowID(wf_id):
        assert memory_retention(TICK, ctx) == first
    assert state(worker.engine, mem)["obs"] == after_first["obs"] + 2
    later = run(ALL_DAYS, at=TICK.replace(minute=5))  # a new tick does
    assert later["kinds"]["observations"]["deleted"] >= 1 and state(worker.engine, mem)["obs"] == 2


# ------------------------------------------------------------------------- crash between batches

DRIVER = '''
import json, os, sys
from datetime import datetime
from dbos import DBOS, SetWorkflowID
from eap_run_store import MemoryStore
mode, log = os.environ["DRIVER_CRASH"], os.environ["DRIVER_LOG"]
calls = []
orig = MemoryStore.delete_retention_batch
def patched(self, kind, tenant, cutoff, *, limit, after):
    with open(log, "a") as f:
        f.write(f"start {after}\\n")
    if mode == "before" and os.path.getsize(log) > 0 and open(log).read().count("start") == 2:
        os._exit(137)  # the 2nd batch never ran
    result = orig(self, kind, tenant, cutoff, limit=limit, after=after)
    with open(log, "a") as f:
        f.write(f"end {after}\\n")
    if mode == "after" and open(log).read().count("end") == 2:
        os._exit(137)  # the 2nd batch committed, its checkpoint did not
    return result
MemoryStore.delete_retention_batch = patched
from eap_worker.app import launch
from eap_worker.retention import memory_retention
launch()
wf = os.environ["DRIVER_WF"]
if os.environ.get("DRIVER_START"):
    with SetWorkflowID(wf):
        out = memory_retention(datetime.fromisoformat(os.environ["DRIVER_TICK"]), json.loads(os.environ["DRIVER_CTX"]))
else:
    out = DBOS.retrieve_workflow(wf).get_result()  # recovered by launch()
print("RESULT " + json.dumps(out), flush=True)
DBOS.destroy()
'''


def driver(tmp_path: Path, schema: str, executor: str, mode: str, wf: str, start: bool, ctx: dict) -> subprocess.Popen:
    script = tmp_path / "driver.py"
    script.write_text(DRIVER)
    env = _env(schema, executor, DRIVER_CRASH=mode, DRIVER_LOG=str(tmp_path / "batches.log"), DRIVER_WF=wf,
               DRIVER_TICK=TICK.isoformat(), DRIVER_CTX=json.dumps(ctx), DRIVER_START="1" if start else "")
    env.pop("EAP_MEMORY_RETENTION_DAYS", None)
    return subprocess.Popen([sys.executable, str(script)], env=env, cwd=str(tmp_path),
                            stdout=(tmp_path / f"out-{start}.log").open("w"), stderr=subprocess.STDOUT)


@pytest.mark.parametrize("crash", ["before", "after"])
def test_crash_between_batches_resumes_without_double_counting(worker, mem, crash_schema, tmp_path, crash):
    plant(worker.engine, mem, convs=4)
    ctx = RetentionConfig(days={"observations": 1}, batch_size=1).to_context()
    wf, executor = f"acc-crash-{uuid.uuid4().hex[:8]}", f"ret-crash-{uuid.uuid4().hex[:8]}"
    first = driver(tmp_path, crash_schema, executor, crash, wf, True, ctx)
    assert first.wait(timeout=90) == 137, (tmp_path / "out-True.log").read_text()[-2000:]
    died_with = state(worker.engine, mem)["obs"]
    assert died_with == (8 - 1 if crash == "before" else 8 - 2)  # batches 1 (and 2) committed
    second = driver(tmp_path, crash_schema, executor, "none", wf, False, ctx)
    try:
        assert second.wait(timeout=90) == 0, (tmp_path / "out-False.log").read_text()[-2000:]
    finally:
        second.kill()
    result = json.loads(next(line for line in (tmp_path / "out-False.log").read_text().splitlines()
                             if line.startswith("RESULT "))[7:])
    assert state(worker.engine, mem)["obs"] == 4  # all 4 old rows gone, the 4 new ones stay
    assert result["complete"] is True
    # never above the truth; in the window after the commit the replayed batch finds nothing left
    assert (3 if crash == "after" else 4) <= result["kinds"]["observations"]["deleted"] <= 4
    starts = [line.split()[1] for line in (tmp_path / "batches.log").read_text().splitlines()
              if line.startswith("start")]
    redone = [c for c in set(starts) if starts.count(c) > 1]
    # Only the interrupted batch ran twice. After a commit-then-crash, its replay (same cursor) takes
    # the next conversation instead of the one already gone, so one batch fewer is needed.
    assert len(redone) == 1 and len(starts) == (6 if crash == "before" else 5)


# ------------------------------------------------------------------------- schedule and config

# 100 years of retention: the real ticks below sweep nothing, whatever the database holds.
HARMLESS = {"EAP_MEMORY_RETENTION_DAYS": "36500"}


def client_of(schema: str) -> DBOSClient:
    return DBOSClient(system_database_url=DBOS_DB_URL, dbos_system_schema=schema)


def test_several_workers_applying_one_schedule_make_one_workflow_per_tick(crash_schema, tmp_path):
    workers: list[WorkerProcess] = []
    client = None
    try:
        for i in range(3):
            env = _env(crash_schema, f"ret-multi-{i}-{uuid.uuid4().hex[:6]}", EAP_MEMORY_RETENTION_SCHEDULE="* * * * * *",
                       **HARMLESS)
            workers.append(WorkerProcess(env, tmp_path / f"w{i}.log"))
        for w in workers:
            w.wait_ready()
        client = client_of(crash_schema)
        wait_for(lambda: len(_ticks(client)) >= 4, timeout=60, what="four scheduled ticks")
        assert len(client.list_schedules(schedule_name_prefix=RETENTION_SCHEDULE_NAME)) == 1  # one schedule, not three
    finally:
        for w in workers:
            w.stop()
    client = client or client_of(crash_schema)
    try:
        ids = [w.workflow_id for w in _ticks(client)]
        assert len(ids) == len(set(ids)) and all(i.startswith("sched-eap.memory_retention-") for i in ids)
        assert len(ids) == len({i.rsplit("-", 1)[-1] for i in ids})  # one per tick: never a duplicate of a tick
        assert {w.status for w in _ticks(client)} <= {"SUCCESS", "PENDING", "CANCELLED", "ENQUEUED"}
    finally:
        client.destroy()


def _ticks(client: DBOSClient):
    return client.list_workflows(name="eap.memory_retention", load_input=False, load_output=False)


def test_bad_config_stops_the_worker_at_start_and_schedules_nothing(crash_schema, tmp_path):
    for env_name, value in (("EAP_MEMORY_RETENTION_DAYS", "0"), ("EAP_MEMORY_RETENTION_EMBEDDING_DAYS", "soon"),
                            ("EAP_MEMORY_RETENTION_BATCH_SIZE", "-3")):
        worker = WorkerProcess(_env(crash_schema, f"ret-bad-{uuid.uuid4().hex[:6]}", **{env_name: value}),
                               tmp_path / f"bad-{env_name}.log")
        try:
            code = worker.wait_exit(timeout=60)
        finally:
            worker.stop()
        log = (tmp_path / f"bad-{env_name}.log").read_text()
        assert code != 0 and "RetentionConfigError" in log and env_name in log and "EAP_WORKER_READY" not in log
    # The settings are validated before DBOS launches (eap_worker.app.validate_startup), so the
    # system schema may not even have its schedule table: either way nothing is scheduled.
    from sqlalchemy.exc import ProgrammingError

    client = client_of(crash_schema)
    try:
        try:
            assert client.list_schedules(schedule_name_prefix=RETENTION_SCHEDULE_NAME) == []
        except ProgrammingError as exc:
            assert "workflow_schedules" in str(exc)
    finally:
        client.destroy()


def test_unset_env_leaves_the_schedule_and_off_removes_it(crash_schema, tmp_path):
    """Decided change: a worker without the env never turns retention off for the others; only an
    explicit EAP_MEMORY_RETENTION_DAYS=off removes the shared schedule."""
    client = client_of(crash_schema)

    def schedules():
        return [s["schedule"] for s in client.list_schedules(schedule_name_prefix=RETENTION_SCHEDULE_NAME)]

    def start(name: str, **env):
        w = WorkerProcess(_env(crash_schema, f"ret-{name}-{uuid.uuid4().hex[:6]}", **env), tmp_path / f"{name}.log")
        try:
            w.wait_ready()
        finally:
            w.stop()

    try:
        start("on", EAP_MEMORY_RETENTION_SCHEDULE="0 0 4 * * *", **HARMLESS)
        assert schedules() == ["0 0 4 * * *"]
        start("unset")
        assert schedules() == ["0 0 4 * * *"]  # left for the configured workers
        start("off", EAP_MEMORY_RETENTION_DAYS="off")
        assert schedules() == []
    finally:
        client.destroy()
