"""RM.20 crash matrix: the RAG and memory milestone's durable data under kill -9, whole-system
(README "RAG and memory").

Every scenario goes through the real API (knowledge bases, documents, reindex, agents with memory,
conversations and turns are created over the API; runs are read back through `GET /runs/{id}` and
SSE) and kills a real `python -m eap_worker` at one of the RM crash points (eap_worker.crash). A
second worker with the same executor id then recovers the workflow. The worker-level crash tests
(apps/worker/worker_tests/test_kb_ingest_crash.py, test_kb_ingest_reindex_crash.py,
test_working_memory.py, test_observational_memory.py, test_semantic_recall.py) enqueue straight
through DBOSClient; this file adds the API hand-off and the P0.21 invariants on top. Each scenario
asserts:

- exactly one DBOS workflow for the run, in SUCCESS, and a `Recovering` line in the second worker;
- contiguous `seq` from 0, no duplicate `event_key`, the expected story, and every event stored
  before the crash unchanged;
- one `run_model_requests` row per request key with the expected model and operation, rows written
  before the crash unchanged (first write wins), and run totals (row and `GET /runs/{id}`) equal to
  the sum over the run's rows;
- the SSE replay from every resume point equals the database's events after it, up to the terminal
  event (post-turn memory events are stored after `run_completed`: a live stream ends before them,
  a resume from the terminal event or later returns them);
- the task's own data: exactly the expected chunk rows and the version or generation switch (RM.10),
  one note version and one ledger row (RM.12), one observation (RM.13), one recall event and one
  index row per role (RM.14).

| # | Kill | Proves |
|---|------|--------|
| K1 | `kb_chunk`, `kb_batch_before_commit` / `kb_batch_after_commit` on `embed:batch:1`, `kb_finalize` during an API upload of version 2 | no duplicate chunk rows; one usage row per batch; version 1 stays the active (searchable) one until the switch and is gone after it; the API search after recovery returns version 2 only |
| K2 | `kb_batch_before_commit` on `embed:batch:1` during an API reindex to `test:hash768` | the old generation stays active until the switch, one row per ordinal in the new one, one usage row per batch, then only the new generation at 768 dimensions |
| M1 | `after_tool_call` in `update_working_memory`, `load_memory` | the note is written once (version 1, one `memory_updated`, one ledger row) and the turn answers once |
| O1 | `observer_request`, `after_observation_write` in the post-turn observation of turn 2 | the answer was already delivered (`completed`) at the kill; one observation row, one `memory:observe:2` event and usage row |
| S1 | `recall_embedding` (pre-turn), `after_memory_index` (post-turn) in conversation B | one `embed:recall:1` and one `embed:index:1` event and usage row, one indexed row per role, and the fact from conversation A recalled |
"""

from __future__ import annotations

import json
import math
import uuid
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest

from conftest import REPO_ROOT, EventRow, Harness, WorkerProcess, wait_for

PRICES_FILE = str(REPO_ROOT / "config" / "model-prices.test.json")
BATCH = 4
WORKER_ENV = {
    "EAP_MODEL_PRICES_FILE": PRICES_FILE,
    "EAP_ALLOWED_MODELS": "test:*",
    "EAP_ALLOWED_EMBEDDING_MODELS": "test:*",
    "EAP_KB_EMBED_BATCH_CHUNKS": str(BATCH),
}
TERMINAL_TYPES = {"run_completed", "run_failed"}

WM_SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "city": {"type": "string"}},
}


def agent_spec(model: str, memory: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": "Memory agent",
        "instructions": "Answer the user.",
        "model": {"provider": "test", "name": model},
        "tools": [],
        "memory": memory,
    }


def markdown_doc(sections: int, *, tag: str) -> str:
    """`sections` `##` sections, each over min_tokens so it stays its own chunk, with distinct words."""
    parts = [f"# Handbook {tag}\n"]
    for i in range(sections):
        words = " ".join(f"w{tag}{i}x{j}" for j in range(40))
        parts.append(f"## Section {i}\n\nSection {i} of the {tag} handbook explains {words}.\n")
    return "\n".join(parts)


# --------------------------------------------------------------------------- helpers


@pytest.fixture
def rm(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> Harness:
    """The harness, with the in-process API seeing the same embedding allowlist as the workers."""
    for key, value in WORKER_ENV.items():
        monkeypatch.setenv(key, value)
    return harness


def start_worker(h: Harness, name: str, **crash: str) -> WorkerProcess:
    return h.start_worker(name, extra_env=WORKER_ENV, **crash)


def crash(h: Harness, worker: WorkerProcess, crash_at: str) -> None:
    assert worker.wait_exit() == 137, worker.log()[-4000:]
    assert f"EAP_CRASH_AT={crash_at} reached" in worker.log()


def recover(h: Harness, run_id: str) -> WorkerProcess:
    """A second worker (same executor id, no crash settings); returns once the run's DBOS workflow
    is SUCCESS (a post-turn step runs after the run row is already completed)."""
    second = start_worker(h, "worker2-recovering")
    try:
        wait_for(lambda: h.workflows(run_id) and h.workflows(run_id)[0][0] == "SUCCESS", timeout=120,
                 what=f"workflow {run_id} SUCCESS")
    except AssertionError as exc:
        raise AssertionError(f"{exc}\n{h.worker_logs()}") from None
    assert "Recovering" in second.log(), second.log()[-4000:]
    return second


def usage_rows(h: Harness, run_id: str) -> dict[str, dict[str, Any]]:
    rows = h.sql(
        "SELECT request_key, model_id, operation, input_tokens, output_tokens, cost_usd, created_at"
        " FROM run_model_requests WHERE run_id = :r",
        r=run_id,
    )
    return {r.request_key: dict(r._mapping) for r in rows}


def assert_recovered_once(
    h: Harness,
    run_id: str,
    *,
    before: list[EventRow],
    usage_before: dict[str, dict[str, Any]],
    expected_keys: list[str],
    expected_requests: dict[str, tuple[str, str]],
) -> list[EventRow]:
    """The RM.20 invariants. `expected_requests` maps request_key -> (model_id, operation)."""
    events = h.events(run_id)
    keys = [e.event_key for e in events]
    assert [e.seq for e in events] == list(range(len(events))), [e.seq for e in events]
    assert len(keys) == len(set(keys)), keys
    assert keys == expected_keys, keys
    assert [(e.seq, e.event_key, e.type, e.data) for e in events[: len(before)]] == [
        (e.seq, e.event_key, e.type, e.data) for e in before
    ], "an event written before the crash changed on recovery"
    assert sum(1 for e in events if e.type in TERMINAL_TYPES) == 1
    workflows = h.workflows(run_id)
    assert len(workflows) == 1 and workflows[0][0] == "SUCCESS", workflows

    usage = usage_rows(h, run_id)
    assert {k: (v["model_id"], v["operation"]) for k, v in usage.items()} == expected_requests, usage
    for key, row in usage_before.items():
        assert usage[key] == row, f"usage row {key} changed on recovery"
    for key, row in usage.items():
        if row["operation"] == "embedding":
            assert row["output_tokens"] == 0, (key, row)
        assert row["cost_usd"] is not None and row["cost_usd"] > 0, (key, row)

    (totals,) = h.sql("SELECT input_tokens, output_tokens, cost_usd FROM runs WHERE id = :r", r=run_id)
    sum_in = sum(r["input_tokens"] for r in usage.values())
    sum_out = sum(r["output_tokens"] for r in usage.values())
    sum_cost = sum((r["cost_usd"] for r in usage.values()), Decimal(0))
    assert (totals.input_tokens, totals.output_tokens, totals.cost_usd) == (sum_in, sum_out, sum_cost)
    body = h.client.get(f"/api/v1/runs/{run_id}", headers=h.headers)
    assert body.status_code == 200, body.text
    assert (body.json()["input_tokens"], body.json()["output_tokens"]) == (sum_in, sum_out)
    assert Decimal(body.json()["cost_usd"]) == sum_cost

    # SSE: from a resume point before the terminal event, the database's events after it up to and
    # including that event (the stream ends there; post-turn events follow it in the table); from a
    # point at or after it, the events after the point (a terminal run with no further events).
    terminal_seq = next(e.seq for e in events if e.type in TERMINAL_TYPES)
    db = [(e.seq, e.type, e.data) for e in events]
    last = events[-1].seq
    for point in sorted({-1, 0, len(before) - 1, terminal_seq // 2, terminal_seq - 1, terminal_seq, last}):
        frames = h.sse(run_id, after_seq=point)
        got = [(f.data["seq"], f.data["type"], f.data["data"]) for f in frames if f.event == "run_event"]
        end = terminal_seq if point < terminal_seq else last
        assert got == [row for row in db if point < row[0] <= end], f"replay after_seq={point} differs from the DB"
    return events


# --------------------------------------------------------------------------- K1 / K2: ingestion


def create_kb(h: Harness, model: str = "test:hash") -> str:
    r = h.client.post(
        "/api/v1/knowledge-bases", json={"name": f"kb-{uuid.uuid4().hex[:8]}", "embedding_model": model},
        headers=h.headers,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def put_doc(h: Harness, kb_id: str, content: str) -> tuple[str, int, str]:
    """PUT the document over the API; (document_id, latest_version, ingest run_id)."""
    r = h.client.put(
        f"/api/v1/knowledge-bases/{kb_id}/documents/handbook",
        json={"title": "Handbook", "content": content, "mime_type": "text/markdown",
              "source_url": "https://docs.example.com/handbook", "metadata": {"team": "support"}},
        headers=h.headers,
    )
    assert r.status_code == 202, r.text
    body = r.json()
    h.run_ids.append(body["run_id"])
    return body["document"]["id"], body["document"]["latest_version"], body["run_id"]


def chunk_stats(h: Harness, kb_id: str) -> list[dict[str, Any]]:
    return [
        dict(r._mapping)
        for r in h.sql(
            "SELECT doc_version, generation, count(*) AS n, count(embedding) AS embedded,"
            " count(DISTINCT ordinal) AS ordinals, max(dimensions) AS dims FROM kb_chunks"
            " WHERE tenant_id = :t AND kb_id = :k GROUP BY doc_version, generation ORDER BY doc_version, generation",
            t=h.tenant, k=kb_id,
        )
    ]


def document(h: Harness, doc_id: str) -> dict[str, Any]:
    (row,) = h.sql(
        "SELECT active_version, latest_version, status, chunk_count FROM kb_documents WHERE tenant_id = :t AND id = :d",
        t=h.tenant, d=doc_id,
    )
    return dict(row._mapping)


def search(h: Harness, kb_id: str, query: str) -> list[dict[str, Any]]:
    r = h.client.post(f"/api/v1/knowledge-bases/{kb_id}/search", json={"query": query, "top_k": 20}, headers=h.headers)
    assert r.status_code == 200, r.text
    body = r.json()
    h.run_ids.append(body["run_id"])
    assert body["status"] == "completed", body
    return body["result"]["hits"]


INGEST_CASES = {
    # name: (crash point, event key filter, version 2 active at the crash)
    "chunk-upsert": ("kb_chunk", None, False),
    "batch-before-commit": ("kb_batch_before_commit", "embed:batch:1", False),
    "batch-after-commit": ("kb_batch_after_commit", "embed:batch:1", False),
    "finalize": ("kb_finalize", None, True),
}


@pytest.mark.parametrize("case", list(INGEST_CASES))
def test_k1_crash_during_document_ingest(rm: Harness, report: Callable[[str], None], case: str) -> None:
    h = rm
    crash_at, event_key, switched = INGEST_CASES[case]
    healthy = start_worker(h, "worker0-healthy")
    kb_id = create_kb(h)
    doc_id, v1, run1 = put_doc(h, kb_id, markdown_doc(3, tag="old"))
    h.wait_completed(run1)
    v1_chunks = chunk_stats(h, kb_id)[0]["n"]
    healthy.stop()

    crash_kwargs = {"crash_at": crash_at, **({"event_key": event_key} if event_key else {})}
    first = start_worker(h, "worker1-crashing", **crash_kwargs)
    _, v2, run_id = put_doc(h, kb_id, markdown_doc(10, tag="new"))
    assert v2 == v1 + 1
    crash(h, first, crash_at)
    assert h.run_row(run_id)["status"] == "running"
    before = h.events(run_id)
    usage_before = usage_rows(h, run_id)
    # Which batches had committed at the kill (each batch's usage row rides on its commit).
    committed = {
        "kb_chunk": set(),
        "kb_batch_before_commit": {"embed:batch:0"},
        "kb_batch_after_commit": {"embed:batch:0", "embed:batch:1"},
        "kb_finalize": {e.event_key for e in before if e.event_key.startswith("embed:batch:")},
    }[crash_at]
    assert set(usage_before) == committed, sorted(usage_before)
    if crash_at == "kb_finalize":
        # Every batch committed before the switch: 0..k-1, and >= 10 chunks make >= 3 batches.
        assert committed == {f"embed:batch:{i}" for i in range(len(committed))} and len(committed) >= 3

    # At the kill: version 1 is the searchable one (fully embedded) unless the switch committed.
    doc = document(h, doc_id)
    assert doc["active_version"] == (v2 if switched else v1)
    if not switched:
        (old,) = [s for s in chunk_stats(h, kb_id) if s["doc_version"] == v1]
        assert old["n"] == old["embedded"] == v1_chunks
    else:
        assert [s["doc_version"] for s in chunk_stats(h, kb_id)] == [v2]  # the switch dropped v1
    pending = [s for s in chunk_stats(h, kb_id) if s["doc_version"] == v2]
    if pending:
        assert pending[0]["n"] == pending[0]["ordinals"], "duplicate chunk rows at the kill"

    recover(h, run_id)
    stats = chunk_stats(h, kb_id)
    assert len(stats) == 1 and stats[0]["doc_version"] == v2, stats
    n = stats[0]["n"]
    assert n >= 10 and stats[0]["ordinals"] == n and stats[0]["embedded"] == n and stats[0]["dims"] == 1536
    doc = document(h, doc_id)
    assert (doc["status"], doc["active_version"], doc["chunk_count"]) == ("ready", v2, n)

    batches = math.ceil(n / BATCH)
    batch_keys = [f"embed:batch:{i}" for i in range(batches)]
    events = assert_recovered_once(
        h, run_id, before=before, usage_before=usage_before,
        expected_keys=["start", "chunk:0", *batch_keys, "finalize:0", "end"],
        expected_requests={k: ("test:hash", "embedding") for k in batch_keys},
    )
    progress = [e.data for e in events if e.event_key.startswith("embed:batch:")]
    assert len(progress) == batches and all(e.type == "ingest_progress" for e in events if e.event_key in batch_keys)
    assert [p["embedded"] for p in progress][-1] == n and all(p["total"] == n for p in progress)
    assert json.loads(h.run_row(run_id)["output"])["chunk_count"] == n

    # Through the API: the new version only, never the old one.
    hits = search(h, kb_id, "handbook section explains")
    assert hits and {hit["doc_version"] for hit in hits} == {v2}
    assert not any("wold" in hit["content"] for hit in hits)
    report(f"RM K1 {case} ({crash_at}): {n} chunks, batches {batches}, usage rows {len(batch_keys)}, "
           f"active v{v2} after recovery (v{doc['active_version']})")


def test_k2_crash_during_reindex(rm: Harness, report: Callable[[str], None]) -> None:
    h = rm
    healthy = start_worker(h, "worker0-healthy")
    kb_id = create_kb(h)
    doc_id, v1, run1 = put_doc(h, kb_id, markdown_doc(10, tag="re"))
    h.wait_completed(run1)
    (old,) = chunk_stats(h, kb_id)
    old_gen, n = old["generation"], old["n"]
    healthy.stop()

    first = start_worker(h, "worker1-crashing", crash_at="kb_batch_before_commit", event_key="embed:batch:1")
    r = h.client.post(f"/api/v1/knowledge-bases/{kb_id}/reindex", json={"embedding_model": "test:hash768"},
                      headers=h.headers)
    assert r.status_code == 202, r.text
    run_id = r.json()["run_id"]
    h.run_ids.append(run_id)
    crash(h, first, "kb_batch_before_commit")
    before = h.events(run_id)
    usage_before = usage_rows(h, run_id)
    assert set(usage_before) == {"embed:batch:0"}  # batch 1 died before its commit

    kb = h.client.get(f"/api/v1/knowledge-bases/{kb_id}", headers=h.headers).json()
    assert (kb["active_generation"], kb["embedding_model"], kb["dimensions"]) == (old_gen, "test:hash", 1536)
    at_crash = {s["generation"]: s for s in chunk_stats(h, kb_id)}
    assert at_crash[old_gen]["n"] == at_crash[old_gen]["embedded"] == n
    new_gen = next(g for g in at_crash if g != old_gen)
    assert at_crash[new_gen]["n"] == at_crash[new_gen]["ordinals"] == n
    assert at_crash[new_gen]["embedded"] == BATCH

    recover(h, run_id)
    kb = h.client.get(f"/api/v1/knowledge-bases/{kb_id}", headers=h.headers).json()
    assert (kb["active_generation"], kb["embedding_model"], kb["dimensions"], kb["status"]) == (
        new_gen, "test:hash768", 768, "ready",
    )
    assert [(s["generation"], s["n"], s["ordinals"], s["embedded"], s["dims"]) for s in chunk_stats(h, kb_id)] == [
        (new_gen, n, n, n, 768)
    ]
    batch_keys = [f"embed:batch:{i}" for i in range(math.ceil(n / BATCH))]
    assert_recovered_once(
        h, run_id, before=before, usage_before=usage_before,
        expected_keys=["start", "chunk:0", *batch_keys, "finalize:0", "end"],
        expected_requests={k: ("test:hash768", "embedding") for k in batch_keys},
    )
    hits = search(h, kb_id, "handbook section explains")
    assert hits and {hit["doc_version"] for hit in hits} == {v1}
    report(f"RM K2 reindex kb_batch_before_commit: generation {old_gen} -> {new_gen}, {n} chunks at 768, "
           f"usage rows {len(batch_keys)}")


# --------------------------------------------------------------------------- M1: working memory


@pytest.mark.parametrize("crash_at", ["after_tool_call", "load_memory"])
def test_m1_crash_in_a_working_memory_turn(rm: Harness, report: Callable[[str], None], crash_at: str) -> None:
    h = rm
    first = start_worker(h, "worker1-crashing", crash_at=crash_at)
    agent_id = h.create_published_agent(agent_spec("memory", {"working": {"json_schema": WM_SCHEMA}}))
    conv = h.create_conversation(agent_id)
    run_id = str(uuid.uuid4())
    assert h.post_turn(conv, run_id, "remember name=Ana").status_code == 201
    crash(h, first, crash_at)
    assert h.run_row(run_id)["status"] == "running"
    before = h.events(run_id)
    usage_before = usage_rows(h, run_id)
    if crash_at == "after_tool_call":
        # The write, its event and the ledger row committed; the tool result did not.
        assert before[-1].type == "memory_updated", [e.event_key for e in before]
        assert not any(e.type == "tool_result" for e in before)
    else:
        assert [e.event_key for e in before] == ["start"]

    recover(h, run_id)
    events = h.events(run_id)
    [updated] = [e for e in events if e.type == "memory_updated"]
    call_id = updated.event_key.removeprefix("memory:wm:")
    assert call_id and call_id != updated.event_key, updated.event_key
    expected = ["start", "model:0:final", f"tool:{call_id}:call", f"memory:wm:{call_id}", f"tool:{call_id}:result",
                "model:1:final", "end"]
    assert_recovered_once(
        h, run_id, before=before, usage_before=usage_before, expected_keys=expected,
        expected_requests={"model:0": ("test:memory", "chat"), "model:1": ("test:memory", "chat")},
    )
    notes = h.sql("SELECT scope, scope_key, version, content FROM working_memories WHERE tenant_id = :t", t=h.tenant)
    assert [(r.scope, r.scope_key, r.version) for r in notes] == [("resource", "alice", 1)]
    assert notes[0].content.get("name") == "Ana", notes[0].content
    # The ledger row (eap_run_store.memory.ledger_row_key: `wm:{run_id}:{tool_call_id}|{agent_id}`).
    ledger = h.sql("SELECT key, state FROM side_effects WHERE tenant_id = :t AND key LIKE 'wm:%'", t=h.tenant)
    assert [(r.key, r.state) for r in ledger] == [(f"wm:{run_id}:{call_id}|{agent_id}", "completed")]
    report(f"RM M1 {crash_at}: note version 1, one memory_updated, one ledger row, {len(events)} events")


# --------------------------------------------------------------------------- O1: observational memory


OBS_MEMORY = {
    "observational": {
        "model": {"provider": "test", "name": "observer"},
        "observe_after_tokens": 1000,
        "keep_recent_turns": 1,
        "reflect_after_tokens": 200_000,
    }
}


@pytest.mark.parametrize("crash_at", ["observer_request", "after_observation_write"])
def test_o1_crash_in_the_post_turn_observation(rm: Harness, report: Callable[[str], None], crash_at: str) -> None:
    h = rm
    healthy = start_worker(h, "worker0-healthy")
    agent_id = h.create_published_agent(agent_spec("echo", OBS_MEMORY))
    conv = h.create_conversation(agent_id)
    run1 = str(uuid.uuid4())
    assert h.post_turn(conv, run1, "Fact 1: " + "word " * 1000).status_code == 201
    h.wait_completed(run1)
    wait_for(lambda: h.workflows(run1) and h.workflows(run1)[0][0] == "SUCCESS", timeout=60, what="turn 1 workflow")
    healthy.stop()
    assert h.sql("SELECT count(*) FROM memory_observations WHERE tenant_id = :t", t=h.tenant)[0][0] == 0

    first = start_worker(h, "worker1-crashing", crash_at=crash_at)
    run2 = str(uuid.uuid4())
    assert h.post_turn(conv, run2, "and the next turn").status_code == 201
    crash(h, first, crash_at)
    assert h.run_row(run2)["status"] == "completed"  # the answer was delivered before the observer ran
    before = h.events(run2)
    usage_before = usage_rows(h, run2)
    assert before[-1].event_key == ("memory:observe:2" if crash_at == "after_observation_write" else "end")

    recover(h, run2)
    assert_recovered_once(
        h, run2, before=before, usage_before=usage_before,
        expected_keys=["start", "model:0:final", "end", "memory:observe:2"],
        expected_requests={"model:0": ("test:echo", "chat"), "memory:observe:2": ("test:observer", "chat")},
    )
    rows = h.sql(
        "SELECT kind, covers_from_turn, covers_to_turn, superseded_by FROM memory_observations"
        " WHERE tenant_id = :t AND conversation_id = :c",
        t=h.tenant, c=conv,
    )
    assert [(r.kind, r.covers_from_turn, r.covers_to_turn, r.superseded_by) for r in rows] == [
        ("observation", 1, 1, None)
    ]
    report(f"RM O1 {crash_at}: run completed before the kill; one observation (turn 1), one usage row")


# --------------------------------------------------------------------------- S1: semantic recall


@pytest.mark.parametrize("crash_at", ["recall_embedding", "after_memory_index"])
def test_s1_crash_in_semantic_recall(rm: Harness, report: Callable[[str], None], crash_at: str) -> None:
    h = rm
    healthy = start_worker(h, "worker0-healthy")
    agent_id = h.create_published_agent(agent_spec("context", {"semantic_recall": {"embedding_model": "test:hash"}}))
    conv_a = h.create_conversation(agent_id)
    run_a = str(uuid.uuid4())
    assert h.post_turn(conv_a, run_a, "the code word is pelican").status_code == 201
    h.wait_completed(run_a)
    wait_for(lambda: h.workflows(run_a) and h.workflows(run_a)[0][0] == "SUCCESS", timeout=60, what="index of A")
    healthy.stop()

    first = start_worker(h, "worker1-crashing", crash_at=crash_at)
    conv_b = h.create_conversation(agent_id)
    run_b = str(uuid.uuid4())
    assert h.post_turn(conv_b, run_b, "what is the code word?").status_code == 201
    crash(h, first, crash_at)
    assert h.run_row(run_b)["status"] == ("running" if crash_at == "recall_embedding" else "completed")
    before = h.events(run_b)
    usage_before = usage_rows(h, run_b)
    indexed_at_kill = h.sql(
        "SELECT count(*) FROM message_embeddings WHERE tenant_id = :t AND conversation_id = :c", t=h.tenant, c=conv_b
    )[0][0]
    if crash_at == "after_memory_index":  # the index transaction committed; its checkpoint did not
        assert before[-1].event_key == "embed:index:1", [e.event_key for e in before]
        assert indexed_at_kill == 2
    else:
        assert indexed_at_kill == 0

    recover(h, run_b)
    run = h.run_row(run_b)
    assert run["status"] == "completed" and "the code word is pelican" in run["output"]
    events = assert_recovered_once(
        h, run_b, before=before, usage_before=usage_before,
        expected_keys=["start", "embed:recall:1", "model:0:final", "end", "embed:index:1"],
        expected_requests={
            "embed:recall:1": ("test:hash", "embedding"),
            "model:0": ("test:context", "chat"),
            "embed:index:1": ("test:hash", "embedding"),
        },
    )
    rows = h.sql(
        "SELECT turn, role FROM message_embeddings WHERE tenant_id = :t AND conversation_id = :c ORDER BY role",
        t=h.tenant, c=conv_b,
    )
    assert [(r.turn, r.role) for r in rows] == [(1, "assistant"), (1, "user")]
    # Events carry counts and ids, never the recalled or indexed text.
    for e in events:
        if e.type == "embedding":
            assert "pelican" not in json.dumps(e.data), e.event_key
    report(f"RM S1 {crash_at}: one recall and one index event and usage row; 2 index rows; fact recalled")
