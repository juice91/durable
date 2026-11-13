"""Tenant and run ids on exported log records, so the API can search logs per tenant (Loki).

A context variable holds the current tenant and run: `run_span` sets both for a run's block in the
worker, and the API binds the tenant from the authenticated principal for the rest of the request.
`LogContextProcessor`, in front of the OTLP exporter, copies them onto each record as the
`eap.tenant_id` and `eap.run_id` attributes, which Loki stores as the structured metadata
`eap_tenant_id` and `eap_run_id`. It is a log-record processor rather than a `logging.Filter` on our
handler so it also covers records from other handlers on the same provider (DBOS's own OTLP handler).

A record that already carries one of the attributes (`log_failure(..., tenant_id=...)`) keeps its
explicit value. A record emitted outside any tenant context gets no attribute, and the logs API
never shows such records to a tenant.

Loki turns every attribute name into a label name by replacing characters outside [A-Za-z0-9_]
with `_`, so `eap-tenant-id`, `eap_tenant_id` and `eap.tenant_id` all become `eap_tenant_id` (the
last one written wins). Any attribute whose name normalises to one of our two labels but is not the
canonical key is dropped, on every record, so nothing can stand in for the tenant or run id.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

from opentelemetry.sdk._logs import LogRecordProcessor, ReadWriteLogRecord

TENANT_LOG_ATTRIBUTE = "eap.tenant_id"
RUN_LOG_ATTRIBUTE = "eap.run_id"

_CANONICAL_KEYS = frozenset({TENANT_LOG_ATTRIBUTE, RUN_LOG_ATTRIBUTE})
_RESERVED_LABELS = frozenset({"eap_tenant_id", "eap_run_id"})
_NON_LABEL_CHARS = re.compile(r"[^A-Za-z0-9_]")


def loki_label_name(key: str) -> str:
    """The Loki label (structured metadata) name an OTLP attribute key becomes."""
    return _NON_LABEL_CHARS.sub("_", key)


def is_impostor_key(key: str) -> bool:
    """True for a key that is not ours but would become `eap_tenant_id` or `eap_run_id` in Loki
    (compared case-insensitively, to be conservative)."""
    return key not in _CANONICAL_KEYS and loki_label_name(key).lower() in _RESERVED_LABELS


_LogContext = tuple[str | None, str | None]  # (tenant_id, run_id)
_log_context: ContextVar[_LogContext] = ContextVar("eap_log_context", default=(None, None))


def bind_log_context(*, tenant_id: str | None, run_id: str | None = None) -> Token[_LogContext]:
    """Set the tenant (and run) for log records from here on in this context; returns the reset token.

    For code that cannot wrap a block (a FastAPI dependency: each request runs in its own task, so
    the binding ends with the request). Prefer `log_context` elsewhere.
    """
    return _log_context.set((tenant_id or None, str(run_id) if run_id else None))


def reset_log_context(token: Token[_LogContext]) -> None:
    _log_context.reset(token)


@contextmanager
def log_context(*, tenant_id: str | None, run_id: str | None = None) -> Iterator[None]:
    """Log records emitted inside the block carry `eap.tenant_id` (and `eap.run_id`)."""
    token = bind_log_context(tenant_id=tenant_id, run_id=run_id)
    try:
        yield
    finally:
        _log_context.reset(token)


def current_log_context() -> dict[str, str]:
    """The attributes the current context adds to log records ({} outside any tenant context)."""
    tenant_id, run_id = _log_context.get()
    attributes: dict[str, str] = {}
    if tenant_id:
        attributes[TENANT_LOG_ATTRIBUTE] = tenant_id
        if run_id:
            attributes[RUN_LOG_ATTRIBUTE] = run_id
    return attributes


class LogContextProcessor(LogRecordProcessor):
    """Adds the current tenant and run ids to each record (unless already set), then hands it on.

    Runs synchronously in the emitting thread (the OpenTelemetry handler calls on_emit from
    `logging.Handler.emit`), so the context variable is the emitter's.
    """

    def __init__(self, inner: LogRecordProcessor) -> None:
        self._inner = inner

    def on_emit(self, log_record: ReadWriteLogRecord) -> None:
        record = log_record.log_record
        if record.attributes:
            for key in [k for k in record.attributes if is_impostor_key(k)]:
                del record.attributes[key]  # type: ignore[attr-defined]
        attributes = current_log_context()
        if attributes:
            if record.attributes is None:
                record.attributes = dict(attributes)
            else:
                for key, value in attributes.items():
                    if key not in record.attributes:
                        record.attributes[key] = value  # type: ignore[index]
        self._inner.on_emit(log_record)

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._inner.force_flush(timeout_millis)


__all__ = [
    "RUN_LOG_ATTRIBUTE",
    "TENANT_LOG_ATTRIBUTE",
    "LogContextProcessor",
    "bind_log_context",
    "current_log_context",
    "is_impostor_key",
    "loki_label_name",
    "log_context",
    "reset_log_context",
]
