"""Log search: `GET /logs`, the tenant's API and worker log records from Loki.

Tenant isolation is the point of this module. The caller never writes LogQL: the query is built here
from structured parameters, and every value goes into it only as an escaped LogQL string literal.
The tenant always comes from the authenticated principal.

How the records are stored (verified against grafana/otel-lgtm): the OTLP log attributes
`eap.tenant_id` and `eap.run_id` (set by eap_observability's LogContextProcessor) become the Loki
*structured metadata* `eap_tenant_id` and `eap_run_id`; only `service_name` and
`service_instance_id` are index labels. So the query is a stream selector on our two services and
label filters on the metadata:

    {service_name=~"eap-api|eap-worker"} | eap_tenant_id="<tenant>" [| eap_run_id="<run>"]
        [| eap_run_id=~"(<run>|<run>...)"] [| severity_number >= <n>] [|= "<text>"]

`agent_id` narrows to one agent of the tenant (404 if it is not theirs). Log records carry the run
id, not the agent id, so the API looks up the agent's runs that may have logged in the window (created
by its end, not finished before its start; child runs included) and filters on their ids: at most
`MAX_AGENT_RUNS`, newest first. The response then says how many runs were searched (`agent_runs`)
and whether older ones were left out (`agent_runs_limited`). An agent with no such run answers an
empty page without querying Loki.

No parser stage is used, so nothing in a log line can create or overwrite a label. A record without
`eap_tenant_id` never matches (the tenant is never empty). Entries returned by Loki are filtered
again on tenant and run before they leave the API.

Loki is reached over plain HTTP (`EAP_LOKI_URL`, e.g. http://127.0.0.1:3100); the API never imports
OpenTelemetry. Loki unset → 503 "Log search is not configured"; unreachable, timing out or 5xx →
503 "Log search is unavailable"; any other bad answer → 502 "Log search failed". Loki's own error
text is never passed through.

Each query scans every tenant's records in its window (up to 7 days), so the process runs at most
`EAP_LOKI_MAX_QUERIES` (default 4) at once; a request finding them all busy gets 503 "Log search is
busy; retry shortly" at once instead of holding a threadpool thread. Each query is bounded by
`EAP_LOKI_TIMEOUT_SECONDS` (default 10, clamped to 1–60). Loki's HTTP API takes no per-request
timeout (its `query_timeout` is server configuration), so the bound is the client's: when it fires
the connection is closed, which cancels the query's request context in Loki.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, Protocol

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse

from eap_observability import log_failure


from eap_api.deps import SessionDep
from eap_api.auth import Principal, current_principal
from eap_api.models import AgentModel
from eap_api.routes.runs import require_run_storage
from eap_api.schemas import ErrorDetail
from eap_run_store import EventLog, RunStore

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


# --- Route --------------------------------------------------------------------------------------

PrincipalDep = Annotated[Principal, Depends(current_principal)]
StoresDep = Annotated[tuple[RunStore, EventLog], Depends(require_run_storage)]
LogSourceDep = Annotated[LogSource | None, Depends(get_log_source)]
QuerySlotsDep = Annotated[QuerySlots, Depends(get_query_slots)]

router = APIRouter(
    tags=["logs"],
    responses={
        404: {"model": ErrorDetail},
        422: {"model": ErrorDetail},
        502: {"model": ErrorDetail},
        503: {"model": ErrorDetail},
    },
)


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


def _unprocessable(message: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=message)


@router.get("/logs")
def search_logs(
    principal: PrincipalDep,
    stores: StoresDep,
    source: LogSourceDep,
    slots: QuerySlotsDep,
    db: SessionDep,
    run_id: uuid.UUID | None = None,
    agent_id: uuid.UUID | None = None,
    q: Annotated[str | None, Query(max_length=MAX_QUERY_TEXT)] = None,
    level: LogLevel | None = None,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> JSONResponse:
    """The tenant's log records, newest first.

    `run_id` narrows to one run of the tenant (404 if it is not theirs). `agent_id` narrows to one
    agent's runs (404 if the agent is not theirs; archived agents included; see the module
    docstring); with both, a run of another agent matches nothing. `q` is a case-sensitive
    substring of the message. `level` is a minimum level. `from`/`to` (ISO 8601; naive = UTC) default
    to the last hour, or, with `run_id`, to a window starting a minute before the run was created;
    the window may span at most 7 days: a longer window is 422 when both `from` and `to` are given,
    and is clamped to the last 7 days when `to` is omitted. The response's `from`/`to` are the
    window actually searched (after defaults and clamping). Dates before 1970-01-01 are 422. `limit` 1–500, default 100.
    """
    tenant_id = principal.tenant_id
    run_store, _ = stores
    run = None
    if run_id is not None:
        run = run_store.get_run(tenant_id, run_id)
        if run is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    if agent_id is not None:
        agent = (
            db.query(AgentModel.id)
            .filter(AgentModel.id == str(agent_id), AgentModel.tenant_id == tenant_id)
            .first()
        )
        if agent is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

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
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=NOT_CONFIGURED)

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
            return JSONResponse(
                content={"items": [], "from": start.isoformat(), "to": end.isoformat(), "limit": limit, **agent_extra}
            )
    query = build_logql(
        tenant_id=tenant_id,
        run_id=run_text,
        q=q or None,
        level=level,
        run_ids=agent_runs if run_text is None else None,
    )
    if not slots.try_acquire():
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=BUSY)
    try:
        entries = source.query_range(query, start_ns=_ns(start), end_ns=_ns(end), limit=limit)
    except LokiUnavailable as exc:
        log_failure(log, "log search: loki unavailable", exc, code="loki_unavailable", tenant_id=tenant_id)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=UNAVAILABLE) from None
    except LokiQueryFailed as exc:
        log_failure(log, "log search: loki query failed", exc, code="loki_query_failed", tenant_id=tenant_id)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=FAILED) from None
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
    return JSONResponse(
        content={
            "items": [_item(e) for e in entries[:limit]],
            "from": start.isoformat(),
            "to": end.isoformat(),
            "limit": limit,
            **agent_extra,
        }
    )


__all__ = [
    "LOG_SERVICES",
    "LokiClient",
    "LokiEntry",
    "LokiQueryFailed",
    "LokiUnavailable",
    "QuerySlots",
    "build_logql",
    "get_log_source",
    "get_query_slots",
    "logql_string",
    "parse_query_range",
    "router",
]
