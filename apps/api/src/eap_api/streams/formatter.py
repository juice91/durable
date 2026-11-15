"""Output formats over the native item stream (D0).

A `StreamFormatter` is pure: items in, frames (strings) out. It never reads the database, the
clock or the network; everything it needs beyond the items comes in its constructor. One
formatter instance serves one stream.

`format_stream` drives a formatter over an `ItemStream`:
1. `start()` for a fresh stream, or `resume_frames(after_seq)` for a continuation (a client that
   already holds the frames up to `after_seq`);
2. `on_item(item)` for each item, until the items end or the formatter says it is `closed`
   (AG-UI ends a stream at an interrupt while the durable run keeps waiting);
3. `finish(reason)` once, with why the items ended (`terminal`, `expired`, `gone`).

An exception from the item source goes to `fail()`: frames to send instead (AG-UI's
`RUN_ERROR stream_interrupted`), or None to re-raise (the native SSE, as before the refactor).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Protocol

from eap_api.streams.native import EndReason, ItemStream, StreamItem


class StreamFormatter(Protocol):
    @property
    def closed(self) -> bool:
        """True once the formatter has ended its stream (no further frames)."""
        ...

    def start(self) -> list[str]: ...

    def resume_frames(self, after_seq: int) -> list[str]: ...

    def on_item(self, item: StreamItem) -> list[str]: ...

    def finish(self, reason: EndReason) -> list[str]: ...

    def fail(self) -> list[str] | None: ...


async def format_stream(
    items: ItemStream, formatter: StreamFormatter, *, resume_after: int | None = None
) -> AsyncIterator[str]:
    """Yield one frame per chunk, exactly as the formatter returns them."""
    opening = formatter.start() if resume_after is None else formatter.resume_frames(resume_after)
    for frame in opening:
        yield frame
    if formatter.closed:
        return
    try:
        async with aclosing(items.__aiter__()) as stream:
            async for item in stream:
                for frame in formatter.on_item(item):
                    yield frame
                if formatter.closed:
                    return
    except Exception:
        frames = formatter.fail()
        if frames is None:
            raise
        for frame in frames:
            yield frame
        return
    for frame in formatter.finish(items.end_reason):
        yield frame
