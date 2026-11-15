"""Synthetic durable event logs in the shapes the worker writes (not a test module).

Every log is complete (ends with run_completed / run_failed) and gap-free from seq 0.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from eap_schemas import RunEvent

RUN = "7d1e0000-0000-4000-8000-0000000000a1"
TS = datetime(2026, 11, 15, tzinfo=timezone.utc)
SECRET = "sk-live-SECRETVALUE-123"


class B:
    """Builds an event log."""

    def __init__(self) -> None:
        self.events: list[RunEvent] = []

    def add(self, type_: str, **data: Any) -> "B":
        self.events.append(RunEvent(run_id=RUN, seq=len(self.events), type=type_, data=data, ts=TS))
        return self


def calls(*pairs: tuple[str, str], text: str = "") -> dict[str, Any]:
    parts: list[dict[str, Any]] = [{"part_kind": "text", "content": text}] if text else []
    parts += [{"part_kind": "tool-call", "tool_name": n, "tool_call_id": c, "args": {}} for c, n in pairs]
    return {"kind": "response", "parts": parts}


def plain() -> B:
    return B().add("run_started", input="hi").add("model_message", model_request=0, text="Echo: hi", message={}) \
        .add("run_completed", output="Echo: hi")


def tool_chain() -> B:
    b = B().add("run_started", input="x")
    b.add("model_message", model_request=0, text="Let me check.", message=calls(("c1", "lookup"), ("c2", "lookup"),
                                                                              text="Let me check."))
    b.add("tool_call", tool_call_id="c1", tool_name="lookup", args={"q": 1})
    b.add("tool_call", tool_call_id="c2", tool_name="lookup", args={"q": 2})
    b.add("tool_result", tool_call_id="c1", tool_name="lookup", status="ok", content={"hit": 1})
    b.add("tool_result", tool_call_id="c2", tool_name="lookup", status="error", content="boom")
    b.add("model_message", model_request=1, text="Done", message={})
    return b.add("run_completed", output="Done")


def retrieve_run() -> B:
    b = B().add("run_started").add("embedding", model_id="test:hash", inputs=1, input_tokens=4, cost_usd=0)
    b.add("model_message", model_request=0, text="", message=calls(("r1", "retrieve")))
    b.add("tool_call", tool_call_id="r1", tool_name="retrieve", args={"query": "jam"})
    b.add("tool_result", tool_call_id="r1", tool_name="retrieve", status="ok", content="[1] Open tray two.",
          sources=[{"n": 1, "url": "https://x.test/a", "text": SECRET}])
    b.add("model_message", model_request=1, text="Open tray two [1].", message={})
    return b.add("run_completed", output="Open tray two [1].")


def memory_run() -> B:
    b = B().add("run_started").add("model_message", model_request=0, text="Noted.", message={})
    b.add("memory_updated", kind="working", key="user-1").add("run_completed", output="Noted.")
    return b.add("memory_observe_failed", error_code="observer_failed")


def structured_run() -> B:
    b = B().add("run_started")
    b.add("model_message", model_request=0, text="", output_candidate=True, message=calls(("o1", "final_result")))
    b.add("output_retry", model_request=0, errors=["/age"], cause=SECRET)
    b.add("model_message", model_request=1, text="", output_candidate=True, message=calls(("o2", "final_result")))
    return b.add("run_completed", output='{"age":3}', output_json={"age": 3})


def failed_mid_text() -> B:
    return B().add("run_started").add("run_failed", status="failed", error="Model unavailable.", error_code="model_error")


def needs_attention() -> B:
    b = B().add("run_started").add("model_message", model_request=0, text="", message=calls(("u1", "unsafe")))
    b.add("tool_call", tool_call_id="u1", tool_name="unsafe", args={})
    return b.add("run_failed", status="needs_attention", error="Needs attention.", error_code="needs_attention")


def processors() -> B:
    b = B().add("run_started").add("processor_result", phase="input", index=0, processor_type="pii", action="redact",
                                   match_counts={"email": 2}, reason=SECRET)
    return b.add("model_message", model_request=0, text="ok", message={}).add("run_completed", output="ok")


def fallback_run() -> B:
    b = B().add("run_started")
    b.add("model_message", model_request=0, text="Hi", message={}, model_id="anthropic:b", requested_model_id="openai:a",
          fallback_index=1, failed_attempts=[{"model_id": "openai:a", "outcome": "error", "error_type": "RateLimit",
                                              "message": SECRET}])
    return b.add("run_completed", output="Hi")


def no_deltas_long_text() -> B:
    text = "word " * 400
    return B().add("run_started").add("model_message", model_request=0, text=text, message={}) \
        .add("run_completed", output=text)


AGENT_CORPUS: dict[str, Callable[[], B]] = {
    "plain": plain, "tool_chain": tool_chain, "retrieve": retrieve_run, "memory": memory_run,
    "structured": structured_run, "failed": failed_mid_text, "needs_attention": needs_attention,
    "processors": processors, "fallback": fallback_run, "long_text": no_deltas_long_text,
}


def approval_cycle(batch: tuple[str, ...] = ("g1", "g2")) -> tuple[B, int]:
    """A gated run: waiting events, then the decisions and the rest. Returns (log, seq of the last
    waiting event): the first AG-UI run ends there, the resume run covers the rest."""
    b = B().add("run_started").add("model_message", model_request=0, text="", message=calls(*[(c, "pay") for c in batch]))
    for c in batch:
        b.add("tool_approval_waiting", tool_call_id=c, tool_name="pay", args={"amount": 5},
              deadline="2026-11-16T00:00:00+00:00", workflow_id=RUN, batch=list(batch))
    waiting = len(b.events) - 1
    b.add("tool_approved", tool_call_id=batch[0], decided_by="user-secret", comment=SECRET)
    if len(batch) > 1:
        b.add("tool_denied", tool_call_id=batch[1], decided_by="user-secret", comment=SECRET)
    for c in batch:
        b.add("tool_result", tool_call_id=c, tool_name="pay", status="ok", content="r-" + c)
    b.add("model_message", model_request=1, text="Done", message={}).add("run_completed", output="Done")
    return b, waiting


def workflow_run() -> tuple[B, int]:
    """A workflow with tool/branch/parallel steps and an approval step; (log, waiting seq)."""
    b = B().add("run_started", kind="workflow", workflow_id="w", input={"x": 1})
    b.add("step_started", step_id="lookup", step_type="tool").add("step_completed", step_id="lookup", output={"v": 1})
    b.add("step_started", step_id="route", step_type="branch").add("step_completed", step_id="route", output={"case": 0})
    b.add("step_started", step_id="fan", step_type="parallel").add("step_started", step_id="a/b", step_type="tool")
    b.add("step_completed", step_id="a/b", output=1).add("step_completed", step_id="fan", output=None)
    b.add("step_started", step_id="ok", step_type="approval")
    b.add("step_waiting", step_id="ok", prompt="Ship it?", timeout_hours=1, workflow_id=RUN,
          deadline="2026-11-16T00:00:00+00:00")
    waiting = len(b.events) - 1
    b.add("step_approved", step_id="ok", decided_by="user-secret", comment=SECRET)
    b.add("step_completed", step_id="ok", output={"approved": True})
    b.add("run_completed", output='{"done":true}', result={"done": True})
    return b, waiting
