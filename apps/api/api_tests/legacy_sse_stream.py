"""A frozen copy of the native SSE engine as it was before AGUI.4 (sse.py at 0cd315b), kept only as
the oracle of the golden test (test_agui4_native_stream.py). Never import it from product code."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from typing import Any

from eap_observability import log_failure
from eap_schemas import RunEvent

log = logging.getLogger("eap_api")

TERMINAL_EVENT_TYPES = frozenset({"run_completed", "run_failed"})
TERMINAL_STATUSES = frozenset({"completed", "failed", "needs_attention"})
_MAX_PENDING_DELTAS = 1000


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
    delta_source: Any,
    settings: Any,
) -> AsyncIterator[str]:
    """Yield SSE frames for one run. `load_events`/`load_run` are blocking and run in a thread."""
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_MAX_PENDING_DELTAS)
    expires_at = time.monotonic() + settings.max_lifetime

    async def pump(deltas: AsyncIterator[dict[str, Any]]) -> None:
        try:
            async for delta in deltas:
                if delta.get("run_id") == run_id and not queue.full():
                    queue.put_nowait(delta)
        except Exception as exc:  # noqa: BLE001 - losing live deltas must not end the durable stream
            log_failure(log, "delta subscription ended", exc, code="delta_subscription_failed", run_id=run_id)

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
                if time.monotonic() >= expires_at:
                    return
                for event in await asyncio.to_thread(load_events, last_seq):
                    if time.monotonic() >= expires_at:
                        return
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
                        if time.monotonic() >= expires_at:
                            return
                        last_seq = event.seq
                        yield _frame("run_event", event_json(event), event.seq)
                        if event.type in TERMINAL_EVENT_TYPES:
                            return
                    return

                # Until the next poll: forward deltas as they arrive, heartbeat when due.
                poll_at = time.monotonic() + settings.poll_interval
                while (now := time.monotonic()) < poll_at:
                    if now >= expires_at:
                        return
                    if now >= next_heartbeat:
                        yield ": heartbeat\n\n"
                        yield _status_frame(run)
                        next_heartbeat = now + settings.heartbeat_interval
                    try:
                        delta = await asyncio.wait_for(
                            queue.get(), min(poll_at, next_heartbeat, expires_at) - now
                        )
                    except TimeoutError:
                        continue
                    yield _frame("delta", delta)
        finally:
            pump_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await pump_task
