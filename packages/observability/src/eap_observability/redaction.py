"""What may leave the process in telemetry: no prompts, no model output, no secrets.

Exception text is untrusted. A provider or tool exception can quote the rejected request, the model's
output, a response body, or a URL or header carrying a credential, and nothing can tell prompt text
apart from any other text. So exported telemetry never carries exception messages or tracebacks:

- Code that handles a failure logs it with `log_failure`: a fixed event text, a stable error code,
  the exception's type name, and ids (run, tenant). The full exception goes only to a DEBUG record
  marked local-only, which the export pipeline drops (it can still reach a local console handler).
- `RedactingLogProcessor` sits in front of the OTLP log exporter, so it also covers records from
  other libraries and from other handlers on the same provider (DBOS's enable_otlp adds its own):
  local-only records are dropped, `exception.message` and `exception.stacktrace` are removed
  (`exception.type` stays), and the body and string attributes pass through `redact`.
- `RedactingSpanExporter` does the same for spans: exception events keep only the type, DBOS's
  step-retry `error` attribute and Pydantic AI's tool definitions (`model_request_parameters`,
  `gen_ai.tool.definitions`: client-chosen schemas for AG-UI frontend tools) are removed, string attributes are redacted, and an error status
  description is reduced to its leading exception type (else "error").

`redact` scrubs credentials (bearer/basic tokens, `sk-` API keys, URL user:password, and values of
api_key/token/secret/password/authorization-like keys) and bounds the length. It is the last line
for free text that must be logged; it cannot recognise prompt text, which is why exception text is
dropped instead of redacted.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any

from opentelemetry.sdk._logs import LogRecordProcessor, ReadWriteLogRecord
from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.trace import Status, StatusCode

from eap_observability.logcontext import RUN_LOG_ATTRIBUTE, TENANT_LOG_ATTRIBUTE

REDACTED = "[REDACTED]"
TRUNCATED = "…[truncated]"
# A log record carrying this attribute (extra={LOCAL_ONLY_LOG_ATTRIBUTE: True}) is never exported.
LOCAL_ONLY_LOG_ATTRIBUTE = "eap.local_only"

_MAX_BODY = 2000
_MAX_ATTRIBUTE = 1000
_MAX_CONTEXT = 128

_SENSITIVE_KEYS = (
    r"x-api-key|api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|id[_-]?token|auth[_-]?token|token"
    r"|client[_-]?secret|secret|password|passwd|pwd|authorization|proxy-authorization|cookie|set-cookie"
)
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Authorization schemes: "Bearer abc...", "Basic abc...".
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{4,}"), r"\1 " + REDACTED),
    # OpenAI-style keys (sk-..., sk-proj-...), wherever they appear.
    (re.compile(r"\bsk-[A-Za-z0-9_-]{6,}"), REDACTED),
    # PydanticOS tenant API keys (pos_<24 hex id>_<secret>; eap_run_store.api_keys), wherever they
    # appear. The whole key goes, the id too: a partial key would still name the row.
    (re.compile(r"\bpos_[0-9a-f]{24}_[A-Za-z0-9_-]+"), REDACTED),
    # user:password@ in any URL (postgresql://, https://, ...).
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]*@"), r"\1" + REDACTED + "@"),
    # key=value, key: value, "key": "value" for credential-like keys (query strings, headers, JSON).
    (
        re.compile(rf"""(?i)(["']?(?<![A-Za-z0-9])(?:{_SENSITIVE_KEYS})["']?\s*[:=]\s*)(["']?)(?!\[REDACTED\])[^\s"'&,;}}]+"""),
        r"\1\2" + REDACTED,
    ),
)
_TYPE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,99}")
_STATUS_TYPE = re.compile(r"([A-Za-z_][A-Za-z0-9_.]{0,99})(?::|$)")
# Attributes whose value is exception or error text: never exported.
# Pydantic AI's chat spans also carry the request's tool definitions (names, descriptions, JSON
# schemas). An AG-UI client's frontend tools are client-chosen text (AGUI.7: schemas never reach
# spans), so the definitions are dropped for every request; tool names stay on the tool spans.
_DROPPED_ATTRIBUTES = frozenset({
    "exception.message", "exception.stacktrace", "error", "model_request_parameters", "gen_ai.tool.definitions",
})
# Ids the logs API filters on: never redacted (a tenant id such as `sk-abcdef` would otherwise become
# [REDACTED] and that tenant's logs unsearchable). They come from the principal or the run row.
_ID_ATTRIBUTES = frozenset({TENANT_LOG_ATTRIBUTE, RUN_LOG_ATTRIBUTE})
_ID_CONTEXT_KEYS = frozenset({"tenant_id", "run_id"})


def redact(text: str, *, max_length: int = 500) -> str:
    """`text` with credentials replaced by [REDACTED], cut to `max_length` characters (+ a marker)."""
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    if len(text) > max_length:
        text = text[:max_length] + TRUNCATED
    return text


def exception_type(exc: BaseException) -> str:
    """The exception's class name when it is a plain identifier (bounded), else "Exception"."""
    name = type(exc).__name__
    return name if _TYPE_NAME.fullmatch(name) else "Exception"


def safe_code(code: str) -> str:
    """`code` when it is a plain identifier (bounded), else "invalid"."""
    return code if _TYPE_NAME.fullmatch(code) else "invalid"


def log_failure(
    logger: logging.Logger,
    event: str,
    exc: BaseException,
    *,
    code: str,
    level: int = logging.WARNING,
    error_type: str | None = None,
    **context: Any,
) -> None:
    """Log a handled failure without its exception text: `event`, `code`, the exception type, ids.

    `event` must be fixed text (no request or exception content). `context` values (run_id,
    tenant_id, ...) become `eap.<key>` attributes and are redacted and bounded. `error_type` names
    the classified cause when it is not `exc` itself (e.g. inside a wrapper). The exception itself
    goes only to a local-only DEBUG record, which the export pipeline drops.
    """
    error_type = safe_code(error_type) if error_type else exception_type(exc)
    code = safe_code(code)
    ctx = {
        k: str(v) if k in _ID_CONTEXT_KEYS else redact(str(v), max_length=_MAX_CONTEXT)
        for k, v in context.items()
        if v is not None
    }
    logger.log(
        level,
        "%s: error_code=%s error_type=%s%s",
        event,
        code,
        error_type,
        "".join(f" {k}={v}" for k, v in ctx.items()),
        extra={"eap.error_code": code, "exception.type": error_type, **{f"eap.{k}": v for k, v in ctx.items()}},
        stacklevel=2,
    )
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "%s: local detail", event, exc_info=(type(exc), exc, exc.__traceback__),
            extra={LOCAL_ONLY_LOG_ATTRIBUTE: True},
            stacklevel=2,
        )


def _clean_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value, max_length=_MAX_ATTRIBUTE)
    if isinstance(value, (list, tuple)) and any(isinstance(v, str) for v in value):
        return type(value)(redact(v, max_length=_MAX_ATTRIBUTE) if isinstance(v, str) else v for v in value)
    return value


def _clean_attributes(attributes: Mapping[str, Any] | None) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, value in (attributes or {}).items():
        if key in _DROPPED_ATTRIBUTES:
            continue
        if key in _ID_ATTRIBUTES and isinstance(value, str):
            cleaned[key] = value
            continue
        if key == "exception.type":
            cleaned[key] = value if isinstance(value, str) and _TYPE_NAME.fullmatch(value) else "Exception"
            continue
        cleaned[key] = _clean_value(value)
    return cleaned


def _known_status_names(span: ReadableSpan) -> set[str]:
    """Names a status description may keep: exception types the span itself recorded (exception
    events) and the stable codes it carries as `eap.error_code` / `eap.run_status`."""
    names = {
        e.attributes.get("exception.type")
        for e in span.events
        if e.name == "exception" and e.attributes
    }
    attributes = span.attributes or {}
    names.update(attributes.get(k) for k in ("eap.error_code", "eap.run_status"))
    return {n for n in names if isinstance(n, str) and _TYPE_NAME.fullmatch(n)}


def _clean_status(status: Status, known: set[str] = frozenset()) -> Status:
    """An error description is free text from whoever set it (a library may put a tool's error body
    there), so only a name in `known` survives; anything else becomes the fixed "error"."""
    if status.status_code != StatusCode.ERROR or not status.description:
        return status
    match = _STATUS_TYPE.match(status.description)
    name = match.group(1) if match else None
    return Status(StatusCode.ERROR, name if name in known else "error")


class RedactingLogProcessor(LogRecordProcessor):
    """Cleans each log record (see the module docstring), then hands it to `inner`."""

    def __init__(self, inner: LogRecordProcessor) -> None:
        self._inner = inner

    def on_emit(self, log_record: ReadWriteLogRecord) -> None:
        record = log_record.log_record
        attributes = record.attributes or {}
        if attributes.get(LOCAL_ONLY_LOG_ATTRIBUTE):
            return
        cleaned = _clean_attributes(attributes)
        if record.attributes is not None:
            for key in list(record.attributes):
                if key not in cleaned:
                    del record.attributes[key]  # type: ignore[attr-defined]
                elif record.attributes[key] != cleaned[key]:
                    record.attributes[key] = cleaned[key]  # type: ignore[index]
        if record.body is not None:
            record.body = redact(record.body if isinstance(record.body, str) else str(record.body), max_length=_MAX_BODY)
        self._inner.on_emit(log_record)

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._inner.force_flush(timeout_millis)


def _clean_span(span: ReadableSpan) -> ReadableSpan:
    return ReadableSpan(
        name=span.name,
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes=_clean_attributes(span.attributes),
        events=[Event(e.name, _clean_attributes(e.attributes), e.timestamp) for e in span.events],
        links=span.links,
        kind=span.kind,
        status=_clean_status(span.status, _known_status_names(span)),
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


class RedactingSpanExporter(SpanExporter):
    """Exports cleaned copies of the spans (see the module docstring) through `inner`."""

    def __init__(self, inner: SpanExporter) -> None:
        self._inner = inner

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        return self._inner.export([_clean_span(s) for s in spans])

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._inner.force_flush(timeout_millis)


__all__ = [
    "LOCAL_ONLY_LOG_ATTRIBUTE",
    "REDACTED",
    "RedactingLogProcessor",
    "RedactingSpanExporter",
    "exception_type",
    "log_failure",
    "redact",
    "safe_code",
]
