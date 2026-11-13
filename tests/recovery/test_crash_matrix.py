"""Task 9 crash-injection matrix (docs/plans/2026-11-11-milestone-1-vertical-slice.md).

Each scenario kills a real process at a named point (the worker's EAP_CRASH_AT hook, or the API
process inside its launcher), recovers with a fresh process, and then asserts the same invariants:

- the external side effect happened exactly once (one fake_external_tickets row for the run; the
  number of calls the idempotent service saw is reported, and may legitimately be 2);
- execution_events.seq is exactly 0..N with no duplicate event_key, and the run reaches completed
  (DBOS has exactly one workflow for the run_id, in SUCCESS);
- a client resuming the SSE stream (`?after_seq=` and `Last-Event-ID`) from any seq, including the
  last seq it could have seen before the crash, gets exactly the database's events after that seq.

The run under test is the `test:ticket` scripted model: request 0 calls `tickets.create`, request 1
streams the answer text. A healthy run's events are:
    0 start, 1 model:0:final, 2 tool:{id}:call, 3 tool:{id}:result, 4 model:1:final, 5 end
"""

from __future__ import annotations

import fnmatch
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest

from conftest import REPO_ROOT, Harness, wait_for

INPUT = "The office printer is on fire"


def _new_run(h: Harness) -> tuple[str, str]:
    return h.create_published_agent(), str(uuid.uuid4())


def _expected_keys(h: Harness, run_id: str) -> list[str]:
    call_id = h.events(run_id)[2].data["tool_call_id"]
    return ["start", "model:0:final", f"tool:{call_id}:call", f"tool:{call_id}:result", "model:1:final", "end"]


def assert_invariants(h: Harness, run_id: str, *, reconnect_from: int) -> dict[str, object]:
    """The four task-9 invariants for a recovered run. Returns facts for the report line."""
    run = h.wait_completed(run_id)

    # 1. Exactly one external side effect (the call count is reported, not asserted here).
    tickets = h.tickets(run_id)
    assert len(tickets) == 1, f"expected exactly one external ticket for run {run_id}, got {len(tickets)}"
    assert h.side_effect_states() == ["completed"]
    calls = h.ticket_calls(run_id)
    assert calls >= 1

    # 2. Contiguous seq from 0, no duplicate event_key, the expected story, completed.
    events = h.events(run_id)
    seqs = [e.seq for e in events]
    keys = [e.event_key for e in events]
    assert seqs == list(range(len(events))), f"seq not contiguous from 0: {seqs}"
    assert len(keys) == len(set(keys)), f"duplicate event_key: {keys}"
    assert keys == _expected_keys(h, run_id), keys
    assert [e.type for e in events] == [
        "run_started", "model_message", "tool_call", "tool_result", "model_message", "run_completed",
    ]
    assert events[3].data["status"] == "ok"
    assert events[3].data["content"]["ticket_id"] == tickets[0].ticket_id
    assert tickets[0].ticket_id in events[-1].data["output"]
    assert run["output"] == events[-1].data["output"]
    workflows = h.workflows(run_id)
    assert len(workflows) == 1 and workflows[0][0] == "SUCCESS", workflows

    # 3. A reconnecting client replays exactly the database's events after its resume point.
    db = [(e.seq, e.type, e.data) for e in events]
    last = events[-1].seq
    for point in sorted({-1, 0, reconnect_from, last // 2, last - 1, last}):
        for how in ("after_seq", "last_event_id"):
            frames = h.sse(run_id, **{how: point})
            assert frames and frames[0].event == "status", frames[:1]
            replayed = [f for f in frames if f.event == "run_event"]
            got = [(f.data["seq"], f.data["type"], f.data["data"]) for f in replayed]
            assert got == [row for row in db if row[0] > point], f"replay {how}={point} differs from the DB"
            assert [int(f.id) for f in replayed] == [f.data["seq"] for f in replayed]
            if replayed:
                assert frames[-1] is replayed[-1] and replayed[-1].data["type"] == "run_completed"
    return {"calls": calls, "tickets": len(tickets), "events": len(events), "ticket_id": tickets[0].ticket_id}


def _crash_and_recover(
    h: Harness, *, crash_at: str, event_key: str | None = None, listen: bool = False
) -> tuple[str, list, object]:
    """Start a crashing worker, POST a run, wait for exit 137, then return the pre-crash state."""
    first = h.start_worker("worker1-crashing", crash_at=crash_at, event_key=event_key)
    agent_id, run_id = _new_run(h)
    listener = h.listen_deltas(run_id) if listen else None
    r = h.post_run(agent_id, run_id, INPUT)
    assert r.status_code == 201, r.text
    assert first.wait_exit() == 137, first.log()[-4000:]
    assert h.crash_marker.exists()
    assert f"EAP_CRASH_AT={crash_at} reached" in first.log()
    assert h.run_row(run_id)["status"] == "running"  # nothing finished it
    return run_id, h.events(run_id), listener


def _recover(h: Harness, run_id: str) -> None:
    second = h.start_worker("worker2-recovering")
    h.wait_completed(run_id)
    assert "Recovering" in second.log(), second.log()[-4000:]


# --------------------------------------------------------------------------- scenario 1


def test_1_crash_during_model_request(harness: Harness, report: Callable[[str], None]) -> None:
    """kill -9 inside the model-request step after its first text delta was NOTIFY'd (request 1)."""
    h = harness
    run_id, before, listener = _crash_and_recover(h, crash_at="model_request", listen=True)
    assert listener is not None
    # Request 0 (the tool call) and the tool were checkpointed; request 1 had started streaming.
    assert [e.event_key for e in before][:2] == ["start", "model:0:final"]
    assert before[-1].event_key.endswith(":result") and len(before) == 4
    assert h.ticket_calls(run_id) == 1
    time.sleep(0.5)  # let the listener drain what the crashed attempt sent
    crashed = list(listener.payloads)
    assert crashed, "the crashed attempt published no delta before dying"

    _recover(h, run_id)
    facts = assert_invariants(h, run_id, reconnect_from=before[-1].seq)
    final_text = h.events(run_id)[4].data["text"]
    assert final_text

    def retried_complete() -> list[dict] | None:
        retried = [p for p in listener.payloads[len(crashed):]]
        return retried if "".join(p["delta"] for p in retried) == final_text else None

    retried = wait_for(retried_complete, timeout=15, what="the retried attempt's deltas to arrive")
    deltas = listener.close()

    crashed_attempts = {p["attempt"] for p in crashed}
    retried_attempts = {p["attempt"] for p in retried}
    assert len(crashed_attempts) == 1 and len(retried_attempts) == 1, (crashed, retried)
    assert crashed_attempts.isdisjoint(retried_attempts), "the retried model request reused the crashed attempt id"
    # Deltas carry the model-request index of the durable model:{n}:final event they lead up to.
    assert {p["model_request"] for p in deltas} == {h.events(run_id)[4].data["model_request"]} == {1}
    assert len(crashed) == 1  # the hook fires right after the first published delta
    assert facts["calls"] == 1  # the tool step was checkpointed before the crash: not called again
    report(
        f"1 model_request: tickets={facts['tickets']} calls={facts['calls']} events=0..{facts['events'] - 1}; "
        f"deltas for request 1: crashed attempt {next(iter(crashed_attempts))[:8]} ({len(crashed)} delta), "
        f"retried attempt {next(iter(retried_attempts))[:8]} ({len(retried)} deltas = final text)"
    )


# --------------------------------------------------------------------------- scenario 2


def test_2_crash_after_external_call_before_result_recorded(harness: Harness, report: Callable[[str], None]) -> None:
    """kill -9 after the ticket service returned, before side_effects.complete and the step checkpoint."""
    h = harness
    run_id, before, _ = _crash_and_recover(h, crash_at="after_tool_call")
    assert [e.event_key for e in before][:2] == ["start", "model:0:final"]
    assert before[-1].event_key.endswith(":call") and len(before) == 3
    assert len(h.tickets(run_id)) == 1 and h.ticket_calls(run_id) == 1
    assert h.side_effect_states() == ["reserved"]  # outcome unknown to us, known to the external service

    _recover(h, run_id)
    facts = assert_invariants(h, run_id, reconnect_from=before[-1].seq)
    # The tool step is at-least-once: re-executed with the same idempotency key, deduplicated remotely.
    assert facts["calls"] == 2
    assert h.events(run_id)[2].event_key == before[-1].event_key  # same tool_call_id replayed
    report(
        f"2 after_tool_call: tickets={facts['tickets']} calls={facts['calls']} (same key re-sent, deduplicated) "
        f"events=0..{facts['events'] - 1}"
    )


# --------------------------------------------------------------------------- scenario 3


@pytest.mark.parametrize(
    ("event_key", "expected_before"),
    [("tool:*:result", 4), ("model:*:final", 2)],
    ids=["tool-result", "model-final"],
)
def test_3_crash_after_event_write_before_checkpoint(
    harness: Harness, report: Callable[[str], None], event_key: str, expected_before: int
) -> None:
    """kill -9 after the durable event committed to eap_app, before eap.append_event's checkpoint."""
    h = harness
    run_id, before, _ = _crash_and_recover(h, crash_at="after_event_write", event_key=event_key)
    # The crash happened right after writing the first event matching the glob: it is the last row.
    assert len(before) == expected_before, [e.event_key for e in before]
    assert fnmatch.fnmatchcase(before[-1].event_key, event_key)
    assert f"event_key={before[-1].event_key})" in h.workers[0].log()
    calls_before = h.ticket_calls(run_id)

    _recover(h, run_id)
    facts = assert_invariants(h, run_id, reconnect_from=before[-1].seq)
    after = h.events(run_id)
    # The re-executed append step hit ON CONFLICT: the pre-crash row is unchanged and kept its seq.
    assert [(e.seq, e.event_key, e.type, e.data) for e in after[: len(before)]] == [
        (e.seq, e.event_key, e.type, e.data) for e in before
    ]
    assert facts["calls"] == 1, "the tool step was already checkpointed (or never ran) at this crash point"
    report(
        f"3 after_event_write[{event_key}]: "
        f"tickets={facts['tickets']} calls={facts['calls']} (before crash {calls_before}) "
        f"events=0..{facts['events'] - 1}; crashed after seq {before[-1].seq} ({before[-1].event_key})"
    )


# --------------------------------------------------------------------------- scenario 4


def test_4_api_crash_after_insert_before_enqueue_then_client_retries(
    harness: Harness, report: Callable[[str], None]
) -> None:
    """The API process dies inside the launcher (row committed, nothing enqueued); the client retries."""
    h = harness
    worker = h.start_worker("worker")
    agent_id, run_id = _new_run(h)
    h.run_ids.append(run_id)

    crashed_api = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("crashing_api.py")), agent_id, run_id, INPUT, h.tenant],
        cwd=REPO_ROOT,
        env=dict(os.environ),  # the harness already exported the EAP_* settings
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert crashed_api.returncode == 137, crashed_api.stdout + crashed_api.stderr
    assert "EAP_API_CRASH launcher reached" in crashed_api.stdout

    # The orphan: a committed runs row that no workflow will ever pick up on its own. Still `queued`
    # (not failed with ENQUEUE_FAILED_ERROR): only an enqueue that raises marks the run failed and
    # answers 503; a process that dies inside the launcher runs no handler, so nothing can mark it.
    assert h.run_row(run_id)["status"] == "queued"
    time.sleep(2.0)  # 10 queue polls of a live worker
    assert h.workflows(run_id) == []
    assert h.events(run_id) == [] and h.ticket_calls(run_id) == 0
    assert h.run_row(run_id)["status"] == "queued"

    # The client retries the same POST (same run_id) against a fresh API instance.
    retry = h.post_run(agent_id, run_id, INPUT)
    assert retry.status_code == 200, retry.text  # 200: the run already existed; it was not created again
    assert retry.json()["run_id"] == run_id
    facts = assert_invariants(h, run_id, reconnect_from=-1)
    assert facts["calls"] == 1

    # A further retry after completion attaches to the same workflow: no second run, nothing new.
    events_before = h.events(run_id)
    again = h.post_run(agent_id, run_id, INPUT)
    assert again.status_code == 200, again.text
    time.sleep(1.0)
    assert h.workflows(run_id)[0][0] == "SUCCESS" and len(h.workflows(run_id)) == 1
    assert h.events(run_id) == events_before and h.ticket_calls(run_id) == 1
    assert h.sql("SELECT count(*) FROM runs WHERE id = :r", r=run_id)[0][0] == 1
    assert worker.proc.poll() is None
    report(
        f"4 api insert->enqueue: tickets={facts['tickets']} calls={facts['calls']} events=0..{facts['events'] - 1}; "
        f"orphan row stayed queued with no workflow until the retry (200), extra retry was a no-op"
    )
