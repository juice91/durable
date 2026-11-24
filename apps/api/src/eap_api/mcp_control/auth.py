"""Who calls the MCP control endpoint, per request.

`Authorization: Bearer pos_<id>_<secret>` is a tenant API key with the `mcp` scope (read tools) or `manage`
(which implies it; mutation tools). A key with neither, such as an AG-UI frontend key, is refused with 403 (checked per call in `eap_api.mcp_control.server`). The dev
bearer works in EAP_AUTH_MODE=dev only, from a loopback peer. A key keeps its `agent_ids` / `workflow_ids` allowlists and its
`allowed_origins`; every request re-reads the key, so a revoked key stops at once. Nothing here logs a key.
"""

from __future__ import annotations

from typing import Any

from eap_iam import ACTION_MCP_READ, AuthError, RequestView

from eap_api.auth import KEY_ROUTE_KINDS, MCP_ROUTE_KINDS, AgUiPrincipal, to_agui_principal  # noqa: F401
from eap_api.deps import iam_of
from eap_observability import bind_log_context

MANAGE_SCOPE = "manage"
MCP_SCOPE = "mcp"
MISSING_MCP_SCOPE = "API key lacks the mcp scope"
PRINCIPAL_SCOPE_KEY = "eap.mcp_principal"
ARCHITECT_RUN_PREFIX = "architect-run:"


class McpAuthError(Exception):
    """A refused request: HTTP `status` and a fixed `message` (never the credential)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def has_manage(principal: AgUiPrincipal) -> bool:
    return principal.kind == "dev" or MANAGE_SCOPE in principal.scopes


def actor_id(principal: AgUiPrincipal) -> str:
    """Who the audit record names: the key id, `architect-run:<run id>` or `dev:<user>`."""
    if principal.kind == "architect_run":
        return str(principal.user_id)
    return principal.key_id or f"dev:{principal.user_id}"


async def authenticate(
    app: Any,
    authorization: str | None,
    origin: str | None,
    *,
    headers: dict[str, str] | None = None,
    peer_host: str | None = None,
) -> AgUiPrincipal:
    """The principal of one request, or McpAuthError (401, or 403 for a disallowed origin), through the identity chain.

    `headers` (lower-cased) and `peer_host` (the TCP peer) come from the ASGI scope; the dev bearer needs the peer."""
    merged = {k.lower(): v for k, v in (headers or {}).items()}
    if authorization is not None:
        merged["authorization"] = authorization
    if origin is not None:
        merged["origin"] = origin
    view = RequestView(headers=merged, peer_host=peer_host, accepts=MCP_ROUTE_KINDS, action=ACTION_MCP_READ)
    try:
        principal = await iam_of(app).chain.authenticate(view)
    except AuthError as exc:
        raise McpAuthError(exc.status, exc.detail) from None
    bind_log_context(tenant_id=principal.tenant_id)
    return to_agui_principal(principal)
