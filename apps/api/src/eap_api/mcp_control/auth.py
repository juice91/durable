"""Who calls the MCP control endpoint, per request.

`Authorization: Bearer pos_<id>_<secret>` is a tenant API key of any scope: read tools need only that,
mutation tools need the `manage` scope (checked per call in `eap_api.mcp_control.server`). The dev
bearer works in EAP_AUTH_MODE=dev only. A key keeps its `agent_ids` / `workflow_ids` allowlists and its
`allowed_origins`; every request re-reads the key, so a revoked key stops at once. Nothing here logs a key.
"""

from __future__ import annotations

from typing import Any

from eap_observability import bind_log_context
from eap_run_store import ApiKeyStore, looks_like_api_key
from starlette.concurrency import run_in_threadpool

from eap_api.auth import (
    DEV_AUTH_DISABLED_DETAIL,
    INVALID_API_KEY_DETAIL,
    AgUiPrincipal,
    current_principal,
    dev_auth_enabled,
    require_allowed_origin,
)
from fastapi import HTTPException

MANAGE_SCOPE = "manage"
PRINCIPAL_SCOPE_KEY = "eap.mcp_principal"


class McpAuthError(Exception):
    """A refused request: HTTP `status` and a fixed `message` (never the credential)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def has_manage(principal: AgUiPrincipal) -> bool:
    return principal.kind == "dev" or MANAGE_SCOPE in principal.scopes


async def authenticate(app: Any, authorization: str | None, origin: str | None) -> AgUiPrincipal:
    """The principal of one request, or McpAuthError (401, or 403 for a disallowed origin)."""
    if not authorization:
        raise McpAuthError(401, "Missing Authorization header")
    scheme, _, credential = authorization.partition(" ")
    credential = credential.strip()
    if scheme.lower() == "bearer" and looks_like_api_key(credential):
        store = getattr(app.state, "api_key_store", None)
        if store is None:
            store = app.state.api_key_store = ApiKeyStore(app.state.database_engine)
        record = await run_in_threadpool(store.authenticate, credential, touch=False)
        if record is None:
            raise McpAuthError(401, INVALID_API_KEY_DETAIL)
        try:
            require_allowed_origin(record.allowed_origins, origin)
        except HTTPException as exc:
            raise McpAuthError(403, str(exc.detail)) from None
        await run_in_threadpool(store.touch_last_used, record.id)
        bind_log_context(tenant_id=record.tenant_id)
        return AgUiPrincipal(
            tenant_id=record.tenant_id,
            kind="api_key",
            key_id=record.id,
            scopes=record.scopes,
            agent_ids=record.agent_ids,
            workflow_ids=record.workflow_ids,
            allowed_origins=record.allowed_origins,
        )
    if not dev_auth_enabled():
        raise McpAuthError(401, DEV_AUTH_DISABLED_DETAIL)
    try:
        dev = await current_principal(authorization)
    except HTTPException as exc:
        raise McpAuthError(401, str(exc.detail)) from None
    return AgUiPrincipal(tenant_id=dev.tenant_id, kind="dev", user_id=dev.user_id)
