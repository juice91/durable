"""Authentication dependency for the API."""

import os
import re
from typing import Annotated

from eap_observability import bind_log_context
from fastapi import Depends, Header, HTTPException, status
from pydantic import BaseModel


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
