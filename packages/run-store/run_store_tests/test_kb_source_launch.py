"""The shared kb source sync launch service: validation, idempotent rows, insert-before-enqueue recovery."""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from eap_run_store import KnowledgeStore, RunStore
from eap_run_store.kb_source_launch import (
    LaunchFailed,
    SourceLaunchError,
    launch_sync,
    tool_run_id,
)
from eap_run_store.kb_sources import ConnectionStore, SourceStore, SyncStore

REFS = {"client_id": "secret:a", "client_secret": "secret:b"}


class FakeLauncher:
    def __init__(self, fail: int = 0) -> None:
        self.calls: list[dict] = []
        self.fail = fail

    def launch_kb_source_sync(self, **kw) -> None:
        self.calls.append(kw)
        if self.fail:
            self.fail -= 1
            raise RuntimeError("postgresql://user:hunter2@db/x is down")


@pytest.fixture
def world(engine):
    tenants = (f"t_{uuid.uuid4().hex[:8]}", f"t_{uuid.uuid4().hex[:8]}")
    out = {}
    for t in tenants:
        kb = KnowledgeStore(engine).create_knowledge_base(t, name="kb", embedding_model="openai:m", dimensions=768)["id"]
        c = ConnectionStore(engine).create(
            t, name="c", kind="oauth2_client_credentials", base_url="https://x.example", config={}, credential_refs=REFS
        )
        s = SourceStore(engine).create(
            t, knowledge_base_id=kb, connection_id=c.id, name="s", kind="product_catalog", provider="fake"
        )
        out[t] = s
    yield out
    with engine.begin() as conn:
        for t in tenants:
            conn.execute(text("DELETE FROM execution_events WHERE tenant_id = :t"), {"t": t})
            conn.execute(text("DELETE FROM runs WHERE tenant_id = :t"), {"t": t})
            conn.execute(text("DELETE FROM knowledge_bases WHERE tenant_id = :t"), {"t": t})
            conn.execute(text("DELETE FROM settings_connections WHERE tenant_id = :t"), {"t": t})


def launch(engine, launcher, tenant, source, **kw):
    args = dict(mode="targeted", part_numbers=["LM317"], run_id=uuid.uuid4(), created_by="alice")
    args.update(kw)
    return launch_sync(engine, launcher, tenant_id=tenant, source_id=source.id, **args)


def test_launch_creates_run_and_sync_rows_and_enqueues_once(engine, world):
    (a, src), = [(t, s) for t, s in list(world.items())[:1]]
    fake, rid = FakeLauncher(), uuid.uuid4()
    out = launch(engine, fake, a, src, run_id=rid)
    assert out == {"run_id": str(rid), "status": "queued"}
    run = RunStore(engine).get_run(a, rid)
    assert run["kind"] == "kb_source_sync" and run["status"] == "queued"
    sync = SyncStore(engine).get_by_run(a, str(rid))
    assert sync.source_id == src.id and sync.mode == "targeted" and sync.parent_run_id is None
    assert len(sync.request_hash) == 64
    (call,) = fake.calls
    assert call["run_id"] == rid and call["tenant_id"] == a and call["source_id"] == src.id
    assert json.loads(call["request_json"]) == {"mode": "targeted", "part_numbers": ["LM317"]}


def test_replay_with_same_run_id_creates_nothing_new_and_reenqueues_idempotently(engine, world):
    a, src = next(iter(world.items()))
    fake, rid = FakeLauncher(), uuid.uuid4()
    launch(engine, fake, a, src, run_id=rid)
    again = launch(engine, fake, a, src, run_id=rid)
    assert again == {"run_id": str(rid), "status": "queued"}
    assert len(SyncStore(engine).list_for_source(a, src.id, 10)) == 1
    assert len(fake.calls) == 2  # DBOS returns the existing workflow of the same id


def test_replay_of_a_finished_run_is_not_enqueued_again(engine, world):
    a, src = next(iter(world.items()))
    fake, rid = FakeLauncher(), uuid.uuid4()
    launch(engine, fake, a, src, run_id=rid)
    RunStore(engine).mark_running(a, rid)
    assert RunStore(engine).finish_with_end(a, rid, "completed", "{}", None, end_data={"report": {}})
    out = launch(engine, fake, a, src, run_id=rid)
    assert out == {"run_id": str(rid), "status": "completed"}
    assert len(fake.calls) == 1


def test_same_run_id_with_a_different_request_is_refused(engine, world):
    a, src = next(iter(world.items()))
    fake, rid = FakeLauncher(), uuid.uuid4()
    launch(engine, fake, a, src, run_id=rid)
    with pytest.raises(SourceLaunchError) as e:
        launch(engine, fake, a, src, run_id=rid, part_numbers=["LM318"])
    assert e.value.code == "run_id_conflict" and e.value.status == 409


def test_another_tenants_run_id_is_refused_without_leaking(engine, world):
    (a, sa), (b, sb) = list(world.items())
    fake, rid = FakeLauncher(), uuid.uuid4()
    launch(engine, fake, a, sa, run_id=rid)
    with pytest.raises(SourceLaunchError) as e:
        launch(engine, fake, b, sb, run_id=rid)
    assert e.value.code == "run_id_conflict"


def test_enqueue_failure_marks_the_run_failed_then_a_retry_requeues_it(engine, world):
    a, src = next(iter(world.items()))
    fake, rid = FakeLauncher(fail=1), uuid.uuid4()
    with pytest.raises(LaunchFailed) as e:
        launch(engine, fake, a, src, run_id=rid)
    assert e.value.run_id == str(rid) and "hunter2" not in str(e.value) and "hunter2" not in e.value.detail
    assert RunStore(engine).get_run(a, rid)["status"] == "failed"
    out = launch(engine, fake, a, src, run_id=rid)
    assert out["status"] == "queued"
    assert RunStore(engine).get_run(a, rid)["status"] == "queued"
    assert len(SyncStore(engine).list_for_source(a, src.id, 10)) == 1


def test_validation_codes(engine, world):
    (a, sa), (b, sb) = list(world.items())
    fake = FakeLauncher()

    def code(**kw):
        tenant, src = kw.pop("tenant", a), kw.pop("src", sa)
        with pytest.raises(SourceLaunchError) as e:
            launch(engine, fake, tenant, src, **kw)
        return e.value.code, e.value.status

    assert code(tenant=b) == ("source_not_found", 404)  # a's source through b
    assert code(mode="full", part_numbers=["LM317"]) == ("part_numbers_not_allowed", 422)
    assert code(mode="incremental", part_numbers=["x"]) == ("part_numbers_not_allowed", 422)
    assert code(mode="bogus", part_numbers=[]) == ("invalid_request", 422)
    assert code(part_numbers=[]) == ("invalid_request", 422)  # targeted needs parts
    assert code(part_numbers=["bad part"]) == ("invalid_request", 422)
    assert code(part_numbers=["x" * 129]) == ("invalid_request", 422)
    assert code(part_numbers=[f"P{i}" for i in range(101)]) == ("invalid_request", 422)
    SourceStore(engine).update(a, sa.id, enabled=False)
    assert code() == ("source_disabled", 409)
    assert fake.calls == []
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM runs WHERE tenant_id IN (:a, :b)"), {"a": a, "b": b}).scalar() == 0


def test_full_and_incremental_launch_without_parts(engine, world):
    a, src = next(iter(world.items()))
    fake = FakeLauncher()
    for mode in ("full", "incremental"):
        assert launch(engine, fake, a, src, mode=mode, part_numbers=[])["status"] == "queued"
    assert [json.loads(c["request_json"]) for c in fake.calls] == [
        {"mode": "full", "part_numbers": []}, {"mode": "incremental", "part_numbers": []}]


def test_parent_run_id_is_stored_and_tool_run_id_is_deterministic():
    a = tool_run_id("t", "s", "p", "call-1")
    assert a == tool_run_id("t", "s", "p", "call-1")
    assert a != tool_run_id("t", "s", "p", "call-2") and a != tool_run_id("t2", "s", "p", "call-1")
    assert a == uuid.uuid5(uuid.NAMESPACE_URL, "kb-source-sync:t:s:p:call-1")


def test_parent_run_id_is_recorded_on_the_sync_row(engine, world):
    a, src = next(iter(world.items()))
    rid = uuid.uuid4()
    launch(engine, FakeLauncher(), a, src, run_id=rid, parent_run_id=str(uuid.uuid4()))
    assert SyncStore(engine).get_by_run(a, str(rid)).parent_run_id is not None
