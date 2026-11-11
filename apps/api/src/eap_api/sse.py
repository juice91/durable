"""Server-Sent Events for a run, per the run hand-off contract.

Frames, in order:
- `event: status` first (current display_status), again whenever it changes, and with every
  heartbeat (durability rule 6);
- durable events after the resume point as `event: run_event` with `id: <seq>`, read from
  `execution_events` (replay, then polling every `poll_interval`);
- live token deltas from `LISTEN eap_run_deltas`, filtered by run_id, as `event: delta` without an
  id (transient, durability rule 4);
- a `: heartbeat` comment every `heartbeat_interval`.

The stream ends right after it sends `run_completed` or `run_failed`, or when the run has a
terminal status and no further events (e.g. a client resuming after the final event).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Any

from fastapi import Request

from eap_schemas import RunEvent

log = logging.getLogger("eap_api")

DELTA_CHANNEL = "eap_run_deltas"
TERMINAL_EVENT_TYPES = frozenset({"run_completed", "run_failed"})
TERMINAL_STATUSES = frozenset({"completed", "failed", "needs_attention"})
_MAX_PENDING_DELTAS = 1000

# A delta source opens a subscription for one run: entering it must start listening (so nothing
# sent after replay begins is lost) and yields an async iterator of delta payload dicts.
DeltaSource = Callable[[str], AbstractAsyncContextManager[AsyncIterator[dict[str, Any]]]]


@dataclass(frozen=True)
class StreamSettings:
    poll_interval: float = 0.5
    heartbeat_interval: float = 10.0


def get_stream_settings() -> StreamSettings:
    return StreamSettings()


def get_delta_source(request: Request) -> DeltaSource:
    return request.app.state.delta_source


class PostgresDeltaSource:
    """One LISTEN connection per stream on the app database (where the worker NOTIFYs)."""

    def __init__(self, conninfo: str) -> None:
        self.conninfo = conninfo

    @asynccontextmanager
    async def __call__(self, run_id: str) -> AsyncIterator[AsyncIterator[dict[str, Any]]]:
        import psycopg

        conn = await psycopg.AsyncConnection.connect(self.conninfo, autocommit=True)
        try:
            await conn.execute(f"LISTEN {DELTA_CHANNEL}")
            yield self._iterate(conn, run_id)
        finally:
            await conn.close()

    @staticmethod
    async def _iterate(conn: Any, run_id: str) -> AsyncIterator[dict[str, Any]]:
        async for notify in conn.notifies():
            try:
                payload = json.loads(notify.payload)
            except ValueError:
                continue
            if isinstance(payload, dict) and payload.get("run_id") == run_id:
                yield payload


@asynccontextmanager
async def no_deltas(run_id: str) -> AsyncIterator[AsyncIterator[dict[str, Any]]]:
    """Delta source for databases without LISTEN/NOTIFY: never yields."""

    async def never() -> AsyncIterator[dict[str, Any]]:
        await asyncio.Event().wait()
        yield {}  # pragma: no cover

    yield never()


def _frame(event: str, data: Any, id_: int | None = None) -> str:
    head = f"id: {id_}\n" if id_ is not None else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


def event_json(event: RunEvent) -> dict[str, Any]:
    return {"seq": event.seq, "type": event.type, "data": event.data, "ts": event.ts.isoformat()}


def _status_frame(run: dict[str, Any]) -> str:
    return _frame("status", {"run_id": str(run["id"]), "status": run["status"], "display_status": run["display_status"]})


async def run_event_stream(
    *,
    run_id: str,
    after_seq: int,
    load_events: Callable[[int], list[RunEvent]],
    load_run: Callable[[], dict[str, Any] | None],
    delta_source: DeltaSource,
    settings: StreamSettings,
) -> AsyncIterator[str]:
    """Yield SSE frames for one run. `load_events`/`load_run` are blocking and run in a thread."""
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_MAX_PENDING_DELTAS)

    async def pump(deltas: AsyncIterator[dict[str, Any]]) -> None:
        try:
            async for delta in deltas:
                if delta.get("run_id") == run_id and not queue.full():
                    queue.put_nowait(delta)
        except Exception:  # noqa: BLE001 - losing live deltas must not end the durable stream
            log.warning("delta subscription for run %s ended", run_id, exc_info=True)

    async with delta_source(run_id) as deltas:
        pump_task = asyncio.create_task(pump(deltas))
        try:
            run = await asyncio.to_thread(load_run)
            if run is None:
                return
            yield _status_frame(run)
            shown_status = run["display_status"]
            last_seq = after_seq
            next_heartbeat = time.monotonic() + settings.heartbeat_interval

            while True:
                for event in await asyncio.to_thread(load_events, last_seq):
                    last_seq = event.seq
                    yield _frame("run_event", event_json(event), event.seq)
                    if event.type in TERMINAL_EVENT_TYPES:
                        return
                run = await asyncio.to_thread(load_run)
                if run is None:
                    return
                if run["display_status"] != shown_status:
                    shown_status = run["display_status"]
                    yield _status_frame(run)
                if run["status"] in TERMINAL_STATUSES:
                    # The worker writes the end event before the terminal status, so one more read
                    # picks up anything written since the read above; then nothing more can come.
                    for event in await asyncio.to_thread(load_events, last_seq):
                        last_seq = event.seq
                        yield _frame("run_event", event_json(event), event.seq)
                        if event.type in TERMINAL_EVENT_TYPES:
                            return
                    return

                # Until the next poll: forward deltas as they arrive, heartbeat when due.
                poll_at = time.monotonic() + settings.poll_interval
                while (now := time.monotonic()) < poll_at:
                    if now >= next_heartbeat:
                        yield ": heartbeat\n\n"
                        yield _status_frame(run)
                        next_heartbeat = now + settings.heartbeat_interval
                    try:
                        delta = await asyncio.wait_for(queue.get(), min(poll_at, next_heartbeat) - now)
                    except TimeoutError:
                        continue
                    yield _frame("delta", delta)
        finally:
            pump_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await pump_task
