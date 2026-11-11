"""Default tool registry with demo tools."""

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pydantic_ai import RunContext

from eap_runtime._registry import ToolRegistry
from eap_runtime._types import RunDeps


def _clock_now(ctx: RunContext[RunDeps]) -> str:
    """Return current UTC ISO time."""
    return datetime.now(timezone.utc).isoformat()


def _tickets_create(
    ctx: RunContext[RunDeps],
    title: str,
    body: str,
) -> dict[str, Any]:
    """Create a ticket with idempotency support."""
    if ctx.tool_call_id is None:
        raise ValueError("tool_call_id is required for tickets.create")
    
    run_id = ctx.deps.run_id
    tool_call_id = ctx.tool_call_id
    idempotency_key = f"{run_id}:{tool_call_id}"
    
    payload = {
        "ticket_id": str(uuid4()),
        "title": title,
        "body": body,
        "created": True,
    }
    
    stored_payload, created = ctx.deps.side_effects.record(idempotency_key, payload)
    return {
        "ticket_id": stored_payload["ticket_id"],
        "created": created,
    }


def default_registry() -> ToolRegistry:
    """Create a registry with default demo tools."""
    registry = ToolRegistry()
    
    registry.register(
        "clock.now",
        _clock_now,
        description="Returns current UTC ISO time",
        side_effect=False,
    )
    
    registry.register(
        "tickets.create",
        _tickets_create,
        description="Create a new ticket",
        side_effect=True,
    )
    
    return registry
