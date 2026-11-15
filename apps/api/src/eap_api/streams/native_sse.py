"""The native SSE format of `GET /runs/{id}/stream` (the core protocol Studio is built on).

Frames, byte-identical to the stream before the D0 refactor:
- `event: status` with `{run_id, status, display_status}` for each `Status` item;
- `id: <seq>` + `event: run_event` with `{seq, type, data, ts}` for each durable event;
- `event: delta` with the NOTIFY payload as received, without an id;
- `: heartbeat` for each `Heartbeat`.
The resume point is the plain integer seq (`Last-Event-ID` / `?after_seq=`), so a continuation
needs no opening frames.
"""

from __future__ import annotations

import json
from typing import Any

from eap_api.streams.native import Delta, DurableEvent, EndReason, Heartbeat, Status, StreamItem
from eap_schemas import RunEvent


def sse_frame(event: str, data: Any, id_: int | str | None = None) -> str:
    head = f"id: {id_}\n" if id_ is not None else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


def event_json(event: RunEvent) -> dict[str, Any]:
    return {"seq": event.seq, "type": event.type, "data": event.data, "ts": event.ts.isoformat()}


def status_frame(run: dict[str, Any]) -> str:
    return sse_frame("status", {"run_id": str(run["id"]), "status": run["status"], "display_status": run["display_status"]})


class NativeSseFormatter:
    closed = False

    def start(self) -> list[str]:
        return []

    def resume_frames(self, after_seq: int) -> list[str]:
        return []

    def on_item(self, item: StreamItem) -> list[str]:
        if isinstance(item, Status):
            return [status_frame(item.run)]
        if isinstance(item, DurableEvent):
            return [sse_frame("run_event", event_json(item.event), item.seq)]
        if isinstance(item, Delta):
            return [sse_frame("delta", item.payload)]
        if isinstance(item, Heartbeat):
            return [": heartbeat\n\n"]
        return []  # RunSummary: not part of the native format

    def finish(self, reason: EndReason) -> list[str]:
        return []

    def fail(self) -> list[str] | None:
        return None
