"""AGUI.8 pure units: resume entries (interrupt ids, payload rules, batches) and the workflow item
stream's merge order. No database."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from eap_api.ag_ui.input import AgUiInputError, ResumeEntry
from eap_api.ag_ui.resume_entries import (
    interrupt_id,
    parse_interrupt_id,
    resume_decisions,
    thread_interrupts,
)
from eap_api.streams.native import DurableEvent, StreamSettings
from eap_api.streams.workflow import WorkflowItemStream, merged_events
from eap_run_store import OrderedEvent
from eap_schemas import RunEvent

RUN = str(uuid.uuid4())
CHILD = str(uuid.uuid4())


def ev(run: str, seq: int, type_: str, **data: Any) -> RunEvent:
    return RunEvent(run_id=run, seq=seq, type=type_, data=data, ts=datetime.now(timezone.utc))


def test_interrupt_ids_round_trip_and_unknown_forms():
    assert parse_interrupt_id(interrupt_id(RUN, "tool", "call:1")).key == "call:1"
    step = parse_interrupt_id(interrupt_id(RUN, "step", "approve"))
    assert (step.kind, step.key, step.run_id) == ("step", "approve", RUN)
    assert parse_interrupt_id(f"approval:{RUN}:tool").kind == "step"  # a step named "tool"
    for bad in ("int-call", f"approval:{RUN}", "approval:not-a-uuid:approve", f"approval:{RUN}:Bad"):
        assert parse_interrupt_id(bad) is None


def entry(status: str = "resolved", payload: Any = None, iid: str | None = None) -> ResumeEntry:
    return ResumeEntry(iid or interrupt_id(RUN, "tool", "c1"), status, payload, payload is not None)  # type: ignore[arg-type]


def test_payload_rules():
    (d,) = resume_decisions([entry(payload={"approved": True, "reason": "ok", "extra": 1})])
    assert (d.approved, d.comment, d.decision()) == (True, "ok", "approved")
    (d,) = resume_decisions([entry("cancelled", payload={"editedArgs": {}})])
    assert (d.decision(), d.comment) == ("denied", "Cancelled by user.")
    (d,) = resume_decisions([entry(payload={"approved": False}, iid=interrupt_id(RUN, "step", "s"))])
    assert d.decision() == "rejected"
    for payload, code in (
        ({"approved": True, "editedArgs": {}}, "edited_args_unsupported"),
        ({"approved": 1}, "resume_payload_invalid"),
        ({"approved": True, "reason": 5}, "resume_payload_invalid"),
        ([True], "resume_payload_invalid"),
        (None, "resume_payload_invalid"),
    ):
        with pytest.raises(AgUiInputError) as exc:
            resume_decisions([entry(payload=payload)])
        assert exc.value.status_code == 422 and exc.value.code == code
    with pytest.raises(AgUiInputError) as exc:
        resume_decisions([entry(iid="nope")])
    assert exc.value.code == "resume_unknown_interrupt"


def test_thread_interrupts_open_only_for_live_runs():
    rows = [
        {"run_id": RUN, "kind": "step", "approval_key": "s", "decided_at": None, "run_status": "running"},
        {"run_id": CHILD, "kind": "tool", "approval_key": "c", "decided_at": None, "run_status": "failed"},
        {"run_id": CHILD, "kind": "tool", "approval_key": "d", "decided_at": "x", "run_status": "running"},
    ]
    found = thread_interrupts(rows)
    assert found.open == [interrupt_id(RUN, "step", "s")]
    assert found.decided == [interrupt_id(CHILD, "tool", "c"), interrupt_id(CHILD, "tool", "d")]


@asynccontextmanager
async def no_deltas(run_id: str):
    async def gen():
        await asyncio.Event().wait()
        yield {}

    yield gen()


def test_workflow_items_follow_the_root_commit_order():
    # The order is the root's commit order (root_order), whatever the timestamps say: here every
    # child event carries an EARLIER ts than its neighbours, and a parallel branch's step (p2) was
    # committed between two child events.
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def at(run: str, seq: int, type_: str, **data: Any) -> RunEvent:
        return RunEvent(run_id=run, seq=seq, type=type_, data=data, ts=t0 - timedelta(seconds=seq))

    order_of = [
        at(RUN, 0, "run_started"),
        at(RUN, 1, "step_started", step_id="ask", step_type="agent", child_run_id=CHILD),
        at(CHILD, 0, "run_started"),
        at(RUN, 2, "step_started", step_id="other", step_type="approval"),
        at(CHILD, 1, "model_message", model_request=0, text="x"),
        at(CHILD, 2, "run_completed", output="x"),
        at(RUN, 3, "step_completed", step_id="ask", output="x"),
        at(RUN, 4, "step_started", step_id="lost", step_type="agent", child_run_id=str(uuid.uuid4())),
        at(RUN, 5, "step_failed", step_id="lost", status="failed"),
        at(RUN, 6, "run_completed", result=None),
    ]
    rows = [OrderedEvent(i, e) for i, e in enumerate(order_of)]
    runs = {RUN: {"id": RUN, "status": "running", "display_status": "running"}}
    stream = WorkflowItemStream(
        run_id=RUN, after=None,
        load_ordered=lambda after: [r for r in rows if after is None or r.position > after],
        load_run=lambda rid: runs.get(rid),
        delta_source=no_deltas, settings=StreamSettings(poll_interval=0.01, heartbeat_interval=5),
    )

    async def collect():
        return [i async for i in stream]

    items = asyncio.run(asyncio.wait_for(collect(), 10))
    order = [(i.scope.step_id if i.scope else "wf", i.type) for i in items if isinstance(i, DurableEvent)]
    assert order == [
        ("wf", "run_started"), ("wf", "step_started"), ("ask", "run_started"), ("wf", "step_started"),
        ("ask", "model_message"), ("ask", "run_completed"),
        ("wf", "step_completed"), ("wf", "step_started"), ("wf", "step_failed"), ("wf", "run_completed"),
    ]
    assert stream.end_reason == "terminal"
    # The same order as the pure merge used for AG-UI run boundaries.
    merged = merged_events(RUN, rows)
    assert [(m.scope.step_id if m.scope else "wf", m.event.type) for m in merged] == order
    # A bound: nothing after position 3 is emitted.
    bounded = WorkflowItemStream(
        run_id=RUN, after=None, stop_after=3,
        load_ordered=lambda after: [r for r in rows if after is None or r.position > after],
        load_run=lambda rid: runs.get(rid),
        delta_source=no_deltas, settings=StreamSettings(poll_interval=0.01, heartbeat_interval=5),
    )

    async def collect_bounded():
        return [i async for i in bounded]

    got = [i for i in asyncio.run(asyncio.wait_for(collect_bounded(), 10)) if isinstance(i, DurableEvent)]
    assert len(got) == 4 and bounded.end_reason == "terminal"
