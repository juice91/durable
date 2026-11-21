"""AG-UI `resume[]` entries as approval decisions (AGUI.8, D15).

Plan: docs/plans/2026-11-13-milestone-ag-ui.md, D15, the Event mapping (D9) and "### AGUI.8".

Interrupt ids (built by `translate`):
- a workflow approval step: `approval:{run_id}:{step_id}` (`run_id` the workflow run);
- a gated tool call: `approval:{run_id}:tool:{tool_call_id}` (`run_id` the agent run: a chat turn,
  or the child run of a workflow agent step).
Run ids are UUIDs and step ids never hold a colon, so the two forms cannot be confused.

The payload rules (`resume_decisions`, all 422 before the stream):
- `status: "cancelled"`: a denial (a step is rejected) with the comment "Cancelled by user."; its
  payload is not read;
- `status: "resolved"`: the payload must be an object (Pydantic AI's `{approved, reason?}`);
  `editedArgs` is 422 `edited_args_unsupported` (Q8: no override path in v1); `approved` must
  be a JSON boolean; `reason`, when present, a string of at most 500 characters, stored as the
  decision's comment. Other keys are ignored (the schema does not forbid them).

`apply_decisions` records each decision through the shared helper `eap_api.approvals.decide`
(the decision event, then the wake-up). A decision recorded earlier (the inbox, REST, the timeout,
a re-POST) answers `exists`: the resume is still accepted and the recorded decision wins; the
stream shows it (`CUSTOM pydanticos.approval_decided`).

Which interrupts a thread has (`thread_interrupts`) comes from the pending_approvals projection:
open ones of a run that is not terminal, and decided ones (a row of a terminal run that was never
decided counts as known, so a late resume is accepted and records nothing).

Resume positions (where the resumed AG-UI run's events start, D4):
- an agent turn: the seq of the latest `tool_approval_waiting` event the entries answer;
- a workflow: where the earliest answered interrupt was raised in the commit order (root_order) of the run
  and its agent steps' child runs (`routes.ag_ui_workflows.run_start`); the answered interrupt
  ids are stored as the resume run's `input_message_ids`.

No import of the AG-UI protocol package (`ag_ui`): it uses this package's `input` (the parsed
entries) and `translate` (`OpenApproval`) only.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import status

from eap_api.ag_ui.input import AgUiInputError, ResumeEntry
from eap_api.ag_ui.translate import OpenApproval
from eap_api.approvals import ApprovalKind, DecisionResult
from eap_schemas import RunEvent

log = logging.getLogger("eap_api")

MAX_REASON_CHARS = 500
CANCELLED_COMMENT = "Cancelled by user."
TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "needs_attention", "skipped"})

EDITED_ARGS_UNSUPPORTED = "edited_args_unsupported"
RESUME_PAYLOAD_INVALID = "resume_payload_invalid"
RESUME_UNKNOWN_INTERRUPT = "resume_unknown_interrupt"
RESUME_WITH_INPUT = "resume_with_input"

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_TOOL_ID = re.compile(rf"^approval:({_UUID}):tool:(.+)$")
_STEP_ID = re.compile(rf"^approval:({_UUID}):([a-z][a-z0-9_]{{0,63}})$")


@dataclass(frozen=True)
class InterruptRef:
    """What an interrupt id names: the run, the approval kind and its key (step id or call id)."""

    run_id: str
    kind: ApprovalKind
    key: str

    @property
    def interrupt_id(self) -> str:
        return interrupt_id(self.run_id, self.kind, self.key)


def interrupt_id(run_id: str, kind: ApprovalKind, key: str) -> str:
    return f"approval:{run_id}:tool:{key}" if kind == "tool" else f"approval:{run_id}:{key}"


def parse_interrupt_id(value: str) -> InterruptRef | None:
    """The approval an interrupt id names, or None when it is not one of ours."""
    found = _TOOL_ID.match(value)
    if found is not None:
        return InterruptRef(found.group(1), "tool", found.group(2))
    found = _STEP_ID.match(value)
    if found is not None:
        return InterruptRef(found.group(1), "step", found.group(2))
    return None


@dataclass(frozen=True)
class ResumeDecision:
    """One resume entry as a decision: `approved`, and the comment to store."""

    ref: InterruptRef
    approved: bool
    comment: str | None

    def decision(self) -> Literal["approved", "rejected", "denied"]:
        if self.approved:
            return "approved"
        return "denied" if self.ref.kind == "tool" else "rejected"


def _invalid(code: str, index: int, field: str | None, msg: str) -> AgUiInputError:
    loc: list[str | int] = ["resume", index] + (["payload", field] if field else ["payload"])
    return AgUiInputError(status.HTTP_422_UNPROCESSABLE_CONTENT, code, loc, msg)


def resume_decisions(entries: Sequence[ResumeEntry]) -> list[ResumeDecision]:
    """The entries as decisions, after the payload rules of the module docstring (422s)."""
    out: list[ResumeDecision] = []
    for i, entry in enumerate(entries):
        ref = parse_interrupt_id(entry.interrupt_id)
        if ref is None:
            raise AgUiInputError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                RESUME_UNKNOWN_INTERRUPT,
                ["resume", i, "interruptId"],
                "this resume entry names no interrupt of the thread",
            )
        if entry.status == "cancelled":
            out.append(ResumeDecision(ref, False, CANCELLED_COMMENT))
            continue
        payload = entry.payload
        if not isinstance(payload, Mapping):
            raise _invalid(RESUME_PAYLOAD_INVALID, i, None, "a resolved entry needs a payload object with 'approved'")
        if "editedArgs" in payload:
            raise _invalid(EDITED_ARGS_UNSUPPORTED, i, "editedArgs", "editedArgs is not supported; approve or deny the call")
        approved = payload.get("approved")
        if not isinstance(approved, bool):
            raise _invalid(RESUME_PAYLOAD_INVALID, i, "approved", "approved must be a boolean")
        reason = payload.get("reason")
        if reason is not None and (not isinstance(reason, str) or len(reason) > MAX_REASON_CHARS):
            raise _invalid(
                RESUME_PAYLOAD_INVALID, i, "reason", f"reason must be a string of at most {MAX_REASON_CHARS} characters"
            )
        out.append(ResumeDecision(ref, approved, reason or None))
    return out


def apply_decisions(service: Any, tenant_id: str, decisions: Iterable[ResumeDecision], decided_by: str) -> list[DecisionResult]:
    """Record every decision through the service's `decide_approval` (eap_api.approvals.decide: idempotent,
    an earlier decision answers `exists`). An outcome other than `created`/`exists` (`not_waiting`: the run
    ended; `run_not_found`) records nothing; it is logged, and the stream shows the run's end."""
    results = []
    for d in decisions:
        result = service.decide_approval(
            tenant_id, run_id=d.ref.run_id, kind=d.ref.kind, key=d.ref.key,
            decision=d.decision(), decided_by=decided_by, comment=d.comment,
        )
        if result.outcome not in ("created", "exists"):
            log.info("ag-ui resume entry for run %s %s approval %s not recorded: %s",
                     d.ref.run_id, d.ref.kind, d.ref.key, result.outcome)
        results.append(result)
    return results


@dataclass(frozen=True)
class ThreadInterrupts:
    """The interrupt ids of a thread's run: `open` (answerable now) and `decided` (known)."""

    open: list[str]
    decided: list[str]
    open_rows: list[dict[str, Any]]


def thread_interrupts(rows: Iterable[Mapping[str, Any]], *, kinds: Iterable[str] = ("tool", "step")) -> ThreadInterrupts:
    """From `ApprovalStore.list_for_run` rows (the run and, for a workflow, its child runs)."""
    wanted = set(kinds)
    open_ids: list[str] = []
    decided: list[str] = []
    open_rows: list[dict[str, Any]] = []
    for row in rows:
        if row.get("kind") not in wanted:
            continue
        iid = interrupt_id(str(row["run_id"]), row["kind"], str(row["approval_key"]))
        if row.get("decided_at") is None and row.get("run_status") not in TERMINAL_RUN_STATUSES:
            open_ids.append(iid)
            open_rows.append(dict(row))
        else:
            decided.append(iid)
    return ThreadInterrupts(open_ids, decided, open_rows)


# --------------------------------------------------------------------------- positions


def waiting_seq(events: Sequence[RunEvent], kind: ApprovalKind, key: str) -> int | None:
    event_type, field = ("tool_approval_waiting", "tool_call_id") if kind == "tool" else ("step_waiting", "step_id")
    return next((e.seq for e in events if e.type == event_type and (e.data or {}).get(field) == key), None)


def step_started_seq(events: Sequence[RunEvent], step_id: str) -> int | None:
    return next((e.seq for e in events if e.type == "step_started" and (e.data or {}).get("step_id") == step_id), None)


def open_approvals(reader: Any, tenant_id: str, open_rows: Iterable[Mapping[str, Any]]) -> list[OpenApproval]:
    """The open approvals of `ThreadInterrupts.open_rows` with their waiting events' data, in the
    order they were requested (requested_at, then the waiting event's seq within a run). A row
    whose waiting event is missing (never expected) is left out."""
    found: list[tuple[Any, int, OpenApproval]] = []
    for row in open_rows:
        kind, key, run_id = row["kind"], str(row["approval_key"]), str(row["run_id"])
        event_key = f"tool:{key}:approval:waiting" if kind == "tool" else f"step:{key}:waiting"
        event = reader.get_event(tenant_id, uuid.UUID(run_id), event_key)
        if event is None:
            continue
        found.append((row.get("requested_at"), event.seq, OpenApproval(run_id, kind, key, dict(event.data or {}))))
    found.sort(key=lambda f: (f[0] is None, f[0] or 0, f[1]))
    return [a for _, _, a in found]


__all__ = [
    "CANCELLED_COMMENT",
    "EDITED_ARGS_UNSUPPORTED",
    "InterruptRef",
    "MAX_REASON_CHARS",
    "RESUME_PAYLOAD_INVALID",
    "RESUME_UNKNOWN_INTERRUPT",
    "RESUME_WITH_INPUT",
    "ResumeDecision",
    "ThreadInterrupts",
    "apply_decisions",
    "interrupt_id",
    "open_approvals",
    "parse_interrupt_id",
    "resume_decisions",
    "step_started_seq",
    "thread_interrupts",
    "waiting_seq",
]
