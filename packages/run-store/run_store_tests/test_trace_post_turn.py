"""P1.C1 / P1.C6 in the event-derived trace: `post_turn_done` is a marker on the run span (no span),
and `memory_observe_trimmed` (written right before the reflection's event) neither makes a span nor
becomes the reflector span's start."""

from __future__ import annotations

from eap_run_store.trace import RunData, TraceInput, build_trace

from .test_trace_rag_memory import AGENT_EVENTS, REQUESTS, _events, _run

TRIMMED = ("memory:reflect:2:trimmed", "memory_observe_trimmed",
           {"stage": "reflect", "covers_from_turn": 0, "covers_to_turn": 2, "observations": 9, "dropped": 4,
            "dropped_tokens": 40000, "kept_tokens": 31000, "max_tokens": 32000})
DONE = ("memory:post_turn:2", "post_turn_done", {"observe": "created", "index": "done"})


def _trace(rows):
    return build_trace(TraceInput(root=RunData(run=_run(), events=_events(rows), requests=dict(REQUESTS))))


def _spans(trace):
    return {s["id"]: s for s in trace["spans"]}


def test_post_turn_done_is_a_run_span_marker_not_a_span():
    base = _trace(AGENT_EVENTS)
    trace = _trace([*AGENT_EVENTS, DONE])
    assert set(_spans(trace)) == set(_spans(base))  # no new span
    run = _spans(trace)["run:r1"]["attributes"]
    assert run["post_turn"]["observe"] == "created" and run["post_turn"]["index"] == "done"
    assert run["post_turn"]["done_at"] == _events([*AGENT_EVENTS, DONE])[-1]["ts"].isoformat()
    assert "post_turn" not in _spans(base)["run:r1"]["attributes"]
    assert trace["totals"] == base["totals"]


def test_post_turn_done_keeps_only_its_status_words():
    rows = [*AGENT_EVENTS, ("memory:post_turn:2", "post_turn_done", {"observe": "skipped", "index": "off", "x": 1,
                                                                   "text": "SECRET"})]
    marker = _spans(_trace(rows))["run:r1"]["attributes"]["post_turn"]
    assert set(marker) == {"observe", "index", "done_at"}


def test_trimmed_marker_is_not_the_reflector_span_start():
    rows = list(AGENT_EVENTS)
    i = next(n for n, (k, _, _) in enumerate(rows) if k == "memory:reflect:2:failed")
    without = _spans(_trace(rows))
    with_marker = _spans(_trace([*rows[:i], TRIMMED, *rows[i:]]))
    assert set(with_marker) == set(without)  # the marker makes no span
    reflect_ids = [sid for sid, s in with_marker.items() if s["kind"] == "reflector"]
    assert reflect_ids
    for sid in reflect_ids:
        # Starts at the observer's event (the previous real event), not at the trimmed marker.
        expected_start = _events([*rows[:i], TRIMMED, *rows[i:]])[i - 1]["ts"]
        assert with_marker[sid]["start"] == expected_start.isoformat()
