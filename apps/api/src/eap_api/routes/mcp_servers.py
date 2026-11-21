"""MCP server registry routes (milestone 3, task 3.4).

- POST   /mcp-servers              register (201); 409 when the name is taken in the tenant
- GET    /mcp-servers              the tenant's servers, by name
- GET    /mcp-servers/{id}         one server
- PATCH  /mcp-servers/{id}         change name, url, enabled and/or replace all headers
- DELETE /mcp-servers/{id}         delete (204), with its secrets
- POST   /mcp-servers/{id}/test    connect, list tools, record last_status (200 with status ok|error)
- POST   /mcp-servers/{id}/tools/{tool}/runs   run one tool of the server as a recorded tool run
- POST   /mcp-servers/from-catalog         install a catalog entry: a package runs as a managed pod
                                           (runtime = managed), a remote registers as an external server
- GET    /mcp-servers/{id}/runtime         a managed server's runtime state (reconciled with the pod)
- POST   /mcp-servers/{id}/runtime/{start|stop|restart}
- GET    /mcp-servers/{id}/runtime/logs?tail=200   the pod's log tail, secrets masked

Every lookup is tenant-scoped: another tenant's or an unknown id is a 404. Only remote servers over
Streamable HTTP; the URL must pass the outbound policy (eap_secrets.outbound) on create and on every
change (422 with the policy's code). Header values are write-only: they are encrypted with the
platform key (EAP_SECRETS_KEY) and bound to (tenant, server, header name); responses list header
names only. Writing or using header secrets without the key answers 503 with how to set it. Request
bodies are validated here rather than by FastAPI so a validation error never echoes a header value.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, Query, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from eap_api.auth import Principal, current_principal
from eap_api.concurrency import IfMatch, etag_responses, read_response, stamp
from eap_api.deps import get_database_engine
from eap_api.mcp_servers import managed
from eap_api.request_guard import GuardedJsonRoute
from eap_api.routes.tools import ToolRunCreate, _tool_run_body
from eap_api.schemas import ErrorDetail
from eap_api.service_deps.connections import (  # noqa: F401  (get_outbound_policy and RuntimeDep: tests and callers import them here)
    McpConnectionServiceDep,
    McpRuntimeServiceDep,
    McpServerServiceDep,
    PolicyDep,
    RuntimeDep,
    build_mcp_connection_service,
    get_outbound_policy,
)
from eap_api.services.mcp_servers import MAX_HEADER_VALUE, NAME_PATTERN  # noqa: F401
from eap_observability import current_trace_context
from eap_secrets import KEY_ENV, SecretsKeyMissing, load_keyring

log = logging.getLogger("eap_api.mcp_servers")

PrincipalDep = Annotated[Principal, Depends(current_principal)]

router = APIRouter(
    tags=["mcp-servers"],
    responses={404: {"model": ErrorDetail}, 422: {"model": ErrorDetail}, 503: {"model": ErrorDetail}},
    route_class=GuardedJsonRoute,
)


def check_secrets_configuration() -> None:
    """At API startup: a malformed EAP_SECRETS_KEY fails startup; an unset one only disables
    header secrets (those requests answer 503 explaining how to set it)."""
    try:
        load_keyring()
    except SecretsKeyMissing:
        log.warning("%s is not set; MCP server header secrets are unavailable until it is", KEY_ENV)


@router.post("/mcp-servers", status_code=status.HTTP_201_CREATED, responses={409: {"model": ErrorDetail}})
def create_mcp_server(
    principal: PrincipalDep, service: McpServerServiceDep, payload: Annotated[Any, Body()] = None
) -> JSONResponse:
    created = service.create(principal.tenant_id, payload)
    return stamp(JSONResponse(status_code=status.HTTP_201_CREATED, content=created), created["revision"])


@router.get("/mcp-servers")
def list_mcp_servers(principal: PrincipalDep, service: McpServerServiceDep) -> JSONResponse:
    return JSONResponse(content=service.list(principal.tenant_id))


@router.get("/mcp-servers/{server_id}", responses={**etag_responses(write=False)})
def get_mcp_server(server_id: uuid.UUID, principal: PrincipalDep, service: McpServerServiceDep) -> JSONResponse:
    found = service.get(principal.tenant_id, server_id)
    return read_response(found, found["revision"])


@router.patch("/mcp-servers/{server_id}", responses={409: {"model": ErrorDetail}, **etag_responses()})
def update_mcp_server(
    server_id: uuid.UUID,
    principal: PrincipalDep,
    service: McpServerServiceDep,
    pre: IfMatch,
    payload: Annotated[Any, Body()] = None,
) -> JSONResponse:
    """Send the ETag you read as `If-Match`: a stale one is 412 `revision_mismatch` with the current
    server (see eap_api.concurrency). Fields left out stay as they are. `headers` replaces the whole set ({} removes all). Changing
    the url or headers clears the last test result, which no longer describes the server. A managed
    server takes `env` (`{VARIABLE: value | "secret:<name>"}`, only the variables it changes): the
    server's pod is restarted so the new values are projected (the response carries the resulting
    `runtime_state`). 422 for `env` on an external server, an undeclared variable or a reference to a
    secret the tenant does not have."""
    changed = service.update(principal.tenant_id, server_id, payload, pre.expected)
    return pre.respond(changed, changed["revision"])


@router.delete(
    "/mcp-servers/{server_id}", status_code=status.HTTP_204_NO_CONTENT, responses={502: {"model": ErrorDetail}}
)
def delete_mcp_server(server_id: uuid.UUID, principal: PrincipalDep, service: McpServerServiceDep) -> Response:
    """Delete the server and its secrets; a managed server's CR, Secret and NetworkPolicy go first.
    Re-running a delete that failed or crashed midway finishes it: the server is disabled before its
    objects are removed (nothing starts it meanwhile), removing a missing object is fine, and the rows
    are removed last (secrets and the runtime row cascade). 502 when the cluster could not be reached
    (nothing is lost; retry)."""
    service.delete(principal.tenant_id, server_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/mcp-servers/from-catalog",
    status_code=status.HTTP_201_CREATED,
    responses={409: {"model": ErrorDetail}},
)
def install_from_catalog(
    principal: PrincipalDep, service: McpServerServiceDep, payload: Annotated[Any, Body()] = None,
) -> JSONResponse:
    """Install a mirrored registry entry. Body `{catalog_name, package_index?, remote_index?, name?, env?, headers?}`.

    A package (the default when the entry has one; `package_index` picks which) becomes a managed
    server: `runtime = "managed"`, no url; variables the entry marks `secret` are sealed (write-only),
    the others stored in the runtime's package document; the pod is started at once (201 carries the
    resulting `runtime_state`, `starting` or `failed` with `runtime_error`). A remote (`remote_index`,
    or the entry's first remote when it has no package) registers as an external server with the
    remote's URL and optional `headers`, exactly like POST /mcp-servers. 404 unknown entry; 409 name
    taken; 422 a removed entry, an index out of range, an env variable not declared or missing, a
    package that cannot be run safely, or a remote that is not Streamable HTTP."""
    return JSONResponse(status_code=status.HTTP_201_CREATED, content=service.install_from_catalog(principal.tenant_id, payload))


# --- a managed server's runtime ---------------------------------------------------------------------


@router.get("/mcp-servers/{server_id}/runtime", responses={409: {"model": ErrorDetail}})
def get_runtime(server_id: uuid.UUID, principal: PrincipalDep, service: McpRuntimeServiceDep) -> JSONResponse:
    """`{state, message, reachable, last_error, last_used_at, updated_at, image, catalog_name, package}`.
    `state` is pending|starting|running|idle|stopped|failed; a `starting` row is compared with the pod
    first. `reachable` false means the cluster could not be asked (the stored state is shown)."""
    return JSONResponse(content=service.get_runtime(principal.tenant_id, server_id))


@router.post("/mcp-servers/{server_id}/runtime/{action}",
             responses={409: {"model": ErrorDetail}, 502: {"model": ErrorDetail}})
def control_runtime(
    server_id: uuid.UUID,
    action: Literal["start", "stop", "restart"],
    principal: PrincipalDep,
    service: McpRuntimeServiceDep,
) -> JSONResponse:
    """Start (a pending, stopped, idle or failed pod; otherwise a no-op), stop (scale to zero, objects
    stay; the next tool call starts it again) or restart (stop, then start). Answers the runtime body
    of GET .../runtime. 409 for an external server or a disabled one (start, restart). 502 when the
    adapter (or the sealed env) fails: the row is then `failed` with `last_error` set, read it with
    GET .../runtime; the error text is never returned."""
    return JSONResponse(content=service.control(principal.tenant_id, server_id, action))


@router.get("/mcp-servers/{server_id}/runtime/logs", responses={409: {"model": ErrorDetail}, 502: {"model": ErrorDetail}})
def get_runtime_logs(
    server_id: uuid.UUID,
    principal: PrincipalDep,
    service: McpRuntimeServiceDep,
    tail: Annotated[int, Query(ge=1, le=managed.MAX_LOG_TAIL)] = managed.DEFAULT_LOG_TAIL,
) -> JSONResponse:
    """`{lines: [...], tail}`: the last `tail` (1 to 1000, default 200) log lines of the pod, with every
    sealed env value (any encoding) and `token=`, `key=`, `secret=`, `password=` assignments masked."""
    return JSONResponse(content=service.logs(principal.tenant_id, server_id, tail))


@router.post("/mcp-servers/{server_id}/test")
async def run_mcp_server_test(
    server_id: uuid.UUID, request: Request, principal: PrincipalDep, policy: PolicyDep, runtime: RuntimeDep
) -> JSONResponse:
    """Connect over Streamable HTTP, initialize and list tools, within a bounded time
    (EAP_MCP_TEST_TIMEOUT_SECONDS, default 10, at most 30). Answers 200 with
    {status: "ok", tools: [{name, description, input_schema}]} or
    {status: "error", error_code, tools: []}; the outcome is stored as last_status/last_error_code.

    A managed server whose runtime is stopped (the idle sweep) is started first and waited for, up to
    EAP_MCP_TEST_START_TIMEOUT_SECONDS (default 60). The answer then carries
    `runtime: {state (before), started, waited_ms, idle_minutes}`; a start still going when the time is up
    answers `error_code: "runtime_starting"` (not stored), a `failed` runtime `runtime_not_running` with
    `runtime.last_error`."""
    service = build_mcp_connection_service(get_database_engine(request), policy, runtime)
    return JSONResponse(content=await service.test(principal.tenant_id, server_id))


@router.post(
    "/mcp-servers/{server_id}/tools/{tool}/runs",
    responses={409: {"model": ErrorDetail}},
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": ToolRunCreate.model_json_schema()}},
        }
    },
)
async def run_mcp_tool(
    server_id: uuid.UUID,
    tool: str,
    principal: PrincipalDep,  # before the body: 401 wins over 422
    body: Annotated[ToolRunCreate, Depends(_tool_run_body)],
    service: McpConnectionServiceDep,
) -> JSONResponse:
    """Run one tool of a registered MCP server and record it on the tool-run ledger (the same runs,
    list/get endpoints and fields as POST /tools/{id}/runs; `tool_id` is `mcp:<server id>/<tool>`).

    The call is made here, within the connection test's bounded time (EAP_MCP_TEST_TIMEOUT_SECONDS),
    with the server's decrypted headers, and answers 200 with the finished run ({run_id, status,
    ...}); read its output and error through GET /runs/{run_id}. 404 server not found (another
    tenant's too) or the server does not offer the tool; 422 server disabled, input too large, or
    input rejected by the tool's input schema (loc/msg/type, never the values; nothing recorded);
    409 `run_id` used by a different request; 503 header secrets without the platform key.
    A connection or call failure is recorded as a failed run, still answered 200 (a call that was
    sent and got no answer, or that raised after it may have been sent: needs_attention, the tool may
    have run; no exception text is stored), and a repeated `run_id` returns the recorded
    run without calling the tool again."""
    trace_id, traceparent = current_trace_context()
    run = await service.run_tool(principal.tenant_id, server_id, tool, body.input, body.run_id, trace_id, traceparent)
    return JSONResponse(content=run)
