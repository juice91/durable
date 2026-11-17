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
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

import anyio
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import delete, select, update
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
from eap_mcp_runtime import McpRuntime, RuntimeHandle, handle_for
from eap_mcp_runtime.images import UnsupportedPackage
from eap_run_store.mcp_catalog import CatalogStore
from eap_run_store.secrets import SecretStore
from eap_secrets.outbound import OutboundBlocked, OutboundPolicy

from eap_api.auth import Principal, current_principal
from eap_api.database import get_session
from eap_api.mcp_servers.connection import call_server_tool, connection_timeout_s, list_server_tools
from eap_api.mcp_servers import managed
from eap_api.mcp_servers.managed import get_mcp_runtime
from eap_api.mcp_servers.models import (
    TRANSPORT_STREAMABLE_HTTP,
    McpRuntimeModel,
    McpServerModel,
    McpServerSecretModel,
)
from eap_api.request_guard import GuardedJsonRoute
from eap_api.routes.runs import StoresDep
from eap_api.routes.tools import MAX_TOOL_INPUT_BYTES, ToolRunCreate, _canonical_json, _tool_run_body
from eap_api.schemas import ErrorDetail
from eap_observability import current_trace_context
from eap_run_store import RunIdConflict
from eap_schemas import mcp_tool_name, status_for_end

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
    env: Any = None  # managed servers: {VARIABLE: value | "secret:<name>"}, validated by managed.check_env


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
RuntimeDep = Annotated[McpRuntime, Depends(get_mcp_runtime)]


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
        .where(McpServerSecretModel.tenant_id == tenant_id, McpServerSecretModel.server_id == server_id,
               McpServerSecretModel.kind == "header")
        .order_by(McpServerSecretModel.header_name)
    ))


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _runtime_rows(db: Session, tenant_id: str) -> dict[str, McpRuntimeModel]:
    return {r.server_id: r for r in db.scalars(select(McpRuntimeModel).where(McpRuntimeModel.tenant_id == tenant_id))}


def _env_names_by_server(db: Session, tenant_id: str) -> dict[str, list[str]]:
    """Names of a managed server's environment variables, sealed ones included (never values)."""
    names: dict[str, list[str]] = {}
    for server_id, name in db.execute(
        select(McpServerSecretModel.server_id, McpServerSecretModel.header_name)
        .where(McpServerSecretModel.tenant_id == tenant_id, McpServerSecretModel.kind == "env")
    ):
        names.setdefault(server_id, []).append(name)
    for row in db.scalars(select(McpRuntimeModel).where(McpRuntimeModel.tenant_id == tenant_id)):
        plain = (row.package or {}).get("env") or {}
        names.setdefault(row.server_id, []).extend(n for n in plain if n != managed.PACKAGE_ENV)
    return {k: sorted(set(v)) for k, v in names.items()}


def _json_of(db: Session, tenant_id: str, server: McpServerModel) -> dict[str, Any]:
    row = db.scalar(select(McpRuntimeModel).where(
        McpRuntimeModel.tenant_id == tenant_id, McpRuntimeModel.server_id == server.id))
    return _server_json(server, _header_names(db, tenant_id, server.id), row,
                        _env_names_by_server(db, tenant_id).get(server.id, []) if row else [])


def _server_json(
    server: McpServerModel,
    header_names: list[str],
    runtime: McpRuntimeModel | None = None,
    env_names: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "runtime": server.runtime,
        "runtime_state": runtime.state if runtime is not None else None,
        "runtime_error": runtime.last_error if runtime is not None else None,
        "catalog_name": runtime.catalog_name if runtime is not None else None,
        "env_names": env_names or [],
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
        .where(McpServerSecretModel.tenant_id == tenant_id, McpServerSecretModel.kind == "header")
        .order_by(McpServerSecretModel.header_name)
    ):
        names.setdefault(server_id, []).append(header_name)
    runtimes = _runtime_rows(db, tenant_id)
    envs = _env_names_by_server(db, tenant_id)
    return JSONResponse(content=[
        _server_json(s, names.get(s.id, []), runtimes.get(s.id), envs.get(s.id, [])) for s in servers
    ])


@router.get("/mcp-servers/{server_id}")
def get_mcp_server(server_id: uuid.UUID, principal: PrincipalDep, db: SessionDep) -> JSONResponse:
    server = _get_or_404(db, principal.tenant_id, server_id)
    return JSONResponse(content=_json_of(db, principal.tenant_id, server))


@router.patch("/mcp-servers/{server_id}", responses={409: {"model": ErrorDetail}})
def update_mcp_server(
    request: Request,
    server_id: uuid.UUID,
    principal: PrincipalDep,
    db: SessionDep,
    policy: PolicyDep,
    runtime: RuntimeDep,
    payload: Annotated[Any, Body()] = None,
) -> JSONResponse:
    """Fields left out stay as they are. `headers` replaces the whole set ({} removes all). Changing
    the url or headers clears the last test result, which no longer describes the server. A managed
    server takes `env` (`{VARIABLE: value | "secret:<name>"}`, only the variables it changes): the
    server's pod is restarted so the new values are projected (the response carries the resulting
    `runtime_state`). 422 for `env` on an external server, an undeclared variable or a reference to a
    secret the tenant does not have."""
    body = _parse(McpServerUpdate, payload)
    assert isinstance(body, McpServerUpdate)
    tenant_id = principal.tenant_id
    server = _get_or_404(db, tenant_id, server_id)
    if server.runtime == "managed" and (body.url is not None or body.headers is not None):
        raise _unprocessable("a managed server has no url or headers; its endpoint is the platform's")
    if body.env is not None and server.runtime != "managed":
        raise _unprocessable("env: only a managed server takes environment variables")
    env_change = _plan_env_change(request, db, tenant_id, server, body.env) if body.env is not None else None
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
            McpServerSecretModel.tenant_id == tenant_id, McpServerSecretModel.server_id == server.id,
            McpServerSecretModel.kind == "header",
        ))
        if ring is not None:
            db.add_all(_seal(ring, tenant_id, server.id, headers))
        server.last_status = server.last_error_code = server.last_checked_at = None
    if env_change is not None:
        _apply_env_change(db, tenant_id, server, env_change)
    server.updated_at = datetime.now(timezone.utc)
    _commit_or_409(db)
    db.refresh(server)
    if env_change is not None and server.enabled:
        row = _runtime_of(db, tenant_id, server.id)
        if row is not None and row.state in managed.RESTARTABLE:
            restart_runtime(db, server, row, runtime)
    return JSONResponse(content=_json_of(db, tenant_id, server))


@router.delete(
    "/mcp-servers/{server_id}", status_code=status.HTTP_204_NO_CONTENT, responses={502: {"model": ErrorDetail}}
)
def delete_mcp_server(
    server_id: uuid.UUID, principal: PrincipalDep, db: SessionDep, runtime: RuntimeDep
) -> Response:
    """Delete the server and its secrets; a managed server's CR, Secret and NetworkPolicy go first.
    Re-running a delete that failed or crashed midway finishes it: the server is disabled before its
    objects are removed (nothing starts it meanwhile), removing a missing object is fine, and the rows
    are removed last (secrets and the runtime row cascade). 502 when the cluster could not be reached
    (nothing is lost; retry)."""
    tenant_id = principal.tenant_id
    server = _get_or_404(db, tenant_id, server_id)
    row = _runtime_of(db, tenant_id, server.id)
    if row is not None:
        if server.enabled:
            server.enabled = False
            db.commit()
        try:
            anyio.from_thread.run(runtime.delete, managed.handle_of(row))
        except Exception as exc:  # noqa: BLE001 - the cluster is unreachable or refused; rows stay for a retry
            log_failure(log, "mcp runtime delete failed", exc, code="runtime_delete_failed",
                        tenant_id=tenant_id, server_id=str(server_id))
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="the runtime could not be removed from the cluster; the server is disabled, retry the delete",
            ) from None
    db.delete(server)  # mcp_server_secrets and mcp_runtimes rows go with it (ON DELETE CASCADE)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@dataclass(frozen=True)
class _Target:
    """Where a server is reached: an external server's URL, or a managed server's runtime row."""

    url: str | None
    runtime_id: str | None = None
    state: str | None = None
    namespace: str | None = None
    cr_name: str | None = None

    @property
    def managed(self) -> bool:
        return self.runtime_id is not None


def _runtime_of(db: Session, tenant_id: str, server_id: str) -> McpRuntimeModel | None:
    return db.scalar(select(McpRuntimeModel).where(
        McpRuntimeModel.tenant_id == tenant_id, McpRuntimeModel.server_id == server_id))


def _target(db: Session, server: McpServerModel) -> _Target:
    if server.runtime != "managed":
        return _Target(url=server.url)
    row = _runtime_of(db, server.tenant_id, server.id)
    if row is None:
        return _Target(url=None, runtime_id="", state="missing")
    return _Target(url=None, runtime_id=str(row.id), state=row.state, namespace=row.namespace, cr_name=row.cr_name)


def _managed_row(db: Session, tenant_id: str, server_id: uuid.UUID) -> tuple[McpServerModel, McpRuntimeModel]:
    server = _get_or_404(db, tenant_id, server_id)
    row = _runtime_of(db, tenant_id, server.id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="this server has no managed runtime")
    return server, row


class _EnvChange(BaseModel):
    """A validated env update: what to put where. Plain values go to the package document, secrets and
    references to sealed rows (a reference also to the document's `secret_refs`)."""

    plain: dict[str, str]
    secret: dict[str, str]
    refs: dict[str, str]

    @property
    def names(self) -> set[str]:
        return set(self.plain) | set(self.secret) | set(self.refs)


def _plan_env_change(request: Request, db: Session, tenant_id: str, server: McpServerModel, env: Any) -> _EnvChange:
    if not isinstance(env, dict):
        raise _unprocessable("env must be an object of variable name to string value")
    row = _runtime_of(db, tenant_id, server.id)
    entry = CatalogStore(request.app.state.database_engine).get(row.catalog_name) if row is not None else None
    package = None
    if row is not None and entry is not None:
        doc = row.package if isinstance(row.package, dict) else {}
        package = next((p for p in entry.packages if p.identifier == doc.get("identifier")), None)
    if package is None:
        raise _unprocessable("env: this server's catalog entry is no longer available, so its variables cannot be checked")
    try:
        plain, secret, refs = managed.check_env(package, env, partial=True)
        managed.require_refs(SecretStore(request.app.state.database_engine, None), tenant_id, refs)
    except ValueError as exc:  # messages name a variable or a secret, never a value
        raise _unprocessable(str(exc)) from None
    return _EnvChange(plain=plain, secret=secret, refs=refs)


def _apply_env_change(db: Session, tenant_id: str, server: McpServerModel, change: _EnvChange) -> None:
    row = _runtime_of(db, tenant_id, server.id)
    assert row is not None
    names = change.names
    sealed = {**change.secret, **managed.ref_secrets(change.refs)}
    ring = _keyring() if sealed else None
    db.execute(delete(McpServerSecretModel).where(
        McpServerSecretModel.tenant_id == tenant_id, McpServerSecretModel.server_id == server.id,
        McpServerSecretModel.kind == "env", McpServerSecretModel.header_name.in_(names),
    ))
    if ring is not None:
        db.add_all(managed.seal_env(ring, tenant_id, server.id, sealed))
    doc = dict(row.package) if isinstance(row.package, dict) else {}
    doc["env"] = {**{k: v for k, v in (doc.get("env") or {}).items() if k not in names}, **change.plain}
    doc["secret_refs"] = {**{k: v for k, v in managed.refs_of(row).items() if k not in names}, **change.refs}
    row.package = doc  # a new dict, so the JSON column registers the change
    server.last_status = server.last_error_code = server.last_checked_at = None


def restart_runtime(db: Session, server: McpServerModel, row: McpRuntimeModel, runtime: McpRuntime) -> bool:
    """Stop the pod and start it again so a changed env (or a changed tenant secret) is projected. False
    when either step failed (the row is then `failed` with a code) or another caller owned the row."""
    if _stop_runtime(db, row, runtime, managed.RESTARTABLE) != "stopped":
        return False
    return _start_runtime(db, server, row, runtime)


def _start_runtime(db: Session, server: McpServerModel, row: McpRuntimeModel, runtime: McpRuntime) -> bool:
    """Start a pending, stopped, idle or failed runtime: claim `starting` (one conditional UPDATE, so the
    worker's on-demand start and this one never both run), decrypt the env secrets and call the adapter.
    A failure is recorded as `failed` with a code (`secret_unreadable`, `start_failed: <class>`), never
    the exception text, and returns False. The worker moves `starting` to `running` once the pod is ready
    (or GET runtime does)."""
    if not managed.claim(db, row, "starting", managed.STARTABLE):
        return True
    try:
        ring = load_keyring()
        secrets = managed.resolve_env(db, ring, server.tenant_id, managed.open_env(db, ring, server.tenant_id, server.id))
    except managed.SecretRefMissing as exc:
        log.warning("mcp runtime env refers to a missing secret tenant=%s server=%s secret=%s",
                    server.tenant_id, server.id, exc.name)
        managed.set_failed(db, row, "secret_not_found")
        return False
    except SecretsError as exc:
        log_failure(log, "mcp runtime env secret could not be decrypted", exc, code="secret_unreadable",
                    tenant_id=server.tenant_id, server_id=server.id)
        managed.set_failed(db, row, "secret_unreadable")
        return False
    try:
        anyio.from_thread.run(runtime.start, managed.build_spec(row, server, secrets))
    except Exception as exc:  # noqa: BLE001 - reduced to a code
        log_failure(log, "mcp runtime start failed", exc, code="runtime_start_failed",
                    tenant_id=server.tenant_id, server_id=server.id)
        managed.set_failed(db, row, f"start_failed: {type(exc).__name__}")
        return False
    return True


def _stop_runtime(db: Session, row: McpRuntimeModel, runtime: McpRuntime, allowed: tuple[str, ...]) -> str:
    """Stop the pod (its objects stay), the way the idle sweep does: claim `stopping` (so no start can
    claim the row meanwhile), call the adapter, then `stopped` only if the row is still `stopping`.
    Returns `stopped`, `skipped` (another caller owned the row, or it moved on during the stop) or
    `failed` (the adapter raised: the row is `failed` with `stop_failed: <class>`)."""
    if not managed.claim(db, row, "stopping", allowed):
        return "skipped"
    try:
        anyio.from_thread.run(runtime.stop, managed.handle_of(row))
    except Exception as exc:  # noqa: BLE001
        log_failure(log, "mcp runtime stop failed", exc, code="runtime_stop_failed", tenant_id=row.tenant_id)
        managed.set_failed(db, row, f"stop_failed: {type(exc).__name__}")
        return "failed"
    return "stopped" if managed.finish_stop(db, row) else "skipped"


def _reconcile(db: Session, row: McpRuntimeModel, runtime: McpRuntime) -> tuple[bool, str]:
    """Compare a `starting` or `running` row with the pod: ready becomes `running`, failed becomes
    `failed`. Returns (adapter reachable, the adapter's message)."""
    try:
        live = anyio.from_thread.run(runtime.status, managed.handle_of(row))
    except Exception as exc:  # noqa: BLE001
        log_failure(log, "mcp runtime status failed", exc, code="runtime_status_failed",
                    level=logging.INFO, tenant_id=row.tenant_id)
        return False, ""
    values: dict[str, Any] | None = None
    if row.state == "starting" and live.state == "running":
        values = {"state": "running", "endpoint": live.endpoint or None, "last_error": None,
                  "last_used_at": datetime.now(timezone.utc)}
    elif row.state in ("starting", "running", "idle") and live.state == "failed":
        values = {"state": "failed", "last_error": "runtime_failed"}
    if values is not None:
        db.execute(update(McpRuntimeModel).where(
            McpRuntimeModel.id == row.id, McpRuntimeModel.state == row.state
        ).values(updated_at=datetime.now(timezone.utc), **values))
        db.commit()
        db.refresh(row)
    return True, managed.redact_lines([live.message], {})[0] if live.message else ""


def _runtime_env(db: Session, row: McpRuntimeModel) -> list[dict[str, Any]]:
    """`[{name, ref}]`: every variable of the server, `ref` = `secret:<name>` for a stored-secret
    reference and null otherwise. Never a value."""
    refs = {v: managed.SECRET_REF_PREFIX + n for v, n in managed.refs_of(row).items()}
    names = set(refs) | {n for n in ((row.package or {}).get("env") or {}) if n != managed.PACKAGE_ENV}
    names |= set(db.scalars(select(McpServerSecretModel.header_name).where(
        McpServerSecretModel.tenant_id == row.tenant_id, McpServerSecretModel.server_id == row.server_id,
        McpServerSecretModel.kind == "env")))
    return [{"name": n, "ref": refs.get(n)} for n in sorted(names)]


def _runtime_body(row: McpRuntimeModel, reachable: bool = True, message: str = "", env: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    package = row.package or {}
    return {
        "env": env or [],
        "state": row.state,
        "message": message,
        "reachable": reachable,
        "last_error": row.last_error,
        "last_used_at": _iso(row.last_used_at),
        "updated_at": _iso(row.updated_at),
        "image": row.image,
        "catalog_name": row.catalog_name,
        "package": {k: package.get(k) for k in ("registry_type", "identifier", "version", "transport")},
    }


async def _resolve_target(
    request: Request, tenant_id: str, server_id: str, target: _Target, runtime: McpRuntime, policy: OutboundPolicy
) -> tuple[str | None, OutboundPolicy, str | None]:
    """`(url, policy, error_code)` to connect to a server. A managed server is reached at its runtime's
    endpoint under a policy that allows exactly that endpoint; it must be running (a startable one is
    started for the next try, and `runtime_not_running` is the code meanwhile). `running` and `idle` are
    both ready; using one records `last_used_at`."""
    if not target.managed:
        return target.url, policy, None
    if not target.runtime_id:
        return None, policy, "runtime_missing"

    def check() -> str | None:
        with Session(request.app.state.database_engine) as db:
            row = _runtime_of(db, tenant_id, server_id)
            server = db.scalar(select(McpServerModel).where(
                McpServerModel.id == server_id, McpServerModel.tenant_id == tenant_id))
            if row is None or server is None:
                return "runtime_missing"
            if row.state in ("starting", "running", "idle"):
                _reconcile(db, row, runtime)
            elif row.state in managed.STARTABLE and server.enabled:
                _start_runtime(db, server, row, runtime)
            if row.state in ("running", "idle"):
                managed.touch(db, row)
                return None
            return "runtime_not_running"

    code = await run_in_threadpool(check)
    if code is not None:
        return None, policy, code
    url = runtime.endpoint(RuntimeHandle(target.namespace or "", target.cr_name or ""))
    try:
        return url, policy.for_managed_runtime(target.runtime_id, url), None
    except OutboundBlocked:
        return None, policy, "managed_endpoint_invalid"


# --- the catalog: install a registry entry ---------------------------------------------------------


class FromCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    catalog_name: Annotated[str, Field(min_length=1, max_length=300)]
    package_index: Annotated[int, Field(ge=0)] | None = None
    remote_index: Annotated[int, Field(ge=0)] | None = None
    name: Annotated[str, Field(pattern=NAME_PATTERN)] | None = None
    env: Any = None  # {VARIABLE: value}, validated by managed.check_env (no value echo)
    headers: Any = None  # remote entries only


def _default_name(catalog_name: str) -> str:
    base = re.sub(r"[^A-Za-z0-9_.-]+", "-", catalog_name.rsplit("/", 1)[-1]).strip("-._") or "server"
    return base[:64]


@router.post(
    "/mcp-servers/from-catalog",
    status_code=status.HTTP_201_CREATED,
    responses={409: {"model": ErrorDetail}},
)
def install_from_catalog(
    request: Request, principal: PrincipalDep, db: SessionDep, policy: PolicyDep, runtime: RuntimeDep,
    payload: Annotated[Any, Body()] = None,
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
    body = _parse(FromCatalog, payload)
    assert isinstance(body, FromCatalog)
    entry = CatalogStore(request.app.state.database_engine).get(body.catalog_name)
    if entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="catalog entry not found")
    if entry.status != "active":
        raise _unprocessable("this catalog entry is no longer offered by the registry")
    if body.package_index is not None and body.remote_index is not None:
        raise _unprocessable("give package_index or remote_index, not both")
    as_remote = body.remote_index is not None or (body.package_index is None and not entry.packages)
    tenant_id = principal.tenant_id
    name = body.name or _default_name(entry.name)
    server_id = str(uuid.uuid4())

    if as_remote:
        index = body.remote_index or 0
        if index >= len(entry.remotes):
            raise _unprocessable("remote_index: this entry has no such remote")
        remote = entry.remotes[index]
        url = remote.get("url") if isinstance(remote, dict) else None
        if not isinstance(url, str) or str(remote.get("type", "")).lower() != "streamable-http":
            raise _unprocessable("remote: only Streamable HTTP remotes can be registered")
        if body.env:
            raise _unprocessable("env: a remote entry takes headers, not environment variables")
        headers = _validate_headers(body.headers) if body.headers is not None else {}
        _check_url_sync(policy, url)
        ring = _keyring() if headers else None
        db.add(McpServerModel(id=server_id, tenant_id=tenant_id, name=name, url=url,
                              transport=TRANSPORT_STREAMABLE_HTTP, enabled=True))
        _flush_or_409(db)
        if ring is not None:
            db.add_all(_seal(ring, tenant_id, server_id, headers))
        _commit_or_409(db)
        server = _get_or_404(db, tenant_id, uuid.UUID(server_id))
        return JSONResponse(status_code=status.HTTP_201_CREATED, content=_json_of(db, tenant_id, server))

    index = body.package_index or 0
    if index >= len(entry.packages):
        raise _unprocessable("package_index: this entry has no such package")
    package = entry.packages[index]
    if body.headers:
        raise _unprocessable("headers: a managed server takes environment variables, not headers")
    env = body.env if body.env is not None else {}
    if not isinstance(env, dict):
        raise _unprocessable("env must be an object of variable name to string value")
    try:
        plain, secret, refs = managed.check_env(package, env)
        managed.require_refs(SecretStore(request.app.state.database_engine, None), tenant_id, refs)
        image, document = managed.package_document(package, getattr(entry, "meta", None))
    except ValueError as exc:  # check_env messages and UnsupportedPackage (a ValueError) carry no values
        raise _unprocessable(str(exc)) from None
    document["env"] = {**document["env"], **plain}
    if refs:
        document["secret_refs"] = dict(refs)  # {VARIABLE: secret name}
    sealed = {**secret, **managed.ref_secrets(refs)}
    ring = _keyring() if sealed else None
    handle = handle_for(tenant_id, server_id)
    db.add(McpServerModel(id=server_id, tenant_id=tenant_id, name=name, url=None, runtime="managed",
                          transport=TRANSPORT_STREAMABLE_HTTP, enabled=True))
    _flush_or_409(db)
    if ring is not None:
        db.add_all(managed.seal_env(ring, tenant_id, server_id, sealed))
    row = McpRuntimeModel(
        id=uuid.uuid4(), tenant_id=tenant_id, server_id=server_id, catalog_name=entry.name, package=document,
        image=image, namespace=handle.namespace, cr_name=handle.cr_name, state="pending",
    )
    db.add(row)
    _commit_or_409(db)
    server = _get_or_404(db, tenant_id, uuid.UUID(server_id))
    _start_runtime(db, server, row, runtime)
    return JSONResponse(status_code=status.HTTP_201_CREATED, content=_json_of(db, tenant_id, server))


# --- a managed server's runtime ---------------------------------------------------------------------


@router.get("/mcp-servers/{server_id}/runtime", responses={409: {"model": ErrorDetail}})
def get_runtime(server_id: uuid.UUID, principal: PrincipalDep, db: SessionDep, runtime: RuntimeDep) -> JSONResponse:
    """`{state, message, reachable, last_error, last_used_at, updated_at, image, catalog_name, package}`.
    `state` is pending|starting|running|idle|stopped|failed; a `starting` row is compared with the pod
    first. `reachable` false means the cluster could not be asked (the stored state is shown)."""
    _server, row = _managed_row(db, principal.tenant_id, server_id)
    reachable, message = _reconcile(db, row, runtime) if row.state in ("starting", "running", "idle") else (True, "")
    return JSONResponse(content=_runtime_body(row, reachable, message, _runtime_env(db, row)))


@router.post("/mcp-servers/{server_id}/runtime/{action}",
             responses={409: {"model": ErrorDetail}, 502: {"model": ErrorDetail}})
def control_runtime(
    server_id: uuid.UUID,
    action: Literal["start", "stop", "restart"],
    principal: PrincipalDep,
    db: SessionDep,
    runtime: RuntimeDep,
) -> JSONResponse:
    """Start (a pending, stopped, idle or failed pod; otherwise a no-op), stop (scale to zero, objects
    stay; the next tool call starts it again) or restart (stop, then start). Answers the runtime body
    of GET .../runtime. 409 for an external server or a disabled one (start, restart). 502 when the
    adapter (or the sealed env) fails: the row is then `failed` with `last_error` set, read it with
    GET .../runtime; the error text is never returned."""
    server, row = _managed_row(db, principal.tenant_id, server_id)
    if action != "stop" and not server.enabled:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="the server is disabled")
    ok = True
    if action == "start":
        ok = _start_runtime(db, server, row, runtime)
    elif action == "stop":
        ok = _stop_runtime(db, row, runtime, managed.STOPPABLE) != "failed"
    else:
        outcome = _stop_runtime(db, row, runtime, ("pending", "starting", "running", "idle", "stopped", "failed"))
        ok = outcome != "failed"
        if outcome == "stopped":
            ok = _start_runtime(db, server, row, runtime)
    if not ok:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"the runtime could not be {action}ed"
                            if action != "stop" else "the runtime could not be stopped")
    return JSONResponse(content=_runtime_body(row, env=_runtime_env(db, row)))


@router.get("/mcp-servers/{server_id}/runtime/logs", responses={409: {"model": ErrorDetail}, 502: {"model": ErrorDetail}})
def get_runtime_logs(
    server_id: uuid.UUID,
    principal: PrincipalDep,
    db: SessionDep,
    runtime: RuntimeDep,
    tail: Annotated[int, Query(ge=1, le=managed.MAX_LOG_TAIL)] = managed.DEFAULT_LOG_TAIL,
) -> JSONResponse:
    """`{lines: [...], tail}`: the last `tail` (1 to 1000, default 200) log lines of the pod, with every
    sealed env value (any encoding) and `token=`, `key=`, `secret=`, `password=` assignments masked."""
    server, row = _managed_row(db, principal.tenant_id, server_id)
    try:
        lines = anyio.from_thread.run(runtime.logs, managed.handle_of(row), tail)
    except Exception as exc:  # noqa: BLE001
        log_failure(log, "mcp runtime logs failed", exc, code="runtime_logs_failed",
                    tenant_id=principal.tenant_id, server_id=str(server_id))
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="the logs could not be read") from None
    try:
        ring = load_keyring()
        secrets = managed.resolve_env(
            db, ring, principal.tenant_id, managed.open_env(db, ring, principal.tenant_id, server.id), strict=False)
    except SecretsError:
        secrets = {}  # unreadable secrets cannot be matched by value; the pattern mask still applies
    return JSONResponse(content={"lines": managed.redact_lines(list(lines), secrets), "tail": tail})


def _flush_or_409(db: Session) -> None:
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="an MCP server with this name already exists"
        ) from None


def _load_for_test(request: Request, tenant_id: str, server_id: uuid.UUID) -> tuple[_Target, list[tuple[str, bytes, str]]]:
    with Session(request.app.state.database_engine) as db:
        server = _get_or_404(db, tenant_id, server_id)
        secrets = db.execute(
            select(McpServerSecretModel.header_name, McpServerSecretModel.ciphertext, McpServerSecretModel.key_id)
            .where(McpServerSecretModel.tenant_id == tenant_id, McpServerSecretModel.server_id == server.id,
                   McpServerSecretModel.kind == "header")
        ).all()
        return _target(db, server), [(name, bytes(ct), key_id) for name, ct, key_id in secrets]


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
    server_id: uuid.UUID, request: Request, principal: PrincipalDep, policy: PolicyDep, runtime: RuntimeDep
) -> JSONResponse:
    """Connect over Streamable HTTP, initialize and list tools, within a bounded time
    (EAP_MCP_TEST_TIMEOUT_SECONDS, default 10, at most 30). Answers 200 with
    {status: "ok", tools: [{name, description, input_schema}]} or
    {status: "error", error_code, tools: []}; the outcome is stored as last_status/last_error_code."""
    tenant_id = principal.tenant_id
    target, secrets = await run_in_threadpool(_load_for_test, request, tenant_id, server_id)

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

    url: str | None = None
    if code is None:
        # A managed server is reached at its runtime's endpoint and must be running (see _resolve_target).
        url, policy, code = await _resolve_target(request, tenant_id, str(server_id), target, runtime, policy)

    if code is None:
        assert url is not None
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
    runtime: RuntimeDep,
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

    def load() -> tuple[str, _Target, bool, list[tuple[str, bytes, str]]]:
        with Session(request.app.state.database_engine) as db:
            server = _get_or_404(db, tenant_id, server_id)
            secrets = db.execute(
                select(McpServerSecretModel.header_name, McpServerSecretModel.ciphertext, McpServerSecretModel.key_id)
                .where(McpServerSecretModel.tenant_id == tenant_id, McpServerSecretModel.server_id == server.id,
                       McpServerSecretModel.kind == "header")
            ).all()
            return server.name, _target(db, server), server.enabled, [(n, bytes(c), k) for n, c, k in secrets]

    name, target, enabled, secrets = await run_in_threadpool(load)
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
    url: str | None = None
    if code is None:
        url, policy, code = await _resolve_target(request, tenant_id, sid, target, runtime, policy)
    if code is None:
        assert url is not None
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
                        # An end event that committed but raised to us (connection dropped after the
                        # commit) fixes the status, and finish_with_end refused ours: finish with ITS.
                        end = await run_in_threadpool(event_log.get_event, tenant_id, run_id, "end")
                        if end is None:
                            log.warning("mcp tool run fallback did not write the status: run_id=%s", run_id)
                        else:
                            data = end.data or {}
                            out = data.get("output") if isinstance(data.get("output"), str) else None
                            err = data.get("error") if isinstance(data.get("error"), str) else None
                            await run_in_threadpool(
                                lambda: run_store.finish_with_end(
                                    tenant_id, run_id, status_for_end(end.type, data), out, err,
                                )
                            )
                except Exception as exc:  # noqa: BLE001
                    log_failure(log, "mcp tool run could not be marked failed", exc, code="mcp_call_failed",
                                tenant_id=tenant_id, server_id=sid, run_id=str(run_id))
