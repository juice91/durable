"""The native item stream of one run: format-agnostic, typed items (D0).

`run_items` is the stream engine `GET /runs/{id}/stream` was built on, with the SSE encoding taken
out. It yields, in order:
- `Status` first (the run row), again whenever `display_status` changes, and right after every
  `Heartbeat` (durability rule 6);
- `DurableEvent` for each durable event after the resume point, read from `execution_events`
  (replay, then polling every `poll_interval`);
- `Delta` for each live token delta from `LISTEN eap_run_deltas` of this run (transient,
  durability rule 4; never stored, no seq);
- `Heartbeat` every `heartbeat_interval`;
- `RunSummary` just before the terminal event, only when the caller passed `load_summary`.

The stream ends right after `run_completed` / `run_failed`, when the run has a terminal status
and no further events, when the run disappears, or at `max_lifetime`.

Each output format is a `StreamFormatter` (eap_api.streams.formatter) that turns these items into
frames: the native SSE (eap_api.streams.native_sse) and AG-UI (eap_api.ag_ui). Nothing here knows
any output format.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, suppress
from dataclasses import dataclass, field
from typing import Any, Literal

from eap_observability import log_failure
from eap_schemas import RunEvent

log = logging.getLogger("eap_api")

TERMINAL_EVENT_TYPES = frozenset({"run_completed", "run_failed"})
TERMINAL_STATUSES = frozenset({"completed", "failed", "needs_attention"})
MAX_PENDING_DELTAS = 1000

# A delta source opens a subscription for one run: entering it must start listening (so nothing
# sent after replay begins is lost) and yields an async iterator of delta payload dicts.
DeltaSource = Callable[[str], AbstractAsyncContextManager[AsyncIterator[dict[str, Any]]]]


@dataclass(frozen=True)
class StreamSettings:
    poll_interval: float = 0.5
    heartbeat_interval: float = 10.0
    max_lifetime: float = 3600.0


@dataclass(frozen=True)
class ChildScope:
    """Marks an item of a workflow agent step's child run (merged in by the workflow stream); the
    native `run_items` of one run never sets it."""

    step_id: str
    run_id: str


@dataclass(frozen=True)
class DurableEvent:
    """One durable event (`execution_events` row)."""

    event: RunEvent
    scope: ChildScope | None = None

    @property
    def seq(self) -> int:
        return self.event.seq

    @property
    def type(self) -> str:
        return self.event.type

    @property
    def data(self) -> dict[str, Any]:
        return self.event.data


@dataclass(frozen=True)
class Delta:
    """One live token delta. `payload` is the NOTIFY payload exactly as received."""

    payload: dict[str, Any]
    scope: ChildScope | None = None

    @property
    def model_request(self) -> int | None:
        value = self.payload.get("model_request")
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    @property
    def attempt(self) -> str | None:
        value = self.payload.get("attempt")
        return value if isinstance(value, str) else None

    @property
    def text(self) -> str:
        value = self.payload.get("delta")
        return value if isinstance(value, str) else ""


@dataclass(frozen=True)
class Status:
    """The run row as read (`id`, `status`, `display_status`, ...)."""

    run: dict[str, Any]

    @property
    def display_status(self) -> str:
        return str(self.run.get("display_status"))


@dataclass(frozen=True)
class Heartbeat:
    """Keep-alive; a `Status` of the same run follows immediately."""


@dataclass(frozen=True)
class RunSummary:
    """What the caller's `load_summary` returned, read once just before the terminal event (e.g.
    the run's per-model usage for AG-UI's `RUN_FINISHED.usage`)."""

    data: dict[str, Any] = field(default_factory=dict)


StreamItem = DurableEvent | Delta | Status | Heartbeat | RunSummary
EndReason = Literal["terminal", "expired", "gone"]


class ItemStream:
    """`run_items` as an object, so a formatter driver can learn why it ended (`end_reason`)."""

    def __init__(
        self,
        *,
        run_id: str,
        after_seq: int,
        load_events: Callable[[int], list[RunEvent]],
        load_run: Callable[[], dict[str, Any] | None],
        delta_source: DeltaSource,
        settings: StreamSettings,
        load_summary: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.run_id = run_id
        self.after_seq = after_seq
        self.load_events = load_events
        self.load_run = load_run
        self.delta_source = delta_source
        self.settings = settings
        self.load_summary = load_summary
        self.end_reason: EndReason = "gone"

    async def __aiter__(self) -> AsyncIterator[StreamItem]:  # noqa: C901 - one loop, kept as in sse.py
        run_id = self.run_id
        settings = self.settings
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=MAX_PENDING_DELTAS)
        expires_at = time.monotonic() + settings.max_lifetime

        async def pump(deltas: AsyncIterator[dict[str, Any]]) -> None:
            try:
                async for delta in deltas:
                    if delta.get("run_id") == run_id and not queue.full():
                        queue.put_nowait(delta)
            except Exception as exc:  # noqa: BLE001 - losing live deltas must not end the durable stream
                log_failure(log, "delta subscription ended", exc, code="delta_subscription_failed", run_id=run_id)

        async def summary() -> RunSummary | None:
            if self.load_summary is None:
                return None
            return RunSummary(dict(await asyncio.to_thread(self.load_summary)))

        async with self.delta_source(run_id) as deltas:
            pump_task = asyncio.create_task(pump(deltas))
            try:
                run = await asyncio.to_thread(self.load_run)
                if run is None:
                    self.end_reason = "gone"
                    return
                yield Status(run)
                shown_status = run["display_status"]
                last_seq = self.after_seq
                next_heartbeat = time.monotonic() + settings.heartbeat_interval

                while True:
                    if time.monotonic() >= expires_at:
                        self.end_reason = "expired"
                        return
                    for event in await asyncio.to_thread(self.load_events, last_seq):
                        if time.monotonic() >= expires_at:
                            self.end_reason = "expired"
                            return
                        last_seq = event.seq
                        if event.type in TERMINAL_EVENT_TYPES:
                            if (item := await summary()) is not None:
                                yield item
                            yield DurableEvent(event)
                            self.end_reason = "terminal"
                            return
                        yield DurableEvent(event)
                    run = await asyncio.to_thread(self.load_run)
                    if run is None:
                        self.end_reason = "gone"
                        return
                    if run["display_status"] != shown_status:
                        shown_status = run["display_status"]
                        yield Status(run)
                    if run["status"] in TERMINAL_STATUSES:
                        # The worker writes the end event before the terminal status, so one more
                        # read picks up anything written since the read above; then nothing more.
                        for event in await asyncio.to_thread(self.load_events, last_seq):
                            if time.monotonic() >= expires_at:
                                self.end_reason = "expired"
                                return
                            last_seq = event.seq
                            if event.type in TERMINAL_EVENT_TYPES:
                                if (item := await summary()) is not None:
                                    yield item
                                yield DurableEvent(event)
                                self.end_reason = "terminal"
                                return
                            yield DurableEvent(event)
                        self.end_reason = "terminal"
                        return

                    # Until the next poll: forward deltas as they arrive, heartbeat when due.
                    poll_at = time.monotonic() + settings.poll_interval
                    while (now := time.monotonic()) < poll_at:
                        if now >= expires_at:
                            self.end_reason = "expired"
                            return
                        if now >= next_heartbeat:
                            yield Heartbeat()
                            yield Status(run)
                            next_heartbeat = now + settings.heartbeat_interval
                        try:
                            delta = await asyncio.wait_for(
                                queue.get(), min(poll_at, next_heartbeat, expires_at) - now
                            )
                        except TimeoutError:
                            continue
                        yield Delta(delta)
            finally:
                pump_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await pump_task


def run_items(
    *,
    run_id: str,
    after_seq: int,
    load_events: Callable[[int], list[RunEvent]],
    load_run: Callable[[], dict[str, Any] | None],
    delta_source: DeltaSource,
    settings: StreamSettings,
    load_summary: Callable[[], dict[str, Any]] | None = None,
) -> ItemStream:
    """The typed items of one run. `load_events` / `load_run` / `load_summary` are blocking and
    run in a thread. Iterate it with `async for`; afterwards `end_reason` says why it ended."""
    return ItemStream(
        run_id=run_id,
        after_seq=after_seq,
        load_events=load_events,
        load_run=load_run,
        delta_source=delta_source,
        settings=settings,
        load_summary=load_summary,
    )
