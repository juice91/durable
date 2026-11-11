"""Side effect store implementations."""

from typing import Optional
from uuid import uuid4

from eap_runtime._types import SideEffectStore


class InMemorySideEffectStore(SideEffectStore):
    """In-memory implementation of SideEffectStore."""

    def __init__(self) -> None:
        self._store: dict[str, dict] = {}
        self._order: list[str] = []

    def record(
        self, key: str, payload: dict
    ) -> tuple[dict, bool]:
        """Record a side effect. Returns (stored_payload, created)."""
        if key in self._store:
            return self._store[key], False
        self._store[key] = payload
        self._order.append(key)
        return payload, True
