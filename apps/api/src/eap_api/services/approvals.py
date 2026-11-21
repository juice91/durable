"""The approvals inbox and the tool-approval decisions (agent capabilities milestone, AC.8).

The business logic behind routes/approvals.py: reading the pending_approvals projection with the events
that add to each item, and recording a human decision on a gated tool call through eap_api.approvals.decide.
Refusals are `DefinitionError`s (eap_run_store), whose bodies are what the API has always answered.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from eap_api.approvals import decide
from eap_api.ports import RunLauncher
from eap_run_store import ApprovalStore, EventLog, InvalidRunCursor
from eap_run_store.cursor import MAX_CURSOR_LENGTH
from eap_run_store.definition_errors import refuse

# An inbox cursor carries a provider-chosen tool call id, so it may run longer than a run cursor
# (eap_run_store.approvals.ApprovalCursor.decode accepts up to 4x).
MAX_APPROVAL_CURSOR_CHARS = 4 * MAX_CURSOR_LENGTH
MAX_APPROVAL_LIMIT = ApprovalStore.MAX_LIMIT

RUN_NOT_FOUND_DETAIL = "Run not found"
TOOL_CALL_NOT_FOUND_DETAIL = "Tool call not found"
ALREADY_DECIDED_DETAIL = "tool call is already decided"
NOT_WAITING_DETAIL = "tool call is not waiting for approval"
APPROVAL_NOT_FOUND_DETAIL = "Approval not found"
INVALID_CURSOR_DETAIL = "cursor is not valid; pass the previous page's next_cursor verbatim"


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def approval_json(row: dict[str, Any]) -> dict[str, Any]:
    """One inbox item. `approval_key` is the tool call id (kind tool) or the step id (kind step),
    also given as `tool_call_id` / `step_id`. `root_run_id` is the workflow run for a workflow
    agent step's child run, else the run itself."""
    tool = row["kind"] == "tool"
    return {
        "run_id": str(row["run_id"]),
        "kind": row["kind"],
        "approval_key": row["approval_key"],
        "tool_call_id": row["approval_key"] if tool else None,
        "step_id": None if tool else row["approval_key"],
        "root_run_id": str(row["root_run_id"]),
        "agent_id": row["agent_id"],
        "workflow_id": row["workflow_id"],
        "tool_name": row["tool_name"],
        "args": row["args"],
        "prompt": row["prompt"],
        "status": row["status"],
        "requested_at": _iso(row["requested_at"]),
        "deadline": _iso(row["deadline"]),
        "decided_at": _iso(row["decided_at"]),
        "decision": row["decision"],
        "decided_by": row["decided_by"],
        "run_status": row["run_status"],
    }


class ApprovalService:
    def __init__(self, store: ApprovalStore, event_log: EventLog, launcher: RunLauncher) -> None:
        self._store = store
        self._events = event_log
        self._launcher = launcher

    def _extras(self, tenant_id: str, row: dict[str, Any], *, detail: bool) -> dict[str, Any]:
        if row["kind"] == "tool":
            # `summary`: what the call would do, in words, as the waiting event carries it (null when the
            # tool has no summariser).
            waiting = self._events.get_event(tenant_id, row["run_id"], f"tool:{row['approval_key']}:approval:waiting")
            text = waiting.data.get("summary") if waiting is not None and isinstance(waiting.data, dict) else None
            return {"summary": text if isinstance(text, str) and text else None}
        # What a workflow step's item adds from its events: `resume_schema` (an input step: the
        # schema the person fills in; every item), and for a decided one (`detail`, the single read) the
        # audit of the decision: `comment` and `resume_data` (what was submitted) beside who and when.
        waiting = self._events.get_event(tenant_id, row["run_id"], f"step:{row['approval_key']}:waiting")
        wdata = waiting.data if waiting is not None and isinstance(waiting.data, dict) else {}
        schema = wdata.get("resume_schema")
        # `pause`: a step-by-step run's pause ("Paused after {step}", answered with Continue or Stop).
        extras: dict[str, Any] = {
            "summary": None,
            "resume_schema": schema if isinstance(schema, dict) else None,
            "pause": wdata.get("pause") is True,
        }
        if detail:
            decision = self._events.get_event(tenant_id, row["run_id"], f"step:{row['approval_key']}:decision")
            data = decision.data if decision is not None and isinstance(decision.data, dict) else {}
            extras["comment"] = data.get("comment")
            extras["resume_data"] = data.get("resume_data")
        return extras

    def list(
        self,
        tenant_id: str,
        *,
        status: Literal["pending", "decided"] = "pending",
        kind: Literal["tool", "step"] | None = None,
        agent_id: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """The tenant's approvals: {items, next_cursor}; a cursor this API did not issue is refused (422)."""
        try:
            rows, next_cursor = self._store.list(
                tenant_id, status=status, kind=kind, agent_id=agent_id, cursor=cursor, limit=limit
            )
        except InvalidRunCursor:
            raise refuse(422, INVALID_CURSOR_DETAIL) from None
        items = [{**approval_json(r), **self._extras(tenant_id, r, detail=True)} for r in rows]
        return {"items": items, "next_cursor": next_cursor}

    def get(self, tenant_id: str, run_id: uuid.UUID, kind: str, key: str) -> dict[str, Any]:
        """One approval, pending or decided, in the inbox item's shape; 404 unknown or another tenant's."""
        row = self._store.get(tenant_id, str(run_id), kind, key)
        if row is None:
            raise refuse(404, APPROVAL_NOT_FOUND_DETAIL)
        return {**approval_json(row), **self._extras(tenant_id, row, detail=True)}

    def decide_tool(
        self,
        tenant_id: str,
        user_id: str,
        run_id: uuid.UUID,
        tool_call_id: str,
        decision: Literal["approved", "denied"],
        comment: str | None,
    ) -> dict[str, Any]:
        """Record the decision of a gated tool call and wake the run (`notified` False when the wake-up failed)."""
        result = decide(
            self._events, self._launcher, tenant_id=tenant_id, run_id=run_id, kind="tool", key=tool_call_id,
            decision=decision, decided_by=user_id, comment=comment,
        )
        if result.outcome == "run_not_found":
            raise refuse(404, RUN_NOT_FOUND_DETAIL)
        if result.outcome == "exists":
            # Who got there first: a person ({decision, decided_by}) or the timeout (decision
            # "timed_out", decided_by null), and when.
            raise refuse(409, ALREADY_DECIDED_DETAIL, **(result.recorded_decision() or {}))
        if result.outcome == "not_waiting":
            if not result.waiting:
                raise refuse(404, TOOL_CALL_NOT_FOUND_DETAIL)
            raise refuse(409, NOT_WAITING_DETAIL)
        return {
            "run_id": str(run_id),
            "tool_call_id": tool_call_id,
            "decision": decision,
            "decided_by": user_id,
            "comment": comment,
            "notified": result.notified,
        }
