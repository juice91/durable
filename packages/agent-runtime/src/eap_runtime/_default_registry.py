"""Default tool registry with demo tools."""

from datetime import datetime, timezone
from typing import Any

from pydantic_ai import RunContext

from eap_schemas.tools import TOOL_DECLARATIONS

from eap_runtime._registry import ToolRegistry
from eap_runtime._side_effects import run_side_effect
from eap_runtime._types import RunDeps


def _clock_now(ctx: RunContext[RunDeps]) -> str:
    """Return current UTC ISO time."""
    return datetime.now(timezone.utc).isoformat()


def _workflow_return(ctx: RunContext[RunDeps], value: Any) -> Any:
    """Return the value unchanged."""
    return value


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

    def call():
        return ctx.deps.ticket_service.create(
            title=title,
            body=body,
            idempotency_key=idempotency_key,
        )

    result, performed = run_side_effect(
        ctx.deps.side_effects,
        idempotency_key,
        call,
        external_idempotent=True,
    )

    return {
        "ticket_id": result["ticket_id"],
        "created": performed,
    }


def default_registry() -> ToolRegistry:
    """Create a registry with default demo tools."""
    registry = ToolRegistry()

    registry.register(
        TOOL_DECLARATIONS["clock.now"],
        _clock_now,
    )

    registry.register(
        TOOL_DECLARATIONS["workflow.return"],
        _workflow_return,
    )

    registry.register(
        TOOL_DECLARATIONS["tickets.create"],
        _tickets_create,
    )

    return registry
