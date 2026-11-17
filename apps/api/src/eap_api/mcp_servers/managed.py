"""Managed MCP servers (platform-run pods) for the API: install documents, runtime state changes and
log redaction.

The API and the worker share the `pydanticos-mcp` service account, so the API drives the adapter
directly for start, stop, restart, status, logs and delete (there is no `desired_state` column and no
reconcile loop: a state change is claimed with one conditional UPDATE and then performed). The worker
starts a stopped, idle, pending or failed server on demand on the next tool call and stops idle ones
(eap_worker.mcp, mcp_runtime_sweep); both sides claim `starting` the same way, so only one of them
starts a pod.

A runtime row's `package` document is `{registry_type, identifier, version, transport, port, args, env}`:
`env` holds the non-secret variables (plus the runner's package reference), secret variables are only
ever sealed rows of `mcp_server_secrets` (kind `env`) and Kubernetes Secrets written by the adapter.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from eap_mcp_runtime import McpRuntime, RuntimeHandle, RuntimeSpec
from eap_mcp_runtime.images import PACKAGE_ENV, PackageSpec, UnsupportedPackage, image_for
from eap_schemas import CatalogPackage
from eap_secrets import Keyring, mcp_env_context
from eap_secrets.redaction import redact, secret_variants

from eap_api.mcp_servers.models import McpRuntimeModel, McpServerModel, McpServerSecretModel

log = logging.getLogger("eap_api.mcp_servers.managed")

ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
MAX_ENV_VALUE = 8192
STARTABLE = ("pending", "stopped", "idle", "failed")
STOPPABLE = ("pending", "starting", "running", "idle", "failed")
MAX_LOG_TAIL = 1000
DEFAULT_LOG_TAIL = 200
_KEYLIKE = re.compile(r"(?i)\b([A-Za-z0-9_.-]*(?:token|key|secret|password))=\S+")


class _LazyRuntime:
    """The operator adapter, built on first use: an API that never touches a managed server (and a dev
    machine without a cluster) never loads a Kubernetes client."""

    def __init__(self) -> None:
        self._inner: McpRuntime | None = None

    def _get(self) -> McpRuntime:
        if self._inner is None:
            from eap_mcp_runtime import OperatorRuntime

            self._inner = OperatorRuntime()
        return self._inner

    async def start(self, spec: RuntimeSpec) -> RuntimeHandle:
        return await self._get().start(spec)

    async def stop(self, handle: RuntimeHandle) -> None:
        await self._get().stop(handle)

    async def delete(self, handle: RuntimeHandle) -> None:
        await self._get().delete(handle)

    async def status(self, handle: RuntimeHandle) -> Any:
        return await self._get().status(handle)

    async def logs(self, handle: RuntimeHandle, tail: int = 200) -> list[str]:
        return await self._get().logs(handle, tail)

    def endpoint(self, handle: RuntimeHandle) -> str:
        return self._get().endpoint(handle)


_LAZY = _LazyRuntime()


def get_mcp_runtime() -> McpRuntime:
    """The runtime adapter (FastAPI dependency; tests override it)."""
    return _LAZY


def _now() -> datetime:
    return datetime.now(timezone.utc)


def package_document(package: CatalogPackage) -> tuple[str, dict[str, Any]]:
    """`(image, package document)` for a catalog package; raises UnsupportedPackage."""
    if not package.version:
        raise UnsupportedPackage("the package has no version")
    image, args, env = image_for(PackageSpec(package.registry_type, package.identifier, package.version))
    return image, {
        "registry_type": package.registry_type,
        "identifier": package.identifier,
        "version": package.version,
        "transport": package.transport,
        "port": None,
        "args": list(args),
        "env": dict(env),
    }


def check_env(package: CatalogPackage, env: Mapping[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    """Split the submitted env into `(plain, secret)` by the package's declaration. Raises ValueError
    with a message naming the variable, never its value."""
    declared = {v.name: v for v in package.environment_variables}
    plain: dict[str, str] = {}
    secret: dict[str, str] = {}
    for name, value in env.items():
        if not isinstance(name, str) or not ENV_NAME.match(name) or name == PACKAGE_ENV:
            raise ValueError("env: an entry has an invalid variable name")
        if name not in declared:
            raise ValueError(f"env: {name} is not a variable of this package")
        if not isinstance(value, str) or "\x00" in value or len(value) > MAX_ENV_VALUE:
            raise ValueError(f"env: {name} must be a string of at most {MAX_ENV_VALUE} characters")
        if value == "":
            continue
        (secret if declared[name].secret else plain)[name] = value
    for name, spec in declared.items():
        if spec.required and name not in plain and name not in secret:
            raise ValueError(f"env: {name} is required")
    return plain, secret


def seal_env(ring: Keyring, tenant_id: str, server_id: str, secret: Mapping[str, str]) -> list[McpServerSecretModel]:
    rows = []
    for name, value in secret.items():
        sealed = ring.encrypt(value, context=mcp_env_context(tenant_id, server_id, name))
        rows.append(McpServerSecretModel(
            tenant_id=tenant_id, server_id=server_id, kind="env", header_name=name,
            ciphertext=sealed.ciphertext, key_id=sealed.key_id,
        ))
    return rows


def open_env(db: Session, ring: Keyring, tenant_id: str, server_id: str) -> dict[str, str]:
    """The server's env secrets, decrypted (raises DecryptionFailed)."""
    rows = db.execute(
        select(McpServerSecretModel.header_name, McpServerSecretModel.ciphertext, McpServerSecretModel.key_id)
        .where(McpServerSecretModel.tenant_id == tenant_id, McpServerSecretModel.server_id == server_id,
               McpServerSecretModel.kind == "env")
    ).all()
    return {n: ring.decrypt(bytes(c), k, context=mcp_env_context(tenant_id, server_id, n)) for n, c, k in rows}


def build_spec(row: McpRuntimeModel, server: McpServerModel, secrets: Mapping[str, str]) -> RuntimeSpec:
    package = row.package if isinstance(row.package, dict) else {}
    return RuntimeSpec(
        tenant_id=row.tenant_id,
        server_id=row.server_id,
        name=server.name,
        image=row.image,
        args=tuple(package.get("args") or ()),
        env=dict(package.get("env") or {}),
        secret_refs=dict(secrets),
        transport=package.get("transport") or "stdio",
        port=package.get("port"),
    )


def claim(db: Session, row: McpRuntimeModel, new: str, allowed: tuple[str, ...], error: str | None = None) -> bool:
    """Move the row to `new` when it is in one of `allowed` (one conditional UPDATE). False when another
    caller changed it first."""
    result = db.execute(
        update(McpRuntimeModel)
        .where(McpRuntimeModel.id == row.id, McpRuntimeModel.tenant_id == row.tenant_id,
               McpRuntimeModel.state.in_(allowed))
        .values(state=new, last_error=error, updated_at=_now())
    )
    db.commit()
    db.refresh(row)
    return result.rowcount == 1


def set_failed(db: Session, row: McpRuntimeModel, error: str) -> None:
    db.execute(
        update(McpRuntimeModel).where(McpRuntimeModel.id == row.id).values(state="failed", last_error=error, updated_at=_now())
    )
    db.commit()
    db.refresh(row)


def handle_of(row: McpRuntimeModel) -> RuntimeHandle:
    return RuntimeHandle(row.namespace, row.cr_name)


def redact_lines(lines: list[str], secrets: Mapping[str, str]) -> list[str]:
    """Every sealed value (all its encodings) and anything shaped `token=...`, `password=...`, `API_KEY=...`
    masked; the key names stay."""
    variants = secret_variants(secrets.values()) if secrets else []
    out = []
    for line in lines:
        masked = redact(line, variants) if variants else line
        out.append(_KEYLIKE.sub(lambda m: f"{m.group(1)}=[redacted]", masked))
    return out


__all__ = [
    "DEFAULT_LOG_TAIL", "MAX_LOG_TAIL", "STARTABLE", "STOPPABLE", "build_spec", "check_env", "claim",
    "get_mcp_runtime", "handle_of", "open_env", "package_document", "redact_lines", "seal_env", "set_failed",
]
