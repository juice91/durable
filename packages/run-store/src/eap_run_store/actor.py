"""The actor of the current request (design 4.1, 4.7), carried in a context variable so a run recorded deep inside a
service names who started it without every call site growing a parameter.

The API's identity dependency calls `bind_actor` once per request, after the identity chain answered; `RunStore`
reads `current_actor()` when a run is created and the caller passed no explicit `RunActor`. The worker (scheduled
and child runs) passes explicit actors. A context with nothing bound records a run with no actor (legacy)."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass

_ACTOR: ContextVar[RunActor | None] = ContextVar("eap_run_actor", default=None)


@dataclass(frozen=True)
class RunActor:
    """Who a run acts as: `principal` is `<kind>:<subject>`; `on_behalf_of` the human or service it acts for."""

    kind: str
    subject: str
    on_behalf_of: str | None = None

    @property
    def principal(self) -> str:
        return f"{self.kind}:{self.subject}"


def bind_actor(actor: RunActor | None) -> None:
    _ACTOR.set(actor)


def current_actor() -> RunActor | None:
    return _ACTOR.get()
