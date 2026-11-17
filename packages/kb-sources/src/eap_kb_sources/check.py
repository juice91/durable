"""Checking a connection: the one function the `POST /settings/connections/{id}/check` route and the
`settings.connection.check` tool share.

The connection's secret references are resolved INSIDE this call (through the tenant's secret store, one
value at a time, handed only to the adapter), the adapter's `check_connection` runs under a bounded
timeout (EAP_KB_SOURCE_CHECK_TIMEOUT_SECONDS, default 10 s, clamped to 0.5-30 s) and the outcome is
persisted on the connection (`last_checked_at`, `last_status`, `last_error_code`). Nothing a provider or
a secret store says leaves this function except a stable code: the answer is `{ok, checked_at, error_code}`
and anything an adapter reports that is not one of `ERROR_CODES` becomes `connection_response_invalid`.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Protocol

from eap_run_store.kb_sources import ConnectionStore

from eap_kb_sources.adapter import ConnectionSnapshot, secret_name
from eap_kb_sources.errors import (
    CONNECTION_DISABLED,
    CONNECTION_NOT_FOUND,
    CONNECTION_RESPONSE_INVALID,
    CONNECTION_UNREACHABLE,
    ERROR_CODES,
    SECRET_MISSING,
    SOURCE_ADAPTER_NOT_FOUND,
    SourceError,
)
from eap_kb_sources.registry import adapter_for

log = logging.getLogger("eap_kb_sources.check")

TIMEOUT_ENV = "EAP_KB_SOURCE_CHECK_TIMEOUT_SECONDS"
DEFAULT_TIMEOUT_SECONDS = 10.0
MIN_TIMEOUT_SECONDS = 0.5
MAX_TIMEOUT_SECONDS = 30.0
SOURCE_KIND = "product_catalog"


class SecretReader(Protocol):
    def resolve(self, tenant_id: str, name: str) -> str | None: ...


def check_timeout_seconds() -> float:
    raw = os.environ.get(TIMEOUT_ENV)
    try:
        value = float(raw) if raw is not None and raw.strip() else DEFAULT_TIMEOUT_SECONDS
    except ValueError:
        value = DEFAULT_TIMEOUT_SECONDS
    if value != value:  # NaN
        value = DEFAULT_TIMEOUT_SECONDS
    return min(max(value, MIN_TIMEOUT_SECONDS), MAX_TIMEOUT_SECONDS)


def load_providers() -> None:
    """Import the provider packages, which register their adapters (idempotent)."""
    from eap_kb_sources import providers  # noqa: F401


class _Resolver:
    def __init__(self, tenant_id: str, secrets: SecretReader) -> None:
        self._tenant, self._secrets = tenant_id, secrets

    def resolve(self, ref: str) -> str:
        value = self._secrets.resolve(self._tenant, secret_name(ref))
        if value is None:
            raise SourceError(SECRET_MISSING, "a secret of the connection is not stored")
        return value


def _stable(code: str | None) -> str:
    return code if code in ERROR_CODES else CONNECTION_RESPONSE_INVALID


async def check_connection(
    engine: Any, tenant_id: str, connection_id: str, secrets: SecretReader, *, timeout: float | None = None
) -> dict[str, Any]:
    """`{ok, checked_at, error_code}`; raises `SourceError(connection_not_found)` for a connection that is not
    the tenant's (nothing is persisted then). Every other failure is the answer, never an exception."""
    store = ConnectionStore(engine)
    connection = await asyncio.to_thread(store.get, tenant_id, str(connection_id))
    if connection is None:
        raise SourceError(CONNECTION_NOT_FOUND, "connection not found")
    code: str | None = None
    if not connection.enabled:
        code = CONNECTION_DISABLED
    else:
        provider = connection.config.get("provider")
        try:
            if not isinstance(provider, str):
                raise SourceError(SOURCE_ADAPTER_NOT_FOUND, "the connection names no provider")
            load_providers()
            adapter = adapter_for(SOURCE_KIND, provider)
            snapshot = ConnectionSnapshot(
                tenant_id=tenant_id, connection_id=connection.id, name=connection.name, provider=provider,
                config=connection.config, secret_refs=connection.credential_refs, base_url=connection.base_url,
            )
            result = await asyncio.wait_for(
                adapter.check_connection(snapshot, _Resolver(tenant_id, secrets)),
                timeout=timeout if timeout is not None else check_timeout_seconds(),
            )
            code = None if result.ok else _stable(result.error_code)
        except SourceError as exc:
            code = exc.code
        except (asyncio.TimeoutError, TimeoutError, OSError) as exc:
            log.warning("connection check failed (%s): connection=%s tenant=%s", type(exc).__name__, connection.id, tenant_id)
            code = CONNECTION_UNREACHABLE
        except Exception as exc:  # noqa: BLE001 - the text of an unexpected error can quote a credential
            log.warning("connection check failed (%s): connection=%s tenant=%s", type(exc).__name__, connection.id, tenant_id)
            code = CONNECTION_RESPONSE_INVALID
    await asyncio.to_thread(store.record_check, tenant_id, connection.id, "ok" if code is None else "error", code)
    return {"ok": code is None, "checked_at": datetime.now(timezone.utc).isoformat(), "error_code": code}
