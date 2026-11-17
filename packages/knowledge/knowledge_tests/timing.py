"""Load-tolerant timing for guard tests: CPU time, best of three, cap scaled by EAP_TEST_TIME_FACTOR."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")

# Raised on a loaded or slower runner (CI Heavy sets 2.0); 1.0 elsewhere.
TIME_FACTOR = float(os.environ.get("EAP_TEST_TIME_FACTOR", "1.0"))


def cap(seconds: float) -> float:
    return seconds * TIME_FACTOR


def best_of_3(fn: Callable[[], T]) -> tuple[T, float]:
    """Run fn three times; return the last result and the smallest CPU time (process_time).

    A real quadratic blow-up is slow in all three runs; a noisy neighbour is not.
    """
    best = float("inf")
    result: T
    for _ in range(3):
        start = time.process_time()
        result = fn()
        best = min(best, time.process_time() - start)
    return result, best
