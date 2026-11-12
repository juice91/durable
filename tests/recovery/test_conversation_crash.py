"""Scenario 5 (milestone 2): a worker crash in the middle of a three-turn conversation.

Everything goes through the real API (POST /agents/{id}/conversations, POST /conversations/{id}/runs)
and real `python -m eap_worker` processes. The agent is the `test:history` scripted model, whose
answer lists every user prompt it was given, so the answer shows exactly what history it saw:

    turn 1 "My name is Ada."            completes normally
    turn 2 "I live in Lisbon."          the worker dies inside the model request (EAP_CRASH_AT=model_request);
                                        turn 3 is refused with 409 until turn 2 is terminal;
                                        a fresh worker with the same executor id recovers turn 2
    turn 3 "What is my name?"           its answer carries the fact from turn 1

Asserts: no lost or duplicated messages (each turn appears once in the history the next turn saw,
the stored conversation has turns 1..3 in order), per-run events contiguous with unique keys and
replayable over SSE, and another tenant gets 404 on the conversation.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

from conftest import Harness

HISTORY_SPEC = {
    "name": "Memory",
    "instructions": "Answer using the conversation so far.",
    "model": {"provider": "test", "name": "history"},
    "tools": [],
}


def test_5_crash_mid_conversation(harness: Harness, report: Callable[[str], None]) -> None:
    h = harness
    h.start_worker("worker1")
    agent_id = h.create_published_agent(HISTORY_SPEC)
    conv = h.create_conversation(agent_id)

    run1 = str(uuid.uuid4())
    assert h.post_turn(conv, run1, "My name is Ada.").status_code == 201
    h.wait_completed(run1)
    h.workers[0].stop()

    crashing = h.start_worker("worker2-crashing", crash_at="model_request")
    run2 = str(uuid.uuid4())
    assert h.post_turn(conv, run2, "I live in Lisbon.").status_code == 201
    assert crashing.wait_exit() == 137, crashing.log()[-4000:]
    assert h.run_row(run2)["status"] == "running"

    # The thread is busy: the next turn is refused, the retry of turn 2 is idempotent (200, same turn).
    busy = h.post_turn(conv, str(uuid.uuid4()), "What is my name?")
    assert busy.status_code == 409, busy.text
    retry = h.post_turn(conv, run2, "I live in Lisbon.")
    assert retry.status_code == 200 and retry.json()["turn"] == 2

    recovering = h.start_worker("worker3-recovering")
    h.wait_completed(run2)
    assert "Recovering" in recovering.log()

    run3 = str(uuid.uuid4())
    assert h.post_turn(conv, run3, "What is my name?").status_code == 201
    final = h.wait_completed(run3)
    assert final["output"] == (
        "Turn 3. You said: My name is Ada. | I live in Lisbon. | What is my name?. I answered 2 times before."
    )

    body = h.client.get(f"/api/v1/conversations/{conv}", headers=h.headers).json()
    assert [(r["run_id"], r["turn"], r["status"]) for r in body["runs"]] == [
        (run1, 1, "completed"), (run2, 2, "completed"), (run3, 3, "completed"),
    ]
    assert body["runs"][1]["output"] == "Turn 2. You said: My name is Ada. | I live in Lisbon.. I answered 1 times before."
    for run_id in (run1, run2, run3):
        events = h.events(run_id)
        assert [e.seq for e in events] == list(range(len(events)))
        assert [e.event_key for e in events] == ["start", "model:0:final", "end"]
        replayed = [f for f in h.sse(run_id, after_seq=-1) if f.event == "run_event"]
        assert [(f.data["seq"], f.data["type"]) for f in replayed] == [(e.seq, e.type) for e in events]
        assert len(h.workflows(run_id)) == 1 and h.workflows(run_id)[0][0] == "SUCCESS"

    other = {"Authorization": f"Bearer dev:{h.tenant}x:mallory"}
    assert h.client.get(f"/api/v1/conversations/{conv}", headers=other).status_code == 404
    assert h.post_turn(conv, str(uuid.uuid4()), "hi", headers=other).status_code == 404
    report("5 mid-conversation model_request crash: turns 1..3 completed once each; turn 3 answered from turn 1; "
           "next turn 409 while turn 2 recovered; other tenant 404")
