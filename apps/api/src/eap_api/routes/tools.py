"""Authenticated, tenant-agnostic platform tool catalog."""

from __future__ import annotations

import os
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from eap_api.auth import Principal, current_principal
from eap_schemas.tools import TOOL_DECLARATIONS, ToolDeclaration

router = APIRouter(prefix="/tools", tags=["tools"])


def _allowed_tool_ids() -> frozenset[str] | None:
    """Parse the same optional platform allowlist used by the worker."""
    configured = os.environ.get("EAP_ALLOWED_TOOLS")
    if configured is None:
        return None
    return frozenset(tool_id.strip() for tool_id in configured.split(",") if tool_id.strip())


def _tool_response(declaration: ToolDeclaration, allowed_tools: frozenset[str] | None) -> dict:
    """Render a declaration with the current platform policy view."""
    return {
        "id": declaration.id,
        "title": declaration.title,
        "description": declaration.description,
        "side_effect": declaration.side_effect,
        "external_idempotent": declaration.external_idempotent,
        "input_schema": declaration.input_json_schema(),
        "allowed": allowed_tools is None or declaration.id in allowed_tools,
    }


@router.get("")
def list_tools(
    principal: Annotated[Principal, Depends(current_principal)],
) -> list[dict]:
    """List platform tool declarations and their current allowlist state."""
    del principal
    allowed_tools = _allowed_tool_ids()
    return [_tool_response(declaration, allowed_tools) for declaration in TOOL_DECLARATIONS.values()]


@router.get("/{tool_id}")
def get_tool(
    tool_id: str,
    principal: Annotated[Principal, Depends(current_principal)],
) -> dict:
    """Get one platform tool declaration."""
    del principal
    declaration = TOOL_DECLARATIONS.get(tool_id)
    if declaration is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tool not found")
    return _tool_response(declaration, _allowed_tool_ids())
