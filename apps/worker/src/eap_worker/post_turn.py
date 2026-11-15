"""The end of a turn's post-turn memory steps (RAG P1.C hardening): the `post_turn_done` event.

Only for a spec with a post-turn memory feature (`memory.semantic_recall` and/or
`memory.observational`) AND a conversation turn that COMPLETED. After the post-turn steps
(`eap.memory_index`, then the observer child workflow `eap.memory_observe_run` and its getResult),
the run's workflow appends ONE more event with the existing `eap.append_event` step:

    eap.append_event   memory:post_turn:{turn}    post_turn_done {observe, index}; no text

`observe`: `created` (an observation or a reflection was written, or found written by a re-executed
step), `skipped` (nothing to observe or reflect, or the conversation was deleted), `failed` (a
`memory_observe_failed` event, or the observer workflow itself failed), `off` (no
`memory.observational`). `index`: `done` (the turn was indexed, or already was), `skipped` (nothing
to index, forget watermark, or the conversation was deleted), `failed`, `off` (no
`memory.semantic_recall`).

The event key makes it idempotent: a re-executed step finds the event and writes nothing. Studio's
memory inspector stops its "Memory updating" poll when it arrives (and keeps its 20 s timeout for
older workers). It is appended with the conversation row held `FOR KEY SHARE`
(append_event_step's `conversation`, MemoryStore.append_conversation_event), so a conversation
deleted meanwhile gets no event (the step returns -1).

Step layout: one `eap.append_event` after the last existing post-turn step, so a run recorded
before the change still matches its recorded prefix on recovery; as a precaution the deploy still
drains in-flight runs (the RAG plan's deploy note).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Literal

from eap_observability import log_failure
from eap_worker.errors import classify
from eap_worker.observational import is_dbos_control_flow

log = logging.getLogger("eap_worker.post_turn")

POST_TURN_DONE_EVENT = "post_turn_done"

ObserveOutcome = Literal["created", "skipped", "failed", "off"]
IndexOutcome = Literal["done", "skipped", "failed", "off"]

_WRITTEN = ("created", "existing")


def post_turn_event_key(turn: int) -> str:
    return f"memory:post_turn:{int(turn)}"


def has_post_turn_steps(spec: Any) -> bool:
    """Whether a completed conversation turn of `spec` runs post-turn memory steps."""
    memory = getattr(spec, "memory", None)
    return memory is not None and (memory.semantic_recall is not None or memory.observational is not None)


def observe_outcome(summary: Mapping[str, Any] | None, enabled: bool) -> ObserveOutcome:
    """The `observe` field from observe_after_turn's summary ({observe, reflect[, error]})."""
    if not enabled:
        return "off"
    if summary is None or summary.get("error") is not None:
        return "failed"
    stages = (summary.get("observe"), summary.get("reflect"))
    if any(s == "failed" for s in stages):
        return "failed"
    if any(s in _WRITTEN for s in stages):
        return "created"
    return "skipped"


def index_outcome(result: Mapping[str, Any] | None, enabled: bool) -> IndexOutcome:
    """The `index` field from index_after_turn's result (None: the index step itself failed)."""
    if not enabled:
        return "off"
    if result is None:
        return "failed"
    status = result.get("status")
    if status in ("indexed", "existing"):
        return "done"
    if status == "failed":
        return "failed"
    return "skipped"  # empty, skipped (forget watermark), conversation_deleted


async def finish_post_turn(
    run_id: str, tenant_id: str, agent_id: str, conversation_id: str, turn: int, spec: Any,
    index_result: Mapping[str, Any] | None, observe_summary: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Workflow code, after the post-turn steps: record `post_turn_done`. Never raises an
    `Exception` other than DBOS control flow (the turn stays completed)."""
    memory = spec.memory
    observe = observe_outcome(observe_summary, memory.observational is not None)
    index = index_outcome(index_result, memory.semantic_recall is not None)
    # Local import: eap_worker.workflow imports this module.
    from eap_worker.workflow import append_event_step

    try:
        seq = await append_event_step(
            run_id, tenant_id, post_turn_event_key(turn), POST_TURN_DONE_EVENT, {"observe": observe, "index": index},
            conversation=(agent_id, str(conversation_id)),
        )
        return {"seq": seq, "observe": observe, "index": index}
    except Exception as exc:  # noqa: BLE001 - the user's turn is completed; never fail it from here
        if is_dbos_control_flow(exc):
            raise
        log_failure(log, "post_turn_done not recorded", exc, code=classify(exc).code, run_id=run_id,
                    tenant_id=tenant_id)
        return None


__all__ = [
    "POST_TURN_DONE_EVENT",
    "finish_post_turn",
    "has_post_turn_steps",
    "index_outcome",
    "observe_outcome",
    "post_turn_event_key",
]
