"""MCP server registry routes (milestone 3, task 3.4).

- POST   /mcp-servers              register (201); 409 when the name is taken in the tenant
- GET    /mcp-servers              the tenant's servers, by name
- GET    /mcp-servers/{id}         one server
- PATCH  /mcp-servers/{id}         change name, url, enabled and/or replace all headers
- DELETE /mcp-servers/{id}         delete (204), with its secrets
- POST   /mcp-servers/{id}/test    connect, list tools, record last_status (200 with status ok|error)

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
from eap_secrets import KEY_ENV, DecryptionFailed, Keyring, SecretsError, SecretsKeyMissing, load_keyring
from eap_secrets.outbound import OutboundBlocked, OutboundPolicy

from eap_api.auth import Principal, current_principal
from eap_api.database import get_session
from eap_api.mcp_servers.connection import connection_timeout_s, list_server_tools
from eap_api.mcp_servers.models import TRANSPORT_STREAMABLE_HTTP, McpServerModel, McpServerSecretModel
from eap_api.schemas import ErrorDetail

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
    return f"{tenant_id}/{server_id}/{header_name}".encode()


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
