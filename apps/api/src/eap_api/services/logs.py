"""Log search over Loki (the business logic behind `GET /logs`; see eap_api.routes.logs for the contract).

Tenant isolation is the point of this module: the query is built from structured parameters, every value goes
into LogQL only as an escaped string literal, and the tenant always comes from the authenticated principal.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

from sqlalchemy.orm import Session

from eap_api.models import AgentModel
from eap_api.services.run_filters import plain_refusals
from eap_api.services.errors import InvalidInput, NotFound, Unavailable
from eap_observability.redaction import log_failure
from eap_run_store import RunStore

log = logging.getLogger("eap_api")

LOKI_URL_ENV = "EAP_LOKI_URL"
# The services whose records the API may return (service.name of eap-api and eap-worker).
LOG_SERVICES = ("eap-api", "eap-worker")
TENANT_LABEL = "eap_tenant_id"
RUN_LABEL = "eap_run_id"

MAX_RANGE = timedelta(days=7)
DEFAULT_RANGE = timedelta(hours=1)
# With a run id and no `from`, the window starts this long before the run was created.
RUN_LOOKBACK = timedelta(minutes=1)
DEFAULT_LIMIT = 100
# GET /logs?agent_id=: how many of the agent's runs in the window the query covers.
MAX_AGENT_RUNS = 200
MAX_LIMIT = 500
MAX_QUERY_TEXT = 500
LOKI_TIMEOUT_ENV = "EAP_LOKI_TIMEOUT_SECONDS"
LOKI_TIMEOUT_SECONDS = 10.0
MAX_QUERIES_ENV = "EAP_LOKI_MAX_QUERIES"
DEFAULT_MAX_QUERIES = 4
MAX_LOKI_RESPONSE_BYTES = 16 * 1024 * 1024

LogLevel = Literal["debug", "info", "warn", "error", "fatal"]
# OpenTelemetry severity numbers (Python DEBUG 5, INFO 9, WARNING 13, ERROR 17, CRITICAL 21).
# `level` is a minimum: level=warn returns warn, error and fatal records.
MIN_SEVERITY: dict[str, int] = {"debug": 5, "info": 9, "warn": 13, "error": 17, "fatal": 21}

NOT_CONFIGURED = "Log search is not configured"
UNAVAILABLE = "Log search is unavailable"
FAILED = "Log search failed"
BUSY = "Log search is busy; retry shortly"


# --- LogQL --------------------------------------------------------------------------------------


def logql_string(value: str) -> str:
    """`value` as a double-quoted LogQL string literal (Go strconv.Unquote syntax).

    Backslash and double quote are escaped; every non-printable character (newline, tab, NUL, other
    control and format characters) becomes a \\uXXXX or \\UXXXXXXXX escape. Everything else, including
    backticks, `|`, `}` and LogQL operators, is literal text inside the quotes. Raises ValueError on
    a lone surrogate, which no escape can represent.
    """
    out = ['"']
    for ch in value:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch.isprintable():
            out.append(ch)
        else:
            code = ord(ch)
            if 0xD800 <= code <= 0xDFFF:
                raise ValueError("lone surrogate")
            out.append(f"\\u{code:04x}" if code <= 0xFFFF else f"\\U{code:08x}")
    out.append('"')
    return "".join(out)


def build_logql(
    *,
    tenant_id: str,
    run_id: str | None = None,
    q: str | None = None,
    level: str | None = None,
    run_ids: list[str] | None = None,
) -> str:
    """The LogQL for one tenant's log search (see the module docstring). `run_ids` (an agent's runs)
    must be canonical UUIDs: they become a regex alternation. Raises ValueError on an empty tenant,
    an empty or non-UUID `run_ids`, an unknown level or text that cannot be escaped."""
    if not tenant_id:
        raise ValueError("tenant_id is required")
    selector = "{service_name=~" + logql_string("|".join(LOG_SERVICES)) + "}"
    parts = [selector, f"| {TENANT_LABEL}={logql_string(tenant_id)}"]
    if run_id:
        parts.append(f"| {RUN_LABEL}={logql_string(run_id)}")
    if run_ids is not None:
        if not run_ids or any(str(uuid.UUID(r)) != r for r in run_ids):
            raise ValueError("run_ids must be canonical UUIDs")
        parts.append(f"| {RUN_LABEL}=~{logql_string('(' + '|'.join(run_ids) + ')')}")
    if level is not None:
        if level not in MIN_SEVERITY:
            raise ValueError("unknown level")
        parts.append(f"| severity_number >= {MIN_SEVERITY[level]}")
    if q:
        parts.append(f"|= {logql_string(q)}")
    return " ".join(parts)


# --- Loki client --------------------------------------------------------------------------------


class LokiUnavailable(Exception):
    """Loki could not be reached, timed out or answered 5xx."""


class LokiQueryFailed(Exception):
    """Loki answered, but not with a usable result."""


@dataclass(frozen=True)
class LokiEntry:
    ts_ns: int
    line: str
    labels: dict[str, str]  # stream labels and structured metadata


class LogSource(Protocol):
    def query_range(self, query: str, *, start_ns: int, end_ns: int, limit: int) -> list[LokiEntry]: ...


@dataclass(frozen=True)
class LokiClient:
    """Loki's query_range over plain HTTP (stdlib), newest entries first."""

    base_url: str
    timeout_seconds: float = LOKI_TIMEOUT_SECONDS

    def query_range(self, query: str, *, start_ns: int, end_ns: int, limit: int) -> list[LokiEntry]:
        params = urllib.parse.urlencode(
            {"query": query, "start": str(start_ns), "end": str(end_ns), "limit": str(limit), "direction": "backward"}
        )
        url = f"{self.base_url.rstrip('/')}/loki/api/v1/query_range?{params}"
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read(MAX_LOKI_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            if exc.code >= 500:
                raise LokiUnavailable(exc.code) from None
            raise LokiQueryFailed(exc.code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LokiUnavailable(type(exc).__name__) from None
        if len(body) > MAX_LOKI_RESPONSE_BYTES:
            raise LokiQueryFailed("response too large")
        return parse_query_range(body)


def parse_query_range(body: bytes | str) -> list[LokiEntry]:
    """Entries of a Loki query_range `streams` answer. Raises LokiQueryFailed on anything else."""
    try:
        payload = json.loads(body)
        if payload.get("status") != "success":
            raise LokiQueryFailed("status")
        data = payload["data"]
        if data.get("resultType") != "streams":
            raise LokiQueryFailed("resultType")
        entries: list[LokiEntry] = []
        for stream in data.get("result") or []:
            labels = {str(k): str(v) for k, v in (stream.get("stream") or {}).items()}
            for value in stream.get("values") or []:
                entry_labels = labels
                if len(value) > 2 and isinstance(value[2], dict):
                    # categorize-labels form: metadata per entry.
                    extra = {
                        str(k): str(v)
                        for group in value[2].values()
                        if isinstance(group, dict)
                        for k, v in group.items()
                    }
                    entry_labels = {**labels, **extra}
                entries.append(LokiEntry(ts_ns=int(value[0]), line=str(value[1]), labels=entry_labels))
        return entries
    except LokiQueryFailed:
        raise
    except (ValueError, KeyError, TypeError, AttributeError, IndexError):
        raise LokiQueryFailed("malformed") from None


def get_log_source() -> LogSource | None:
    """The Loki client from EAP_LOKI_URL (http or https), or None when unset or unusable.
    Tests override this dependency."""
    url = os.environ.get(LOKI_URL_ENV, "").strip()
    if not url:
        return None
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return LokiClient(url, timeout_seconds=_timeout_from_env())


def _env_number(name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default
    return min(max(value, low), high) if value == value else default  # NaN → default


def _timeout_from_env() -> float:
    return _env_number(LOKI_TIMEOUT_ENV, LOKI_TIMEOUT_SECONDS, 1.0, 60.0)


class QuerySlots:
    """At most `size` concurrent Loki queries in this process; never waits for a slot."""

    def __init__(self, size: int) -> None:
        self.size = size
        self._semaphore = threading.BoundedSemaphore(size)

    def try_acquire(self) -> bool:
        return self._semaphore.acquire(blocking=False)

    def release(self) -> None:
        self._semaphore.release()


_query_slots: QuerySlots | None = None
_query_slots_lock = threading.Lock()


def get_query_slots() -> QuerySlots:
    """The process-wide query slots, sized from EAP_LOKI_MAX_QUERIES (1–64) on first use.
    Tests override this dependency."""
    global _query_slots
    with _query_slots_lock:
        if _query_slots is None:
            _query_slots = QuerySlots(int(_env_number(MAX_QUERIES_ENV, DEFAULT_MAX_QUERIES, 1, 64)))
        return _query_slots


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _before_epoch(value: datetime | None) -> bool:
    """Whether `value` (naive = UTC) is before 1970-01-01; a value so early that converting it
    overflows counts as before."""
    if value is None:
        return False
    try:
        return _utc(value) < EPOCH
    except OverflowError:
        return True


def _ns(value: datetime) -> int:
    delta = value - EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1_000


def _iso_from_ns(ts_ns: int) -> str:
    seconds, rest = divmod(ts_ns, 1_000_000_000)
    return datetime.fromtimestamp(seconds, UTC).replace(microsecond=rest // 1_000).isoformat()


_LEVEL_NAMES = {"warning": "warn", "critical": "fatal"}


def _level(labels: dict[str, str]) -> str | None:
    text = (labels.get("severity_text") or labels.get("detected_level") or "").strip().lower()
    if not text:
        return None
    return _LEVEL_NAMES.get(text, text)


def _item(entry: LokiEntry) -> dict[str, Any]:
    labels = entry.labels
    return {
        "ts": _iso_from_ns(entry.ts_ns),
        "level": _level(labels),
        "message": entry.line,
        "service": labels.get("service_name"),
        "trace_id": labels.get("trace_id") or None,
        "span_id": labels.get("span_id") or None,
        "run_id": labels.get(RUN_LABEL) or None,
    }


class UpstreamFailed(Exception):
    """Loki answered, but not with a usable result: the route answers 502 (no ServiceError kind is a 502)."""


def _unprocessable(message: str) -> InvalidInput:
    return InvalidInput(message)


@plain_refusals
class LogService:
    def __init__(self, run_store: RunStore, db: Session) -> None:
        self.run_store = run_store
        self.db = db

    def search(
        self,
        tenant_id: str,
        *,
        source: LogSource | None,
        slots: QuerySlots,
        run_id: uuid.UUID | None,
        agent_id: uuid.UUID | None,
        q: str | None,
        level: str | None,
        from_: datetime | None,
        to: datetime | None,
        limit: int,
    ) -> dict[str, Any]:
        """The tenant's log records, newest first, with the window actually searched (see GET /logs)."""
        run_store = self.run_store
        db = self.db
        run = None
        if run_id is not None:
            run = run_store.get_run(tenant_id, run_id)
            if run is None:
                raise NotFound("Run not found")
        if agent_id is not None:
            agent = (
                db.query(AgentModel.id)
                .filter(AgentModel.id == str(agent_id), AgentModel.tenant_id == tenant_id)
                .first()
            )
            if agent is None:
                raise NotFound("Agent not found")

        if _before_epoch(from_) or _before_epoch(to):
            raise _unprocessable("from and to must not be before 1970-01-01")
        end = _utc(to) if to is not None else datetime.now(UTC)
        if from_ is not None:
            start = _utc(from_)
            if to is None and start < end and end - start > MAX_RANGE:
                # `to` defaults to the server's now, which is later than the client's: clamp rather than
                # 422 a client that asked for exactly the maximum window ending now.
                start = end - MAX_RANGE
        elif run is not None and run.get("created_at") is not None:
            start = max(_utc(run["created_at"]) - RUN_LOOKBACK, end - MAX_RANGE)
        else:
            start = end - DEFAULT_RANGE
        if start >= end:
            raise _unprocessable("'from' must be before 'to'")
        if end - start > MAX_RANGE:
            raise _unprocessable("the time range may span at most 7 days")

        run_text = str(run_id) if run_id is not None else None
        if q:
            try:
                logql_string(q)
            except ValueError:
                raise _unprocessable("q contains characters that cannot be searched") from None

        if source is None:
            raise Unavailable(NOT_CONFIGURED)

        agent_runs: list[str] | None = None
        agent_extra: dict[str, Any] = {}
        if agent_id is not None:
            if run is not None:
                agent_runs = [run_text] if run.get("agent_id") == str(agent_id) else []
                agent_extra = {"agent_runs": len(agent_runs), "agent_runs_limited": False}
            else:
                agent_runs, limited = run_store.agent_run_ids(
                    tenant_id, agent_id, start=start, end=end, limit=MAX_AGENT_RUNS
                )
                agent_extra = {"agent_runs": len(agent_runs), "agent_runs_limited": limited}
            if not agent_runs:
                return {"items": [], "from": start.isoformat(), "to": end.isoformat(), "limit": limit, **agent_extra}
        query = build_logql(
            tenant_id=tenant_id,
            run_id=run_text,
            q=q or None,
            level=level,
            run_ids=agent_runs if run_text is None else None,
        )
        if not slots.try_acquire():
            raise Unavailable(BUSY)
        try:
            entries = source.query_range(query, start_ns=_ns(start), end_ns=_ns(end), limit=limit)
        except LokiUnavailable as exc:
            log_failure(log, "log search: loki unavailable", exc, code="loki_unavailable", tenant_id=tenant_id)
            raise Unavailable(UNAVAILABLE) from None
        except LokiQueryFailed as exc:
            log_failure(log, "log search: loki query failed", exc, code="loki_query_failed", tenant_id=tenant_id)
            raise UpstreamFailed(FAILED) from None
        finally:
            slots.release()

        # Defence in depth: whatever Loki returned, only this tenant's (and this run's or agent's) records leave.
        allowed_runs = set(agent_runs) if agent_runs is not None else None
        entries = [
            e
            for e in entries
            if e.labels.get(TENANT_LABEL) == tenant_id
            and (run_text is None or e.labels.get(RUN_LABEL) == run_text)
            and (allowed_runs is None or e.labels.get(RUN_LABEL) in allowed_runs)
        ]
        entries.sort(key=lambda e: e.ts_ns, reverse=True)
        return {
            "items": [_item(e) for e in entries[:limit]],
            "from": start.isoformat(),
            "to": end.isoformat(),
            "limit": limit,
            **agent_extra,
        }
