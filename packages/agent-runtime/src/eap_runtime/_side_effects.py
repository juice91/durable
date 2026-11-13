"""Side effect store implementations."""

import threading
from typing import Callable

from eap_runtime._types import SideEffectStore, TicketService


class SideEffectOutcomeUnknown(Exception):
    """Raised when a side effect's outcome is unknown (reserved state)."""

    def __init__(self, key: str):
        self.key = key
        # args are the constructor arguments, so pickle (DBOS step errors) rebuilds it exactly.
        super().__init__(key)

    def __str__(self) -> str:
        return f"Side effect outcome unknown for key: {self.key}"


def run_side_effect(
    store: SideEffectStore,
    key: str,
    call: Callable[[], dict],
    *,
    external_idempotent: bool = False,
) -> tuple[dict, bool]:
    """Run a side effect with durability guarantees.

    Args:
        store: The side effect store to use.
        key: The unique key for this side effect.
        call: The function to call for the side effect.
        external_idempotent: If True, the external system honors idempotency keys.

    Returns:
        Tuple of (result, performed_now).
        - result: The side effect result.
        - performed_now: True if call() was executed, False if result was from store.

    Raises:
        SideEffectOutcomeUnknown: If state is 'reserved' and external_idempotent is False.
    """
    status, stored = store.reserve(key)

    if status == "completed":
        # Already completed, return stored result
        return stored, False

    if status == "new":
        # New key, execute and complete
        result = call()
        store.complete(key, result)
        return result, True

    # status == 'reserved' - previous attempt crashed between call and complete
    if external_idempotent:
        # Safe to call again - external system will dedupe
        result = call()
        store.complete(key, result)
        return result, True
    else:
        # Not safe to call again - outcome is unknown
        raise SideEffectOutcomeUnknown(key)


class InMemorySideEffectStore(SideEffectStore):
    """In-memory implementation of SideEffectStore (thread-safe with lock)."""

    def __init__(self) -> None:
        self._store: dict[str, dict] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()

    def reserve(self, key: str) -> tuple[str, dict | None]:
        """Reserve a key for a side effect."""
        with self._lock:
            if key in self._store:
                result = self._store[key]
                if "_state" in result and result["_state"] == "reserved":
                    return "reserved", None
                return "completed", result
            # Mark as reserved
            self._store[key] = {"_state": "reserved"}
            self._order.append(key)
            return "new", None

    def complete(self, key: str, result: dict) -> None:
        """Mark a side effect as completed."""
        with self._lock:
            # Create entry if it doesn't exist (allows calling complete directly)
            if key not in self._store:
                self._store[key] = {}
            # Update with actual result, removing reserved state marker
            entry = self._store[key].copy()
            entry.pop("_state", None)
            entry.update(result)
            self._store[key] = entry


class InMemoryTicketService(TicketService):
    """In-memory implementation of TicketService (thread-safe)."""

    def __init__(self) -> None:
        self._tickets: dict[str, dict] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self.calls: int = 0

    @property
    def tickets(self) -> list[dict]:
        """Return distinct tickets (values in order of creation)."""
        with self._lock:
            return [self._tickets[k] for k in self._order]

    def create(
        self, title: str, body: str, *, idempotency_key: str
    ) -> dict:
        """Create a ticket with idempotency support."""
        with self._lock:
            self.calls += 1
            if idempotency_key in self._tickets:
                return self._tickets[idempotency_key]

            ticket_id = f"ticket-{idempotency_key.split(':')[-1]}" if ":" in idempotency_key else f"ticket-{len(self._order)}"
            ticket = {
                "ticket_id": ticket_id,
                "title": title,
                "body": body,
                "idempotency_key": idempotency_key,
            }
            self._tickets[idempotency_key] = ticket
            self._order.append(idempotency_key)
            return ticket
