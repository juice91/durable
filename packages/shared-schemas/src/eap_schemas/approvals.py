"""Per-tool approval policy (agent capabilities milestone, AC.1).

`AgentSpec.approvals: ApprovalPolicy | None`. Each entry of `tools` names a tool of the same spec:
a built-in tool id from `spec.tools` (`tickets.create`) or `"<server>/<tool>"` for an entry of
`spec.mcp_tools`. AgentSpec checks that every entry names one of its tools; platform tools
(`retrieve`, `update_working_memory`) and the output tool `final_result` can never require approval.

When the model calls a gated tool, the run waits durably for a decision (the worker, AC.6). A
timeout counts as a denial. `timeout_hours` defaults to 24; an hour is EAP_WORKFLOW_HOUR_SECONDS.
No I/O.
"""

from __future__ import annotations

from typing import Annotated, Tuple

from pydantic import Field, field_validator

from eap_schemas._omit import OmitNoneModel

DEFAULT_APPROVAL_TIMEOUT_HOURS = 24
MAX_APPROVAL_TIMEOUT_HOURS = 168
MAX_APPROVAL_TOOLS = 64
# "<server>/<tool>": McpToolRef caps a server name at 100 and a tool name at 128 characters.
MAX_APPROVAL_REF_CHARS = 100 + 1 + 128
MAX_APPROVAL_COMMENT_CHARS = 500

DENIED_MESSAGE = "The user denied this tool call."
TIMED_OUT_MESSAGE = "No one approved this tool call in time; it was not run."
EVAL_DENIED_MESSAGE = "Tool approval is not available during evaluation."


class ApprovalPolicy(OmitNoneModel):
    """Tools of the spec that need a person's approval before each call."""

    tools: Tuple[Annotated[str, Field(min_length=1, max_length=MAX_APPROVAL_REF_CHARS)], ...] = Field(
        min_length=1,
        max_length=MAX_APPROVAL_TOOLS,
        description='Built-in tool ids from `tools`, or "<server>/<tool>" from `mcp_tools`',
    )
    timeout_hours: int | None = Field(default=None, ge=1, le=MAX_APPROVAL_TIMEOUT_HOURS)

    @field_validator("tools")
    @classmethod
    def _unique(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        seen: set[str] = set()
        for ref in v:
            if ref in seen:
                raise ValueError(f"duplicate approval tool: {ref}")
            seen.add(ref)
        return v

    def effective_timeout_hours(self) -> int:
        return self.timeout_hours if self.timeout_hours is not None else DEFAULT_APPROVAL_TIMEOUT_HOURS


def denial_message(comment: str | None = None) -> str:
    """The model-visible message for a denied call, with the approver's comment when given."""
    if comment:
        return f"{DENIED_MESSAGE} Comment: {comment}"
    return DENIED_MESSAGE


__all__ = [
    "ApprovalPolicy",
    "DEFAULT_APPROVAL_TIMEOUT_HOURS",
    "DENIED_MESSAGE",
    "EVAL_DENIED_MESSAGE",
    "MAX_APPROVAL_COMMENT_CHARS",
    "MAX_APPROVAL_REF_CHARS",
    "MAX_APPROVAL_TIMEOUT_HOURS",
    "MAX_APPROVAL_TOOLS",
    "TIMED_OUT_MESSAGE",
    "denial_message",
]
