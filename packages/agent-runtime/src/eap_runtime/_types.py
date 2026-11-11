"""Core types for the agent runtime."""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from pydantic_ai import RunContext


@dataclass(frozen=True)
class RunDeps:
    """Dependencies passed to the agent on each run."""

    tenant_id: str
    run_id: UUID
    side_effects: "SideEffectStore"


class SideEffectStore(Protocol):
    """Protocol for side-effect storage."""

    def record(
        self, key: str, payload: dict
    ) -> tuple[dict, bool]:
        """Record a side effect. Returns (stored_payload, created)."""
        ...
