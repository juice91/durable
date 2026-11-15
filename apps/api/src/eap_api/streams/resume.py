"""Frame ids, `Last-Event-ID` parsing and the resume decision (D4, D7), shared by every output
format that resumes by SSE id.

- The native SSE frame id is the durable seq itself (`id: 7`).
- A protocol run's frame id is `{run_id}:{seq}` (`run_id` is the protocol's own run id, e.g. the
  AG-UI `runId`, which may itself contain colons: parsing splits on the **last** colon).
- Only frames derived from a durable event carry an id; when one event yields several frames
  only the last one does (`with_frame_id`).

`plan_stream` is the D7 server table as a pure function: given what the request carries and what
is recorded, it answers how to stream (fresh, attach from start, or a continuation after seq N),
or the HTTP error to send before the stream opens.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

LAST_EVENT_ID_DETAIL = "Last-Event-ID must be <runId>:<seq> for this run"


def frame_id(run_id: str, seq: int) -> str:
    return f"{run_id}:{seq}"


def with_frame_id(frames: list[str], id_: str | None) -> list[str]:
    """Put `id: <id_>` on the last frame of `frames` (each a complete `data: ...\\n\\n` frame)."""
    if id_ is None or not frames:
        return frames
    if "\n" in id_ or "\r" in id_:
        raise ValueError("an SSE id cannot contain a line break")
    return [*frames[:-1], f"id: {id_}\n{frames[-1]}"]


class LastEventIdError(ValueError):
    """A malformed `Last-Event-ID` (400 before the stream). Pickle-safe."""

    def __init__(self, value: str = "") -> None:
        super().__init__(value)
        self.value = value

    def __str__(self) -> str:
        return LAST_EVENT_ID_DETAIL


def parse_last_event_id(value: str, run_id: str, max_seq: int | None) -> int:
    """The seq N of `Last-Event-ID: {run_id}:{N}`. Rejected (LastEventIdError): no colon, a run id
    other than `run_id`, a non-integer N, N < -1, or N above `max_seq` (the run's highest seq;
    None = the run has no events yet, so only -1 is valid)."""
    raw = value.strip()
    head, sep, tail = raw.rpartition(":")
    if not sep or head != run_id:
        raise LastEventIdError(value)
    tail = tail.strip()
    if not tail or not (tail.isdigit() or (tail.startswith("-") and tail[1:].isdigit())):
        raise LastEventIdError(value)
    seq = int(tail)
    if seq < -1 or seq > (max_seq if max_seq is not None else -1):
        raise LastEventIdError(value)
    return seq


StreamMode = Literal["fresh", "attach", "continue"]


@dataclass(frozen=True)
class StreamPlan:
    """How to serve one protocol request. `mode` is set when `status` is 200; `after_seq` is the
    seq the durable events start after (from_seq for fresh/attach, N for a continuation)."""

    status: int
    mode: StreamMode | None = None
    after_seq: int = -1
    detail: str | None = None


def plan_stream(
    *,
    run_id: str,
    recorded: bool,
    same_fingerprint: bool,
    created_now: bool,
    from_seq: int,
    last_event_id: str | None,
    max_seq: int | None,
    end_seq: int | None = None,
) -> StreamPlan:
    """The D7 table.

    - `recorded`: the protocol run id is known (an `ag_ui_runs` row exists, possibly just written);
    - `created_now`: this request recorded it (a new run id);
    - `same_fingerprint`: the request's fingerprint equals the recorded one (ignored when created now);
    - `from_seq`: the recorded seq the run's own events start after (-1 for an agent turn);
    - `last_event_id`: the raw header (None or blank = none);
    - `max_seq`: the durable run's highest seq (None = no events yet);
    - `end_seq`: the seq of the event that ended this protocol run when it is not the durable
      run's end (an interrupt: the durable run continues under the next runId). A cursor past it
      names frames this run never sent, so it is 400 like any other foreign cursor.
    """
    header = last_event_id.strip() if last_event_id is not None else ""
    if not recorded:
        # A POST records a new runId before planning, so this is a GET attach of an unknown run, or
        # a POST carrying `Last-Event-ID` for a runId never seen (which must not record anything).
        return StreamPlan(status=404, detail="Unknown runId")
    if not created_now and not same_fingerprint:
        return StreamPlan(status=409, detail="runId was already used with a different input")
    if header:
        try:
            bound = max_seq if end_seq is None or max_seq is None else min(max_seq, end_seq)
            seq = parse_last_event_id(header, run_id, bound)
        except LastEventIdError:
            return StreamPlan(status=400, detail=LAST_EVENT_ID_DETAIL)
        return StreamPlan(status=200, mode="continue", after_seq=max(seq, from_seq))
    if created_now:
        return StreamPlan(status=200, mode="fresh", after_seq=from_seq)
    return StreamPlan(status=200, mode="attach", after_seq=from_seq)
