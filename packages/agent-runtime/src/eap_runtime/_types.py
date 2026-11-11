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
    ticket_service: "TicketService"


class SideEffectStore(Protocol):
    """Protocol for side-effect storage.

    State transitions: new -> reserved -> completed
    - 'new': no entry exists
    - 'reserved': a call is in progress, result unknown
    - 'completed': result is stored
    """

    def reserve(self, key: str) -> tuple[str, dict | None]:
        """Reserve a key for a side effect.

        Returns:
            ('new', None): key not yet used
            ('reserved', None): previous attempt crashed between call and complete
            ('completed', result): side effect already completed
        """
        ...

    def complete(self, key: str, result: dict) -> None:
        """Mark a side effect as completed with the given result."""
        ...


class TicketService(Protocol):
    """Protocol for ticket creation with idempotency support."""

    def create(
        self, title: str, body: str, *, idempotency_key: str
    ) -> dict:
        """Create a ticket.

        Returns:
            dict with 'ticket_id' key.
        """
        ...
