"""Stable error codes of the source sync (design: Failure, retry and recovery matrix).

Provider failures are classified into this fixed set, counted in the sync report and recorded on the
sync row, never raised out of a run except the auth family, which ends it `needs_attention`. Codes
and the short messages are safe to store and show: a message never carries a provider body or a
credential, and the generic classifier keeps only the exception type."""

from __future__ import annotations

CONNECTION_NOT_FOUND = "connection_not_found"
CONNECTION_DISABLED = "connection_disabled"
SECRET_MISSING = "secret_missing"
CONNECTION_AUTH_FAILED = "connection_auth_failed"
CONNECTION_UNREACHABLE = "connection_unreachable"
CONNECTION_RESPONSE_INVALID = "connection_response_invalid"
SOURCE_ADAPTER_NOT_FOUND = "source_adapter_not_found"
RATE_LIMITED = "rate_limited"
NOT_FOUND = "not_found"
RESPONSE_TOO_LARGE = "response_too_large"
URL_NOT_ALLOWED = "url_not_allowed"
PROVIDER_ERROR = "provider_error"
RUN_CANCELLED = "run_cancelled"
# Platform side of the hand-off and the request (not raised by adapters).
SOURCE_NOT_FOUND = "source_not_found"
INVALID_REQUEST = "invalid_request"
KB_CAPACITY = "kb_capacity"  # the knowledge base is at its 2,000 document cap
INVALID_DOCUMENT = "invalid_document"  # the bytes are not a document the knowledge base takes
ENQUEUE_FAILED = "enqueue_failed"  # the document is stored but its ingest could not be started
INTERNAL_ERROR = "internal_error"

ERROR_CODES = frozenset({
    CONNECTION_NOT_FOUND, CONNECTION_DISABLED, SECRET_MISSING, CONNECTION_AUTH_FAILED, CONNECTION_UNREACHABLE,
    CONNECTION_RESPONSE_INVALID, SOURCE_ADAPTER_NOT_FOUND, RATE_LIMITED, NOT_FOUND, RESPONSE_TOO_LARGE,
    URL_NOT_ALLOWED, PROVIDER_ERROR, RUN_CANCELLED, SOURCE_NOT_FOUND, INVALID_REQUEST, KB_CAPACITY,
    INVALID_DOCUMENT, ENQUEUE_FAILED, INTERNAL_ERROR,
})
# A sync that hits one of these stops and needs a person (credentials or the connection itself).
NEEDS_ATTENTION_CODES = frozenset({CONNECTION_AUTH_FAILED, SECRET_MISSING})
# Failures that end the run `failed` at once: the work cannot proceed whatever is retried.
FATAL_CODES = NEEDS_ATTENTION_CODES | {
    CONNECTION_NOT_FOUND, CONNECTION_DISABLED, SOURCE_ADAPTER_NOT_FOUND, SOURCE_NOT_FOUND, INVALID_REQUEST,
}
MAX_MESSAGE_CHARS = 200


class SourceError(Exception):
    """A classified failure. `message` is short and secret-free."""

    def __init__(self, code: str, message: str = "", *, retry_after: float | None = None) -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown source error code {code!r}")
        self.code = code
        self.message = message[:MAX_MESSAGE_CHARS]
        self.retry_after = retry_after
        super().__init__(code, self.message, retry_after)  # pickle-safe

    def __str__(self) -> str:
        return f"{self.code}: {self.message}" if self.message else self.code


class BudgetExhausted(Exception):
    """The daily quota of `bucket` is spent: not a failure; the segment returns `complete=False`."""

    def __init__(self, bucket: str) -> None:
        self.bucket = bucket
        super().__init__(bucket)


def error_entry(external_id: str | None, exc: BaseException) -> dict[str, str]:
    """`{external_id?, code, message <= 200}` for a bounded error list."""
    if isinstance(exc, SourceError):
        entry = {"code": exc.code, "message": exc.message}
    else:
        # Type only: the text of an unexpected error can quote a request, a URL or a credential.
        entry = {"code": PROVIDER_ERROR, "message": type(exc).__name__}
    if external_id:
        entry = {"external_id": external_id[:MAX_MESSAGE_CHARS], **entry}
    return entry
