"""The native item stream of a workflow run with its agent steps' child runs merged in (AGUI.8).

`WorkflowItemStream` yields the workflow run's items (Status, DurableEvent, Heartbeat) and, for every
agent step, its child run's `DurableEvent` and `Delta` items tagged with
`ChildScope(step_id, child_run_id)`. A child's own status and heartbeats are not forwarded: the
workflow run's `display_status` already shows a child's `waiting_approval`.

**One commit order (D7).** Every event append takes the ROOT run's row lock and stamps
`execution_events.root_order` from a counter on that row (eap_run_store.lock_run_for_append,
0016_ag_ui). Commit order therefore equals `root_order`, and a reader always sees a gapless prefix:
no event can later appear before one already seen. The stream reads
`EventLog.list_root_events` (the workflow run and its children, by `root_order`) and emits in that
order, live and on any replay alike, with no clock and no grace window. A position is the event's
`root_order` (events recorded before 0016 have negative positions, in (ts, run_id, seq) order).

Deltas are transient (never replayed): they are forwarded as they arrive, except that a child's
deltas wait until that child's first durable item has been emitted.

`after` is the position the durable items start after; `scopes` names the child runs of agent steps
started at or before it (the stream learns later ones from the `step_started` it emits).
`stop_after` (a position) bounds the items: an AG-UI run that has ended never emits past its end;
reaching the bound ends the items (`end_reason` "terminal").
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass
from typing import Any

from eap_api.streams.native import (
    TERMINAL_EVENT_TYPES,
    TERMINAL_STATUSES,
    ChildScope,
    Delta,
    DeltaSource,
    DurableEvent,
    EndReason,
    Heartbeat,
    Status,
    StreamItem,
    StreamSettings,
)
from eap_schemas import RunEvent


def agent_step_child(event: RunEvent) -> ChildScope | None:
    """The child run an agent step's `step_started` names, else None."""
    if event.type != "step_started":
        return None
    data = event.data or {}
    step_id, child = data.get("step_id"), data.get("child_run_id")
    if data.get("step_type") != "agent" or not isinstance(step_id, str) or not isinstance(child, str):
        return None
    try:
        return ChildScope(step_id, str(uuid.UUID(child)))
    except ValueError:
        return None


@dataclass(frozen=True)
class Merged:
    """One event of the merged order: its position, its scope (None: the workflow run) and itself."""

    key: int
    scope: ChildScope | None
    event: RunEvent


def merged_events(run_id: str, ordered: Sequence[Any]) -> list[Merged]:
    """`EventLog.list_root_events` rows (`position`, `event`) as Merged items, scoped: an event of
    a child run gets the scope of the agent step whose `step_started` named it (that event always
    comes first in the order: the child row is created after it)."""
    scopes: dict[str, ChildScope] = {}
    out: list[Merged] = []
    for row in ordered:
        event = row.event
        if str(event.run_id) == str(run_id):
            child = agent_step_child(event)
            if child is not None:
                scopes[child.run_id] = child
            out.append(Merged(row.position, None, event))
        elif str(event.run_id) in scopes:
            out.append(Merged(row.position, scopes[str(event.run_id)], event))
    return out


class WorkflowItemStream:
    """See the module docstring. Iterate with `async for`; then `end_reason` says why it ended."""

    def __init__(
        self,
        *,
        run_id: str,
        after: int | None,
        load_ordered: Callable[[int | None], list[Any]],
        load_run: Callable[[str], dict[str, Any] | None],
        delta_source: DeltaSource,
        settings: StreamSettings,
        scopes: Mapping[str, ChildScope] | None = None,
        stop_after: int | None = None,
    ) -> None:
        self.run_id = str(run_id)
        self.after = after
        self.load_ordered = load_ordered
        self.load_run = load_run
        self.delta_source = delta_source
        self.settings = settings
        self.scopes = dict(scopes or {})
        self.stop_after = stop_after
        self.end_reason: EndReason = "gone"

    async def __aiter__(self) -> AsyncIterator[StreamItem]:  # noqa: C901 - one loop
        settings = self.settings
        expires_at = time.monotonic() + settings.max_lifetime
        deltas: asyncio.Queue[tuple[ChildScope | None, dict[str, Any]]] = asyncio.Queue(maxsize=1000)
        held_deltas: dict[str, list[dict[str, Any]]] = {}
        started: set[str] = set()  # child runs whose first durable item was emitted
        subscribed: set[str] = set()
        tasks: list[asyncio.Task[None]] = []
        cursor = self.after

        async with AsyncExitStack() as stack:

            async def subscribe(run_id: str, scope: ChildScope | None) -> None:
                if run_id in subscribed:
                    return
                subscribed.add(run_id)
                stream = await stack.enter_async_context(self.delta_source(run_id))

                async def pump() -> None:
                    with suppress(Exception):
                        async for delta in stream:
                            if delta.get("run_id") == run_id and not deltas.full():
                                deltas.put_nowait((scope, delta))

                tasks.append(asyncio.create_task(pump()))

            try:
                await subscribe(self.run_id, None)
                for child_id, scope in self.scopes.items():
                    await subscribe(child_id, scope)
                run = await asyncio.to_thread(self.load_run, self.run_id)
                if run is None:
                    self.end_reason = "gone"
                    return
                yield Status(run)
                shown_status = run["display_status"]
                next_heartbeat = time.monotonic() + settings.heartbeat_interval
                while True:
                    if time.monotonic() >= expires_at:
                        self.end_reason = "expired"
                        return
                    # The run row first, then the events: the worker writes the end event before
                    # the terminal status, so a terminal status read here means every event is in
                    # the read below.
                    run = await asyncio.to_thread(self.load_run, self.run_id)
                    if run is None:
                        self.end_reason = "gone"
                        return
                    for row in await asyncio.to_thread(self.load_ordered, cursor):
                        if self.stop_after is not None and row.position > self.stop_after:
                            self.end_reason = "terminal"
                            return
                        cursor = row.position
                        event = row.event
                        run_of = str(event.run_id)
                        if run_of == self.run_id:
                            yield DurableEvent(event)
                            child = agent_step_child(event)
                            if child is not None:
                                self.scopes[child.run_id] = child
                                await subscribe(child.run_id, child)
                            if event.type in TERMINAL_EVENT_TYPES:
                                self.end_reason = "terminal"
                                return
                            continue
                        scope = self.scopes.get(run_of)
                        if scope is None:
                            continue  # not an agent step's child (never expected)
                        yield DurableEvent(event, scope)
                        if run_of not in started:
                            started.add(run_of)
                            for payload in held_deltas.pop(run_of, []):
                                yield Delta(payload, scope)
                    if run["status"] in TERMINAL_STATUSES:
                        self.end_reason = "terminal"
                        return
                    if run["display_status"] != shown_status:
                        shown_status = run["display_status"]
                        yield Status(run)
                    poll_at = time.monotonic() + settings.poll_interval
                    while (now := time.monotonic()) < poll_at:
                        if now >= next_heartbeat:
                            yield Heartbeat()
                            yield Status(run)
                            next_heartbeat = now + settings.heartbeat_interval
                        try:
                            scope, payload = await asyncio.wait_for(deltas.get(), min(poll_at, next_heartbeat) - now)
                        except TimeoutError:
                            continue
                        run_of = str(payload.get("run_id"))
                        if scope is not None and run_of not in started:
                            held = held_deltas.setdefault(run_of, [])
                            if len(held) < 1000:
                                held.append(payload)
                            continue
                        yield Delta(payload, scope)
            finally:
                for task in tasks:
                    task.cancel()
                for task in tasks:
                    with suppress(asyncio.CancelledError, Exception):
                        await task


__all__ = ["Merged", "WorkflowItemStream", "agent_step_child", "merged_events"]
