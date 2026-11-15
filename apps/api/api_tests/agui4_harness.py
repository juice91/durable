"""Shared helpers for the AGUI.4 acceptance tests (not a test module): drive the legacy engine,
the native SSE engine and the AG-UI formatter over one recorded durable event log."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from eap_api import sse
from eap_api.ag_ui.conformance import events_of, lifecycle_errors, parse_frames, schema_errors
from eap_api.ag_ui.translate import AgUiFormatter, AgUiStreamContext
from eap_api.streams.formatter import format_stream
from eap_api.streams.native import run_items
from eap_schemas import RunEvent

from . import legacy_sse_stream as legacy

ROOT = Path(__file__).resolve().parents[3]
SCHEMA = json.loads((ROOT / "tests/fixtures/ag-ui/schema-1.0.json").read_text())
SETTINGS = sse.StreamSettings(poll_interval=0.01, heartbeat_interval=60.0, max_lifetime=30.0)


@asynccontextmanager
async def no_deltas(run_id: str) -> AsyncIterator[AsyncIterator[dict[str, Any]]]:
    async def gen() -> AsyncIterator[dict[str, Any]]:
        await asyncio.Event().wait()
        yield {}

    yield gen()


def _loaders(run_id: str, events: list[RunEvent]):
    def load_events(after: int) -> list[RunEvent]:
        return [e for e in events if e.seq > after]

    def load_run() -> dict[str, Any]:
        return {"id": run_id, "status": "completed", "display_status": "completed"}

    return load_events, load_run


async def _collect(stream: AsyncIterator[str]) -> list[str]:
    return [chunk async for chunk in stream]


def legacy_chunks(run_id: str, events: list[RunEvent], after_seq: int = -1) -> list[str]:
    load_events, load_run = _loaders(run_id, events)
    return asyncio.run(_collect(legacy.run_event_stream(
        run_id=run_id, after_seq=after_seq, load_events=load_events, load_run=load_run,
        delta_source=no_deltas, settings=SETTINGS)))


def native_chunks(run_id: str, events: list[RunEvent], after_seq: int = -1) -> list[str]:
    load_events, load_run = _loaders(run_id, events)
    return asyncio.run(_collect(sse.run_event_stream(
        run_id=run_id, after_seq=after_seq, load_events=load_events, load_run=load_run,
        delta_source=no_deltas, settings=SETTINGS)))


def agui_chunks(run_id: str, events: list[RunEvent], *, resume_after: int | None = None,
                summary: dict[str, Any] | None = None, **ctx_kw: Any) -> list[str]:
    """The AG-UI stream: fresh, or a continuation after durable seq `resume_after`."""
    load_events, load_run = _loaders(run_id, events)
    prior = [e for e in events if resume_after is not None and e.seq <= resume_after]
    ctx = AgUiStreamContext(thread_id="thread-x", run_id="agui-run", native_run_id=run_id,
                            prior_events=prior, **ctx_kw)
    items = run_items(run_id=run_id, after_seq=-1 if resume_after is None else resume_after,
                      load_events=load_events, load_run=load_run, delta_source=no_deltas,
                      settings=SETTINGS, load_summary=(lambda: summary) if summary is not None else None)
    return asyncio.run(_collect(format_stream(items, AgUiFormatter(ctx), resume_after=resume_after)))


def conformance(chunks: list[str], **kw: Any) -> list[dict[str, Any]]:
    """Schema + lifecycle check of an AG-UI stream; returns the events."""
    frames = parse_frames(chunks)
    events = events_of(frames)
    bad = schema_errors(events, SCHEMA)
    assert bad == [], bad
    errors = lifecycle_errors(frames, **kw)
    assert errors == [], errors
    return events


def frame_seq(frame_id: str | None) -> int | None:
    return None if frame_id is None else int(frame_id.rsplit(":", 1)[1])


def assert_resume_is_a_suffix(run_id: str, events: list[RunEvent], full: list[str], n: int, **kw: Any) -> None:
    """A continuation after N is the opening frames (run info, snapshot), then exactly the full stream's frames that
    follow the last id-frame with seq <= N: no duplicate, no gap."""
    chunks = agui_chunks(run_id, events, resume_after=n, **kw)
    got = parse_frames(chunks)
    full_frames = parse_frames(full)
    def body(frames):
        out = [f for f in frames if not (f.event and (f.event.get("name") == "pydanticos.status"
                                                       or f.event["type"] == "RUN_STARTED"))]
        while out and out[0].event and (out[0].event["type"] in {"MESSAGES_SNAPSHOT", "STATE_SNAPSHOT"}
                                        or out[0].event.get("name") == "pydanticos.run"):
            out = out[1:]
        return out

    cut = max((i for i, f in enumerate(full_frames) if (s := frame_seq(f.id)) is not None and s <= n), default=-1)
    assert body(got) == body(full_frames[cut + 1:]), f"resume after {n}"
