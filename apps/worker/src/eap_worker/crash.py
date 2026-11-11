"""Env-controlled crash hook for crash-injection tests.

`EAP_CRASH_AT` names one point; when execution reaches it the process calls `os._exit(137)`
(no cleanup, no checkpoint, like `kill -9`):

- `model_request`: inside the DBOS model-request step, right after the first text delta of a
  model response has been published over NOTIFY (the stream has started, no final message is
  checkpointed yet). Only responses that stream text reach it; with the `test:ticket` model that
  is model request 1 (request 0 is a tool call).
- `after_tool_call`: inside the `tickets.create` tool step, right after the external ticket
  service returned and before `side_effects.complete` and the DBOS step checkpoint (the side
  effect row stays `reserved`).
- `after_event_write`: inside the `eap.append_event` DBOS step, right after the durable event
  committed to `eap_app` and before the step's checkpoint reaches `eap_dbos`. Only fires for
  events whose `event_key` matches the glob `EAP_CRASH_EVENT_KEY` (default `*`, i.e. the first
  event written, `start`).

`EAP_CRASH_MARKER`, if set, is a file path: the hook fires only while that file does not exist
and creates it before exiting, so a supervised worker that inherits the env crashes once and
then recovers instead of crash-looping.
"""

from __future__ import annotations

import fnmatch
import os
import sys

CRASH_POINTS = frozenset({"model_request", "after_tool_call", "after_event_write"})


def crash_point(name: str, *, event_key: str | None = None) -> None:
    target = os.environ.get("EAP_CRASH_AT")
    if target != name:
        return
    if event_key is not None and not fnmatch.fnmatchcase(event_key, os.environ.get("EAP_CRASH_EVENT_KEY", "*")):
        return
    marker = os.environ.get("EAP_CRASH_MARKER")
    if marker:
        try:
            fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return
        os.close(fd)
    print(f"eap-worker: EAP_CRASH_AT={name} reached (event_key={event_key}); exiting 137", file=sys.stderr, flush=True)
    os._exit(137)
