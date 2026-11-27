"""Tenant connections to external providers (kb source sync). A connection holds `credential_refs`
(logical name -> `secret:<name>`), never a value; the check resolves the references inside the call and
answers a stable code, never a token, a provider body or an exception text."""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Callable
from urllib.parse import urlsplit

import anyio
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.engine import Engine

from eap_api.services import access as acl
from eap_api.services.access import Access, AccessBound
from eap_api.services.errors import Conflict, InvalidInput, NotFound
from eap_api.services.revision_conflict import stale_revision
from eap_kb_sources.check import check_connection
from eap_kb_sources.errors import CONNECTION_NOT_FOUND, SourceError
from eap_run_store.kb_sources import (
    API_TOKEN_REF_KEYS,
    CONNECTION_KINDS,
    Connection,
    ConnectionInUse,
    ConnectionNameConflict,
    ConnectionStore,
)
from eap_run_store.revisions import RevisionMismatch
from eap_run_store.secrets import SecretStore
from eap_schemas.secrets import is_secret_ref


class ConnectionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=100)]
    kind: str = CONNECTION_KINDS[0]
    base_url: Annotated[str, Field(max_length=500)] | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    credential_refs: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class ConnectionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    base_url: Annotated[str, Field(max_length=500)] | None = None
    config: dict[str, Any] | None = None
    credential_refs: dict[str, Any] | None = None
    enabled: bool | None = None


def view(c: Connection) -> dict[str, Any]:
    checked = c.last_checked_at
    return {
        "id": c.id, "name": c.name, "kind": c.kind, "base_url": c.base_url, "config": c.config,
        "credential_refs": c.credential_refs, "enabled": c.enabled,
        "last_checked_at": checked.isoformat() if checked is not None else None,
        "last_status": c.last_status, "last_error_code": c.last_error_code,
        "created_at": c.created_at.isoformat(), "updated_at": c.updated_at.isoformat(), "revision": c.revision,
    }


def _invalid(code: str, detail: str) -> InvalidInput:
    return InvalidInput(detail, code=code)


def parse_body(payload: Any, model: type[BaseModel]) -> Any:
    """The validated model, or an InvalidInput built from locations and fixed messages (never the input)."""
    if not isinstance(payload, dict):
        raise _invalid("invalid_request", "body must be a JSON object")
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        parts = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors(include_input=False, include_url=False)]
        raise _invalid("invalid_request", "; ".join(parts)) from None


def parse_uuid(value: str) -> str | None:
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def _check_refs(refs: dict[str, Any] | None) -> None:
    if refs is None:
        return
    for key, ref in refs.items():
        if not isinstance(key, str) or not key or is_secret_ref(ref) is None:
            raise _invalid("secret_ref_required", "credential_refs maps logical names to 'secret:<name>' references")


def _check_url(base_url: str | None) -> None:
    if base_url is None:
        return
    parts = urlsplit(base_url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise _invalid("invalid_request", "base_url must be an https URL without credentials")


def _check_provider(config: dict[str, Any] | None) -> None:
    if config is None:
        return
    provider = config.get("provider")
    if not isinstance(provider, str) or not provider:
        raise _invalid("invalid_request", "config.provider is required")


def _not_found() -> NotFound:
    return NotFound("connection not found", code=CONNECTION_NOT_FOUND)


def _name_conflict() -> Conflict:
    return Conflict("a connection with this name exists", code="connection_name_conflict")


class ConnectionService(AccessBound):
    """Source connections (they name the secrets a knowledge-base source reads with): tenant configuration. Anyone who
    can read the tenant lists them (names and references, never a value); changing, deleting or checking one is the
    tenant's `manage_connections` (admin, as for secrets)."""

    def __init__(
        self, engine: Engine, secret_store: Callable[[Engine], SecretStore], access: Access | None = None
    ) -> None:
        self._access = access  # None: no caller to check
        self._engine = engine
        self._store = ConnectionStore(engine)
        self._secret_store = secret_store  # raises Unavailable while the platform key is unset

    def create(self, tenant_id: str, payload: Any) -> dict[str, Any]:
        acl.require(self._access, "manage_connections", "tenant", None)
        body = parse_body(payload, ConnectionCreate)
        if body.kind not in CONNECTION_KINDS:
            raise _invalid("invalid_request", "unknown connection kind")
        if body.kind == "api_token" and not set(body.credential_refs) <= API_TOKEN_REF_KEYS:
            raise _invalid("invalid_request", "an api_token connection takes only the 'token' reference")
        _check_refs(body.credential_refs)
        _check_url(body.base_url)
        _check_provider(body.config)
        try:
            made = self._store.create(
                tenant_id, name=body.name, kind=body.kind, base_url=body.base_url, config=body.config,
                credential_refs=body.credential_refs, enabled=body.enabled,
            )
        except ConnectionNameConflict:
            raise _name_conflict() from None
        return view(made)

    def list(self, tenant_id: str) -> dict[str, Any]:
        acl.require(self._access, "read", "tenant", None)
        return {"items": [view(c) for c in self._store.list(tenant_id)]}

    def get(self, tenant_id: str, connection_id: str) -> dict[str, Any]:
        acl.require(self._access, "read", "tenant", None)
        cid = parse_uuid(connection_id)
        found = self._store.get(tenant_id, cid) if cid else None
        if found is None:
            raise _not_found()
        return view(found)

    def update(self, tenant_id: str, connection_id: str, payload: Any, expected_revision: int | None) -> dict[str, Any]:
        """Change a connection. A stale `expected_revision` is a Conflict (412) carrying the current one."""
        acl.require(self._access, "manage_connections", "tenant", None)
        body = parse_body(payload, ConnectionPatch)
        cid = parse_uuid(connection_id)
        if cid is None:
            raise _not_found()
        _check_refs(body.credential_refs)
        _check_url(body.base_url)
        _check_provider(body.config)

        def stale() -> Conflict:
            found = self._store.get(tenant_id, cid)
            return stale_revision(expected_revision, *((view(found), found.revision) if found is not None else ({}, 0)))

        try:
            if expected_revision is not None:
                now = self._store.get(tenant_id, cid)
                if now is not None and now.revision != expected_revision:  # a missing row is the 404 below
                    raise stale()
            changed = self._store.update(
                tenant_id, cid, name=body.name, base_url=body.base_url, config=body.config,
                credential_refs=body.credential_refs, enabled=body.enabled, expected_revision=expected_revision,
            )
        except RevisionMismatch:
            raise stale() from None
        except ConnectionNameConflict:
            raise _name_conflict() from None
        if changed is None:
            raise _not_found()
        return view(changed)

    def delete(self, tenant_id: str, connection_id: str) -> None:
        acl.require(self._access, "manage_connections", "tenant", None)
        cid = parse_uuid(connection_id)
        if cid is None:
            raise _not_found()
        try:
            removed = self._store.delete(tenant_id, cid)
        except ConnectionInUse:
            raise Conflict("a source refers to this connection", code="connection_in_use") from None
        if not removed:
            raise _not_found()

    async def check(self, tenant_id: str, connection_id: str) -> dict[str, Any]:
        """Check a connection against its provider: `{ok, checked_at, error_code}`."""
        await anyio.to_thread.run_sync(acl.require, self._access, "manage_connections", "tenant", None)
        cid = parse_uuid(connection_id)
        if cid is None:
            raise _not_found()
        secrets = self._secret_store(self._engine)
        try:
            return await check_connection(self._engine, tenant_id, cid, secrets)
        except SourceError as exc:
            if exc.code == CONNECTION_NOT_FOUND:
                raise _not_found() from None
            raise
