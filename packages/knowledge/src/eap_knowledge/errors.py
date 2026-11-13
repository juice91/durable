"""Errors raised by eap_knowledge.

Both may be raised inside a DBOS step (``eap.kb_chunk``), so they are pickle-safe: the constructor
args go to ``super().__init__`` and the message is built in ``__str__``.
"""

from __future__ import annotations


class UnsupportedMimeType(ValueError):
    def __init__(self, mime_type: str) -> None:
        super().__init__(mime_type)
        self.mime_type = mime_type

    def __str__(self) -> str:
        return (
            f"unsupported document mime type {self.mime_type!r}: "
            "expected text/markdown, text/html or text/plain"
        )


class InvalidChunkingConfig(ValueError):
    def __init__(self, field: str, value: object, reason: str) -> None:
        super().__init__(field, value, reason)
        self.field = field
        self.value = value
        self.reason = reason

    def __str__(self) -> str:
        return f"invalid chunking config: {self.field}={self.value!r} ({self.reason})"


class ExtractionFailed(ValueError):
    """The content cannot be extracted safely (``reason`` is a stable code, e.g. ``too_many_attributes``)."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason, detail)
        self.reason = reason
        self.detail = detail

    def __str__(self) -> str:
        return f"document extraction failed: {self.reason}" + (f" ({self.detail})" if self.detail else "")
