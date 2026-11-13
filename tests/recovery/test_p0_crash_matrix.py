"""P0.21 crash matrix: the P0 milestone's durable data under kill -9, whole-system (README "P0").

Every scenario goes through the real API (agents created, published and run over the API, the run
read back through `GET /runs/{id}` and SSE) and kills a real `python -m eap_worker` at one of the
worker's existing crash points (eap_worker.crash). A second worker with the same executor id then
recovers the run. Each scenario asserts:

- exactly-once side effects: one external ticket (ticket agent), one `processor_result` event per
  processor, exactly one DBOS workflow for the run, in SUCCESS;
- one `run_model_requests` row per model request (moderation requests included), with the tokens
  and cost its `model_message` / `processor_result` event carries, and rows written before the
  crash unchanged (first write wins: cost is never recomputed);
- contiguous `seq` from 0 and no duplicate `event_key`;
- correct run totals: `runs.input_tokens` / `output_tokens` / `cost_usd` (and `GET /runs/{id}`) equal
  the sum over the run's usage rows, cost equal to the tokens priced with the test price file;
- identical replayed text: events stored before the crash are unchanged, the final text equals the
  healthy answer, and an SSE replay from any resume point returns exactly the database's events.

| # | Kill | Proves |
|---|------|--------|
| U1 | `after_event_write` on `model:0:final` / `model:1:final` / `end` of a ticket run | the usage row (written in the event's transaction) is not duplicated or re-priced by the re-executed append; finish_run's totals are right when the crash precedes it |
| P1 | `input_processors` (PII + moderation input chain) | the step re-runs on the same text; one result event per processor; one moderation usage row |
| P2 | `moderation_request`, input and output phase | the moderation model is called again (2 calls: the crash log proves the answered first call, DBOS holds one checkpointed moderation step from recovery) but its usage row and result event are written once; an agent with output processors publishes no delta in either attempt |
| O1 | `after_event_write` on `start` of a run and of a conversation turn started with a model override and a request context; the row's model and context are rewritten while no worker runs | the replay answers with the model and context recorded by `eap.start_run`, and the usage row names that model |
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text

from conftest import REPO_ROOT, Harness, WorkerProcess

PRICES_FILE = str(REPO_ROOT / "config" / "model-prices.test.json")
WORKER_ENV = {"EAP_MODEL_PRICES_FILE": PRICES_FILE, "EAP_ALLOWED_MODELS": "test:*"}
# config/model-prices.test.json prices every test:* model at these USD per million tokens.
PRICE_IN, PRICE_OUT = Decimal("2"), Decimal("8")

TICKET_INPUT = "The office printer is on fire"
EMAIL = "ada.lovelace@example.com"
PII_INPUT = f"mail {EMAIL} now"
PII = {"type": "pii_redact", "config": {"kinds": ["email"], "action": "redact"}}
MODERATION = {"type": "moderation", "config": {"model": {"provider": "test", "name": "moderation"}}}


def echo_spec(**processors: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "name": "Guarded echo",
        "instructions": "Echo the user.",
        "model": {"provider": "test", "name": "echo"},
        "tools": [],
        **processors,
    }


CONTEXT_SPEC = {
    "name": "Context agent",
    "instructions": "Greet {{context.customer}} on the {{context.plan}} plan.",
    "model": {"provider": "test", "name": "echo"},
    "tools": [],
    "context_fields": [
        {"name": "customer", "type": "string", "required": True},
        {"name": "plan", "type": "enum", "values": ["free", "pro"], "required": True},
    ],
}


# --------------------------------------------------------------------------- helpers


def start_worker(h: Harness, name: str, **crash: str) -> WorkerProcess:
    return h.start_worker(name, extra_env=WORKER_ENV, **crash)


def post(h: Harness, url: str, run_id: str, body: dict[str, Any]) -> Any:
    h.run_ids.append(run_id)
    return h.client.post(url, json={"run_id": run_id, **body}, headers=h.headers)


def usage_rows(h: Harness, run_id: str) -> dict[str, dict[str, Any]]:
    rows = h.sql(
        "SELECT request_key, model_id, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, "
        "usage_measured, cost_usd, price_source, created_at FROM run_model_requests WHERE run_id = :r",
        r=run_id,
    )
    return {r.request_key: dict(r._mapping) for r in rows}


def run_totals(h: Harness, run_id: str) -> dict[str, Any]:
    (row,) = h.sql(
        "SELECT input_tokens, output_tokens, cost_usd, started_at, finished_at, model_id, model_overridden "
        "FROM runs WHERE id = :r",
        r=run_id,
    )
    return dict(row._mapping)


def crash_and_wait(h: Harness, worker: WorkerProcess, crash_at: str, run_id: str) -> list[Any]:
    assert worker.wait_exit() == 137, worker.log()[-4000:]
    assert f"EAP_CRASH_AT={crash_at} reached" in worker.log()
    assert h.run_row(run_id)["status"] == "running"  # nothing finished it
    return h.events(run_id)


def recover(h: Harness, run_id: str) -> dict[str, Any]:
    second = start_worker(h, "worker2-recovering")
    run = h.wait_completed(run_id)
    assert "Recovering" in second.log(), second.log()[-4000:]
    return run


def assert_recovered_once(
    h: Harness,
    run_id: str,
    *,
    before: list[Any],
    usage_before: dict[str, dict[str, Any]],
    expected_keys: list[str],
    expected_requests: dict[str, str],
    reconnect_from: int,
) -> dict[str, Any]:
    """The P0.21 invariants for a recovered run. `expected_requests` maps request_key -> model_id."""
    events = h.events(run_id)
    keys = [e.event_key for e in events]
    # Contiguous seq, no duplicate key, the expected story; pre-crash events unchanged.
    assert [e.seq for e in events] == list(range(len(events))), [e.seq for e in events]
    assert len(keys) == len(set(keys)), keys
    assert keys == expected_keys, keys
    assert [(e.seq, e.event_key, e.type, e.data) for e in events[: len(before)]] == [
        (e.seq, e.event_key, e.type, e.data) for e in before
    ], "an event written before the crash changed on recovery"
    assert events[-1].type == "run_completed"
    workflows = h.workflows(run_id)
    assert len(workflows) == 1 and workflows[0][0] == "SUCCESS", workflows

    # One usage row per request; rows written before the crash are unchanged (never re-priced).
    usage = usage_rows(h, run_id)
    assert {k: v["model_id"] for k, v in usage.items()} == expected_requests, usage
    for key, row in usage_before.items():
        assert usage[key] == row, f"usage row {key} changed on recovery"
    by_event = {}
    for e in events:
        if "usage" in e.data:
            request_key = e.event_key.removesuffix(":final") if e.type == "model_message" else None
            if request_key is None:  # a moderation processor_result names its request in the key
                phase_index = e.event_key.removeprefix("processor:")
                request_key = f"moderation:{phase_index}"
            by_event[request_key] = e.data
    assert set(by_event) == set(usage), (sorted(by_event), sorted(usage))
    for key, row in usage.items():
        data = by_event[key]
        assert data["model_id"] == row["model_id"]
        assert data["usage"]["input_tokens"] == row["input_tokens"]
        assert data["usage"]["output_tokens"] == row["output_tokens"]
        assert row["usage_measured"] is True and row["price_source"] is not None
        expected_cost = (
            PRICE_IN * row["input_tokens"] + PRICE_OUT * row["output_tokens"]
        ) / Decimal(1_000_000)
        assert row["cost_usd"] == expected_cost, (key, row["cost_usd"], expected_cost)
        assert Decimal(data["cost_usd"]) == row["cost_usd"]

    # Run totals = the sum over the run's rows, on the row and through the API.
    totals = run_totals(h, run_id)
    sum_in = sum(r["input_tokens"] for r in usage.values())
    sum_out = sum(r["output_tokens"] for r in usage.values())
    sum_cost = sum((r["cost_usd"] for r in usage.values()), Decimal(0))
    assert (totals["input_tokens"], totals["output_tokens"], totals["cost_usd"]) == (sum_in, sum_out, sum_cost)
    assert totals["started_at"] is not None and totals["finished_at"] is not None
    assert totals["started_at"] <= totals["finished_at"]
    api_run = h.client.get(f"/api/v1/runs/{run_id}", headers=h.headers)
    assert api_run.status_code == 200, api_run.text
    body = api_run.json()
    assert (body["input_tokens"], body["output_tokens"]) == (sum_in, sum_out)
    assert Decimal(body["cost_usd"]) == sum_cost
    assert body["output"] == events[-1].data["output"]

    # SSE replay from any resume point is exactly the database's events after it.
    db = [(e.seq, e.type, e.data) for e in events]
    last = events[-1].seq
    for point in sorted({-1, 0, reconnect_from, last // 2, last - 1, last}):
        for how in ("after_seq", "last_event_id"):
            frames = h.sse(run_id, **{how: point})
            replayed = [f for f in frames if f.event == "run_event"]
            got = [(f.data["seq"], f.data["type"], f.data["data"]) for f in replayed]
            assert got == [row for row in db if row[0] > point], f"replay {how}={point} differs from the DB"
    return {"events": events, "usage": usage, "totals": totals}


# --------------------------------------------------------------------------- U1: usage recording


def _ticket_keys(h: Harness, run_id: str) -> list[str]:
    call_id = h.events(run_id)[2].data["tool_call_id"]
    return ["start", "model:0:final", f"tool:{call_id}:call", f"tool:{call_id}:result", "model:1:final", "end"]


@pytest.mark.parametrize("event_key", ["model:0:final", "model:1:final", "end"])
def test_u1_crash_while_recording_usage(harness: Harness, report: Callable[[str], None], event_key: str) -> None:
    """kill -9 right after a `model:{n}:final` event and its usage row committed (same transaction),
    or after `end` (before eap.finish_run writes the totals), before the step checkpoint."""
    h = harness
    first = start_worker(h, "worker1-crashing", crash_at="after_event_write", event_key=event_key)
    agent_id = h.create_published_agent()
    run_id = str(uuid.uuid4())
    r = h.post_run(agent_id, run_id, TICKET_INPUT)
    assert r.status_code == 201, r.text
    before = crash_and_wait(h, first, "after_event_write", run_id)
    assert before[-1].event_key == event_key
    usage_before = usage_rows(h, run_id)
    written = {e.event_key.removesuffix(":final") for e in before if e.event_key.startswith("model:")}
    assert set(usage_before) == written, "the usage row is not in the model event's transaction"
    if event_key == "end":
        assert run_totals(h, run_id)["finished_at"] is None  # finish_run never ran

    recover(h, run_id)
    facts = assert_recovered_once(
        h, run_id, before=before, usage_before=usage_before,
        expected_keys=_ticket_keys(h, run_id),
        expected_requests={"model:0": "test:ticket", "model:1": "test:ticket"},
        reconnect_from=before[-1].seq,
    )
    tickets = h.tickets(run_id)
    assert len(tickets) == 1 and h.side_effect_states() == ["completed"]
    assert h.ticket_calls(run_id) == 1  # the tool step was checkpointed (or had not run) at the kill
    assert tickets[0].ticket_id in facts["events"][-1].data["output"]
    report(
        f"P0 U1 after_event_write[{event_key}]: usage rows {sorted(facts['usage'])}, "
        f"totals in={facts['totals']['input_tokens']} out={facts['totals']['output_tokens']} "
        f"cost={facts['totals']['cost_usd']}, tickets=1"
    )


# --------------------------------------------------------------------------- P1 / P2: processors


PROCESSOR_CASES = {
    # name: (crash point, spec processors, events stored before the crash)
    "input-step": ("input_processors", {"input_processors": [PII, MODERATION]}, []),
    "input-moderation": ("moderation_request", {"input_processors": [PII, MODERATION]}, []),
    "output-moderation": ("moderation_request", {"output_processors": [PII, MODERATION]}, ["start"]),
}


@pytest.mark.parametrize("case", list(PROCESSOR_CASES))
def test_p1_p2_crash_in_processors(harness: Harness, report: Callable[[str], None], case: str) -> None:
    """kill -9 inside the input processor step, or inside the moderation model's request step
    (after the moderation model answered, before the step checkpoint)."""
    h = harness
    crash_at, processors, expected_before = PROCESSOR_CASES[case]
    phase = "input" if "input_processors" in processors else "output"
    first = start_worker(h, "worker1-crashing", crash_at=crash_at)
    agent_id = h.create_published_agent(echo_spec(**processors))
    run_id = str(uuid.uuid4())
    listener = h.listen_deltas(run_id)
    r = h.post_run(agent_id, run_id, PII_INPUT)
    assert r.status_code == 201, r.text
    before = crash_and_wait(h, first, crash_at, run_id)
    assert [e.event_key for e in before] == expected_before
    usage_before = usage_rows(h, run_id)
    assert usage_before == {}  # the moderation row rides on its processor_result event, not yet written

    recover(h, run_id)
    # Moderation calls: the crash point fires only after the moderation model answered (call 1,
    # proven by the crash log line above), and the recovered workflow holds exactly one checkpointed
    # moderation request step (call 2): the moderation model was called again, exactly once more.
    moderation_steps = [name for _, name in h.dbos_steps(run_id) if name == "eap-moderator__model.request"]
    assert len(moderation_steps) == 1, h.dbos_steps(run_id)
    moderation_calls = 1 + len(moderation_steps) if crash_at == "moderation_request" else len(moderation_steps)
    if crash_at == "moderation_request":
        assert moderation_calls == 2
    suffix = "" if phase == "input" else ":model:0"
    result_keys = [f"processor:{phase}:0{suffix}", f"processor:{phase}:1{suffix}"]
    expected_keys = ["start", *result_keys, "model:0:final", "end"]
    facts = assert_recovered_once(
        h, run_id, before=before, usage_before=usage_before, expected_keys=expected_keys,
        expected_requests={"model:0": "test:echo", f"moderation:{phase}:1{suffix}": "test:moderation"},
        reconnect_from=before[-1].seq if before else -1,
    )
    events = facts["events"]
    results = [e for e in events if e.type == "processor_result"]
    assert [e.event_key for e in results] == result_keys  # one event per processor
    # Identical processed text: the healthy answer, and the raw email nowhere in the run's events.
    assert facts["events"][-1].data["output"] == "Echo: mail [REDACTED:email] now"
    if phase == "input":
        assert events[0].data["input"] == "mail [REDACTED:email] now"
    model_message = next(e for e in events if e.event_key == "model:0:final")
    assert model_message.data["text"] == "Echo: mail [REDACTED:email] now"
    stored = json.dumps([e.data for e in events if not (phase == "output" and e.event_key == "start")])
    assert EMAIL not in stored
    assert h.run_row(run_id)["output"] == "Echo: mail [REDACTED:email] now"
    deltas = listener.close()
    if phase == "output":
        assert deltas == [], "an agent with output processors published a live delta"
    report(
        f"P0 {case} ({crash_at}): events {len(events)}, results {[e.event_key for e in results]}, "
        f"usage {sorted(facts['usage'])}, deltas {len(deltas)}, moderation calls {moderation_calls}"
    )


# --------------------------------------------------------------------------- O1: run options


@pytest.mark.parametrize("via", ["run", "turn"])
def test_o1_crash_after_start_with_model_override_and_context(
    harness: Harness, report: Callable[[str], None], via: str
) -> None:
    """kill -9 right after the `start` event (eap.start_run is checkpointed with the run's model and
    context); while no worker runs, the row's model and context are rewritten. The replay must use
    what eap.start_run recorded."""
    h = harness
    first = start_worker(h, "worker1-crashing", crash_at="after_event_write", event_key="start")
    agent_id = h.create_published_agent(CONTEXT_SPEC)
    run_id = str(uuid.uuid4())
    options = {
        "input": "hi",
        "model": {"provider": "test", "name": "context"},
        "request_context": {"customer": "Ada", "plan": "pro"},
    }
    if via == "run":
        r = post(h, f"/api/v1/agents/{agent_id}/runs", run_id, {**options, "version": None})
    else:
        conversation_id = h.create_conversation(agent_id)
        r = post(h, f"/api/v1/conversations/{conversation_id}/runs", run_id, options)
    assert r.status_code == 201, r.text
    recorded = run_totals(h, run_id)
    assert (recorded["model_id"], recorded["model_overridden"]) == ("test:context", True)

    before = crash_and_wait(h, first, "after_event_write", run_id)
    assert [e.event_key for e in before] == ["start"]
    assert "Ada" not in json.dumps(before[0].data)  # context values are not stored in events
    with h.engine.begin() as conn:
        conn.execute(
            text("UPDATE runs SET model_id = 'test:echo', request_context = CAST(:c AS jsonb) WHERE id = :r"),
            {"c": json.dumps({"customer": "Eve", "plan": "free"}), "r": run_id},
        )

    recover(h, run_id)
    facts = assert_recovered_once(
        h, run_id, before=before, usage_before={}, expected_keys=["start", "model:0:final", "end"],
        expected_requests={"model:0": "test:context"}, reconnect_from=0,
    )
    answer = "Instructions: Greet Ada on the pro plan."
    assert facts["events"][-1].data["output"] == answer
    assert facts["events"][1].data["text"] == answer
    if via == "turn":
        r = h.client.get(f"/api/v1/conversations/{conversation_id}", headers=h.headers)
        assert r.status_code == 200, r.text
        assert answer in json.dumps(r.json())
    report(f"P0 O1 after start ({via}): answered by test:context with the recorded context; usage {sorted(facts['usage'])}")
