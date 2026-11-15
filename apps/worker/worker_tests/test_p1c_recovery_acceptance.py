"""P1.C acceptance, recovery under kill -9 with real worker subprocesses (same executor id):

- `post_turn_done`: a crash after the post-turn steps and before the event is recorded exactly once
  (event_key `memory:post_turn:{turn}`), whatever the spec's mix of post-turn features;
- the embed split: a crash between eap.kb_embed_call and eap.kb_embed_write recovers by writing
  once, without a second provider call, without a duplicate chunk row;
- an observer checkpoint recorded before P1.C (its plan still holds the prompt) replays unchanged;
- a kb_embed_batch run (enqueued without `embed_split`) recovers on the new worker.
"""

from __future__ import annotations

import base64
import json
import math
import pickle
import uuid

import psycopg
import pytest
from dbos import DBOSClient

from eap_run_store import KnowledgeStore, MemoryStore
from worker_tests.conftest import DBOS_DB_URL, enqueue_run, event_rows, wait_terminal
from worker_tests.kb_ingest_helpers import (
    assert_contiguous,
    chunk_stats,
    cleanup_knowledge,
    create_kb,
    enqueue_kb_ingest,
    ingest_input,
    markdown_doc,
    upload,
    usage_rows,
)
from worker_tests.test_crash_recovery import APP_VERSION, WorkerProcess, _env, crash_schema  # noqa: F401 - fixture
from worker_tests.test_kb_embed_split import split_input
from worker_tests.test_observational_memory import (  # noqa: F401 - tenant fixture (cleans observations)
    completed_turn,
    fact,
    obs_spec,
    observations,
    tenant,
    usage_keys,
    wait_workflow,
)
from worker_tests.test_semantic_recall import recall_spec
from worker_tests.test_working_memory import conversation

DONE = "post_turn_done"
EMBED_ENV = {"EAP_ALLOWED_EMBEDDING_MODELS": "test:*", "EAP_ALLOWED_MODELS": "test:*"}


def _post_turn_events(worker, run_id) -> list[tuple[int, str, dict]]:
    return [(s, k, d) for s, k, t, d in event_rows(worker.engine, run_id) if t == DONE]


def _both_spec(**obs) -> dict:
    return recall_spec(observational=obs_spec(**obs)["memory"]["observational"])


@pytest.mark.parametrize(("label", "point", "expected"), [
    ("recall-only, crash after the index", "after_memory_index", {"observe": "off", "index": "done"}),
    ("both, crash after the index", "after_memory_index", {"observe": "created", "index": "done"}),
    ("both, crash after the observation write", "after_observation_write", {"observe": "created", "index": "done"}),
    ("observational-only, crash after the observation write", "after_observation_write",
     {"observe": "created", "index": "off"}),
    ("observational-only, crash in the observer request", "observer_request", {"observe": "created", "index": "off"}),
])
def test_a_crash_before_post_turn_done_records_it_exactly_once(worker, tenant, crash_schema, tmp_path, label, point,
                                                                expected):  # noqa: F811
    agent = uuid.uuid4()
    conv = conversation(worker, tenant, agent)
    if expected["observe"] == "off":
        spec = recall_spec()
    elif expected["index"] == "off":
        spec = obs_spec(keep=0)
    else:
        spec = _both_spec(keep=0)
    executor_id = f"ptd-{uuid.uuid4().hex[:8]}"
    first = WorkerProcess(_env(crash_schema, executor_id, EAP_CRASH_AT=point, **EMBED_ENV), tmp_path / "w1.log")
    second: WorkerProcess | None = None
    client: DBOSClient | None = None
    try:
        first.wait_ready()
        client = DBOSClient(system_database_url=DBOS_DB_URL, dbos_system_schema=crash_schema)
        text_in = fact(1, 1000)
        run_id, created = worker.run_store.create_turn(tenant, conv["id"], 1, text_in, uuid.uuid4())
        assert created
        enqueue_run(client, worker.run_store, tenant_id=tenant, spec=spec, input_text=text_in,
                    app_version=APP_VERSION, run_id=run_id, agent_id=agent, create_row=False)
        assert first.wait_exit() == 137, (tmp_path / "w1.log").read_text()
        assert _post_turn_events(worker, run_id) == [], label  # killed before the event
        assert worker.run_store.get_run(tenant, run_id)["status"] == "completed"

        second = WorkerProcess(_env(crash_schema, executor_id, **EMBED_ENV), tmp_path / "w2.log")
        wait_workflow(crash_schema, run_id, timeout=90)
        events = event_rows(worker.engine, run_id)
        assert [e[0] for e in events] == list(range(len(events)))  # contiguous seq
        keys = [e[1] for e in events]
        assert len(keys) == len(set(keys)), keys
        assert [(k, d) for _, k, d in _post_turn_events(worker, run_id)] == [("memory:post_turn:1", expected)], label
        assert keys[-1] == "memory:post_turn:1" and keys.index("end") < keys.index("memory:post_turn:1")
        assert "word" not in json.dumps(_post_turn_events(worker, run_id))  # no text
        if expected["observe"] == "created":
            assert [o.kind for o in observations(worker, tenant, conv["id"])] == ["observation"]  # one, not two
    finally:
        first.stop()
        if second is not None:
            second.stop()
        if client is not None:
            client.destroy()


# --------------------------------------------------------------------------- the embed split


def _checkpoint_names(schema: str, run_id) -> list[str]:
    with psycopg.connect(DBOS_DB_URL, connect_timeout=5) as conn:
        return [r[0] for r in conn.execute(
            f'SELECT function_name FROM "{schema}".operation_outputs WHERE workflow_uuid = %s ORDER BY function_id',
            (str(run_id),)).fetchall()]


def test_a_crash_between_embed_call_and_embed_write_writes_once_without_a_second_provider_call(
    worker, tenant, crash_schema, tmp_path  # noqa: F811
):
    store = KnowledgeStore(worker.engine)
    kb_id = create_kb(store, tenant)
    doc_id, version, run_id = upload(store, tenant, kb_id, markdown_doc(6))
    executor_id = f"split-{uuid.uuid4().hex[:8]}"
    # Killed inside eap.kb_embed_write, before its transaction: the call's checkpoint is recorded.
    first = WorkerProcess(_env(crash_schema, executor_id, EAP_CRASH_AT="kb_batch_before_commit",
                               EAP_CRASH_EVENT_KEY="embed:batch:0", **EMBED_ENV), tmp_path / "w1.log")
    # The recovering worker may NOT call the provider: test:hash is no longer an allowed embedding
    # model there, so a second provider call would end the run `failed` (embedding_model_not_allowed).
    second: WorkerProcess | None = None
    client: DBOSClient | None = None
    try:
        first.wait_ready()
        client = DBOSClient(system_database_url=DBOS_DB_URL, dbos_system_schema=crash_schema)
        enqueue_kb_ingest(client, worker.run_store, tenant_id=tenant, kb_id=kb_id, input_json=split_input(doc_id, version),
                          app_version=APP_VERSION, run_id=run_id)
        assert first.wait_exit() == 137, (tmp_path / "w1.log").read_text()
        names = _checkpoint_names(crash_schema, run_id)
        assert names.count("eap.kb_embed_call") == 1 and "eap.kb_embed_write" not in names, names
        assert usage_rows(worker.engine, run_id) == [] and chunk_stats(worker.engine, tenant, doc_id)[0]["embedded"] == 0

        second = WorkerProcess(_env(crash_schema, executor_id, **{**EMBED_ENV, "EAP_ALLOWED_EMBEDDING_MODELS": "test:hash768=768"}),
                               tmp_path / "w2.log")
        second.wait_ready()
        run = wait_terminal(worker.run_store, tenant, run_id, timeout=120)
        assert run["status"] == "completed", (run["error"], (tmp_path / "w2.log").read_text()[-3000:])
        # The provider was called once in all: by the first worker (its "embedding call" log line).
        assert (tmp_path / "w1.log").read_text().count("embedding call") == 1
        assert "embedding call" not in (tmp_path / "w2.log").read_text()
        stats = chunk_stats(worker.engine, tenant, doc_id)
        assert len(stats) == 1 and stats[0]["n"] == stats[0]["ordinals"] == stats[0]["embedded"] >= 6  # no duplicates
        assert [u["request_key"] for u in usage_rows(worker.engine, run_id)] == ["embed:batch:0"]  # one usage row
        events = event_rows(worker.engine, run_id)
        assert_contiguous(events)
        assert [e[1] for e in events] == ["start", "chunk:0", "embed:batch:0", "finalize:0", "end"]
        assert _checkpoint_names(crash_schema, run_id).count("eap.kb_embed_call") == 2  # the final "done" probe
    finally:
        first.kill()
        if second is not None:
            second.stop()
        if client is not None:
            client.destroy()
        cleanup_knowledge(worker.engine, tenant)


# --------------------------------------------------------------------------- a pre-deploy observer checkpoint


def _rewrite_plan(schema: str, workflow_id: str, rewrite) -> dict:
    """Replace the recorded output of the observer workflow's `eap.memory_observe` step with
    `rewrite(plan)` (what a worker from before P1.C would have checkpointed). Returns the new plan."""
    with psycopg.connect(DBOS_DB_URL, connect_timeout=5, autocommit=True) as conn:
        row = conn.execute(
            f'SELECT function_id, output FROM "{schema}".operation_outputs WHERE workflow_uuid = %s'
            " AND function_name = 'eap.memory_observe'", (workflow_id,)).fetchone()
        assert row is not None, "the plan step was not checkpointed"
        plan = rewrite(pickle.loads(base64.b64decode(row[1])))
        conn.execute(
            f'UPDATE "{schema}".operation_outputs SET output = %s WHERE workflow_uuid = %s AND function_id = %s',
            (base64.b64encode(pickle.dumps(plan)).decode(), workflow_id, row[0]))
    return plan


def _old_observe_plan(plan: dict) -> dict:
    old = {k: v for k, v in plan.items() if k != "turn_numbers"}
    old["prompt"] = ('TASK: observe (observer-v1)\n<turn number="1" date="2026-11-01">\n'
                     'User: OLDSHAPE marker from the checkpoint\n</turn>')
    return old


def _old_reflect_plan(plan: dict) -> dict:
    old = {k: v for k, v in plan.items() if k not in ("input_ids", "observations", "kept_tokens", "dropped",
                                                      "dropped_tokens", "max_tokens")}
    old["prompt"] = ("TASK: reflect (observer-v1)\n" + "".join(
        f'<notes turns="{a}-{b}" kind="observation">\n- 2026-11-01 [medium] OLDREFLECT {a}\n</notes>\n'
        for a, b in ((1, 1), (2, 2), (2, 2))))
    return old


@pytest.mark.parametrize("stage", ["observe", "reflect"])
def test_a_checkpoint_recorded_before_the_change_still_holding_its_prompt_replays_unchanged(
    worker, tenant, crash_schema, tmp_path, stage  # noqa: F811
):
    from eap_worker.observational import observer_workflow_id

    agent = uuid.uuid4()
    conv = conversation(worker, tenant, agent)
    store = MemoryStore(worker.engine)
    if stage == "observe":
        completed_turn(worker, tenant, conv["id"], fact(1, 1000))
        spec, rewrite = obs_spec(keep=1), _old_observe_plan
    else:
        for i in (1, 2):
            prior = completed_turn(worker, tenant, conv["id"], f"turn {i}")
            store.insert_observation(tenant, str(agent), conv["id"], covers_from_turn=i, covers_to_turn=i,
                                     content=f"- 2026-11-0{i} [medium] " + "note " * 700, run_id=str(prior))
        spec, rewrite = obs_spec(keep=4, observe_after=200_000, reflect_after=1000), _old_reflect_plan
    executor_id = f"old-plan-{uuid.uuid4().hex[:8]}"
    first = WorkerProcess(_env(crash_schema, executor_id, EAP_CRASH_AT="observer_request", **EMBED_ENV), tmp_path / "w1.log")
    second: WorkerProcess | None = None
    client: DBOSClient | None = None
    try:
        first.wait_ready()
        client = DBOSClient(system_database_url=DBOS_DB_URL, dbos_system_schema=crash_schema)
        turn_no = 2 if stage == "observe" else 3
        run_id, created = worker.run_store.create_turn(tenant, conv["id"], 1, "next turn", uuid.uuid4())
        assert created
        enqueue_run(client, worker.run_store, tenant_id=tenant, spec=spec, input_text="next turn",
                    app_version=APP_VERSION, run_id=run_id, agent_id=agent, create_row=False)
        assert first.wait_exit() == 137, (tmp_path / "w1.log").read_text()
        plan = _rewrite_plan(crash_schema, observer_workflow_id(str(run_id)), rewrite)
        assert "prompt" in plan and "input_ids" not in plan and "turn_numbers" not in plan

        second = WorkerProcess(_env(crash_schema, executor_id, **EMBED_ENV), tmp_path / "w2.log")
        wait_workflow(crash_schema, run_id, timeout=90)
        [obs] = observations(worker, tenant, conv["id"])
        if stage == "observe":
            assert obs.kind == "observation" and "turn 1: OLDSHAPE marker" in obs.content, obs.content
        else:  # three notes in the recorded prompt, over turns 1-2: the recorded prompt was sent as is
            assert obs.kind == "reflection" and "reflection of 3 notes over turns 1-2" in obs.content, obs.content
        keys = [k for _, k, _, _ in event_rows(worker.engine, run_id)]
        assert keys.count(f"memory:{stage}:{turn_no}") == 1 and keys.count(f"memory:post_turn:{turn_no}") == 1
        assert not any(k.endswith(":trimmed") or k.endswith(":failed") for k in keys), keys
        assert usage_keys(worker.engine, run_id) == [f"memory:{stage}:{turn_no}", "model:0"]
    finally:
        first.stop()
        if second is not None:
            second.stop()
        if client is not None:
            client.destroy()


# --------------------------------------------------------------------------- an old-shape kb_embed_batch run

BATCH = 4


@pytest.mark.parametrize("point", ["kb_batch_before_commit", "kb_batch_after_commit"])
def test_a_run_enqueued_without_embed_split_recovers_on_the_new_worker_with_the_old_step(
    worker, tenant, crash_schema, tmp_path, point  # noqa: F811
):
    store = KnowledgeStore(worker.engine)
    kb_id = create_kb(store, tenant)
    doc_id, version, run_id = upload(store, tenant, kb_id, markdown_doc(10))
    input_json = ingest_input(doc_id, version)
    assert "embed_split" not in input_json  # the run as an older API enqueued it
    executor_id = f"old-batch-{uuid.uuid4().hex[:8]}"
    extra = {"EAP_CRASH_AT": point, "EAP_CRASH_EVENT_KEY": "embed:batch:1", "EAP_KB_EMBED_BATCH_CHUNKS": str(BATCH),
             **EMBED_ENV}
    first = WorkerProcess(_env(crash_schema, executor_id, **extra), tmp_path / "w1.log")
    second: WorkerProcess | None = None
    client: DBOSClient | None = None
    try:
        first.wait_ready()
        client = DBOSClient(system_database_url=DBOS_DB_URL, dbos_system_schema=crash_schema)
        enqueue_kb_ingest(client, worker.run_store, tenant_id=tenant, kb_id=kb_id, input_json=input_json,
                          app_version=APP_VERSION, run_id=run_id)
        assert first.wait_exit() == 137, (tmp_path / "w1.log").read_text()
        before = _checkpoint_names(crash_schema, run_id)
        assert before.count("eap.kb_embed_batch") == 1  # batch 0 recorded; batch 1 was in flight
        second = WorkerProcess(_env(crash_schema, executor_id, EAP_KB_EMBED_BATCH_CHUNKS=str(BATCH), **EMBED_ENV),
                               tmp_path / "w2.log")
        run = wait_terminal(worker.run_store, tenant, run_id, timeout=120)
        assert run["status"] == "completed", (run["error"], (tmp_path / "w2.log").read_text()[-3000:])
        names = _checkpoint_names(crash_schema, run_id)
        assert "eap.kb_embed_call" not in names and "eap.kb_embed_write" not in names
        stats = chunk_stats(worker.engine, tenant, doc_id)
        n = stats[0]["n"]
        assert len(stats) == 1 and n >= 10 and stats[0]["ordinals"] == n and stats[0]["embedded"] == n
        batches = math.ceil(n / BATCH)
        assert names.count("eap.kb_embed_batch") == batches + 1  # the last one finds nothing left
        assert [u["request_key"] for u in usage_rows(worker.engine, run_id)] == sorted(
            f"embed:batch:{i}" for i in range(batches))
        events = event_rows(worker.engine, run_id)
        assert_contiguous(events)
        assert [e[1] for e in events] == ["start", "chunk:0"] + [f"embed:batch:{i}" for i in range(batches)] + [
            "finalize:0", "end"]
    finally:
        first.kill()
        if second is not None:
            second.stop()
        if client is not None:
            client.destroy()
        cleanup_knowledge(worker.engine, tenant)
