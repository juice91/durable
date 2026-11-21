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

Since AGUI.4 (D0) the engine is the format-agnostic item stream `eap_api.streams.native.run_items`
and these frames are its first formatter, `eap_api.streams.native_sse.NativeSseFormatter`; the
output is byte-identical to the engine that used to live here (pinned by a golden test).
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from typing import Any



from eap_api.streams.formatter import format_stream
# StreamSettings, DeltaSource and the terminal sets are re-exported: callers import them from here.
from eap_api.streams.native import (
    MAX_PENDING_DELTAS as _MAX_PENDING_DELTAS,
    TERMINAL_EVENT_TYPES,
    TERMINAL_STATUSES,
    DeltaSource,
    StreamSettings,
    run_items,
)
from eap_api.streams.native_sse import NativeSseFormatter, event_json
from eap_observability import log_failure
from eap_schemas import RunEvent

log = logging.getLogger("eap_api")

DELTA_CHANNEL = "eap_run_deltas"

def get_stream_settings() -> StreamSettings:
    return StreamSettings(max_lifetime=float(os.environ.get("EAP_SSE_MAX_LIFETIME_S", "3600")))


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
                # A fresh, empty context: the listener outlives this request and serves every
                # tenant, so it must not inherit the request's tenant (log context) or trace.
                self._listener_task = asyncio.create_task(self._listen_forever(), context=contextvars.Context())
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
    items = run_items(
        run_id=run_id,
        after_seq=after_seq,
        load_events=load_events,
        load_run=load_run,
        delta_source=delta_source,
        settings=settings,
    )
    async for frame in format_stream(items, NativeSseFormatter()):
        yield frame


def __getattr__(name: str):
    # The providers live in eap_api.deps (the one module that reads app.state); importing them here
    # eagerly would be a cycle, since deps imports DeltaSource and StreamLimiter from this module.
    if name in ("get_delta_source", "get_stream_limiter"):
        from eap_api import deps

        return getattr(deps, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
