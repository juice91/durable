"""Recording a human decision on an approval a run waits for: one helper for both kinds.

`decide` records the decision event under the run-row lock, with its pending_approvals projection
update in the same transaction (EventLog.append_step_decision / append_tool_decision, 0014), and
only then sends the wake-up through the launcher. The decision event is the source of truth and
the message is only a wake-up: when the send fails the decision still stands (`notified` is
False) and the waiting worker picks it up at its next poll.

- kind `step` (a workflow approval step, key = step id): event `step:{step_id}:decision`, type
  step_approved | step_rejected, data {step_id, decided_by, comment}; wake-up
  `launcher.notify_approval` on topic `approval:{step_id}` of the DBOS workflow named in the
  step_waiting event (a parallel branch's child workflow, or the run).
- kind `tool` (an agent run's gated tool call, key = tool call id): event
  `tool:{tool_call_id}:approval:decision`, type tool_approved | tool_denied, data {tool_call_id,
  decided_by, comment}; wake-up `launcher.notify_tool_approval` on topic
  `tool_approval:{run_id}` of the DBOS workflow named in the tool_approval_waiting event (else the
  run's own, workflow id = run id).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from eap_observability import log_failure
from eap_run_store import EventLog, RunNotFound
from eap_run_store.approvals import DECISION_BY_EVENT_TYPE, waiting_event_key
from eap_schemas import RunEvent

from eap_api.launcher import RunLauncher

log = logging.getLogger("eap_api")

ApprovalKind = Literal["tool", "step"]
# The route's word: a workflow step is "approved" | "rejected", a tool call "approved" | "denied".
Decision = Literal["approved", "rejected", "denied"]
DecisionOutcome = Literal["created", "exists", "not_waiting", "run_not_found"]

_EVENT_TYPES: dict[tuple[str, str], str] = {
    ("step", "approved"): "step_approved",
    ("step", "rejected"): "step_rejected",
    ("tool", "approved"): "tool_approved",
    ("tool", "denied"): "tool_denied",
}


@dataclass(frozen=True)
class DecisionResult:
    """What `decide` did. `outcome`: `created` (this call recorded the decision), `exists` (a
    decision was already recorded, by a person or by the timeout), `not_waiting` (no waiting event
    for this key, or the run has ended) or `run_not_found` (unknown run or another tenant's).
    `waiting` tells, for `not_waiting`, whether the waiting event exists at all (so a route can
    answer 404 for a key that never waited and 409 for one that no longer does). `notified` is True
    only when the wake-up was sent. `event` is the decision event: the one this call wrote
    (`created`) or the earlier one (`exists`), else None."""

    outcome: DecisionOutcome
    waiting: bool = False
    notified: bool = False
    event: RunEvent | None = None

    def recorded_decision(self) -> dict[str, Any] | None:
        """Who decided and when, from `event`: {decision: approved | denied | timed_out,
        decided_by (None for a timeout, which no person decided), decided_at (ISO 8601)}; None
        without an event. A rejected workflow step reads `denied`, as in the inbox."""
        if self.event is None:
            return None
        data = self.event.data if isinstance(self.event.data, dict) else {}
        return {
            "decision": DECISION_BY_EVENT_TYPE.get(self.event.type),
            "decided_by": data.get("decided_by"),
            "decided_at": self.event.ts.isoformat() if self.event.ts is not None else None,
        }


def decide(
    event_log: EventLog,
    launcher: RunLauncher,
    *,
    tenant_id: str,
    run_id: UUID | str,
    kind: ApprovalKind,
    key: str,
    decision: Decision,
    decided_by: str,
    comment: str | None,
) -> DecisionResult:
    """Record `decision` on the approval (`kind`, `key`) of the tenant's run, then wake the waiting
    workflow. Never raises for an ordinary outcome; see DecisionResult. A failed wake-up is logged
    (code `approval_notify_failed`) and leaves `notified` False."""
    try:
        event_type = _EVENT_TYPES[(kind, decision)]
    except KeyError:
        raise ValueError(f"{decision!r} is not a decision for a {kind} approval") from None
    id_field = "tool_call_id" if kind == "tool" else "step_id"
    data = {id_field: key, "decided_by": decided_by, "comment": comment}
    append = event_log.append_tool_decision if kind == "tool" else event_log.append_step_decision
    try:
        outcome, event = append(tenant_id, run_id, key, event_type, data)
    except RunNotFound:
        return DecisionResult("run_not_found")
    waiting = event_log.get_event(tenant_id, run_id, waiting_event_key(kind, key))
    if outcome != "created":
        return DecisionResult(outcome, waiting=waiting is not None, event=event)
    waiting_data = waiting.data if waiting is not None and isinstance(waiting.data, dict) else {}
    target = str(waiting_data.get("workflow_id") or run_id)
    notified = True
    try:
        if kind == "tool":
            launcher.notify_tool_approval(
                run_id=str(run_id), tool_call_id=key, decision=decision, workflow_id=target
            )
        else:
            launcher.notify_approval(workflow_id=target, step_id=key, decision=decision)
    except Exception as exc:  # noqa: BLE001 - the decision is durable; the worker polls for it
        notified = False
        log_failure(
            log, "approval wake-up failed", exc, code="approval_notify_failed", level=logging.ERROR,
            run_id=str(run_id), tenant_id=tenant_id,
        )
    log.info("run %s %s approval %s %s by %s", run_id, kind, key, decision, decided_by)
    return DecisionResult("created", waiting=True, notified=notified, event=event)


__all__ = ["ApprovalKind", "Decision", "DecisionResult", "decide"]
