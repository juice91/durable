"""Following a run for an external caller (design 5.1, 7.2): polling is the truth. A surface holds a call open for a
bounded time only, never for the length of a workflow, and answers as soon as the run ends or needs a person."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

FIRST_PAUSE_SECONDS = 0.25
MAX_PAUSE_SECONDS = 2.0


def settled(view: dict[str, Any]) -> bool:
    """Nothing more happens by waiting: the run ended, or it waits for a person."""
    return bool(view["done"]) or view["state"] == "input_required"


async def wait_for_run(
    view: dict[str, Any],
    read: Callable[[], Awaitable[dict[str, Any]]],
    seconds: float,
    *,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """The run view once it is settled, or the latest one after `seconds`. `read` reads the run again (and refuses a
    caller who lost the right to see it); between reads nothing is held."""
    end = clock() + seconds
    pause = FIRST_PAUSE_SECONDS
    while not settled(view):
        left = end - clock()
        if left <= 0:
            break
        await sleep(min(pause, left))
        pause = min(pause * 2, MAX_PAUSE_SECONDS)
        view = await read()
    return view
