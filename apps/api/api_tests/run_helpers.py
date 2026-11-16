"""Helpers for the run tests: a bounded SSE reader, a hard per-test timeout, tenant cleanup."""

from __future__ import annotations

import contextlib
import faulthandler
import json
import os
import signal
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from sqlalchemy import text

PG_URL = os.environ.get("EAP_TEST_DATABASE_URL")
requires_pg = pytest.mark.skipif(
    not (PG_URL or "").startswith("postgresql"),
    reason="runs and events require Postgres: set EAP_TEST_DATABASE_URL=postgresql+psycopg://...",
)


@contextlib.contextmanager
def hard_timeout(seconds: float) -> Iterator[None]:
    """Fail the test (SIGALRM) after `seconds`; kill the process with a traceback dump if even
    that cannot unwind it, so a stuck stream can never hang the suite."""
    if sys.platform == "win32":  # pragma: no cover
        yield
        return

    def _alarm(signum: int, frame: Any) -> None:
        raise TimeoutError(f"test exceeded its {seconds}s hard timeout")

    previous = signal.signal(signal.SIGALRM, _alarm)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    faulthandler.dump_traceback_later(seconds + 30, exit=True)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        faulthandler.cancel_dump_traceback_later()


@dataclass
class Frame:
    event: str | None = None
    id: str | None = None
    data: Any = None
    comments: list[str] = field(default_factory=list)


def iter_frames(response: Any, *, deadline_s: float = 20.0, max_frames: int = 500) -> Iterator[Frame]:
    """Parse SSE frames from a streaming httpx response, bounded in time and frame count."""
    deadline = time.monotonic() + deadline_s
    frame = Frame()
    count = 0
    for line in response.iter_lines():
        if time.monotonic() > deadline:
            raise AssertionError(f"SSE read exceeded {deadline_s}s")
        if line == "":
            if frame.event or frame.comments or frame.data is not None:
                yield frame
                count += 1
                if count >= max_frames:
                    raise AssertionError(f"more than {max_frames} SSE frames")
            frame = Frame()
        elif line.startswith(":"):
            frame.comments.append(line[1:].strip())
        else:
            name, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if name == "event":
                frame.event = value
            elif name == "id":
                frame.id = value
            elif name == "data":
                frame.data = json.loads(value)


def read_all_frames(client: Any, url: str, headers: dict[str, str], *, deadline_s: float = 20.0) -> list[Frame]:
    with client.stream("GET", url, headers=headers) as response:
        assert response.status_code == 200, response.read()
        assert response.headers["content-type"].startswith("text/event-stream")
        return list(iter_frames(response, deadline_s=deadline_s))


def cleanup_tenant(engine: Any, tenant_id: str) -> None:
    with engine.begin() as conn:
        run_ids = [r[0] for r in conn.execute(text("SELECT id FROM runs WHERE tenant_id = :t"), {"t": tenant_id})]
        for run_id in run_ids:
            conn.execute(text("DELETE FROM execution_events WHERE run_id = :r"), {"r": run_id})
        conn.execute(text("DELETE FROM spans WHERE tenant_id = :t"), {"t": tenant_id})
        conn.execute(text("DELETE FROM run_model_requests WHERE tenant_id = :t"), {"t": tenant_id})
        conn.execute(text("DELETE FROM runs WHERE tenant_id = :t"), {"t": tenant_id})
        conn.execute(text("DELETE FROM conversations WHERE tenant_id = :t"), {"t": tenant_id})
        conn.execute(text("DELETE FROM side_effects WHERE tenant_id = :t"), {"t": tenant_id})
        conn.execute(
            text("DELETE FROM agent_versions WHERE agent_id IN (SELECT id FROM agents WHERE tenant_id = :t)"),
            {"t": tenant_id},
        )
        conn.execute(text("DELETE FROM agents WHERE tenant_id = :t"), {"t": tenant_id})
