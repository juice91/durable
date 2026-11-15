"""AGUI.4 acceptance: native SSE stays byte-identical to the frozen old engine across a corpus of
event logs, from every Last-Event-ID offset; and a large run streams with bounded memory."""

from __future__ import annotations

import asyncio
import tracemalloc
from datetime import datetime, timezone

import pytest

from eap_api import sse
from eap_api.streams.native import Delta, DurableEvent, Status, run_items
from eap_schemas import RunEvent

from . import legacy_sse_stream as legacy
from .agui4_corpus import AGENT_CORPUS, RUN, B, approval_cycle, workflow_run
from .agui4_harness import SETTINGS, legacy_chunks, native_chunks, no_deltas


def all_logs() -> dict[str, list[RunEvent]]:
    logs = {name: make().events for name, make in AGENT_CORPUS.items()}
    logs["approval_cycle"] = approval_cycle(("g1",))[0].events
    logs["approval_batch"] = approval_cycle(("g1", "g2"))[0].events
    logs["workflow"] = workflow_run()[0].events
    return logs


LOGS = all_logs()


@pytest.mark.parametrize("name", list(LOGS))
def test_native_sse_matches_the_legacy_engine_from_every_offset(name):
    events = LOGS[name]
    assert any(e.type in {"run_completed", "run_failed"} for e in events)
    for after in range(-1, events[-1].seq + 2):
        old = legacy_chunks(RUN, events, after)
        new = native_chunks(RUN, events, after)
        assert new == old, f"{name}: after_seq={after}"
        assert len(old) >= 1


@pytest.mark.parametrize("name", list(LOGS))
def test_native_frames_carry_the_seq_as_id_and_each_event_once(name):
    events = LOGS[name]
    chunks = native_chunks(RUN, events, -1)
    ids = [int(c.split("\n", 1)[0][4:]) for c in chunks if c.startswith("id: ")]
    end = next(e.seq for e in events if e.type in {"run_completed", "run_failed"})
    assert ids == list(range(end + 1))  # nothing after the end event is streamed


def test_native_resume_from_every_offset_is_a_suffix_of_the_full_stream():
    for name, events in LOGS.items():
        full = native_chunks(RUN, events, -1)
        ids = [i for i, c in enumerate(full) if c.startswith("id: ")]
        for n in range(-1, len(ids) - 1):  # past the end event the old engine also streams later rows
            tail = native_chunks(RUN, events, n)
            cut = ids[n] + 1 if n >= 0 else 0
            assert tail[0] == full[0] and tail[0].startswith("event: status")  # the opening status frame
            assert tail[1:] == full[max(cut, 1):], f"{name} after {n}"


def test_resume_past_the_end_yields_no_event_frames():
    for events in LOGS.values():
        tail = native_chunks(RUN, events, events[-1].seq)
        assert not any(c.startswith("id: ") for c in tail)


# --------------------------------------------------------------------------- load


def _big_run(deltas: int):
    events = [RunEvent(run_id=RUN, seq=0, type="run_started", data={}, ts=datetime(2026, 11, 15, tzinfo=timezone.utc)),
              RunEvent(run_id=RUN, seq=1, type="run_completed", data={"output": "x"},
                       ts=datetime(2026, 11, 15, tzinfo=timezone.utc))]
    return events


def test_thousands_of_deltas_stream_with_bounded_memory():
    events = _big_run(0)
    n = 20000
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def firehose(run_id):
        async def gen():
            for i in range(n):
                yield {"run_id": RUN, "model_request": 0, "attempt": "a", "delta": "tok%d " % i}
                if i % 500 == 0:
                    await asyncio.sleep(0)
            await asyncio.Event().wait()

        yield gen()

    async def go() -> tuple[int, int]:
        got = 0
        settle = {"after": 0}

        def load_events(after):
            return [e for e in events if e.seq > after] if settle["after"] > 3 else events[:1] if after < 0 else []

        def load_run():
            settle["after"] += 1
            return {"id": RUN, "status": "completed" if settle["after"] > 3 else "running",
                    "display_status": "running"}

        tracemalloc.start()
        stream = sse.run_event_stream(run_id=RUN, after_seq=-1, load_events=load_events, load_run=load_run,
                                      delta_source=firehose, settings=SETTINGS)
        async for chunk in stream:
            got += chunk.count("event: delta")
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        return got, peak

    got, peak = asyncio.run(go())
    assert got > 0
    assert peak < 20 * 1024 * 1024, f"peak {peak} bytes: the stream must not hold the run in memory"
