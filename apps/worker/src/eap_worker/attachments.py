"""Chat attachments in an agent run (W2.4): a turn's uploaded files become the user prompt's parts.

The API ties each uploaded file to the user message that sends it (`chat_attachments.message_id`);
the worker finds a turn's files through the turn's `ag_ui_runs.input_message_ids`. The bytes are
read through the BlobStore (eap_blob_store, built from EAP_BLOB_* at worker start) by each row's
`storage_key`, never from Postgres. They are immutable, so reading them outside a checkpointed step
is deterministic on replay.

Text files (plain text, Markdown, CSV, JSON) are inlined as `TextContent` for any model. Images and
PDFs go as `BinaryContent` to a model that takes them. Whether it does was decided once at run start from model discovery and
recorded on the run (`runs.model_settings.attachment_capabilities`: `{image, pdf, source}`); the
worker only reads that and never calls discovery. A run recorded without a decision falls back to
`model_accepts`, the assumed provider rules. For a model that does not take the type the run fails
with a compact error (the API already answers 422 at run start; this is the re-check).
"""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic_ai import BinaryContent, TextContent

from eap_blob_store import BlobNotFound, BlobStore, blob_store_from_env
from eap_run_store.chat_attachments import (
    ATTACHMENT_CAPABILITIES_KEY,
    AttachmentMeta,
    ChatAttachmentStore,
    accepts_by_capabilities,
    is_text_type,
    max_attachment_bytes,
    model_accepts,
)

_store: BlobStore | None = None


def configure_blob_store(store: BlobStore | None = None) -> BlobStore:
    """Set the worker's BlobStore (from EAP_BLOB_* when none is given). Called at launch so a bad
    setting fails the start; a ValueError names the variable."""
    global _store
    _store = store if store is not None else blob_store_from_env()
    return _store


def blob_store() -> BlobStore:
    return _store if _store is not None else configure_blob_store()


class AttachmentUnsupported(ValueError):
    """The run's model does not accept an attached file's media type."""


class AttachmentGone(ValueError):
    """An attachment's blob is no longer in the store (expired, deleted or lost). Carries the id only."""

    def __init__(self, attachment_id: str) -> None:
        self.attachment_id = attachment_id
        super().__init__(attachment_id)  # pickle-safe

    def __str__(self) -> str:
        return f"attachment {self.attachment_id} is no longer available"


class AttachmentTooLarge(ValueError):
    """An attachment's recorded size, or its stored bytes, exceed the size cap."""

    def __init__(self, attachment_id: str) -> None:
        self.attachment_id = attachment_id
        super().__init__(attachment_id)  # pickle-safe

    def __str__(self) -> str:
        return f"attachment {self.attachment_id} is larger than allowed"


async def _read(store: BlobStore, meta: AttachmentMeta) -> bytes:
    """The blob's bytes, bounded by the recorded size and the attachment cap (10 MiB by default):
    reading stops with AttachmentTooLarge as soon as either is exceeded, never buffering more.
    A missing blob is AttachmentGone."""
    limit = min(meta.size, max_attachment_bytes())
    if meta.size > limit:
        raise AttachmentTooLarge(meta.id)
    parts: list[bytes] = []
    total = 0
    try:
        async for chunk in store.open(meta.storage_key):
            total += len(chunk)
            if total > limit:
                raise AttachmentTooLarge(meta.id)
            parts.append(chunk)
    except BlobNotFound:
        raise AttachmentGone(meta.id) from None
    return b"".join(parts)


def recorded_capabilities(model_settings: dict[str, Any] | None) -> dict[str, Any] | None:
    """The run's recorded attachment decision `{image, pdf, source}`, or None when it has none."""
    value = (model_settings or {}).get(ATTACHMENT_CAPABILITIES_KEY)
    return value if isinstance(value, dict) and value else None


async def build_prompt(
    text: str,
    metas: list[AttachmentMeta],
    model_id: str,
    store: BlobStore,
    capabilities: dict[str, Any] | None = None,
) -> str | list[Any]:
    """`text` alone when there are no files, else [text, one part per file], the bytes read through
    `store`. `capabilities` is the run's recorded decision (recorded_capabilities); without one the
    assumed rules apply. Raises AttachmentUnsupported for a model that cannot take a file, AttachmentGone for a file
    whose blob is gone, AttachmentTooLarge for one over its recorded size or the cap."""
    if not metas:
        return text
    provider, _, name = model_id.partition(":")
    parts: list[Any] = [text]
    for meta in metas:
        if is_text_type(meta.media_type):
            body = (await _read(store, meta)).decode("utf-8", errors="replace")
            parts.append(TextContent(content=f"Attached file {meta.name}:\n{body}", metadata={"attachment_id": meta.id}))
            continue
        accepted = (
            accepts_by_capabilities(capabilities, meta.media_type)
            if capabilities
            else model_accepts(provider, name, meta.media_type)
        )
        if not accepted:
            raise AttachmentUnsupported(f"model {model_id} does not accept {meta.media_type} attachments")
        data = await _read(store, meta)
        parts.append(BinaryContent(data=data, media_type=meta.media_type, identifier=meta.id))
    return parts


async def resolve_prompt(
    engine: Any, tenant_id: str, run_id: str, text: str, model_id: str, model_settings: dict[str, Any] | None = None
) -> str | list[Any]:
    metas = await asyncio.to_thread(ChatAttachmentStore(engine).for_run, tenant_id, run_id)
    return await build_prompt(text, metas, model_id, blob_store(), recorded_capabilities(model_settings))
