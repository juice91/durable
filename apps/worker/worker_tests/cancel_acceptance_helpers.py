"""Helpers for the LP.8 cancel acceptance tests: a gated scripted model and tool, and tree checks.

The in-process worker resolves `test:echo` through `eap_worker.models.scripted_echo_model`, so a
test patches that name with a model that parks inside the request until `gate.release` is set.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import text

from worker_tests.conftest import event_rows


class Gate:
    """`entered` is set when a model request (or tool call) is parked; `release` lets it go."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0


def gated_echo_model(gate: Gate):
    def build() -> FunctionModel:
        async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[str]:
            gate.calls += 1
            gate.entered.set()
            while not gate.release.is_set():
                await asyncio.sleep(0.05)
            yield "Echo: gated"

        def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            raise AssertionError("the gated model is only used streamed")

        return FunctionModel(respond, stream_function=stream, model_name="scripted-echo")

    return build


def tree_ids(engine, root: Any) -> list[str]:
    """The root run and every descendant run id (by parent_run_id)."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "WITH RECURSIVE t AS (SELECT id FROM runs WHERE id = :r UNION ALL "
                "SELECT c.id FROM runs c JOIN t ON c.parent_run_id = t.id::text) SELECT id FROM t"
            ),
            {"r": str(root)},
        ).fetchall()
    return [str(r[0]) for r in rows]


def snapshot(engine, ids: list[str]) -> dict[str, list]:
    return {i: event_rows(engine, i) for i in ids}


def ends(events) -> list[tuple]:
    return [e for e in events if e[1] == "end"]


def assert_cancelled_tree(engine, run_store, tenant: str, ids: list[str], *, status: str = "failed") -> None:
    """Every run of the tree: one `end`, the status equals that end, and it is run_cancelled."""
    for rid in ids:
        run = run_store.get_run(tenant, uuid.UUID(rid))
        events = event_rows(engine, rid)
        end = ends(events)
        assert len(end) == 1, (rid, events)
        assert events[-1] == end[0], f"{rid}: an event follows the end: {events}"
        assert run["status"] == status, (rid, run["status"])
        assert end[0][3].get("error_code") == "run_cancelled", (rid, end[0])


def usage_rows(engine, run_id: Any) -> int:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT count(*) FROM run_model_requests WHERE run_id = :r"), {"r": str(run_id)}
        ).scalar_one()


def stays(predicate, seconds: float = 4.0) -> bool:
    """True when `predicate()` holds at every poll for `seconds`."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not predicate():
            return False
        time.sleep(0.2)
    return True
