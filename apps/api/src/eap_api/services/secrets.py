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
from eap_api.services.errors import Conflict, InvalidInput, NotFound, Unavailable
from eap_api.services.mcp_runtime import McpRuntimeService
from eap_run_store.definition_errors import DefinitionInvalid
from eap_run_store.secrets import SecretStore
from eap_schemas.secrets import SECRET_VALUE_MAX_CHARS, is_secret_name
from eap_secrets import SecretsError, load_keyring

SECRET_NOT_FOUND = "secret_not_found"
SECRET_NAME_INVALID = "secret_name_invalid"
SECRET_IN_USE = "secret_in_use"


class SecretPut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: Annotated[str, Field(min_length=1, max_length=SECRET_VALUE_MAX_CHARS)]


class SecretInUse(Conflict):
    """A managed MCP server refers to the secret; `servers` is `[{server_id, name}]`."""

    code = SECRET_IN_USE

    def __init__(self, servers: list[dict[str, Any]]) -> None:
        super().__init__("secret is in use by a managed MCP server", code=SECRET_IN_USE)
        self.servers = servers


def sealed_secret_store(engine: Engine) -> SecretStore:
    """The SecretStore on the engine and the platform keyring; Unavailable (503) when the key is unset."""
    try:
        ring = load_keyring()
    except SecretsError as exc:  # messages carry no key material
        raise Unavailable(str(exc), code="unavailable") from None
    return SecretStore(engine, ring)


class SecretService:
    def __init__(
        self,
        engine: Engine,
        db: Session,
        runtime: McpRuntimeService,
        keyed_store: Callable[[Engine], SecretStore] = sealed_secret_store,
    ) -> None:
        self._engine = engine
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
        self._keyed_store(self._engine).put(tenant_id, name, body.value, user_id)
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

    def list(self, tenant_id: str) -> list[dict[str, Any]]:
        rows = SecretStore(self._engine, None).names(tenant_id)  # names never decrypt
        used = managed.servers_using(self._db, tenant_id)
        return [
            {"name": r["name"], "created_at": r["created_at"].isoformat(), "updated_at": r["updated_at"].isoformat(),
             "used_by": used.get(r["name"], [])}
            for r in rows
        ]

    def delete(self, tenant_id: str, name: str) -> None:
        users = managed.servers_using(self._db, tenant_id).get(name, [])
        if users:
            raise SecretInUse(users)
        if not SecretStore(self._engine, None).delete(tenant_id, name):
            raise NotFound("secret not found", code=SECRET_NOT_FOUND)
