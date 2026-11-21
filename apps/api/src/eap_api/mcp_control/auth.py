"""Who calls the MCP control endpoint, per request.

`Authorization: Bearer pos_<id>_<secret>` is a tenant API key with the `mcp` scope (read tools) or `manage`
(which implies it; mutation tools). A key with neither, such as an AG-UI frontend key, is refused with 403 (checked per call in `eap_api.mcp_control.server`). The dev
bearer works in EAP_AUTH_MODE=dev only. A key keeps its `agent_ids` / `workflow_ids` allowlists and its
`allowed_origins`; every request re-reads the key, so a revoked key stops at once. Nothing here logs a key.
"""

from __future__ import annotations

from typing import Any

from eap_observability import bind_log_context
from starlette.concurrency import run_in_threadpool

from eap_api.service_deps.auth_lookup import auth_lookup_service_of
from eap_api.services.auth_lookup import AuthLookupService
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


async def _authenticate_run_token(lookup: AuthLookupService, credential: str) -> AgUiPrincipal:
    """A live run of the built-in Architect: a signed, unexpired token AND a run row that still exists in
    the tenant, belongs to the built-in `architect` agent and has not ended. Anything else is a plain 401."""
    run = await run_in_threadpool(lookup.architect_run, credential)
    if run is None:
        raise McpAuthError(401, INVALID_API_KEY_DETAIL)
    bind_log_context(tenant_id=run.tenant_id)
    return AgUiPrincipal(
        tenant_id=run.tenant_id,
        kind="architect_run",
        user_id=f"{ARCHITECT_RUN_PREFIX}{run.run_id}",
        scopes=(MCP_SCOPE, MANAGE_SCOPE),
    )


async def authenticate(app: Any, authorization: str | None, origin: str | None) -> AgUiPrincipal:
    """The principal of one request, or McpAuthError (401, or 403 for a disallowed origin)."""
    if not authorization:
        raise McpAuthError(401, "Missing Authorization header")
    scheme, _, credential = authorization.partition(" ")
    credential = credential.strip()
    lookup = auth_lookup_service_of(app)
    if scheme.lower() == "bearer" and lookup.is_run_token(credential):
        return await _authenticate_run_token(lookup, credential)
    if scheme.lower() == "bearer" and lookup.is_api_key(credential):
        record = await run_in_threadpool(lookup.api_key, credential)
        if record is None:
            raise McpAuthError(401, INVALID_API_KEY_DETAIL)
        if MCP_SCOPE not in record.scopes and MANAGE_SCOPE not in record.scopes:
            raise McpAuthError(403, MISSING_MCP_SCOPE)
        try:
            require_allowed_origin(record.allowed_origins, origin)
        except HTTPException as exc:
            raise McpAuthError(403, str(exc.detail)) from None
        await run_in_threadpool(lookup.touch_api_key, record.id)
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
