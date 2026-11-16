"""MCP server registry routes (milestone 3, task 3.4).

- POST   /mcp-servers              register (201); 409 when the name is taken in the tenant
- GET    /mcp-servers              the tenant's servers, by name
- GET    /mcp-servers/{id}         one server
- PATCH  /mcp-servers/{id}         change name, url, enabled and/or replace all headers
- DELETE /mcp-servers/{id}         delete (204), with its secrets
- POST   /mcp-servers/{id}/test    connect, list tools, record last_status (200 with status ok|error)
- POST   /mcp-servers/{id}/tools/{tool}/runs   run one tool of the server as a recorded tool run

Every lookup is tenant-scoped: another tenant's or an unknown id is a 404. Only remote servers over
Streamable HTTP; the URL must pass the outbound policy (eap_secrets.outbound) on create and on every
change (422 with the policy's code). Header values are write-only: they are encrypted with the
platform key (EAP_SECRETS_KEY) and bound to (tenant, server, header name); responses list header
names only. Writing or using header secrets without the key answers 503 with how to set it. Request
bodies are validated here rather than by FastAPI so a validation error never echoes a header value.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

import anyio
from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from eap_observability import log_failure
from eap_secrets import (
    KEY_ENV,
    DecryptionFailed,
    Keyring,
    SecretsError,
    SecretsKeyMissing,
    load_keyring,
    mcp_header_context,
)
from eap_secrets.outbound import OutboundBlocked, OutboundPolicy

from eap_api.auth import Principal, current_principal
from eap_api.database import get_session
from eap_api.mcp_servers.connection import call_server_tool, connection_timeout_s, list_server_tools
from eap_api.mcp_servers.models import TRANSPORT_STREAMABLE_HTTP, McpServerModel, McpServerSecretModel
from eap_api.request_guard import GuardedJsonRoute
from eap_api.routes.runs import StoresDep
from eap_api.routes.tools import MAX_TOOL_INPUT_BYTES, ToolRunCreate, _canonical_json, _tool_run_body
from eap_api.schemas import ErrorDetail
from eap_observability import current_trace_context
from eap_run_store import RunIdConflict
from eap_schemas import mcp_tool_name

log = logging.getLogger("eap_api.mcp_servers")

SessionDep = Annotated[Session, Depends(get_session)]
PrincipalDep = Annotated[Principal, Depends(current_principal)]

NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"  # no ':' or '/': agents reference mcp:<server>/<tool>
MAX_HEADERS = 20
MAX_HEADER_VALUE = 8192
REGISTRATION_DNS_TIMEOUT_S = 5.0
_HEADER_NAME = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,128}$")
# Set by the HTTP client or the MCP transport; a tenant may not override them.
_RESERVED_HEADERS = frozenset({
    "host", "content-length", "content-type", "transfer-encoding", "connection", "keep-alive",
    "proxy-connection", "te", "trailer", "upgrade", "accept", "accept-encoding",
    "mcp-session-id", "mcp-protocol-version", "last-event-id",
})

router = APIRouter(
    tags=["mcp-servers"],
    responses={404: {"model": ErrorDetail}, 422: {"model": ErrorDetail}, 503: {"model": ErrorDetail}},
    route_class=GuardedJsonRoute,
)


class McpServerCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(pattern=NAME_PATTERN)]
    url: str
    transport: Literal["streamable_http"] = TRANSPORT_STREAMABLE_HTTP
    enabled: bool = True
    headers: Any = None  # validated by _validate_headers, never by pydantic (no value echo)


class McpServerUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(pattern=NAME_PATTERN)] | None = None
    url: str | None = None
    enabled: bool | None = None
    headers: Any = None


def check_secrets_configuration() -> None:
    """At API startup: a malformed EAP_SECRETS_KEY fails startup; an unset one only disables
    header secrets (those requests answer 503 explaining how to set it)."""
    try:
        load_keyring()
    except SecretsKeyMissing:
        log.warning("%s is not set; MCP server header secrets are unavailable until it is", KEY_ENV)


def get_outbound_policy() -> OutboundPolicy:
    """The outbound policy from the environment (EAP_MCP_DEV_ALLOW_HOSTS); tests override it."""
    return OutboundPolicy.from_env()


PolicyDep = Annotated[OutboundPolicy, Depends(get_outbound_policy)]


def _keyring() -> Keyring:
    try:
        return load_keyring()
    except SecretsError as exc:  # messages carry no key material
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from None


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail)


def _parse(model: type[BaseModel], payload: Any) -> BaseModel:
    """Validate without echoing input: only locations and pydantic's fixed messages are returned."""
    if not isinstance(payload, dict):
        raise _unprocessable("body must be a JSON object")
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        parts = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors(include_input=False)]
        raise _unprocessable("; ".join(parts)) from None


def _validate_headers(headers: Any) -> dict[str, str]:
    """Header name (lowercased) -> value. Messages name the entry's position, never its content."""
    if not isinstance(headers, dict):
        raise _unprocessable("headers must be an object of header name to string value")
    if len(headers) > MAX_HEADERS:
        raise _unprocessable(f"headers: at most {MAX_HEADERS} entries")
    result: dict[str, str] = {}
    for i, (name, value) in enumerate(headers.items(), start=1):
        if not _HEADER_NAME.match(name):
            raise _unprocessable(f"headers: entry {i} has an invalid header name")
        lowered = name.lower()
        if lowered in _RESERVED_HEADERS:
            raise _unprocessable(f"headers: entry {i} is a header the MCP client sets itself")
        if lowered in result:
            raise _unprocessable(f"headers: entry {i} repeats a header name (names are case-insensitive)")
        if not isinstance(value, str) or not value:
            raise _unprocessable(f"headers: entry {i} must have a non-empty string value")
        if len(value) > MAX_HEADER_VALUE:
            raise _unprocessable(f"headers: entry {i} value is longer than {MAX_HEADER_VALUE} characters")
        if any(not (0x20 <= ord(c) <= 0x7E or c == "\t") for c in value):
            raise _unprocessable(f"headers: entry {i} value has characters not allowed in a header")
        result[lowered] = value
    return result


async def _check_url(policy: OutboundPolicy, url: str) -> None:
    try:
        with anyio.fail_after(REGISTRATION_DNS_TIMEOUT_S):
            await policy.check(url)
    except TimeoutError:
        raise OutboundBlocked("host_unresolvable") from None


def _check_url_sync(policy: OutboundPolicy, url: str) -> None:
    try:
        anyio.from_thread.run(_check_url, policy, url)
    except OutboundBlocked as blocked:
        raise _unprocessable(f"url refused by the outbound policy: {blocked.code}") from None


def _secret_context(tenant_id: str, server_id: str, header_name: str) -> bytes:
    return mcp_header_context(tenant_id, server_id, header_name)  # shared with the worker


def _seal(ring: Keyring, tenant_id: str, server_id: str, headers: dict[str, str]) -> list[McpServerSecretModel]:
    rows = []
    for name, value in headers.items():
        sealed = ring.encrypt(value, context=_secret_context(tenant_id, server_id, name))
        rows.append(McpServerSecretModel(
            tenant_id=tenant_id, server_id=server_id, header_name=name,
            ciphertext=sealed.ciphertext, key_id=sealed.key_id,
        ))
    return rows


def _header_names(db: Session, tenant_id: str, server_id: str) -> list[str]:
    return list(db.scalars(
        select(McpServerSecretModel.header_name)
        .where(McpServerSecretModel.tenant_id == tenant_id, McpServerSecretModel.server_id == server_id)
        .order_by(McpServerSecretModel.header_name)
    ))


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _server_json(server: McpServerModel, header_names: list[str]) -> dict[str, Any]:
    return {
        "id": server.id,
        "name": server.name,
        "url": server.url,
        "transport": server.transport,
        "enabled": server.enabled,
        "header_names": header_names,
        "created_at": _iso(server.created_at),
        "updated_at": _iso(server.updated_at),
        "last_checked_at": _iso(server.last_checked_at),
        "last_status": server.last_status,
        "last_error_code": server.last_error_code,
    }


def _get_or_404(db: Session, tenant_id: str, server_id: uuid.UUID) -> McpServerModel:
    server = db.scalar(
        select(McpServerModel).where(McpServerModel.tenant_id == tenant_id, McpServerModel.id == str(server_id))
    )
    if server is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="MCP server not found")
    return server


def _commit_or_409(db: Session) -> None:
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="an MCP server with this name already exists"
        ) from None


@router.post("/mcp-servers", status_code=status.HTTP_201_CREATED, responses={409: {"model": ErrorDetail}})
def create_mcp_server(
    principal: PrincipalDep, db: SessionDep, policy: PolicyDep, payload: Annotated[Any, Body()] = None
) -> JSONResponse:
    body = _parse(McpServerCreate, payload)
    assert isinstance(body, McpServerCreate)
    headers = _validate_headers(body.headers) if body.headers is not None else {}
    _check_url_sync(policy, body.url)
    ring = _keyring() if headers else None
    tenant_id = principal.tenant_id
    server = McpServerModel(
        id=str(uuid.uuid4()), tenant_id=tenant_id, name=body.name, url=body.url,
        transport=TRANSPORT_STREAMABLE_HTTP, enabled=body.enabled,
    )
    db.add(server)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="an MCP server with this name already exists"
        ) from None
    if ring is not None:
        db.add_all(_seal(ring, tenant_id, server.id, headers))
    _commit_or_409(db)
    return JSONResponse(status_code=status.HTTP_201_CREATED, content=_server_json(server, sorted(headers)))


@router.get("/mcp-servers")
def list_mcp_servers(principal: PrincipalDep, db: SessionDep) -> JSONResponse:
    tenant_id = principal.tenant_id
    servers = db.scalars(
        select(McpServerModel).where(McpServerModel.tenant_id == tenant_id).order_by(McpServerModel.name)
    ).all()
    names: dict[str, list[str]] = {}
    for server_id, header_name in db.execute(
        select(McpServerSecretModel.server_id, McpServerSecretModel.header_name)
        .where(McpServerSecretModel.tenant_id == tenant_id)
        .order_by(McpServerSecretModel.header_name)
    ):
        names.setdefault(server_id, []).append(header_name)
    return JSONResponse(content=[_server_json(s, names.get(s.id, [])) for s in servers])


@router.get("/mcp-servers/{server_id}")
def get_mcp_server(server_id: uuid.UUID, principal: PrincipalDep, db: SessionDep) -> JSONResponse:
    server = _get_or_404(db, principal.tenant_id, server_id)
    return JSONResponse(content=_server_json(server, _header_names(db, principal.tenant_id, server.id)))


@router.patch("/mcp-servers/{server_id}", responses={409: {"model": ErrorDetail}})
def update_mcp_server(
    server_id: uuid.UUID,
    principal: PrincipalDep,
    db: SessionDep,
    policy: PolicyDep,
    payload: Annotated[Any, Body()] = None,
) -> JSONResponse:
    """Fields left out stay as they are. `headers` replaces the whole set ({} removes all). Changing
    the url or headers clears the last test result, which no longer describes the server."""
    body = _parse(McpServerUpdate, payload)
    assert isinstance(body, McpServerUpdate)
    tenant_id = principal.tenant_id
    server = _get_or_404(db, tenant_id, server_id)
    headers = _validate_headers(body.headers) if body.headers is not None else None
    if body.url is not None:
        _check_url_sync(policy, body.url)
    ring = _keyring() if headers else None

    if body.name is not None:
        server.name = body.name
    if body.enabled is not None:
        server.enabled = body.enabled
    if body.url is not None and body.url != server.url:
        server.url = body.url
        server.last_status = server.last_error_code = server.last_checked_at = None
    if headers is not None:
        db.execute(delete(McpServerSecretModel).where(
            McpServerSecretModel.tenant_id == tenant_id, McpServerSecretModel.server_id == server.id
        ))
        if ring is not None:
            db.add_all(_seal(ring, tenant_id, server.id, headers))
        server.last_status = server.last_error_code = server.last_checked_at = None
    server.updated_at = datetime.now(timezone.utc)
    _commit_or_409(db)
    db.refresh(server)
    return JSONResponse(content=_server_json(server, _header_names(db, tenant_id, server.id)))


@router.delete("/mcp-servers/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_mcp_server(server_id: uuid.UUID, principal: PrincipalDep, db: SessionDep) -> Response:
    server = _get_or_404(db, principal.tenant_id, server_id)
    db.delete(server)  # mcp_server_secrets rows go with it (ON DELETE CASCADE)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _load_for_test(request: Request, tenant_id: str, server_id: uuid.UUID) -> tuple[str, list[tuple[str, bytes, str]]]:
    with Session(request.app.state.database_engine) as db:
        server = _get_or_404(db, tenant_id, server_id)
        secrets = db.execute(
            select(McpServerSecretModel.header_name, McpServerSecretModel.ciphertext, McpServerSecretModel.key_id)
            .where(McpServerSecretModel.tenant_id == tenant_id, McpServerSecretModel.server_id == server.id)
        ).all()
        return server.url, [(name, bytes(ct), key_id) for name, ct, key_id in secrets]


def _record_result(request: Request, tenant_id: str, server_id: uuid.UUID, outcome: str, code: str | None) -> None:
    with Session(request.app.state.database_engine) as db:
        server = db.scalar(
            select(McpServerModel).where(McpServerModel.tenant_id == tenant_id, McpServerModel.id == str(server_id))
        )
        if server is None:  # deleted while testing
            return
        server.last_checked_at = datetime.now(timezone.utc)
        server.last_status = outcome
        server.last_error_code = code
        db.commit()


@router.post("/mcp-servers/{server_id}/test")
async def run_mcp_server_test(
    server_id: uuid.UUID, request: Request, principal: PrincipalDep, policy: PolicyDep
) -> JSONResponse:
    """Connect over Streamable HTTP, initialize and list tools, within a bounded time
    (EAP_MCP_TEST_TIMEOUT_SECONDS, default 10, at most 30). Answers 200 with
    {status: "ok", tools: [{name, description, input_schema}]} or
    {status: "error", error_code, tools: []}; the outcome is stored as last_status/last_error_code."""
    tenant_id = principal.tenant_id
    url, secrets = await run_in_threadpool(_load_for_test, request, tenant_id, server_id)

    headers: dict[str, str] = {}
    code: str | None = None
    if secrets:
        ring = _keyring()
        try:
            for name, ciphertext, key_id in secrets:
                headers[name] = ring.decrypt(ciphertext, key_id, context=_secret_context(tenant_id, str(server_id), name))
        except DecryptionFailed as exc:
            code = "secret_unreadable"
            log_failure(log, "mcp server secret could not be decrypted", exc, code=code,
                        tenant_id=tenant_id, server_id=str(server_id))

    if code is None:
        result = await list_server_tools(url, headers, policy, connection_timeout_s())
        headers.clear()
        if result.status == "ok":
            await run_in_threadpool(_record_result, request, tenant_id, server_id, "ok", None)
            return JSONResponse(content={"status": "ok", "tools": result.tools})
        code = result.error_code or "connection_failed"
        assert result.error is not None
        log_failure(log, "mcp server connection test failed", result.error, code=code,
                    tenant_id=tenant_id, server_id=str(server_id))

    await run_in_threadpool(_record_result, request, tenant_id, server_id, "error", code)
    return JSONResponse(content={"status": "error", "error_code": code, "tools": []})


# --- running one tool of a server ------------------------------------------------------------------

MCP_TOOL_ID_PREFIX = "mcp:"
TOOL_ERROR_MESSAGE = "The MCP server reported that this tool call failed. Details are withheld."


def mcp_run_tool_id(server_id: str, tool: str) -> str:
    """The ledger's `tool_id` for a server's tool: `mcp:<server id>/<tool>` (the id, not the name,
    so a rename keeps the history; the built-in tools' ids never start with `mcp:`)."""
    return f"{MCP_TOOL_ID_PREFIX}{server_id}/{tool}"


def _input_errors(schema: dict[str, Any], args: dict[str, Any]) -> list[dict[str, Any]]:
    """The tool's own input schema applied to `args`, as FastAPI-style loc/msg/type entries that
    never carry the rejected values. A schema that cannot be applied checks nothing (the server
    decides)."""
    from jsonschema import Draft202012Validator

    errors: list[dict[str, Any]] = []
    try:
        validator = Draft202012Validator(schema)
        found = sorted(validator.iter_errors(args), key=lambda e: [str(p) for p in e.absolute_path])
    except Exception:  # noqa: BLE001 - a malformed or unresolvable schema
        return []
    for err in found[:20]:
        loc: list[Any] = ["body", "input", *err.absolute_path]
        if err.validator == "required" and isinstance(err.instance, dict):
            for name in err.validator_value:
                if name not in err.instance:
                    errors.append({"loc": [*loc, name], "msg": "Field required", "type": "missing"})
        elif err.validator == "type":
            errors.append({"loc": loc, "msg": f"Input should be of type {err.validator_value}", "type": "type_error"})
        else:
            errors.append({"loc": loc, "msg": f"Input violates the schema's {err.validator} rule", "type": "schema_error"})
    return errors


def _run_body(run: dict[str, Any], status_value: str | None = None) -> dict[str, Any]:
    return {
        "run_id": str(run["id"]),
        "kind": "tool",
        "tool_id": run["tool_id"],
        "status": status_value or run["status"],
        "display_status": status_value or run["display_status"],
        "trace_id": run["trace_id"],
    }


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
    request: Request,
    principal: PrincipalDep,  # before the body: 401 wins over 422
    body: Annotated[ToolRunCreate, Depends(_tool_run_body)],
    stores: StoresDep,
    policy: PolicyDep,
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
    tenant_id = principal.tenant_id
    run_store, event_log = stores
    run_id = body.run_id or uuid.uuid4()
    sid = str(server_id)
    tool_id = mcp_run_tool_id(sid, tool)

    def load() -> tuple[str, str, bool, list[tuple[str, bytes, str]]]:
        with Session(request.app.state.database_engine) as db:
            server = _get_or_404(db, tenant_id, server_id)
            secrets = db.execute(
                select(McpServerSecretModel.header_name, McpServerSecretModel.ciphertext, McpServerSecretModel.key_id)
                .where(McpServerSecretModel.tenant_id == tenant_id, McpServerSecretModel.server_id == server.id)
            ).all()
            return server.name, server.url, server.enabled, [(n, bytes(c), k) for n, c, k in secrets]

    name, url, enabled, secrets = await run_in_threadpool(load)
    if not enabled:
        raise _unprocessable("MCP server is disabled")
    input_json = _canonical_json(body.input)
    if len(input_json.encode("utf-8")) > MAX_TOOL_INPUT_BYTES:
        raise _unprocessable(f"input must be at most {MAX_TOOL_INPUT_BYTES // 1024} KiB as JSON")

    existing = await run_in_threadpool(run_store.get_run, tenant_id, run_id)
    if existing is not None:
        if existing["kind"] != "tool" or existing["tool_id"] != tool_id or existing["input"] != input_json:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="run_id is already used by a different run request"
            )
        return JSONResponse(content=_run_body(existing))  # the recorded run; the tool is never called twice

    headers: dict[str, str] = {}
    code: str | None = None
    if secrets:
        ring = _keyring()
        try:
            for hname, ciphertext, key_id in secrets:
                headers[hname] = ring.decrypt(ciphertext, key_id, context=_secret_context(tenant_id, sid, hname))
        except DecryptionFailed as exc:
            code = "secret_unreadable"
            log_failure(log, "mcp server secret could not be decrypted", exc, code=code,
                        tenant_id=tenant_id, server_id=sid)

    timeout = connection_timeout_s()
    schema: dict[str, Any] = {}
    if code is None:
        listed = await list_server_tools(url, headers, policy, timeout)
        if listed.status == "ok":
            found = next((t for t in listed.tools if t["name"] == tool), None)
            if found is None:
                headers.clear()
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="MCP tool not found")
            schema = found["input_schema"]
            errors = _input_errors(schema, body.input)
            if errors:
                headers.clear()
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=errors)
        else:
            code = listed.error_code or "connection_failed"
            assert listed.error is not None
            log_failure(log, "mcp tool listing failed", listed.error, code=code, tenant_id=tenant_id, server_id=sid)

    trace_id, traceparent = current_trace_context()
    # Shielded from the create on: a client that disconnects must not leave a recorded run unfinished.
    with anyio.CancelScope(shield=True):
        try:
            _, created = await run_in_threadpool(
                lambda: run_store.create_tool_run(
                    tenant_id, tool_id, input_json, run_id, trace_id=trace_id, traceparent=traceparent
                )
            )
        except RunIdConflict:
            headers.clear()
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="run_id is already in use")
        if not created:  # a concurrent request with the same run_id owns this run
            run = await run_in_threadpool(run_store.get_run, tenant_id, run_id)
            headers.clear()
            return JSONResponse(content=_run_body(run or {"id": run_id, "tool_id": tool_id, "status": "running",
                                                           "display_status": "running", "trace_id": trace_id}))

        finished = False
        end_recorded = False
        called = False
        final_status, output, error, error_code = "failed", None, None, code
        try:
            call_data = {"tool_call_id": "tool_run", "tool_name": mcp_tool_name(name, tool), "tool_id": tool_id}

            def record(key: str, type_: str, data: dict[str, Any]) -> None:
                event_log.append(tenant_id, run_id, key, type_, data)

            await run_in_threadpool(run_store.mark_running, tenant_id, run_id)
            await run_in_threadpool(record, "start", "run_started", {"kind": "tool", "tool_id": tool_id, "input": body.input})

            content: Any = None
            if code is None:
                await run_in_threadpool(record, "tool:tool_run:call", "tool_call", {**call_data, "args": body.input})
                called = True
                try:
                    result = await call_server_tool(url, headers, policy, timeout, tool, body.input)
                except Exception as exc:  # the ledger row must never stay `running`
                    # No exception text is stored: it can carry a URL or a header value. The type and
                    # code are logged. The request may have gone out before the raise, so the tool may
                    # have run: the same outcome as a sent call that got no answer.
                    log_failure(log, "mcp tool call raised", exc, code="mcp_call_failed",
                                tenant_id=tenant_id, server_id=sid, run_id=str(run_id))
                    final_status, error_code = "needs_attention", "side_effect_outcome_unknown"
                    error = "a tool's external side effect may or may not have happened; check the external system"
                    result = None
                if result is None:
                    pass
                elif result.status == "ok":
                    content = result.content
                    final_status = "completed"
                    output = _canonical_json(content)
                else:
                    error_code = result.error_code or "call_failed"
                    if result.error is not None:
                        log_failure(log, "mcp tool call failed", result.error, code=error_code,
                                    tenant_id=tenant_id, server_id=sid, run_id=str(run_id))
                    if error_code == "tool_error":
                        error = TOOL_ERROR_MESSAGE
                    elif result.sent:
                        final_status, error_code = "needs_attention", "side_effect_outcome_unknown"
                        error = "a tool's external side effect may or may not have happened; check the external system"
            headers.clear()
            if error is None and final_status == "failed":
                error = f"MCP tool '{name}/{tool}' is unavailable ({error_code})"

            if final_status == "completed":
                await run_in_threadpool(record, "tool:tool_run:result", "tool_result", {**call_data, "status": "ok", "content": content})
                await run_in_threadpool(record, "end", "run_completed", {"output": output, "result": content})
                end_recorded = True
            else:
                if called:
                    await run_in_threadpool(record, "tool:tool_run:result", "tool_result",
                                            {**call_data, "status": "error", "error": error, "error_code": error_code})
                await run_in_threadpool(record, "end", "run_failed",
                                        {"status": final_status, "error": error, "error_code": error_code})
                end_recorded = True
            await run_in_threadpool(run_store.set_status, tenant_id, run_id, final_status, output, error)
            finished = True
            run = await run_in_threadpool(run_store.get_run, tenant_id, run_id)
            assert run is not None
            return JSONResponse(content=_run_body(run))
        finally:
            headers.clear()
            if not finished:  # a store error must not leave the row `running` (best effort)
                if end_recorded:  # the end event fixes the status: finish with what it says
                    fb_status, fb_output, fb_error, fb_code = final_status, output, error, error_code
                elif called:  # the call went out, so the tool may have run
                    fb_status, fb_output, fb_code = "needs_attention", None, "side_effect_outcome_unknown"
                    fb_error = "a tool's external side effect may or may not have happened; check the external system"
                else:
                    fb_status, fb_output, fb_code = "failed", None, "mcp_call_failed"
                    fb_error = f"MCP tool '{name}/{tool}' is unavailable (mcp_call_failed)"
                try:
                    # A terminal status needs the run's `end` event: write both in one transaction.
                    written = await run_in_threadpool(
                        lambda: run_store.finish_with_end(
                            tenant_id, run_id, fb_status, fb_output, fb_error,
                            end_data={"error_code": fb_code},
                        )
                    )
                    if not written:
                        log.warning("mcp tool run fallback did not write the status: run_id=%s", run_id)
                except Exception as exc:  # noqa: BLE001
                    log_failure(log, "mcp tool run could not be marked failed", exc, code="mcp_call_failed",
                                tenant_id=tenant_id, server_id=sid, run_id=str(run_id))
