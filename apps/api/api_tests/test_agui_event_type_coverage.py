"""Every durable event type is either rendered by the AG-UI translator or explicitly silent.

Guards merges that bring new RunEventType values (RAG P1 added `extracted`, `memory_observe_trimmed`
and `post_turn_done`): a new type must be classified here on purpose."""

from typing import get_args

from eap_api.ag_ui.translate import SILENT_EVENT_TYPES, STEP_DECISIONS, TOOL_DECISIONS
from eap_schemas import RunEventType

RENDERED = {
    "run_started", "model_message", "tool_call", "tool_result", "run_completed", "run_failed",
    "processor_result", "output_retry", "tool_approval_waiting", *TOOL_DECISIONS,
    "step_started", "step_waiting", "step_completed", "step_failed", *STEP_DECISIONS,
    # Workflow loops (LP.13, D17): iteration frames and the loop-step counters.
    "loop_iteration_started", "loop_iteration_completed", "loop_iteration_failed",
    # W2.4 message feedback: the custom event `pydanticos.message_feedback`.
    "message_feedback",
}


def test_every_event_type_is_rendered_or_silent():
    types = set(get_args(RunEventType))
    assert types - RENDERED - SILENT_EVENT_TYPES == set()
    assert RENDERED & SILENT_EVENT_TYPES == set()


def test_rag_p1_event_types_are_silent():
    assert {"extracted", "memory_observe_trimmed", "post_turn_done"} <= SILENT_EVENT_TYPES
