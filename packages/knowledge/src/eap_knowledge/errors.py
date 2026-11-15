"""Errors raised by eap_knowledge.

All may be raised inside a DBOS step (``eap.kb_chunk``, ``eap.kb_extract_chunk``), so they are
pickle-safe: the constructor args go to ``super().__init__`` and the message is built in ``__str__``.
"""

from __future__ import annotations

from eap_schemas.knowledge import EXTRACTION_ERROR_MESSAGES, extraction_error_message


class UnsupportedMimeType(ValueError):
    """`expected` names the supported types in the message; it defaults to the API-extracted three
    (``extract()``'s), and the format registry passes its full list."""

    def __init__(self, mime_type: str, expected: tuple[str, ...] | None = None) -> None:
        super().__init__(mime_type, expected)
        self.mime_type = mime_type
        self.expected = expected

    def __str__(self) -> str:
        expected = (
            ", ".join(self.expected) if self.expected else "text/markdown, text/html or text/plain"
        )
        return f"unsupported document mime type {self.mime_type!r}: expected {expected}"


class InvalidChunkingConfig(ValueError):
    def __init__(self, field: str, value: object, reason: str) -> None:
        super().__init__(field, value, reason)
        self.field = field
        self.value = value
        self.reason = reason

    def __str__(self) -> str:
        return f"invalid chunking config: {self.field}={self.value!r} ({self.reason})"


class ExtractionFailed(ValueError):
    """The content cannot be extracted safely (``reason`` is a stable code, e.g. ``too_many_attributes``).

    RAG P1: the worker-side extractors and the sandbox use the codes of
    ``eap_schemas.knowledge.EXTRACTION_ERROR_CODES`` as ``reason`` (``extract_timeout``,
    ``document_encrypted``, ...). ``code`` is that code, or ``extract_failed`` for any other reason
    (the HTML extractor's own ones), and ``user_message`` is its fixed, user-safe message.
    ``detail`` is for logs only: it may name parser internals and is never shown to users.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason, detail)
        self.reason = reason
        self.detail = detail

    @property
    def code(self) -> str:
        return self.reason if self.reason in EXTRACTION_ERROR_MESSAGES else "extract_failed"

    @property
    def user_message(self) -> str:
        return extraction_error_message(self.code)

    def __str__(self) -> str:
        return f"document extraction failed: {self.reason}" + (f" ({self.detail})" if self.detail else "")
