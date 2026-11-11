"""End-to-end durable runs through the real worker (in-process DBOS, private schema, real Postgres)."""

from __future__ import annotations

import json
import threading
import uuid

import psycopg
import pytest
from pydantic_ai import RunContext

from worker_tests.conftest import APP_DB_URL, APP_VERSION, enqueue_run, event_rows, ticket_spec, wait_for, wait_terminal


@pytest.fixture
def run_store(worker):
    return worker.run_store


class DeltaListener:
    """LISTEN eap_run_deltas on eap_app in a background thread."""

    def __init__(self) -> None:
        self.payloads: list[dict] = []
        self._stop = threading.Event()
        self._conn = psycopg.connect(APP_DB_URL.replace("postgresql+psycopg://", "postgresql://", 1), autocommit=True)
        self._conn.execute("LISTEN eap_run_deltas")
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            for note in self._conn.notifies(timeout=0.2, stop_after=50):
                self.payloads.append(json.loads(note.payload))

    def for_run(self, run_id) -> list[dict]:
        return [p for p in self.payloads if p["run_id"] == str(run_id)]

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self._conn.close()


def test_ticket_run_completes_with_contract_events(worker, client, run_store, tenant):
    listener = DeltaListener()
    try:
        run_id = enqueue_run(
            client, run_store, tenant_id=tenant, spec=ticket_spec(), input_text="My printer is on fire",
            app_version=APP_VERSION,
        )
        run = wait_terminal(run_store, tenant, run_id)
        assert run["status"] == "completed", run["error"]
        assert run["heartbeat_at"] is not None

        result = client.retrieve_workflow(str(run_id)).get_result()
        assert result["status"] == "completed"

        events = event_rows(worker.engine, run_id)
        assert [e[0] for e in events] == list(range(len(events))), "seq must be contiguous from 0"
        keys = [e[1] for e in events]
        call_id = events[2][3]["tool_call_id"]
        assert keys == [
            "start",
            "model:0:final",
            f"tool:{call_id}:call",
            f"tool:{call_id}:result",
            "model:1:final",
            "end",
        ]
        assert [e[2] for e in events] == [
            "run_started", "model_message", "tool_call", "tool_result", "model_message", "run_completed",
        ]
        assert "text_delta" not in {e[2] for e in events}
        assert events[2][3]["tool_name"] == "tickets__create"
        ticket_id = events[3][3]["content"]["ticket_id"]
        assert events[3][3]["status"] == "ok"
        assert ticket_id in events[4][3]["text"]
        assert events[5][3]["output"] == run["output"] == events[4][3]["text"]

        # Exactly one external ticket, created once, under the run's idempotency key.
        assert worker.tickets.ticket_count(tenant, f"{run_id}:") == 1
        assert worker.tickets.call_count(tenant, f"{run_id}:") == 1

        # Live deltas: only request 1 streams text; one attempt; they add up to the final message.
        deltas = wait_for(lambda: listener.for_run(run_id), timeout=5, what="deltas")
        assert {d["model_request"] for d in deltas} == {1}
        attempts = {d["attempt"] for d in deltas}
        assert len(attempts) == 1 and uuid.UUID(attempts.pop())
        assert "".join(d["delta"] for d in deltas) == events[4][3]["text"]
    finally:
        listener.close()


def test_same_run_id_twice_is_one_workflow_and_one_run(worker, client, run_store, tenant):
    run_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    for _ in range(2):  # a client retry: same run_id -> same row, same workflow
        enqueue_run(
            client, run_store, tenant_id=tenant, spec=ticket_spec(), input_text="hello", app_version=APP_VERSION,
            run_id=run_id, agent_id=agent_id,
        )
    run = wait_terminal(run_store, tenant, run_id)
    assert run["status"] == "completed"
    # Enqueue once more after completion: still deduplicated by workflow id.
    enqueue_run(
        client, run_store, tenant_id=tenant, spec=ticket_spec(), input_text="hello", app_version=APP_VERSION,
        run_id=run_id, agent_id=agent_id,
    )
    workflows = client.list_workflows(workflow_ids=[str(run_id)])
    assert len(workflows) == 1
    with worker.engine.connect() as conn:
        from sqlalchemy import text

        assert conn.execute(text("SELECT count(*) FROM runs WHERE id = :r"), {"r": str(run_id)}).scalar_one() == 1
    events = event_rows(worker.engine, run_id)
    assert [e[0] for e in events] == list(range(6))
    assert worker.tickets.ticket_count(tenant, f"{run_id}:") == 1
    assert worker.tickets.call_count(tenant, f"{run_id}:") == 1


def test_tool_not_in_registry_fails_the_run(worker, client, run_store, tenant):
    run_id = enqueue_run(
        client, run_store, tenant_id=tenant, spec=ticket_spec(tools=("tickets.create", "payments.refund")),
        input_text="refund me", app_version=APP_VERSION,
    )
    run = wait_terminal(run_store, tenant, run_id)
    assert run["status"] == "failed"
    assert "payments.refund" in run["error"]
    events = event_rows(worker.engine, run_id)
    assert [(e[0], e[1], e[2]) for e in events] == [(0, "start", "run_started"), (1, "end", "run_failed")]
    assert "UnknownToolError" in events[1][3]["error"]
    assert worker.tickets.call_count(tenant, f"{run_id}:") == 0


def test_tool_not_allowlisted_fails_the_run(worker, client, run_store, tenant, monkeypatch):
    # Registered but not on this worker's allowlist.
    monkeypatch.setitem(worker.__dict__, "allowed_tools", frozenset({"clock.now"}))
    run_id = enqueue_run(
        client, run_store, tenant_id=tenant, spec=ticket_spec(), input_text="x", app_version=APP_VERSION,
    )
    run = wait_terminal(run_store, tenant, run_id)
    assert run["status"] == "failed"
    events = event_rows(worker.engine, run_id)
    assert [e[2] for e in events] == ["run_started", "run_failed"]
    assert "ToolNotAllowedError" in events[1][3]["error"]
    assert worker.tickets.call_count(tenant, f"{run_id}:") == 0


def test_text_only_model_run(worker, client, run_store, tenant):
    run_id = enqueue_run(
        client, run_store, tenant_id=tenant, spec=ticket_spec(tools=(), model="echo"), input_text="ping",
        app_version=APP_VERSION,
    )
    run = wait_terminal(run_store, tenant, run_id)
    assert run["status"] == "completed", run["error"]
    assert run["output"] == "Echo: ping"
    assert [e[1] for e in event_rows(worker.engine, run_id)] == ["start", "model:0:final", "end"]


def _unsafe_side_effect(ctx: RunContext) -> dict:
    """A non-idempotent side effect whose previous attempt 'crashed' after reserving its key."""
    from eap_runtime import run_side_effect

    key = f"{ctx.deps.run_id}:{ctx.tool_call_id}"
    ctx.deps.side_effects.reserve(key)  # simulate: reserved by an attempt whose outcome is unknown
    result, _ = run_side_effect(ctx.deps.side_effects, key, lambda: {"ok": True}, external_idempotent=False)
    return result


def test_side_effect_outcome_unknown_needs_attention(worker, client, run_store, tenant, monkeypatch):
    if "test.unsafe" not in worker.registry.tool_ids():
        worker.registry.register("test.unsafe", _unsafe_side_effect, side_effect=True, external_idempotent=False)
    monkeypatch.setitem(worker.__dict__, "allowed_tools", frozenset({"test.unsafe"}))
    run_id = enqueue_run(
        client, run_store, tenant_id=tenant, spec=ticket_spec(tools=("test.unsafe",), model="first-tool"),
        input_text="go", app_version=APP_VERSION,
    )
    run = wait_terminal(run_store, tenant, run_id)
    assert run["status"] == "needs_attention", run
    events = event_rows(worker.engine, run_id)
    assert [e[0] for e in events] == list(range(len(events)))
    assert [e[2] for e in events] == ["run_started", "model_message", "tool_call", "run_failed"]
    assert events[-1][1] == "end"
    assert events[-1][3]["status"] == "needs_attention"
