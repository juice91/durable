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
import os
import threading
import time
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Any

from fastapi import Request

from eap_observability import log_failure
from eap_schemas import RunEvent

log = logging.getLogger("eap_api")

DELTA_CHANNEL = "eap_run_deltas"
TERMINAL_EVENT_TYPES = frozenset({"run_completed", "run_failed"})
TERMINAL_STATUSES = frozenset({"completed", "failed", "needs_attention"})
_MAX_PENDING_DELTAS = 1000
_LIMITER_INIT_LOCK = threading.Lock()

# A delta source opens a subscription for one run: entering it must start listening (so nothing
# sent after replay begins is lost) and yields an async iterator of delta payload dicts.
DeltaSource = Callable[[str], AbstractAsyncContextManager[AsyncIterator[dict[str, Any]]]]


@dataclass(frozen=True)
class StreamSettings:
    poll_interval: float = 0.5
    heartbeat_interval: float = 10.0
    max_lifetime: float = 3600.0


def get_stream_settings() -> StreamSettings:
    return StreamSettings(max_lifetime=float(os.environ.get("EAP_SSE_MAX_LIFETIME_S", "3600")))


def get_delta_source(request: Request) -> DeltaSource:
    return request.app.state.delta_source


class PostgresDeltaSource:
    """One reconnecting LISTEN connection that fans deltas out to all local subscribers."""

    def __init__(self, conninfo: str) -> None:
        self.conninfo = conninfo
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}
        self._listener_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def __call__(self, run_id: str) -> AsyncIterator[AsyncIterator[dict[str, Any]]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_MAX_PENDING_DELTAS)
        async with self._lock:
            self._subscribers.setdefault(run_id, set()).add(queue)
            if self._listener_task is None or self._listener_task.done():
                self._listener_task = asyncio.create_task(self._listen_forever())
        try:
            yield self._iterate(queue)
        finally:
            async with self._lock:
                queues = self._subscribers.get(run_id)
                if queues is not None:
                    queues.discard(queue)
                    if not queues:
                        self._subscribers.pop(run_id, None)

    async def _listen_forever(self) -> None:
        import psycopg

        while True:
            conn = None
            try:
                conn = await psycopg.AsyncConnection.connect(self.conninfo, autocommit=True)
                await conn.execute(f"LISTEN {DELTA_CHANNEL}")
                async for notify in conn.notifies():
                    self._dispatch(notify.payload)
                raise ConnectionError("delta LISTEN connection closed")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnecting listener must survive database outages
                # The error can quote the conninfo; only its type is logged.
                log_failure(log, "delta LISTEN connection ended; reconnecting", exc, code="delta_listen_failed")
                await asyncio.sleep(1.0)
            finally:
                if conn is not None:
                    with suppress(Exception):
                        await conn.close()

    def _dispatch(self, raw_payload: str) -> None:
        try:
            payload = json.loads(raw_payload)
        except ValueError:
            return
        if not isinstance(payload, dict) or not isinstance(payload.get("run_id"), str):
            return
        for queue in tuple(self._subscribers.get(payload["run_id"], ())):
            if not queue.full():
                queue.put_nowait(payload)

    @staticmethod
    async def _iterate(queue: asyncio.Queue[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
        while True:
            yield await queue.get()

    @property
    def subscriber_count(self) -> int:
        return sum(len(queues) for queues in self._subscribers.values())

    async def close(self) -> None:
        task, self._listener_task = self._listener_task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task


class StreamLease:
    """One admitted stream slot; release is idempotent for disconnect-safe cleanup."""

    def __init__(self, limiter: "StreamLimiter", principal: str) -> None:
        self._limiter = limiter
        self._principal = principal
        self._released = False

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._limiter._release(self._principal)


class StreamLimiter:
    """Process-local global and per-principal SSE admission control."""

    def __init__(self, max_global: int, max_per_principal: int) -> None:
        self.max_global = max_global
        self.max_per_principal = max_per_principal
        self._active = 0
        self._by_principal: dict[str, int] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls) -> "StreamLimiter":
        return cls(
            max_global=int(os.environ.get("EAP_SSE_MAX_STREAMS_GLOBAL", "200")),
            max_per_principal=int(os.environ.get("EAP_SSE_MAX_STREAMS_PER_PRINCIPAL", "10")),
        )

    def acquire(self, principal: str) -> StreamLease | None:
        with self._lock:
            if self._active >= self.max_global:
                return None
            if self._by_principal.get(principal, 0) >= self.max_per_principal:
                return None
            self._active += 1
            self._by_principal[principal] = self._by_principal.get(principal, 0) + 1
        return StreamLease(self, principal)

    def _release(self, principal: str) -> None:
        with self._lock:
            count = self._by_principal[principal] - 1
            self._active -= 1
            if count:
                self._by_principal[principal] = count
            else:
                self._by_principal.pop(principal)

    @property
    def active(self) -> int:
        with self._lock:
            return self._active


def get_stream_limiter(request: Request) -> StreamLimiter:
    limiter = getattr(request.app.state, "stream_limiter", None)
    if limiter is None:
        with _LIMITER_INIT_LOCK:
            limiter = getattr(request.app.state, "stream_limiter", None)
            if limiter is None:
                limiter = request.app.state.stream_limiter = StreamLimiter.from_env()
    return limiter


async def release_stream_slot(stream: AsyncIterator[str], lease: StreamLease) -> AsyncIterator[str]:
    try:
        async for chunk in stream:
            yield chunk
    finally:
        lease.release()


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
