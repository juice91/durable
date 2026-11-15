"""Standalone AG-UI frames the agent routes send outside the formatter (AGUI.6).

The formatter (`translate.AgUiFormatter`) owns every frame derived from the run's items. A few
frames come before the run exists or after the stream must stop for a reason of the route:
- the previous-turn wait (D7, "External clients"): `RUN_STARTED`, then `CUSTOM pydanticos.waiting`,
  keep-alives while waiting, and `RUN_ERROR previous_turn_running` when the wait times out;
- a failure to record or start the turn after the wait: `RUN_ERROR` with the refusal's code;
- the API key re-check of an open stream: `RUN_ERROR unauthorized` once the key is revoked or
  expired, `RUN_ERROR stream_interrupted` when the check itself fails (the run continues).

None of these carries a frame id: they derive from no durable event (D7).
"""

from __future__ import annotations

from typing import Any

from ag_ui.core import PROTOCOL_VERSION, RunErrorEvent, RunStartedEvent

from eap_api.ag_ui.translate import KEEP_ALIVE, STREAM_INTERRUPTED_MESSAGE, custom, encode

PREVIOUS_TURN_RUNNING = "previous_turn_running"
PREVIOUS_TURN_RUNNING_MESSAGE = "The previous turn of this thread is still running; send the message again later."
UNAUTHORIZED = "unauthorized"
UNAUTHORIZED_MESSAGE = "The API key of this stream was revoked or has expired; the run continues."


def run_started_frame(thread_id: str, run_id: str) -> str:
    return encode(RunStartedEvent(thread_id=thread_id, run_id=run_id, protocol_version=PROTOCOL_VERSION))


def custom_frame(name: str, value: Any) -> str:
    """`CUSTOM pydanticos.<name>`."""
    return encode(custom(name, value))


def run_error_frame(message: str, code: str | None = None) -> str:
    return encode(RunErrorEvent(message=message, code=code))


def previous_turn_timeout_frame() -> str:
    return run_error_frame(PREVIOUS_TURN_RUNNING_MESSAGE, PREVIOUS_TURN_RUNNING)


def stream_interrupted_frame() -> str:
    """The route could not go on (e.g. the key re-check failed); the run continues, resume (D7)."""
    return run_error_frame(STREAM_INTERRUPTED_MESSAGE, "stream_interrupted")


def unauthorized_frame() -> str:
    return run_error_frame(UNAUTHORIZED_MESSAGE, UNAUTHORIZED)


__all__ = [
    "KEEP_ALIVE",
    "PREVIOUS_TURN_RUNNING",
    "UNAUTHORIZED",
    "custom_frame",
    "previous_turn_timeout_frame",
    "run_error_frame",
    "run_started_frame",
    "stream_interrupted_frame",
    "unauthorized_frame",
]
