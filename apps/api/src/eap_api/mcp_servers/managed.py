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
A variable may instead be a reference `secret:<name>` to a tenant secret (secret input): the sealed row
then holds the reference text (the worker resolves it when it starts the pod on demand), the package
document's `secret_refs` maps `{VARIABLE: <name>}` (so usage can be listed without decrypting),
and the API resolves it into the projected Kubernetes Secret at start. A tenant reference can only name
that tenant's own secret (SecretStore is keyed by tenant).
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
from eap_run_store.secrets import SecretStore
from eap_schemas import CatalogPackage
from eap_schemas.secrets import SECRET_REF_PREFIX, is_secret_ref
from eap_secrets import Keyring, mcp_env_context
from eap_secrets.redaction import redact, secret_variants

from eap_api.mcp_servers.models import McpRuntimeModel, McpServerModel, McpServerSecretModel

log = logging.getLogger("eap_api.mcp_servers.managed")

ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
MAX_ENV_VALUE = 8192
STARTABLE = ("pending", "stopped", "idle", "failed")
RESTARTABLE = ("starting", "running", "idle", "failed")  # a pod that exists or just failed: re-project on change
STOPPABLE = ("pending", "starting", "running", "idle", "failed")
MAX_LOG_TAIL = 1000
DEFAULT_LOG_TAIL = 200
_KEYLIKE = re.compile(
    r"""(?i)\b([A-Za-z0-9_.-]*(?:token|key|secret|password)["']?\s*[:=]\s*)("[^"]*"|'[^']*'|\S+)"""
)  # token=x, token: x, "password":"x y", API_KEY = 'x y'
_KEYLIKE_FLAG = re.compile(r"(?i)(--[A-Za-z0-9_.-]*(?:token|key|secret|password)\s+)(\S+)")  # --api-key x
_BEARER = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]+")  # Authorization: Bearer x
# Variables that change how the process or its loader runs; a catalog entry declaring one is refused.
DENIED_ENV = frozenset({
    "NODE_OPTIONS", "LD_PRELOAD", "LD_LIBRARY_PATH", "PATH", "PYTHONPATH", "PYTHONSTARTUP", "HOME",
})
DENIED_ENV_PREFIXES = ("DYLD_",)


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


def network_of(meta: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The entry meta's `network` block `{allow_host, allow_port, insecure_allow_all}` normalised, or None
    when the entry has none (then the default egress applies). An empty block means no network."""
    raw = meta.get("network") if isinstance(meta, Mapping) else None
    if not isinstance(raw, Mapping):
        return None
    hosts = raw.get("allow_host") or []
    ports = raw.get("allow_port") or []
    return {
        "allow_host": [h for h in hosts if isinstance(h, str) and h.strip()],
        "allow_port": [p for p in ports if isinstance(p, int) and not isinstance(p, bool) and 0 < p < 65536],
        "insecure_allow_all": raw.get("insecure_allow_all") is True,
    }


def package_document(package: CatalogPackage, meta: Mapping[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    """`(image, package document)` for a catalog package; raises UnsupportedPackage. The entry's network
    block (from its `meta`) is carried into the document as `network`; no block, no key."""
    if not package.version:
        raise UnsupportedPackage("the package has no version")
    image, args, env = image_for(PackageSpec(package.registry_type, package.identifier, package.version))
    document: dict[str, Any] = {
        "registry_type": package.registry_type,
        "identifier": package.identifier,
        "version": package.version,
        "transport": package.transport,
        "port": None,
        "args": list(args),
        "env": dict(env),
    }
    network = network_of(meta)
    if network is not None:
        document["network"] = network
    return image, document


class SecretRefMissing(ValueError):
    """An env variable refers to a tenant secret that does not exist (the message names the secret)."""

    def __init__(self, name: str) -> None:
        super().__init__(f"env: secret {name} does not exist")
        self.name = name


def check_env(
    package: CatalogPackage, env: Mapping[str, Any], *, partial: bool = False
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """Split the submitted env into `(plain, secret, refs)` by the package's declaration; `refs` maps a
    variable to the tenant secret name it refers to (`secret:<name>`, any declared variable). `partial`
    skips the required check (an update names only the variables it changes). Raises ValueError with a
    message naming the variable, never its value."""
    declared = {v.name: v for v in package.environment_variables}
    for name in declared:
        if name.upper() in DENIED_ENV or name.upper().startswith(DENIED_ENV_PREFIXES):
            raise ValueError(f"env_name_denied: {name} cannot be set on a managed server")
    plain: dict[str, str] = {}
    secret: dict[str, str] = {}
    refs: dict[str, str] = {}
    for name, value in env.items():
        if not isinstance(name, str) or not ENV_NAME.match(name) or name == PACKAGE_ENV:
            raise ValueError("env: an entry has an invalid variable name")
        if name not in declared:
            raise ValueError(f"env: {name} is not a variable of this package")
        if not isinstance(value, str) or "\x00" in value or len(value) > MAX_ENV_VALUE:
            raise ValueError(f"env: {name} must be a string of at most {MAX_ENV_VALUE} characters")
        if value == "":
            continue
        if value.startswith(SECRET_REF_PREFIX):
            ref = is_secret_ref(value)
            if ref is None:
                raise ValueError(f"env: {name} is not a valid secret reference")
            refs[name] = ref
            continue
        (secret if declared[name].secret else plain)[name] = value
    if not partial:
        for name, spec in declared.items():
            if spec.required and name not in plain and name not in secret and name not in refs:
                raise ValueError(f"env: {name} is required")
    return plain, secret, refs


def require_refs(store: SecretStore, tenant_id: str, refs: Mapping[str, str]) -> None:
    """Every referenced secret must exist for this tenant (SecretMissing otherwise)."""
    if not refs:
        return
    have = {r["name"] for r in store.names(tenant_id)}
    for name in sorted(set(refs.values())):
        if name not in have:
            raise SecretRefMissing(name)


def ref_secrets(refs: Mapping[str, str]) -> dict[str, str]:
    """`{VARIABLE: "secret:<name>"}`: the sealed text of a reference-valued variable."""
    return {var: SECRET_REF_PREFIX + name for var, name in refs.items()}


def refs_of(row: McpRuntimeModel) -> dict[str, str]:
    package = row.package if isinstance(row.package, dict) else {}
    return {v: n for v, n in (package.get("secret_refs") or {}).items() if isinstance(n, str)}


def servers_using(db: Session, tenant_id: str) -> dict[str, list[dict[str, str]]]:
    """`{secret name: [{server_id, name}]}` for the tenant's managed servers, from the package documents
    (no decryption)."""
    rows = db.execute(
        select(McpRuntimeModel, McpServerModel.name)
        .join(McpServerModel, McpServerModel.id == McpRuntimeModel.server_id)
        .where(McpRuntimeModel.tenant_id == tenant_id, McpServerModel.tenant_id == tenant_id)
    ).all()
    used: dict[str, list[dict[str, str]]] = {}
    for runtime_row, server_name in rows:
        for name in sorted(set(refs_of(runtime_row).values())):
            used.setdefault(name, []).append({"server_id": runtime_row.server_id, "name": server_name})
    return used


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


def resolve_env(db: Session, ring: Keyring, tenant_id: str, values: Mapping[str, str], *, strict: bool = True) -> dict[str, str]:
    """Replace reference values (`secret:<name>`) by the tenant's stored secret. A missing secret raises
    SecretRefMissing (`strict`), or keeps the reference text out of the result (not strict: log redaction)."""
    store = SecretStore(db.get_bind(), ring)  # type: ignore[arg-type]
    out: dict[str, str] = {}
    for var, value in values.items():
        ref = is_secret_ref(value)
        if ref is None:
            out[var] = value
            continue
        resolved = store.resolve(tenant_id, ref)
        if resolved is None:
            if strict:
                raise SecretRefMissing(ref)
            continue
        out[var] = resolved
    return out


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
        network=package.get("network"),
    )


def claim(db: Session, row: McpRuntimeModel, new: str, allowed: tuple[str, ...], error: str | None = None) -> bool:
    """Move the row to `new` when it is in one of `allowed` (one conditional UPDATE). False when another
    caller changed it first. Claiming `starting` also records `last_used_at`, so the idle sweep does not
    stop a pod that was just started."""
    now = _now()
    values: dict[str, Any] = {"state": new, "last_error": error, "updated_at": now}
    if new == "starting":
        values["last_used_at"] = now
    result = db.execute(
        update(McpRuntimeModel)
        .where(McpRuntimeModel.id == row.id, McpRuntimeModel.tenant_id == row.tenant_id,
               McpRuntimeModel.state.in_(allowed))
        .values(**values)
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


def finish_stop(db: Session, row: McpRuntimeModel) -> bool:
    """`stopping` to `stopped` after the adapter scaled the pod down (conditional on `stopping`, so a row
    that moved on is left alone). False when it had moved on."""
    return claim(db, row, "stopped", ("stopping",))


def touch(db: Session, row: McpRuntimeModel) -> None:
    """Record a use of a running or idle runtime (what the idle sweep reads)."""
    db.execute(update(McpRuntimeModel).where(
        McpRuntimeModel.id == row.id, McpRuntimeModel.state.in_(("running", "idle"))
    ).values(last_used_at=_now()))
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
        masked = _KEYLIKE.sub(lambda m: f"{m.group(1)}[redacted]", masked)
        masked = _KEYLIKE_FLAG.sub(lambda m: f"{m.group(1)}[redacted]", masked)
        out.append(_BEARER.sub(lambda m: f"{m.group(1)}[redacted]", masked))
    return out


__all__ = [
    "DEFAULT_LOG_TAIL", "RESTARTABLE", "SECRET_REF_PREFIX", "PACKAGE_ENV", "MAX_LOG_TAIL", "STARTABLE", "STOPPABLE", "DENIED_ENV", "finish_stop", "touch", "build_spec", "check_env", "claim",
    "get_mcp_runtime", "handle_of", "open_env", "resolve_env", "SecretRefMissing", "require_refs", "ref_secrets", "refs_of", "servers_using", "package_document", "network_of", "redact_lines", "seal_env", "set_failed",
]
