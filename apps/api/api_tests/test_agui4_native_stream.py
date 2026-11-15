"""AGUI.4: the native item stream and the native SSE formatter.

Golden test: the refactored `sse.run_event_stream` (run_items + NativeSseFormatter) yields the same
chunks, byte for byte, as the engine it replaced (`legacy_sse_stream`, a frozen copy) for the same
scripted event log, deltas, status changes, heartbeats, resume point and lifetime. A fake clock and
a fake `wait_for` make both runs deterministic; the scripted loaders answer by call count.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import pytest

import eap_api.streams.native as native
from eap_api.sse import StreamSettings, run_event_stream
from eap_api.streams.formatter import format_stream
from eap_api.streams.native import Delta, DurableEvent, Heartbeat, RunSummary, Status, run_items
from eap_schemas import RunEvent

from . import legacy_sse_stream as legacy

RUN = "5b0e0a4e-8f00-4c51-a7a0-000000000001"
TS = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)


def ev(seq: int, type_: str, data: dict[str, Any] | None = None) -> RunEvent:
    return RunEvent(run_id=RUN, seq=seq, type=type_, data=data or {}, ts=TS)


def run_row(status: str = "running", display: str | None = None) -> dict[str, Any]:
    return {"id": RUN, "status": status, "display_status": display or status}


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def monotonic(self) -> float:
        return self.t


class Script:
    """Answers load_events / load_run by call index (the last answer repeats)."""

    def __init__(self, events: list[list[RunEvent]], runs: list[dict[str, Any] | None]) -> None:
        self.events = events
        self.runs = runs
        self.event_calls = 0
        self.run_calls = 0

    def load_events(self, after: int) -> list[RunEvent]:
        batch = self.events[min(self.event_calls, len(self.events) - 1)]
        self.event_calls += 1
        return [e for e in batch if e.seq > after]

    def load_run(self) -> dict[str, Any] | None:
        row = self.runs[min(self.run_calls, len(self.runs) - 1)]
        self.run_calls += 1
        return row


def delta_source(payloads: list[dict[str, Any]]):
    @asynccontextmanager
    async def source(run_id: str) -> AsyncIterator[AsyncIterator[dict[str, Any]]]:
        async def gen() -> AsyncIterator[dict[str, Any]]:
            for payload in payloads:
                yield payload
            await asyncio.Event().wait()

        yield gen()

    return source


async def _collect(stream: AsyncIterator[str], limit: int = 500) -> list[str]:
    out: list[str] = []
    async for chunk in stream:
        out.append(chunk)
        if len(out) >= limit:
            break
    return out


def _run_both(monkeypatch, make_script, payloads, settings, after_seq=-1) -> tuple[list[str], list[str]]:
    real_wait_for = asyncio.wait_for

    def run(module_with_time, stream_fn) -> list[str]:
        clock = FakeClock()

        async def fake_wait_for(awaitable, timeout):
            task = asyncio.ensure_future(awaitable)
            for _ in range(5):
                await asyncio.sleep(0)
                if task.done():
                    return task.result()
            task.cancel()
            clock.t += max(timeout, 0.0) + 1e-6
            raise TimeoutError

        monkeypatch.setattr(module_with_time, "time", clock)
        monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)
        script = make_script()
        try:
            return asyncio.run(
                _collect(
                    stream_fn(
                        run_id=RUN,
                        after_seq=after_seq,
                        load_events=script.load_events,
                        load_run=script.load_run,
                        delta_source=delta_source(payloads),
                        settings=settings,
                    )
                )
            )
        finally:
            monkeypatch.setattr(asyncio, "wait_for", real_wait_for)

    return run(legacy, legacy.run_event_stream), run(native, run_event_stream)


def _delta(n: int, attempt: str, text: str, run_id: str = RUN) -> dict[str, Any]:
    return {"run_id": run_id, "model_request": n, "attempt": attempt, "delta": text}


SCENARIOS: dict[str, dict[str, Any]] = {
    "replay to the end event": dict(
        script=lambda: Script(
            [[ev(0, "run_started", {"input": "hi"}), ev(1, "model_message", {"model_request": 0, "text": "yo"}),
              ev(2, "run_completed", {"output": "yo"}), ev(3, "tool_call", {})]],
            [run_row()],
        ),
        payloads=[],
        settings=StreamSettings(poll_interval=0.5, heartbeat_interval=10.0),
    ),
    "live deltas, status changes, heartbeats, then terminal status without an end event": dict(
        script=lambda: Script(
            [[], [], [ev(0, "run_started")], [ev(0, "run_started")],
             [ev(0, "run_started"), ev(1, "model_message", {"model_request": 0, "text": "abc"})]],
            [run_row(), run_row(display="recovering"), run_row(), run_row(), run_row("completed")],
        ),
        payloads=[_delta(0, "a", "ab"), {"run_id": "other", "delta": "x"}, _delta(0, "a", "c"), _delta(1, "b", "é\"\n")],
        settings=StreamSettings(poll_interval=0.5, heartbeat_interval=0.7),
    ),
    "resume after a seq": dict(
        script=lambda: Script(
            [[ev(0, "run_started"), ev(1, "tool_call", {"tool_call_id": "c1"}), ev(2, "tool_result", {}),
              ev(3, "run_failed", {"error": "boom", "error_code": "x"})]],
            [run_row()],
        ),
        payloads=[],
        settings=StreamSettings(),
        after_seq=1,
    ),
    "end event written between the two reads of a terminal run": dict(
        script=lambda: Script([[ev(0, "run_started")], [ev(0, "run_started"), ev(1, "run_completed", {"output": "x"})]],
                              [run_row("completed")]),
        payloads=[],
        settings=StreamSettings(),
    ),
    "maximum lifetime": dict(
        script=lambda: Script([[]], [run_row()]),
        payloads=[_delta(0, "a", "x")],
        settings=StreamSettings(poll_interval=0.5, heartbeat_interval=0.3, max_lifetime=2.0),
    ),
    "run disappears": dict(
        script=lambda: Script([[ev(0, "run_started")]], [run_row(), None]),
        payloads=[],
        settings=StreamSettings(),
    ),
    "run never found": dict(
        script=lambda: Script([[]], [None]),
        payloads=[],
        settings=StreamSettings(),
    ),
}


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_native_sse_is_byte_identical_to_the_legacy_engine(monkeypatch, name):
    scenario = SCENARIOS[name]
    old, new = _run_both(
        monkeypatch, scenario["script"], scenario["payloads"], scenario["settings"], scenario.get("after_seq", -1)
    )
    assert old or name == "run never found", "the scenario produced no frames"
    assert new == old


def test_the_live_scenario_covers_every_frame_kind(monkeypatch):
    scenario = SCENARIOS["live deltas, status changes, heartbeats, then terminal status without an end event"]
    _old, new = _run_both(monkeypatch, scenario["script"], scenario["payloads"], scenario["settings"])
    text = "".join(new)
    assert ": heartbeat\n\n" in new
    assert '"display_status":"recovering"' in text
    assert "event: delta\n" in text and '"run_id":"other"' not in text
    assert "id: 1\nevent: run_event\n" in text


# --------------------------------------------------------------------------- items


class EchoFormatter:
    """A test formatter: echoes item types. Needs nothing of AG-UI (the interface is generic)."""

    closed = False

    def __init__(self, close_after: int | None = None) -> None:
        self.close_after = close_after
        self.seen = 0

    def start(self) -> list[str]:
        return ["start"]

    def resume_frames(self, after_seq: int) -> list[str]:
        return [f"resume:{after_seq}"]

    def on_item(self, item) -> list[str]:
        self.seen += 1
        if self.close_after is not None and self.seen >= self.close_after:
            self.closed = True
        if isinstance(item, DurableEvent):
            return [f"event:{item.seq}:{item.type}"]
        return [type(item).__name__]

    def finish(self, reason) -> list[str]:
        return [f"finish:{reason}"]

    def fail(self):
        return ["failed"]


def _items(script: Script, **kw) -> native.ItemStream:
    return run_items(
        run_id=RUN, after_seq=kw.pop("after_seq", -1), load_events=script.load_events, load_run=script.load_run,
        delta_source=kw.pop("deltas", native_no_deltas()), settings=kw.pop("settings", StreamSettings(0.01, 10.0)),
        **kw,
    )


def native_no_deltas():
    from eap_api.sse import no_deltas

    return no_deltas


def test_echo_formatter_sees_typed_items_and_summary_before_the_terminal_event():
    script = Script([[ev(0, "run_started"), ev(1, "run_completed", {"output": "x"})]], [run_row()])
    frames = asyncio.run(_collect(format_stream(_items(script, load_summary=lambda: {"usage": [1]}), EchoFormatter())))
    assert frames == ["start", "Status", "event:0:run_started", "RunSummary", "event:1:run_completed", "finish:terminal"]


def test_items_carry_typed_fields():
    script = Script([[], [], [], [ev(0, "run_failed", {"error": "e"})]], [run_row()])
    seen: list[Any] = []

    async def scenario():
        async for item in _items(script, deltas=delta_source([_delta(2, "att", "hey")]),
                                 settings=StreamSettings(poll_interval=0.05, heartbeat_interval=0.02),
                                 load_summary=lambda: {"k": "v"}):
            seen.append(item)

    asyncio.run(scenario())
    deltas = [i for i in seen if isinstance(i, Delta)]
    assert deltas and (deltas[0].model_request, deltas[0].attempt, deltas[0].text) == (2, "att", "hey")
    assert any(isinstance(i, Heartbeat) for i in seen)
    assert isinstance(seen[0], Status) and seen[0].display_status == "running"
    assert [i.data for i in seen if isinstance(i, RunSummary)] == [{"k": "v"}]
    assert isinstance(seen[-1], DurableEvent) and seen[-1].type == "run_failed"


def test_resume_frames_replace_start_and_closed_formatter_stops_the_stream():
    script = Script([[ev(0, "run_started"), ev(1, "tool_call"), ev(2, "run_completed")]], [run_row()])
    frames = asyncio.run(_collect(format_stream(_items(script), EchoFormatter(close_after=2), resume_after=-1)))
    assert frames == ["resume:-1", "Status", "event:0:run_started"]  # no finish once closed


def test_end_reasons_and_source_failure():
    gone = Script([[]], [run_row(), None])
    assert asyncio.run(_collect(format_stream(_items(gone), EchoFormatter())))[-1] == "finish:gone"
    expired = Script([[]], [run_row()])
    frames = asyncio.run(_collect(format_stream(_items(expired, settings=StreamSettings(0.01, 10.0, 0.05)), EchoFormatter())))
    assert frames[-1] == "finish:expired"

    def boom(after: int) -> list[RunEvent]:
        raise RuntimeError("db down")

    broken = native.run_items(run_id=RUN, after_seq=-1, load_events=boom, load_run=lambda: run_row(),
                              delta_source=native_no_deltas(), settings=StreamSettings())
    assert asyncio.run(_collect(format_stream(broken, EchoFormatter()))) == ["start", "Status", "failed"]
