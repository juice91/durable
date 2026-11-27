"""The MCP server registry: register, list, read, change, delete and install from the catalog.

A server is external (a remote Streamable HTTP URL that passes the outbound policy) or managed (a platform-run
pod, see eap_api.services.mcp_runtime). Header values are write-only: encrypted with the platform key
(EAP_SECRETS_KEY), bound to (tenant, server, header name); a result lists header names only. Request bodies are
validated here, not by FastAPI, so a validation error never echoes a header value.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

import anyio
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from eap_api.mcp_servers import managed
from eap_api.mcp_servers.models import (
    TRANSPORT_STREAMABLE_HTTP,
    McpRuntimeModel,
    McpServerModel,
    McpServerSecretModel,
)
from eap_api.services import access as acl
from eap_api.services.access import Access, AccessBound
from eap_api.services.mcp_runtime import McpRuntimeService, get_server_or_404, runtime_of
from eap_api.services.revision_conflict import stale_revision
from eap_mcp_runtime import handle_for
from eap_run_store.definition_errors import (
    DefinitionConflict,
    DefinitionInvalid,
    DefinitionNotFound,
    DefinitionUnavailable,
)
from eap_run_store.mcp_catalog import CatalogStore
from eap_run_store.revisions import RevisionMismatch, bump_row
from eap_run_store.secrets import SecretStore
from eap_secrets import Keyring, SecretsError, load_keyring, mcp_header_context
from eap_secrets.outbound import OutboundBlocked, OutboundPolicy

log = logging.getLogger("eap_api.mcp_servers")

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


class McpServerCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(pattern=NAME_PATTERN)]
    url: str
    transport: Literal["streamable_http"] = TRANSPORT_STREAMABLE_HTTP
    enabled: bool = True
    headers: Any = None  # validated by validate_headers, never by pydantic (no value echo)


class McpServerUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(pattern=NAME_PATTERN)] | None = None
    url: str | None = None
    enabled: bool | None = None
    keep_warm: bool | None = None  # managed servers: the idle sweep never stops the runtime
    headers: Any = None
    env: Any = None  # managed servers: {VARIABLE: value | "secret:<name>"}, validated by managed.check_env


class FromCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    catalog_name: Annotated[str, Field(min_length=1, max_length=300)]
    package_index: Annotated[int, Field(ge=0)] | None = None
    remote_index: Annotated[int, Field(ge=0)] | None = None
    name: Annotated[str, Field(pattern=NAME_PATTERN)] | None = None
    env: Any = None  # {VARIABLE: value}, validated by managed.check_env (no value echo)
    headers: Any = None  # remote entries only


class _EnvChange(BaseModel):
    """A validated env update: what to put where. Plain values go to the package document, secrets and
    references to sealed rows (a reference also to the document's `secret_refs`)."""

    plain: dict[str, str]
    secret: dict[str, str]
    refs: dict[str, str]

    @property
    def names(self) -> set[str]:
        return set(self.plain) | set(self.secret) | set(self.refs)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def unprocessable(detail: str) -> DefinitionInvalid:
    return DefinitionInvalid(detail)


def keyring() -> Keyring:
    try:
        return load_keyring()
    except SecretsError as exc:  # messages carry no key material
        raise DefinitionUnavailable(str(exc)) from None


def secret_context(tenant_id: str, server_id: str, header_name: str) -> bytes:
    return mcp_header_context(tenant_id, server_id, header_name)  # shared with the worker


def parse(model: type[BaseModel], payload: Any) -> Any:
    """Validate without echoing input: only locations and pydantic's fixed messages are returned."""
    if not isinstance(payload, dict):
        raise unprocessable("body must be a JSON object")
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        parts = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors(include_input=False)]
        raise unprocessable("; ".join(parts)) from None


def validate_headers(headers: Any) -> dict[str, str]:
    """Header name (lowercased) -> value. Messages name the entry's position, never its content."""
    if not isinstance(headers, dict):
        raise unprocessable("headers must be an object of header name to string value")
    if len(headers) > MAX_HEADERS:
        raise unprocessable(f"headers: at most {MAX_HEADERS} entries")
    result: dict[str, str] = {}
    for i, (name, value) in enumerate(headers.items(), start=1):
        if not _HEADER_NAME.match(name):
            raise unprocessable(f"headers: entry {i} has an invalid header name")
        lowered = name.lower()
        if lowered in _RESERVED_HEADERS:
            raise unprocessable(f"headers: entry {i} is a header the MCP client sets itself")
        if lowered in result:
            raise unprocessable(f"headers: entry {i} repeats a header name (names are case-insensitive)")
        if not isinstance(value, str) or not value:
            raise unprocessable(f"headers: entry {i} must have a non-empty string value")
        if len(value) > MAX_HEADER_VALUE:
            raise unprocessable(f"headers: entry {i} value is longer than {MAX_HEADER_VALUE} characters")
        if any(not (0x20 <= ord(c) <= 0x7E or c == "\t") for c in value):
            raise unprocessable(f"headers: entry {i} value has characters not allowed in a header")
        result[lowered] = value
    return result


async def _check_url(policy: OutboundPolicy, url: str) -> None:
    try:
        with anyio.fail_after(REGISTRATION_DNS_TIMEOUT_S):
            await policy.check(url)
    except TimeoutError:
        raise OutboundBlocked("host_unresolvable") from None


def check_url_sync(policy: OutboundPolicy, url: str) -> None:
    try:
        anyio.from_thread.run(_check_url, policy, url)
    except OutboundBlocked as blocked:
        raise unprocessable(f"url refused by the outbound policy: {blocked.code}") from None


def seal(ring: Keyring, tenant_id: str, server_id: str, headers: dict[str, str]) -> list[McpServerSecretModel]:
    rows = []
    for name, value in headers.items():
        sealed = ring.encrypt(value, context=secret_context(tenant_id, server_id, name))
        rows.append(McpServerSecretModel(
            tenant_id=tenant_id, server_id=server_id, header_name=name,
            ciphertext=sealed.ciphertext, key_id=sealed.key_id,
        ))
    return rows


def _name_taken() -> DefinitionConflict:
    return DefinitionConflict("an MCP server with this name already exists")


def _default_name(catalog_name: str) -> str:
    base = re.sub(r"[^A-Za-z0-9_.-]+", "-", catalog_name.rsplit("/", 1)[-1]).strip("-._") or "server"
    return base[:64]


def server_json(
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
        "keep_warm": server.keep_warm,
        "header_names": header_names,
        "created_at": _iso(server.created_at),
        "updated_at": _iso(server.updated_at),
        "last_checked_at": _iso(server.last_checked_at),
        "last_status": server.last_status,
        "last_error_code": server.last_error_code,
        "revision": server.revision,
    }


class McpServerService(AccessBound):
    """Registered MCP servers (design 2.2: admin creates, edits and deletes; viewers read; `run` is use)."""

    def __init__(
        self, db: Session, engine: Engine, policy: OutboundPolicy, runtime: McpRuntimeService, catalog: CatalogStore,
        access: Access | None = None,
    ) -> None:
        self._access = access  # None: no caller to check
        self._db = db
        self._engine = engine
        self._policy = policy
        self._runtime = runtime
        self._catalog = catalog

    # --- views -------------------------------------------------------------------------------------

    def _header_names(self, tenant_id: str, server_id: str) -> list[str]:
        return list(self._db.scalars(
            select(McpServerSecretModel.header_name)
            .where(McpServerSecretModel.tenant_id == tenant_id, McpServerSecretModel.server_id == server_id,
                   McpServerSecretModel.kind == "header")
            .order_by(McpServerSecretModel.header_name)
        ))

    def _env_names_by_server(self, tenant_id: str) -> dict[str, list[str]]:
        """Names of a managed server's environment variables, sealed ones included (never values)."""
        db = self._db
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

    def _json_of(self, tenant_id: str, server: McpServerModel) -> dict[str, Any]:
        row = runtime_of(self._db, tenant_id, server.id)
        return server_json(server, self._header_names(tenant_id, server.id), row,
                           self._env_names_by_server(tenant_id).get(server.id, []) if row else [])

    def _commit_or_409(self) -> None:
        try:
            self._db.commit()
        except IntegrityError:
            self._db.rollback()
            raise _name_taken() from None

    def _flush_or_409(self) -> None:
        try:
            self._db.flush()
        except IntegrityError:
            self._db.rollback()
            raise _name_taken() from None

    # --- reads -------------------------------------------------------------------------------------

    def _require(self, action: str, server_id: uuid.UUID | str) -> None:
        acl.require(self._access, action, "mcp_server", str(server_id))

    def list(self, tenant_id: str) -> list[dict[str, Any]]:
        db = self._db
        servers = db.scalars(
            select(McpServerModel).where(McpServerModel.tenant_id == tenant_id).order_by(McpServerModel.name)
        ).all()
        allowed = set(acl.keep(acl.visible(self._access, "mcp_server"), [s.id for s in servers]))
        servers = [s for s in servers if s.id in allowed]
        names: dict[str, list[str]] = {}
        for server_id, header_name in db.execute(
            select(McpServerSecretModel.server_id, McpServerSecretModel.header_name)
            .where(McpServerSecretModel.tenant_id == tenant_id, McpServerSecretModel.kind == "header")
            .order_by(McpServerSecretModel.header_name)
        ):
            names.setdefault(server_id, []).append(header_name)
        runtimes = {r.server_id: r for r in db.scalars(
            select(McpRuntimeModel).where(McpRuntimeModel.tenant_id == tenant_id))}
        envs = self._env_names_by_server(tenant_id)
        return [server_json(s, names.get(s.id, []), runtimes.get(s.id), envs.get(s.id, [])) for s in servers]

    def get(self, tenant_id: str, server_id: uuid.UUID) -> dict[str, Any]:
        self._require("read", server_id)
        return self._json_of(tenant_id, get_server_or_404(self._db, tenant_id, server_id))

    # --- writes ------------------------------------------------------------------------------------

    def create(self, tenant_id: str, payload: Any) -> dict[str, Any]:
        acl.require(self._access, "create", "mcp_server", None)
        body = parse(McpServerCreate, payload)
        headers = validate_headers(body.headers) if body.headers is not None else {}
        check_url_sync(self._policy, body.url)
        ring = keyring() if headers else None
        db = self._db
        server = McpServerModel(
            id=str(uuid.uuid4()), tenant_id=tenant_id, name=body.name, url=body.url,
            transport=TRANSPORT_STREAMABLE_HTTP, enabled=body.enabled,
        )
        db.add(server)
        self._flush_or_409()
        if ring is not None:
            db.add_all(seal(ring, tenant_id, server.id, headers))
        self._commit_or_409()
        acl.register(self._access, "mcp_server", server.id)  # before the response: the creator owns it
        return server_json(server, sorted(headers))

    def _plan_env_change(self, tenant_id: str, server: McpServerModel, env: Any) -> _EnvChange:
        if not isinstance(env, dict):
            raise unprocessable("env must be an object of variable name to string value")
        row = runtime_of(self._db, tenant_id, server.id)
        entry = self._catalog.get(row.catalog_name) if row is not None else None
        package = None
        if row is not None and entry is not None:
            doc = row.package if isinstance(row.package, dict) else {}
            package = next((p for p in entry.packages if p.identifier == doc.get("identifier")), None)
        if package is None:
            raise unprocessable("env: this server's catalog entry is no longer available, so its variables cannot be checked")
        try:
            plain, secret, refs = managed.check_env(package, env, partial=True)
            managed.require_refs(SecretStore(self._engine, None), tenant_id, refs)
        except ValueError as exc:  # messages name a variable or a secret, never a value
            raise unprocessable(str(exc)) from None
        return _EnvChange(plain=plain, secret=secret, refs=refs)

    def _apply_env_change(self, tenant_id: str, server: McpServerModel, change: _EnvChange) -> None:
        db = self._db
        row = runtime_of(db, tenant_id, server.id)
        assert row is not None
        names = change.names
        sealed = {**change.secret, **managed.ref_secrets(change.refs)}
        ring = keyring() if sealed else None
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

    def update(self, tenant_id: str, server_id: uuid.UUID, payload: Any, expected_revision: int | None) -> dict[str, Any]:
        """Fields left out stay as they are. `headers` replaces the whole set ({} removes all). Changing the url
        or headers clears the last test result. A managed server takes `env`: its pod is restarted so the new
        values are projected. A stale `expected_revision` is a Conflict (412) carrying the current server."""
        self._require("edit", server_id)
        db = self._db
        body = parse(McpServerUpdate, payload)
        server = get_server_or_404(db, tenant_id, server_id)

        def stale() -> Any:
            db.refresh(server)
            return stale_revision(expected_revision, self._json_of(tenant_id, server), server.revision)

        if expected_revision is not None and server.revision != expected_revision:
            raise stale()
        if server.runtime == "managed" and (body.url is not None or body.headers is not None):
            raise unprocessable("a managed server has no url or headers; its endpoint is the platform's")
        if body.env is not None and server.runtime != "managed":
            raise unprocessable("env: only a managed server takes environment variables")
        if body.keep_warm is not None and server.runtime != "managed":
            raise unprocessable("keep_warm: only a managed server has a runtime to keep running")
        env_change = self._plan_env_change(tenant_id, server, body.env) if body.env is not None else None
        headers = validate_headers(body.headers) if body.headers is not None else None
        if body.url is not None:
            check_url_sync(self._policy, body.url)
        ring = keyring() if headers else None

        try:
            bump_row(db, server, expected_revision)
        except RevisionMismatch:
            db.rollback()
            raise stale() from None
        if body.name is not None:
            server.name = body.name
        if body.enabled is not None:
            server.enabled = body.enabled
        if body.keep_warm is not None:
            server.keep_warm = body.keep_warm
        if body.url is not None and body.url != server.url:
            server.url = body.url
            server.last_status = server.last_error_code = server.last_checked_at = None
        if headers is not None:
            db.execute(delete(McpServerSecretModel).where(
                McpServerSecretModel.tenant_id == tenant_id, McpServerSecretModel.server_id == server.id,
                McpServerSecretModel.kind == "header",
            ))
            if ring is not None:
                db.add_all(seal(ring, tenant_id, server.id, headers))
            server.last_status = server.last_error_code = server.last_checked_at = None
        if env_change is not None:
            self._apply_env_change(tenant_id, server, env_change)
        server.updated_at = datetime.now(timezone.utc)
        self._commit_or_409()
        db.refresh(server)
        if env_change is not None and server.enabled:
            row = runtime_of(db, tenant_id, server.id)
            if row is not None and row.state in managed.RESTARTABLE:
                self._runtime.restart_runtime(server, row)
        return self._json_of(tenant_id, server)

    def delete(self, tenant_id: str, server_id: uuid.UUID) -> None:
        """Delete the server and its secrets; a managed server's CR, Secret and NetworkPolicy go first.
        Re-running a delete that failed or crashed midway finishes it: the server is disabled before its
        objects are removed (nothing starts it meanwhile), removing a missing object is fine, and the rows
        are removed last. BadGateway when the cluster could not be reached (nothing is lost; retry)."""
        self._require("archive", server_id)
        db = self._db
        server = get_server_or_404(db, tenant_id, server_id)
        row = runtime_of(db, tenant_id, server.id)
        if row is not None:
            if server.enabled:
                server.enabled = False
                db.commit()
            self._runtime.delete_pod(row, str(server_id))
        db.delete(server)  # mcp_server_secrets and mcp_runtimes rows go with it (ON DELETE CASCADE)
        db.commit()
        acl.unregister(self._access, "mcp_server", str(server_id))

    # --- the catalog: install a registry entry -----------------------------------------------------

    def install_from_catalog(self, tenant_id: str, payload: Any) -> dict[str, Any]:
        """Install a mirrored registry entry. A package becomes a managed server (the pod is started at once);
        a remote registers as an external server with the remote's URL and optional `headers`."""
        db = self._db
        acl.require(self._access, "create", "mcp_server", None)
        body = parse(FromCatalog, payload)
        entry = self._catalog.get(body.catalog_name)
        if entry is None:
            raise DefinitionNotFound("catalog entry not found")
        if entry.status != "active":
            raise unprocessable("this catalog entry is no longer offered by the registry")
        if body.package_index is not None and body.remote_index is not None:
            raise unprocessable("give package_index or remote_index, not both")
        as_remote = body.remote_index is not None or (body.package_index is None and not entry.packages)
        name = body.name or _default_name(entry.name)
        server_id = str(uuid.uuid4())

        if as_remote:
            index = body.remote_index or 0
            if index >= len(entry.remotes):
                raise unprocessable("remote_index: this entry has no such remote")
            remote = entry.remotes[index]
            url = remote.get("url") if isinstance(remote, dict) else None
            if not isinstance(url, str) or str(remote.get("type", "")).lower() != "streamable-http":
                raise unprocessable("remote: only Streamable HTTP remotes can be registered")
            if body.env:
                raise unprocessable("env: a remote entry takes headers, not environment variables")
            headers = validate_headers(body.headers) if body.headers is not None else {}
            check_url_sync(self._policy, url)
            ring = keyring() if headers else None
            db.add(McpServerModel(id=server_id, tenant_id=tenant_id, name=name, url=url,
                                  transport=TRANSPORT_STREAMABLE_HTTP, enabled=True))
            self._flush_or_409()
            if ring is not None:
                db.add_all(seal(ring, tenant_id, server_id, headers))
            self._commit_or_409()
            acl.register(self._access, "mcp_server", server_id)
            return self._json_of(tenant_id, get_server_or_404(db, tenant_id, server_id))

        index = body.package_index or 0
        if index >= len(entry.packages):
            raise unprocessable("package_index: this entry has no such package")
        package = entry.packages[index]
        if body.headers:
            raise unprocessable("headers: a managed server takes environment variables, not headers")
        env = body.env if body.env is not None else {}
        if not isinstance(env, dict):
            raise unprocessable("env must be an object of variable name to string value")
        try:
            plain, secret, refs = managed.check_env(package, env)
            managed.require_refs(SecretStore(self._engine, None), tenant_id, refs)
            image, document = managed.package_document(package, getattr(entry, "meta", None))
        except ValueError as exc:  # check_env messages and UnsupportedPackage (a ValueError) carry no values
            raise unprocessable(str(exc)) from None
        document["env"] = {**document["env"], **plain}
        if refs:
            document["secret_refs"] = dict(refs)  # {VARIABLE: secret name}
        sealed = {**secret, **managed.ref_secrets(refs)}
        ring = keyring() if sealed else None
        handle = handle_for(tenant_id, server_id)
        db.add(McpServerModel(id=server_id, tenant_id=tenant_id, name=name, url=None, runtime="managed",
                              transport=TRANSPORT_STREAMABLE_HTTP, enabled=True))
        self._flush_or_409()
        if ring is not None:
            db.add_all(managed.seal_env(ring, tenant_id, server_id, sealed))
        row = McpRuntimeModel(
            id=uuid.uuid4(), tenant_id=tenant_id, server_id=server_id, catalog_name=entry.name, package=document,
            image=image, namespace=handle.namespace, cr_name=handle.cr_name, state="pending",
        )
        db.add(row)
        self._commit_or_409()
        acl.register(self._access, "mcp_server", server_id)
        server = get_server_or_404(db, tenant_id, server_id)
        self._runtime.start_runtime(server, row)
        return self._json_of(tenant_id, server)
