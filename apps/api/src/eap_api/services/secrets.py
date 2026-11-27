"""Tenant secrets (write-only values) and the keyring behind them."""

from __future__ import annotations

from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from typing import Annotated

from eap_api.mcp_servers import managed
from eap_api.mcp_servers.models import McpRuntimeModel, McpServerModel
from eap_api.services import access as acl
from eap_api.services.access import Access, AccessBound
from eap_api.services.errors import Conflict, InvalidInput, NotFound, Unavailable
from eap_api.services.mcp_runtime import McpRuntimeService
from eap_run_store.definition_errors import DefinitionInvalid
from eap_run_store.secrets import SecretStore
from eap_run_store.webhooks import WebhookStore
from eap_schemas.secrets import SECRET_VALUE_MAX_CHARS, is_secret_name
from eap_secrets import SecretsError, load_keyring

SECRET_NOT_FOUND = "secret_not_found"
SECRET_NAME_INVALID = "secret_name_invalid"
SECRET_IN_USE = "secret_in_use"


class SecretPut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: Annotated[str, Field(min_length=1, max_length=SECRET_VALUE_MAX_CHARS)]


class SecretInUse(Conflict):
    """A managed MCP server or a webhook trigger refers to the secret; `servers` is `[{server_id, name}]` and
    `triggers` is `[{trigger_id, name}]`."""

    code = SECRET_IN_USE

    def __init__(self, servers: list[dict[str, Any]], triggers: list[dict[str, Any]] | None = None) -> None:
        super().__init__(
            "secret is in use by a managed MCP server" if servers else "secret is in use by a webhook trigger", code=SECRET_IN_USE
        )
        self.servers = servers
        self.triggers = triggers or []


def sealed_secret_store(engine: Engine) -> SecretStore:
    """The SecretStore on the engine and the platform keyring; Unavailable (503) when the key is unset."""
    try:
        ring = load_keyring()
    except SecretsError as exc:  # messages carry no key material
        raise Unavailable(str(exc), code="unavailable") from None
    return SecretStore(engine, ring)


class SecretService(AccessBound):
    """A secret is identified by its name (design 2.3). `read` is its metadata (never a value), `edit` replaces the
    value, `archive` deletes it; creating one is a tenant action (admin)."""

    def __init__(
        self,
        engine: Engine,
        db: Session,
        runtime: McpRuntimeService,
        webhooks: WebhookStore,
        keyed_store: Callable[[Engine], SecretStore] = sealed_secret_store,
        access: Access | None = None,
    ) -> None:
        self._access = access  # None: no caller to check
        self._engine = engine
        self._webhooks = webhooks
        self._db = db
        self._runtime = runtime
        self._keyed_store = keyed_store

    def put(self, tenant_id: str, user_id: str | None, name: str, payload: Any) -> dict[str, Any]:
        """Store or replace the secret, then restart the tenant's managed servers that refer to it. The value
        is parsed here, not by FastAPI, so a validation error never echoes it."""
        if not is_secret_name(name):
            raise InvalidInput("secret name must match ^[a-z][a-z0-9_.-]{0,63}$", code=SECRET_NAME_INVALID)
        if not isinstance(payload, dict):
            raise DefinitionInvalid("body must be a JSON object")
        try:
            body = SecretPut.model_validate(payload)
        except ValidationError as exc:  # locations and fixed messages only, never the input
            parts = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors(include_input=False, include_url=False)]
            raise DefinitionInvalid("; ".join(parts)) from None
        exists = True
        if self._access is not None:
            exists = any(r["name"] == name for r in SecretStore(self._engine, None).names(tenant_id))
            if exists:
                self._access.require("edit", "secret", name)
            else:
                self._access.require("create", "secret", None)
        self._keyed_store(self._engine).put(tenant_id, name, body.value, user_id)
        if not exists:
            acl.register(self._access, "secret", name)  # before the response: the creator owns it
        return {"restarted": self._restart_users(tenant_id, name)}

    def _restart_users(self, tenant_id: str, name: str) -> list[str]:
        """Restart the tenant's enabled managed servers that refer to `name` and have a pod (a stopped or
        pending one picks the new value up when it next starts). Returns the server ids restarted."""
        db = self._db
        restarted: list[str] = []
        for used in managed.servers_using(db, tenant_id).get(name, []):
            server = db.scalar(select(McpServerModel).where(
                McpServerModel.tenant_id == tenant_id, McpServerModel.id == used["server_id"]))
            row = db.scalar(select(McpRuntimeModel).where(
                McpRuntimeModel.tenant_id == tenant_id, McpRuntimeModel.server_id == used["server_id"]))
            if server is None or row is None or not server.enabled or row.state not in managed.RESTARTABLE:
                continue
            if self._runtime.restart_runtime(server, row):
                restarted.append(server.id)
        return restarted

    def _triggers_of(self, tenant_id: str, name: str) -> list[dict[str, Any]]:
        """The live webhook triggers that reference `secret:<name>`: a secret a trigger needs is not deleted from under it."""
        return self._webhooks.triggers_using_secret(tenant_id, name)

    def list(self, tenant_id: str) -> list[dict[str, Any]]:
        rows = SecretStore(self._engine, None).names(tenant_id)  # names never decrypt
        if self._access is not None:
            allowed = set(acl.keep(acl.visible(self._access, "secret"), [r["name"] for r in rows]))
            rows = [r for r in rows if r["name"] in allowed]
        used = managed.servers_using(self._db, tenant_id)
        return [
            {"name": r["name"], "created_at": r["created_at"].isoformat(), "updated_at": r["updated_at"].isoformat(),
             "used_by": used.get(r["name"], [])}
            for r in rows
        ]

    def delete(self, tenant_id: str, name: str) -> None:
        acl.require(self._access, "archive", "secret", name)
        users = managed.servers_using(self._db, tenant_id).get(name, [])
        triggers = self._triggers_of(tenant_id, name)
        if users or triggers:
            raise SecretInUse(users, triggers)
        if not SecretStore(self._engine, None).delete(tenant_id, name):
            raise NotFound("secret not found", code=SECRET_NOT_FOUND)
        acl.unregister(self._access, "secret", name)
