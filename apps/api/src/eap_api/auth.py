"""Authentication dependency for the API."""

import os
import re
from typing import Annotated, Literal

from eap_observability import bind_log_context
from eap_run_store import ApiKeyStore, looks_like_api_key
from fastapi import Depends, Header, HTTPException, Request, status
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool


class Principal(BaseModel):
    """Represents an authenticated principal (tenant + user)."""

    tenant_id: str
    user_id: str


# Regex for tenant/user: non-empty, [A-Za-z0-9_-]+
TENANT_USER_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
AUTH_CONFIGURATION_ERROR = (
    "no production authentication is configured; set EAP_AUTH_MODE=dev for local development only"
)


def require_dev_auth_mode() -> None:
    """Refuse to expose the forgeable local auth scheme unless explicitly enabled."""
    if os.environ.get("EAP_AUTH_MODE") != "dev":
        raise RuntimeError(AUTH_CONFIGURATION_ERROR)


async def current_principal(
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    """
    Parse Authorization header: Bearer dev:<tenant>:<user>.
    
    Returns Principal with tenant_id and user_id.
    Raises 401 if missing or malformed.
    """
    require_dev_auth_mode()

    if authorization is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
        )
    
    # Parse "Bearer dev:<tenant>:<user>"
    parts = authorization.split(":")
    if len(parts) != 3 or parts[0] != "Bearer dev":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Authorization header format",
        )
    
    tenant_id, user_id = parts[1], parts[2]
    
    if not tenant_id or not tenant_id.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Tenant ID cannot be empty",
        )
    
    if not user_id or not user_id.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User ID cannot be empty",
        )
    
    # Validate tenant_id and user_id match pattern
    if not TENANT_USER_PATTERN.match(tenant_id):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid tenant ID format",
        )
    
    if not TENANT_USER_PATTERN.match(user_id):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid user ID format",
        )
    
    principal = Principal(tenant_id=tenant_id.strip(), user_id=user_id.strip())
    # Log records for the rest of this request carry eap.tenant_id (the logs API filters on it). An
    # async dependency runs in the request's own task, so the binding ends with the request.
    bind_log_context(tenant_id=principal.tenant_id)
    return principal


# --- AG-UI principals: a tenant API key or the dev bearer (AGUI.3; D16 of the AG-UI plan) -------

# One answer for a malformed, unknown, wrong-secret, revoked or expired key.
INVALID_API_KEY_DETAIL = "Invalid API key"
DEV_AUTH_DISABLED_DETAIL = "dev bearer authentication is not enabled"
AGUI_RUN_SCOPE = "agui:run"
MISSING_SCOPE_DETAIL = f"API key lacks the {AGUI_RUN_SCOPE} scope"
AGENT_NOT_FOUND_DETAIL = "Agent not found"
WORKFLOW_NOT_FOUND_DETAIL = "Workflow not found"


class AgUiPrincipal(BaseModel):
    """Who calls an AG-UI route: a tenant API key (`kind="api_key"`) or the dev bearer (`"dev"`).

    A key carries its scopes and optional allowlists. Both `agent_ids` and `workflow_ids` None is
    a tenant-wide key; once either is set, a None list on the other dimension allows nothing
    (fail closed). `allowed_origins` None means any origin. The dev bearer has no limits. Keys may not run drafts or override the model (D16)."""

    tenant_id: str
    kind: Literal["dev", "api_key"]
    user_id: str | None = None
    key_id: str | None = None
    scopes: tuple[str, ...] = ()
    agent_ids: tuple[str, ...] | None = None
    workflow_ids: tuple[str, ...] | None = None
    allowed_origins: tuple[str, ...] | None = None

    @property
    def is_api_key(self) -> bool:
        return self.kind == "api_key"

    @property
    def may_run_drafts(self) -> bool:
        return self.kind == "dev"

    @property
    def may_override_model(self) -> bool:
        return self.kind == "dev"

    @property
    def default_resource_id(self) -> str:
        """The resource of a conversation derived from a thread when the client names none (D3):
        the dev bearer's user id, else `apikey:<key_id>`."""
        return self.user_id if self.kind == "dev" and self.user_id else f"apikey:{self.key_id}"

    @property
    def is_unrestricted(self) -> bool:
        """Neither allowlist is set: every agent and workflow of the tenant (the dev bearer, or a
        key created without allowlists)."""
        return self.agent_ids is None and self.workflow_ids is None

    def allows_agent(self, agent_id: str) -> bool:
        """Fail closed: a NULL agent list allows everything only when the workflow list is NULL too."""
        if self.is_unrestricted:
            return True
        return self.agent_ids is not None and str(agent_id) in self.agent_ids

    def allows_workflow(self, workflow_id: str) -> bool:
        """Fail closed: a NULL workflow list allows everything only when the agent list is NULL too."""
        if self.is_unrestricted:
            return True
        return self.workflow_ids is not None and str(workflow_id) in self.workflow_ids


def get_api_key_store(request: Request) -> ApiKeyStore:
    """The ApiKeyStore on the app's database engine, made once per app."""
    store = getattr(request.app.state, "api_key_store", None)
    if store is None:
        store = request.app.state.api_key_store = ApiKeyStore(request.app.state.database_engine)
    return store


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail=detail, headers={"WWW-Authenticate": "Bearer"}
    )


async def agui_principal(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> AgUiPrincipal:
    """The principal of an AG-UI route (`/api/v1/ag-ui/*` only; every other route keeps
    `current_principal`, so a key is refused there).

    `Authorization: Bearer pos_<id>_<secret>` is an API key: it works whatever EAP_AUTH_MODE is.
    A malformed, unknown, wrong-secret, revoked or expired key is 401 `Invalid API key`; a key
    without the `agui:run` scope is 403. Anything else is parsed as the dev bearer, which needs
    EAP_AUTH_MODE=dev (else 401 `dev bearer authentication is not enabled`). The key itself is
    never logged."""
    if authorization is None:
        raise _unauthorized("Missing Authorization header")

    scheme, _, credential = authorization.partition(" ")
    credential = credential.strip()
    if scheme.lower() == "bearer" and looks_like_api_key(credential):
        record = await run_in_threadpool(get_api_key_store(request).authenticate, credential)
        if record is None:
            raise _unauthorized(INVALID_API_KEY_DETAIL)
        if AGUI_RUN_SCOPE not in record.scopes:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=MISSING_SCOPE_DETAIL)
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
    if os.environ.get("EAP_AUTH_MODE") != "dev":
        raise _unauthorized(DEV_AUTH_DISABLED_DETAIL)
    dev = await current_principal(authorization)
    return AgUiPrincipal(tenant_id=dev.tenant_id, kind="dev", user_id=dev.user_id, scopes=(AGUI_RUN_SCOPE,))


def require_agui_agent(principal: AgUiPrincipal, agent_id: str) -> None:
    """404 `Agent not found` for an agent outside the key's allowlist (the same answer as an
    agent that does not exist, so a key cannot probe for ids). The caller still loads the agent
    tenant-scoped, which gives the same 404 for another tenant's agent."""
    if not principal.allows_agent(agent_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=AGENT_NOT_FOUND_DETAIL)


def require_agui_workflow(principal: AgUiPrincipal, workflow_id: str) -> None:
    """404 `Workflow not found` for a workflow outside the key's allowlist (see require_agui_agent)."""
    if not principal.allows_workflow(workflow_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=WORKFLOW_NOT_FOUND_DETAIL)


AgUiPrincipalDep = Annotated[AgUiPrincipal, Depends(agui_principal)]
