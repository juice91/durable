"""A managed MCP server's runtime (the platform-run pod): start, stop, restart, reconcile with the pod, read
logs, and the endpoint a connection test or a tool call is made against.

Every state change is claimed with one conditional UPDATE and then performed (eap_api.mcp_servers.managed), so
this code and the worker's on-demand start never both start a pod. A failure is recorded on the row as a code,
never as exception text.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from collections.abc import Callable
from typing import Any, Literal

import anyio
from sqlalchemy import select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from eap_api.mcp_servers import managed
from eap_api.mcp_servers.models import McpRuntimeModel, McpServerModel, McpServerSecretModel
from eap_mcp_runtime import McpRuntime, RuntimeHandle
from eap_run_store.definition_errors import DefinitionConflict, DefinitionError, DefinitionNotFound
from eap_secrets import SecretsError, load_keyring
from eap_secrets.outbound import OutboundBlocked, OutboundPolicy

log = logging.getLogger("eap_api.mcp_servers")

START_TIMEOUT_ENV = "EAP_MCP_TEST_START_TIMEOUT_SECONDS"
DEFAULT_START_TIMEOUT_S = 60.0
MAX_START_TIMEOUT_S = 300.0
START_POLL_S = 0.5  # module constant so tests can shorten it


class BadGateway(DefinitionError):
    """The cluster (or the adapter) could not do what was asked; nothing is lost, retry."""

    status_code = 502


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def start_timeout_s() -> float:
    """EAP_MCP_TEST_START_TIMEOUT_SECONDS, clamped to [0, 300]; 60 when unset or unreadable."""
    try:
        value = float(os.environ.get(START_TIMEOUT_ENV, DEFAULT_START_TIMEOUT_S))
    except ValueError:
        value = DEFAULT_START_TIMEOUT_S
    return min(max(value, 0.0), MAX_START_TIMEOUT_S)


def get_server_or_404(db: Session, tenant_id: str, server_id: uuid.UUID | str) -> McpServerModel:
    server = db.scalar(
        select(McpServerModel).where(McpServerModel.tenant_id == tenant_id, McpServerModel.id == str(server_id))
    )
    if server is None:
        raise DefinitionNotFound("MCP server not found")
    return server


def runtime_of(db: Session, tenant_id: str, server_id: str) -> McpRuntimeModel | None:
    return db.scalar(select(McpRuntimeModel).where(
        McpRuntimeModel.tenant_id == tenant_id, McpRuntimeModel.server_id == server_id))


@dataclass(frozen=True)
class Target:
    """Where a server is reached: an external server's URL, or a managed server's runtime row."""

    url: str | None
    runtime_id: str | None = None
    state: str | None = None
    namespace: str | None = None
    cr_name: str | None = None

    @property
    def managed(self) -> bool:
        return self.runtime_id is not None


def target_of(db: Session, server: McpServerModel) -> Target:
    if server.runtime != "managed":
        return Target(url=server.url)
    row = runtime_of(db, server.tenant_id, server.id)
    if row is None:
        return Target(url=None, runtime_id="", state="missing")
    return Target(url=None, runtime_id=str(row.id), state=row.state, namespace=row.namespace, cr_name=row.cr_name)


def runtime_env(db: Session, row: McpRuntimeModel) -> list[dict[str, Any]]:
    """`[{name, ref}]`: every variable of the server, `ref` = `secret:<name>` for a stored-secret
    reference and null otherwise. Never a value."""
    refs = {v: managed.SECRET_REF_PREFIX + n for v, n in managed.refs_of(row).items()}
    names = set(refs) | {n for n in ((row.package or {}).get("env") or {}) if n != managed.PACKAGE_ENV}
    names |= set(db.scalars(select(McpServerSecretModel.header_name).where(
        McpServerSecretModel.tenant_id == row.tenant_id, McpServerSecretModel.server_id == row.server_id,
        McpServerSecretModel.kind == "env")))
    return [{"name": n, "ref": refs.get(n)} for n in sorted(names)]


def runtime_body(
    row: McpRuntimeModel, reachable: bool = True, message: str = "", env: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
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


class McpRuntimeService:
    """The runtime lifecycle of a tenant's managed servers, on one database session and the runtime adapter.

    `db` may be None for the operations that open sessions of their own (`resolve_target`, `start_and_wait`)."""

    def __init__(
        self, db: Session | None, engine: Engine, runtime: McpRuntime, log_failure: Callable[..., None]
    ) -> None:
        self.log_failure = log_failure  # eap_observability's, given by the provider (its package root imports the web)
        self._db = db
        self._engine = engine
        self._runtime = runtime

    # --- state changes -----------------------------------------------------------------------------

    def restart_runtime(self, server: McpServerModel, row: McpRuntimeModel) -> bool:
        """Stop the pod and start it again so a changed env (or a changed tenant secret) is projected. False
        when either step failed (the row is then `failed` with a code) or another caller owned the row."""
        if self.stop_runtime(row, managed.RESTARTABLE) != "stopped":
            return False
        return self.start_runtime(server, row)

    def start_runtime(self, server: McpServerModel, row: McpRuntimeModel) -> bool:
        """Start a pending, stopped, idle or failed runtime: claim `starting` (one conditional UPDATE, so the
        worker's on-demand start and this one never both run), decrypt the env secrets and call the adapter.
        A failure is recorded as `failed` with a code (`secret_unreadable`, `start_failed: <class>`), never
        the exception text, and returns False. The worker moves `starting` to `running` once the pod is ready
        (or GET runtime does)."""
        db = self._db
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
            self.log_failure(log, "mcp runtime env secret could not be decrypted", exc, code="secret_unreadable",
                        tenant_id=server.tenant_id, server_id=server.id)
            managed.set_failed(db, row, "secret_unreadable")
            return False
        try:
            anyio.from_thread.run(self._runtime.start, managed.build_spec(row, server, secrets))
        except Exception as exc:  # noqa: BLE001 - reduced to a code
            self.log_failure(log, "mcp runtime start failed", exc, code="runtime_start_failed",
                        tenant_id=server.tenant_id, server_id=server.id)
            managed.set_failed(db, row, f"start_failed: {type(exc).__name__}")
            return False
        return True

    def stop_runtime(self, row: McpRuntimeModel, allowed: tuple[str, ...]) -> str:
        """Stop the pod (its objects stay), the way the idle sweep does: claim `stopping` (so no start can
        claim the row meanwhile), call the adapter, then `stopped` only if the row is still `stopping`.
        Returns `stopped`, `skipped` (another caller owned the row, or it moved on during the stop) or
        `failed` (the adapter raised: the row is `failed` with `stop_failed: <class>`)."""
        db = self._db
        if not managed.claim(db, row, "stopping", allowed):
            return "skipped"
        try:
            anyio.from_thread.run(self._runtime.stop, managed.handle_of(row))
        except Exception as exc:  # noqa: BLE001
            self.log_failure(log, "mcp runtime stop failed", exc, code="runtime_stop_failed", tenant_id=row.tenant_id)
            managed.set_failed(db, row, f"stop_failed: {type(exc).__name__}")
            return "failed"
        return "stopped" if managed.finish_stop(db, row) else "skipped"

    def delete_pod(self, row: McpRuntimeModel, server_id: str) -> None:
        """Remove the server's CR, Secret and NetworkPolicy; BadGateway when the cluster could not be reached."""
        try:
            anyio.from_thread.run(self._runtime.delete, managed.handle_of(row))
        except Exception as exc:  # noqa: BLE001 - the cluster is unreachable or refused; rows stay for a retry
            self.log_failure(log, "mcp runtime delete failed", exc, code="runtime_delete_failed",
                        tenant_id=row.tenant_id, server_id=server_id)
            raise BadGateway(
                "the runtime could not be removed from the cluster; the server is disabled, retry the delete"
            ) from None

    def reconcile(self, row: McpRuntimeModel) -> tuple[bool, str]:
        """Compare a `starting` or `running` row with the pod: ready becomes `running`, failed becomes
        `failed`. Returns (adapter reachable, the adapter's message)."""
        db = self._db
        try:
            live = anyio.from_thread.run(self._runtime.status, managed.handle_of(row))
        except Exception as exc:  # noqa: BLE001
            self.log_failure(log, "mcp runtime status failed", exc, code="runtime_status_failed",
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

    # --- the routes' operations --------------------------------------------------------------------

    def _managed_row(self, tenant_id: str, server_id: uuid.UUID) -> tuple[McpServerModel, McpRuntimeModel]:
        server = get_server_or_404(self._db, tenant_id, server_id)
        row = runtime_of(self._db, tenant_id, server.id)
        if row is None:
            raise DefinitionConflict("this server has no managed runtime")
        return server, row

    def get_runtime(self, tenant_id: str, server_id: uuid.UUID) -> dict[str, Any]:
        """`{state, message, reachable, last_error, ...}`; a `starting`, `running` or `idle` row is compared
        with the pod first. `reachable` false means the cluster could not be asked (the stored state is shown)."""
        _server, row = self._managed_row(tenant_id, server_id)
        reachable, message = self.reconcile(row) if row.state in ("starting", "running", "idle") else (True, "")
        return runtime_body(row, reachable, message, runtime_env(self._db, row))

    def control(self, tenant_id: str, server_id: uuid.UUID, action: Literal["start", "stop", "restart"]) -> dict[str, Any]:
        """Start (a pending, stopped, idle or failed pod; otherwise a no-op), stop (scale to zero) or restart
        (stop, then start). 409 for an external server or a disabled one (start, restart); BadGateway when the
        adapter (or the sealed env) fails: the row is then `failed` with `last_error` set."""
        server, row = self._managed_row(tenant_id, server_id)
        if action != "stop" and not server.enabled:
            raise DefinitionConflict("the server is disabled")
        ok = True
        if action == "start":
            ok = self.start_runtime(server, row)
        elif action == "stop":
            ok = self.stop_runtime(row, managed.STOPPABLE) != "failed"
        else:
            outcome = self.stop_runtime(row, ("pending", "starting", "running", "idle", "stopped", "failed"))
            ok = outcome != "failed"
            if outcome == "stopped":
                ok = self.start_runtime(server, row)
        if not ok:
            raise BadGateway(f"the runtime could not be {action}ed" if action != "stop" else "the runtime could not be stopped")
        return runtime_body(row, env=runtime_env(self._db, row))

    def logs(self, tenant_id: str, server_id: uuid.UUID, tail: int) -> dict[str, Any]:
        """`{lines, tail}`: the last `tail` log lines of the pod, with every sealed env value (any encoding) and
        `token=`, `key=`, `secret=`, `password=` assignments masked."""
        db = self._db
        server, row = self._managed_row(tenant_id, server_id)
        try:
            lines = anyio.from_thread.run(self._runtime.logs, managed.handle_of(row), tail)
        except Exception as exc:  # noqa: BLE001
            self.log_failure(log, "mcp runtime logs failed", exc, code="runtime_logs_failed",
                        tenant_id=tenant_id, server_id=str(server_id))
            raise BadGateway("the logs could not be read") from None
        try:
            ring = load_keyring()
            secrets = managed.resolve_env(
                db, ring, tenant_id, managed.open_env(db, ring, tenant_id, server.id), strict=False)
        except SecretsError:
            secrets = {}  # unreadable secrets cannot be matched by value; the pattern mask still applies
        return {"lines": managed.redact_lines(list(lines), secrets), "tail": tail}

    # --- reaching a server for a test or a tool call -----------------------------------------------

    async def resolve_target(
        self, tenant_id: str, server_id: str, target: Target, policy: OutboundPolicy
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
            with Session(self._engine) as db:
                svc = McpRuntimeService(db, self._engine, self._runtime, self.log_failure)
                row = runtime_of(db, tenant_id, server_id)
                server = db.scalar(select(McpServerModel).where(
                    McpServerModel.id == server_id, McpServerModel.tenant_id == tenant_id))
                if row is None or server is None:
                    return "runtime_missing"
                if row.state in ("starting", "running", "idle"):
                    svc.reconcile(row)
                elif row.state in managed.STARTABLE and server.enabled:
                    svc.start_runtime(server, row)
                if row.state in ("running", "idle"):
                    managed.touch(db, row)
                    return None
                return "runtime_not_running"

        code = await anyio.to_thread.run_sync(check)
        if code is not None:
            return None, policy, code
        url = self._runtime.endpoint(RuntimeHandle(target.namespace or "", target.cr_name or ""))
        try:
            return url, policy.for_managed_runtime(target.runtime_id, url), None
        except OutboundBlocked:
            return None, policy, "managed_endpoint_invalid"

    def endpoint_policy(self, target: Target, policy: OutboundPolicy) -> tuple[str, OutboundPolicy | None]:
        """The managed runtime's endpoint and the policy that allows exactly it (None when the endpoint is refused)."""
        url = self._runtime.endpoint(RuntimeHandle(target.namespace or "", target.cr_name or ""))
        try:
            return url, policy.for_managed_runtime(target.runtime_id or "", url)
        except OutboundBlocked:
            return url, None

    async def start_and_wait(self, tenant_id: str, server_id: str, info: dict[str, Any]) -> str | None:
        """The test button's start path for a managed server: a `stopped`, `pending` or `stopping` runtime is
        started (the same start as POST .../runtime/start; a `stopping` one is waited out first), then polled
        until `running`, for up to EAP_MCP_TEST_START_TIMEOUT_SECONDS. Returns None when it is running, else the
        error code: `runtime_starting` (not ready in time), `runtime_not_running` (`failed`, disabled),
        `runtime_missing`. Fills `info` with `state` (before), `started`, `waited_ms`, `idle_minutes`."""
        loop = asyncio.get_running_loop()
        began = loop.time()
        deadline = began + start_timeout_s()
        first = True

        def step() -> tuple[str, str, str | None, int | None]:
            with Session(self._engine) as db:
                svc = McpRuntimeService(db, self._engine, self._runtime, self.log_failure)
                row = runtime_of(db, tenant_id, server_id)
                server = db.scalar(select(McpServerModel).where(
                    McpServerModel.id == server_id, McpServerModel.tenant_id == tenant_id))
                if row is None or server is None:
                    return "missing", "missing", None, None
                before = row.state
                idle = None
                if first:
                    since = row.last_used_at
                    if since is not None:
                        idle = max(int((datetime.now(timezone.utc) - since).total_seconds() // 60), 0)
                if row.state in ("starting", "running", "idle"):
                    svc.reconcile(row)
                elif row.state in ("pending", "stopped") and server.enabled:
                    svc.start_runtime(server, row)
                if row.state in ("running", "idle"):
                    managed.touch(db, row)
                return before, row.state, row.last_error, idle

        def enabled() -> bool:
            with Session(self._engine) as db:
                return bool(db.scalar(select(McpServerModel.enabled).where(
                    McpServerModel.id == server_id, McpServerModel.tenant_id == tenant_id)))

        while True:
            before, state, last_error, idle = await anyio.to_thread.run_sync(step)
            if first:
                info.update(state=before, started=False, waited_ms=0, idle_minutes=idle)
                first = False
            if state == "missing":
                return "runtime_missing"
            if state in ("running", "idle"):
                waited = int((loop.time() - began) * 1000)
                info.update(waited_ms=waited, started=info["state"] not in ("running", "idle"))
                return None
            if state == "failed" or (state in ("pending", "stopped") and not await anyio.to_thread.run_sync(enabled)):
                info["last_error"] = last_error
                info["waited_ms"] = int((loop.time() - began) * 1000)
                return "runtime_not_running"
            if loop.time() >= deadline:
                info["waited_ms"] = int((loop.time() - began) * 1000)
                return "runtime_starting"
            await asyncio.sleep(min(START_POLL_S, max(deadline - loop.time(), 0.001)))
