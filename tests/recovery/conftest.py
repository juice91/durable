"""Harness for the task-9 crash-injection matrix (see README.md in this directory).

Everything here is real: the dev Postgres (eap_app + eap_dbos), `python -m eap_worker` subprocesses
killed by the worker's own crash hook (`os._exit(137)`, no cleanup, like `kill -9`), the FastAPI app
(in-process, through TestClient) for POST/SSE, and a LISTEN connection for the transient deltas.

Isolation: every test gets a private DBOS system schema, application version and executor id, so
its workers never recover or dequeue anything but its own run; the schema is dropped afterwards and
the test's tenant rows, events, side effects and fake tickets are deleted.
"""

from __future__ import annotations

import contextlib
import faulthandler
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_DB_URL = os.environ.get(
    "EAP_TEST_DATABASE_URL", "postgresql+psycopg://eap_app:eap_app_password@localhost:54329/eap_app"
)
DBOS_DB_URL = os.environ.get("EAP_TEST_DBOS_URL", "postgresql://eap_dbos:eap_dbos_password@localhost:54329/eap_dbos")
TEST_HARD_TIMEOUT_S = 240
TICKET_SPEC = {
    "name": "Support triage",
    "instructions": "Open a ticket for the user's problem, then confirm it.",
    "model": {"provider": "test", "name": "ticket"},
    "tools": ["tickets.create"],
}


def _libpq(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def _postgres_unavailable() -> str | None:
    import psycopg

    for name, url in (("eap_app", APP_DB_URL), ("eap_dbos", DBOS_DB_URL)):
        if not url.startswith("postgresql"):
            return f"{name} URL is not Postgres ({url.split(':', 1)[0]})"
        try:
            psycopg.connect(_libpq(url), connect_timeout=3).close()
        except Exception as exc:  # noqa: BLE001
            return f"Postgres {name} unreachable ({type(exc).__name__}); start it with: bash scripts/dev-db.sh up"
    return None


_PG_SKIP_REASON = _postgres_unavailable()


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if _PG_SKIP_REASON is None:
        return
    marker = pytest.mark.skip(reason=f"crash-injection matrix needs the dev Postgres: {_PG_SKIP_REASON}")
    for item in items:
        if Path(str(item.fspath)).resolve().is_relative_to(Path(__file__).parent):
            item.add_marker(marker)


# --------------------------------------------------------------------------- timeouts


@pytest.fixture(autouse=True)
def hard_timeout() -> Iterator[None]:
    """Fail a test (SIGALRM) after TEST_HARD_TIMEOUT_S; dump tracebacks and exit if even that hangs."""

    def _alarm(signum: int, frame: Any) -> None:
        raise TimeoutError(f"test exceeded its {TEST_HARD_TIMEOUT_S}s hard timeout")

    previous = signal.signal(signal.SIGALRM, _alarm)
    signal.setitimer(signal.ITIMER_REAL, TEST_HARD_TIMEOUT_S)
    faulthandler.dump_traceback_later(TEST_HARD_TIMEOUT_S + 30, exit=True)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        faulthandler.cancel_dump_traceback_later()


def wait_for(predicate: Callable[[], Any], *, timeout: float, what: str, interval: float = 0.2) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


# --------------------------------------------------------------------------- worker subprocess


class WorkerProcess:
    """`python -m eap_worker` with stdout+stderr in a log file."""

    def __init__(self, env: dict[str, str], log_path: Path) -> None:
        self.log_path = log_path
        self._log = log_path.open("w")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "eap_worker"], cwd=REPO_ROOT, env=env, stdout=self._log, stderr=subprocess.STDOUT
        )

    def log(self) -> str:
        return self.log_path.read_text() if self.log_path.exists() else ""

    def wait_ready(self, timeout: float = 60) -> None:
        def ready() -> bool:
            if self.proc.poll() is not None:
                raise AssertionError(f"worker exited early ({self.proc.returncode}):\n{self.log()[-4000:]}")
            return "EAP_WORKER_READY" in self.log()

        wait_for(ready, timeout=timeout, what=f"worker ready ({self.log_path.name})")

    def wait_exit(self, timeout: float = 90) -> int:
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.kill()
            raise AssertionError(f"worker did not hit its crash point in {timeout}s:\n{self.log()[-4000:]}")

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.proc.wait(timeout=10)
        self._log.close()

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                pass
        self.kill()


# --------------------------------------------------------------------------- deltas (LISTEN)


class DeltaListener:
    """LISTEN eap_run_deltas on eap_app (where the worker NOTIFYs); keeps payloads for one run."""

    def __init__(self, conninfo: str, run_id: str) -> None:
        self.conninfo = conninfo
        self.run_id = run_id
        self.payloads: list[dict[str, Any]] = []
        self.error: BaseException | None = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="delta-listener", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=15):
            raise AssertionError(f"LISTEN did not start: {self.error!r}")

    def _run(self) -> None:
        import psycopg

        try:
            with psycopg.connect(self.conninfo, autocommit=True, connect_timeout=5) as conn:
                conn.execute("LISTEN eap_run_deltas")
                self._ready.set()
                while not self._stop.is_set():
                    for notify in conn.notifies(timeout=0.3):
                        payload = json.loads(notify.payload)
                        if payload.get("run_id") == self.run_id:
                            self.payloads.append(payload)
        except BaseException as exc:  # noqa: BLE001 - surfaced through .error / close()
            self.error = exc
            self._ready.set()

    def close(self) -> list[dict[str, Any]]:
        self._stop.set()
        self._thread.join(timeout=10)
        if self.error is not None:
            raise AssertionError(f"delta listener failed: {self.error!r}")
        return list(self.payloads)


# --------------------------------------------------------------------------- SSE


@dataclass
class Frame:
    event: str | None = None
    id: str | None = None
    data: Any = None
    comments: list[str] = field(default_factory=list)


def read_sse(client: Any, url: str, headers: dict[str, str], *, deadline_s: float = 30.0) -> list[Frame]:
    """Read one SSE response to its end (the server closes it after the terminal event)."""
    frames: list[Frame] = []
    deadline = time.monotonic() + deadline_s
    with client.stream("GET", url, headers=headers) as response:
        assert response.status_code == 200, response.read()
        assert response.headers["content-type"].startswith("text/event-stream")
        frame = Frame()
        for line in response.iter_lines():
            if time.monotonic() > deadline:
                raise AssertionError(f"SSE read of {url} exceeded {deadline_s}s")
            if line == "":
                if frame.event or frame.comments or frame.data is not None:
                    frames.append(frame)
                    if len(frames) > 1000:
                        raise AssertionError("more than 1000 SSE frames")
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
    return frames


# --------------------------------------------------------------------------- the harness


@dataclass
class EventRow:
    seq: int
    event_key: str
    type: str
    data: dict[str, Any]


class Harness:
    """One test's world: private DBOS schema/version/executor, a tenant, the API, worker processes."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from fastapi.testclient import TestClient
        from sqlalchemy import create_engine

        from eap_api.app import create_app
        from eap_api.sse import StreamSettings, get_stream_settings

        suffix = uuid.uuid4().hex[:10]
        self.tmp_path = tmp_path
        self.schema = f"eap_recovery_{suffix}"
        self.app_version = f"m1-recovery-{suffix}"
        self.executor_id = f"recovery-{suffix}"
        self.tenant = f"trec{suffix}"
        self.headers = {"Authorization": f"Bearer dev:{self.tenant}:alice"}
        self.run_ids: list[str] = []
        self.workers: list[WorkerProcess] = []
        self.listeners: list[DeltaListener] = []
        self.env = {
            "EAP_DATABASE_URL": APP_DB_URL,
            "EAP_DBOS_URL": DBOS_DB_URL,
            "EAP_DBOS_SYSTEM_SCHEMA": self.schema,
            "EAP_APP_VERSION": self.app_version,
            "EAP_EXECUTOR_ID": self.executor_id,
            "EAP_QUEUE_POLL_SECONDS": "0.2",
            "PYTHONUNBUFFERED": "1",
        }
        # The in-process API reads these at create_app (LauncherSettings.from_env).
        for key, value in self.env.items():
            monkeypatch.setenv(key, value)
        for key in [k for k in os.environ if k.startswith("EAP_CRASH")]:
            monkeypatch.delenv(key)

        self.engine = create_engine(APP_DB_URL, pool_pre_ping=True)
        self.app = create_app(database_url=APP_DB_URL)
        self.app.dependency_overrides[get_stream_settings] = lambda: StreamSettings(
            poll_interval=0.1, heartbeat_interval=10.0
        )
        self._client_cm = TestClient(self.app)
        self.client = self._client_cm.__enter__()

    # ---- processes

    def start_worker(self, name: str, *, crash_at: str | None = None, event_key: str | None = None) -> WorkerProcess:
        env = {k: v for k, v in os.environ.items() if not k.startswith("EAP_CRASH")}
        env.update(self.env)
        if crash_at:
            env["EAP_CRASH_AT"] = crash_at
            # Belt and braces: even an inherited env cannot crash twice.
            env["EAP_CRASH_MARKER"] = str(self.crash_marker)
            if event_key:
                env["EAP_CRASH_EVENT_KEY"] = event_key
        worker = WorkerProcess(env, self.tmp_path / f"{name}.log")
        self.workers.append(worker)
        worker.wait_ready()
        return worker

    @property
    def crash_marker(self) -> Path:
        return self.tmp_path / "crash.marker"

    def listen_deltas(self, run_id: str) -> DeltaListener:
        listener = DeltaListener(_libpq(APP_DB_URL), run_id)
        self.listeners.append(listener)
        return listener

    # ---- API

    def create_published_agent(self) -> str:
        r = self.client.post("/api/v1/agents", json={"spec": TICKET_SPEC}, headers=self.headers)
        assert r.status_code == 201, r.text
        agent_id = r.json()["id"]
        r = self.client.post(f"/api/v1/agents/{agent_id}/versions", headers=self.headers)
        assert r.status_code == 201, r.text
        return agent_id

    def post_run(self, agent_id: str, run_id: str, input_text: str) -> Any:
        if run_id not in self.run_ids:
            self.run_ids.append(run_id)
        return self.client.post(
            f"/api/v1/agents/{agent_id}/runs",
            json={"run_id": run_id, "input": input_text, "version": None},
            headers=self.headers,
        )

    def sse(self, run_id: str, *, after_seq: int | None = None, last_event_id: int | None = None) -> list[Frame]:
        url = f"/api/v1/runs/{run_id}/stream"
        if after_seq is not None:
            url += f"?after_seq={after_seq}"
        headers = dict(self.headers)
        if last_event_id is not None:
            headers["Last-Event-ID"] = str(last_event_id)
        return read_sse(self.client, url, headers)

    # ---- database inspection

    def sql(self, query: str, **params: Any) -> list[Any]:
        from sqlalchemy import text

        with self.engine.connect() as conn:
            return list(conn.execute(text(query), params))

    def events(self, run_id: str) -> list[EventRow]:
        return [
            EventRow(r.seq, r.event_key, r.type, r.data)
            for r in self.sql(
                "SELECT seq, event_key, type, data FROM execution_events WHERE run_id = :r ORDER BY seq", r=run_id
            )
        ]

    def run_row(self, run_id: str) -> dict[str, Any]:
        rows = self.sql("SELECT status, output, error FROM runs WHERE id = :r AND tenant_id = :t", r=run_id, t=self.tenant)
        assert len(rows) == 1, f"expected one runs row for {run_id}, got {len(rows)}"
        return dict(rows[0]._mapping)

    def tickets(self, run_id: str) -> list[Any]:
        return self.sql("SELECT * FROM fake_external_tickets WHERE idempotency_key LIKE :p", p=f"{run_id}:%")

    def ticket_calls(self, run_id: str) -> int:
        return self.sql(
            "SELECT count(*) FROM fake_external_ticket_calls WHERE idempotency_key LIKE :p", p=f"{run_id}:%"
        )[0][0]

    def side_effect_states(self) -> list[str]:
        return [r[0] for r in self.sql("SELECT state FROM side_effects WHERE tenant_id = :t", t=self.tenant)]

    def workflows(self, run_id: str) -> list[tuple[str, str, int]]:
        """(status, executor_id, recovery_attempts) of every DBOS workflow with this id."""
        import psycopg

        with psycopg.connect(_libpq(DBOS_DB_URL), connect_timeout=5) as conn:
            exists = conn.execute("SELECT to_regclass(%s)", (f'"{self.schema}".workflow_status',)).fetchone()[0]
            if exists is None:
                return []
            return [
                tuple(r)
                for r in conn.execute(
                    f'SELECT status, executor_id, recovery_attempts FROM "{self.schema}".workflow_status '
                    "WHERE workflow_uuid = %s",
                    (run_id,),
                )
            ]

    def wait_completed(self, run_id: str, timeout: float = 90) -> dict[str, Any]:
        terminal = {"completed", "failed", "needs_attention"}

        def done() -> dict[str, Any] | None:
            run = self.run_row(run_id)
            return run if run["status"] in terminal else None

        try:
            run = wait_for(done, timeout=timeout, what=f"run {run_id} to reach a terminal status")
        except AssertionError as exc:
            raise AssertionError(f"{exc}\n{self.worker_logs()}") from None
        assert run["status"] == "completed", (run, self.worker_logs())
        return run

    def worker_logs(self) -> str:
        return "\n".join(f"--- {w.log_path.name}\n{w.log()[-3000:]}" for w in self.workers)

    # ---- teardown

    def close(self) -> None:
        import psycopg
        from sqlalchemy import text

        for listener in self.listeners:
            with contextlib.suppress(Exception):
                listener.close()
        for worker in self.workers:
            with contextlib.suppress(Exception):
                worker.stop()
        with contextlib.suppress(Exception):
            self._client_cm.__exit__(None, None, None)
        try:
            with self.engine.begin() as conn:
                run_ids = [r[0] for r in conn.execute(text("SELECT id FROM runs WHERE tenant_id = :t"), {"t": self.tenant})]
                for run_id in {*run_ids, *self.run_ids}:
                    conn.execute(text("DELETE FROM execution_events WHERE run_id = :r"), {"r": run_id})
                    for table in ("fake_external_tickets", "fake_external_ticket_calls"):
                        if conn.execute(text("SELECT to_regclass(:t)"), {"t": table}).scalar():
                            conn.execute(text(f"DELETE FROM {table} WHERE idempotency_key LIKE :p"), {"p": f"{run_id}:%"})
                conn.execute(text("DELETE FROM runs WHERE tenant_id = :t"), {"t": self.tenant})
                conn.execute(text("DELETE FROM side_effects WHERE tenant_id = :t"), {"t": self.tenant})
                conn.execute(
                    text("DELETE FROM agent_versions WHERE agent_id IN (SELECT id FROM agents WHERE tenant_id = :t)"),
                    {"t": self.tenant},
                )
                conn.execute(text("DELETE FROM agents WHERE tenant_id = :t"), {"t": self.tenant})
        finally:
            self.engine.dispose()
            with psycopg.connect(_libpq(DBOS_DB_URL), autocommit=True, connect_timeout=5) as conn:
                conn.execute(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE')


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    h = Harness(tmp_path, monkeypatch)
    try:
        yield h
    finally:
        h.close()


# --------------------------------------------------------------------------- end-of-run summary

RECOVERY_REPORT: list[str] = []


@pytest.fixture
def report() -> Callable[[str], None]:
    return RECOVERY_REPORT.append


def pytest_terminal_summary(terminalreporter: Any) -> None:
    if RECOVERY_REPORT:
        terminalreporter.section("crash-injection matrix")
        for line in RECOVERY_REPORT:
            terminalreporter.write_line(line)
