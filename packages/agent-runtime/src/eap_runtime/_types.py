"""Core types for the agent runtime."""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

from pydantic_ai import RunContext


class RequestContext(Mapping[str, Any]):
    """A run's request context (the caller's validated `request_context`), read-only.

    Tools read values with `ctx.deps.request_context.get("<field>")`. The values are caller data and
    must never reach spans or logs, so `repr` names only the keys. Picklable (plain dict inside).
    """

    __slots__ = ("_data",)

    def __init__(self, data: Mapping[str, Any] | None = None) -> None:
        object.__setattr__(self, "_data", dict(data or {}))

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("RequestContext is read-only")

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return f"RequestContext(keys={sorted(self._data)!r})"

    def __reduce__(self) -> tuple[Any, ...]:
        return (RequestContext, (self._data,))


@dataclass(frozen=True)
class RunDeps:
    """Dependencies passed to the agent on each run.

    `request_context` is keyword-only with an empty default, so code that builds RunDeps without it
    (tool test runs, workflow children, tests) keeps working and sees an empty context.
    """

    tenant_id: str
    run_id: UUID
    side_effects: "SideEffectStore"
    ticket_service: "TicketService"
    request_context: RequestContext = field(default_factory=RequestContext, kw_only=True, repr=False)


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
