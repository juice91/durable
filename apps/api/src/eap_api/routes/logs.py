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

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse

from eap_api.auth import Principal, current_principal
from eap_api.schemas import ErrorDetail
from eap_api.service_deps.runs import LogServiceDep
from eap_api.services.logs import (  # noqa: F401 - re-exported: tests and callers import them from here
    DEFAULT_LIMIT,
    LOG_SERVICES,
    MAX_LIMIT,
    MAX_QUERY_TEXT,
    LogLevel,
    LogSource,
    LokiClient,
    LokiEntry,
    LokiQueryFailed,
    LokiUnavailable,
    QuerySlots,
    UpstreamFailed,
    build_logql,
    get_log_source,
    get_query_slots,
    logql_string,
    parse_query_range,
)

PrincipalDep = Annotated[Principal, Depends(current_principal)]
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


@router.get("/logs")
def search_logs(
    principal: PrincipalDep,
    service: LogServiceDep,
    source: LogSourceDep,
    slots: QuerySlotsDep,
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
    try:
        found = service.search(
            principal.tenant_id, source=source, slots=slots, run_id=run_id, agent_id=agent_id, q=q, level=level,
            from_=from_, to=to, limit=limit,
        )
    except UpstreamFailed as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from None
    return JSONResponse(content=found)


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
