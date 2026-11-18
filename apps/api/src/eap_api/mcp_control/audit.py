"""One audit record per MCP call: tool, tenant, who (key id or dev user) and outcome. Never arguments or
results: they can carry anything an agent or workflow handles."""

from __future__ import annotations

import logging

log = logging.getLogger("eap_api.mcp_control")


def audit_call(*, tool: str, tenant_id: str, principal_id: str, outcome: str, code: str | None = None) -> None:
    """`outcome` is ok | error | denied | rate_limited; `code` the stable error code of a non-ok one."""
    log.info(
        "mcp_call tool=%s outcome=%s tenant=%s principal=%s%s",
        tool, outcome, tenant_id, principal_id, f" code={code}" if code else "",
        extra={
            "eap.audit": "mcp_call",
            "eap.tool": tool,
            "eap.tenant_id": tenant_id,
            "eap.principal": principal_id,
            "eap.outcome": outcome,
            **({"eap.code": code} if code else {}),
        },
    )
