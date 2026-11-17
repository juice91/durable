"""Chat attachments in an agent run (W2.4): a turn's uploaded files become the user prompt's parts.

The API ties each uploaded file to the user message that sends it (`chat_attachments.message_id`);
the worker finds a turn's files through the turn's `ag_ui_runs.input_message_ids`. The bytes are
read through the BlobStore (eap_blob_store, built from EAP_BLOB_* at worker start) by each row's
`storage_key`, never from Postgres. They are immutable, so reading them outside a checkpointed step
is deterministic on replay.

Text files (plain text, Markdown, CSV, JSON) are inlined as `TextContent` for any model. Images and
PDFs go as `BinaryContent` to a model that takes them (`model_accepts`); for another model the run
fails with a compact error (the API already answers 422 at run start, this is the re-check for a
model changed since).
"""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic_ai import BinaryContent, TextContent

from eap_blob_store import BlobStore, blob_store_from_env
from eap_run_store.chat_attachments import AttachmentMeta, ChatAttachmentStore, is_text_type, model_accepts

_store: BlobStore | None = None


def configure_blob_store(store: BlobStore | None = None) -> BlobStore:
    """Set the worker's BlobStore (from EAP_BLOB_* when none is given). Called at launch so a bad
    setting fails the start; a ValueError names the variable."""
    global _store
    _store = store if store is not None else blob_store_from_env()
    return _store


def blob_store() -> BlobStore:
    return _store if _store is not None else configure_blob_store()


async def _read(store: BlobStore, key: str) -> bytes:
    return b"".join([chunk async for chunk in store.open(key)])  # BlobNotFound when the blob is gone


class AttachmentUnsupported(ValueError):
    """The run's model does not accept an attached file's media type."""


async def build_prompt(text: str, metas: list[AttachmentMeta], model_id: str, store: BlobStore) -> str | list[Any]:
    """`text` alone when there are no files, else [text, one part per file], the bytes read through
    `store`. Raises AttachmentUnsupported for a model that cannot take a file, BlobNotFound for a file
    whose blob is gone."""
    if not metas:
        return text
    provider, _, name = model_id.partition(":")
    parts: list[Any] = [text]
    for meta in metas:
        if is_text_type(meta.media_type):
            body = (await _read(store, meta.storage_key)).decode("utf-8", errors="replace")
            parts.append(TextContent(content=f"Attached file {meta.name}:\n{body}", metadata={"attachment_id": meta.id}))
            continue
        if not model_accepts(provider, name, meta.media_type):
            raise AttachmentUnsupported(f"model {model_id} does not accept {meta.media_type} attachments")
        data = await _read(store, meta.storage_key)
        parts.append(BinaryContent(data=data, media_type=meta.media_type, identifier=meta.id))
    return parts


async def resolve_prompt(engine: Any, tenant_id: str, run_id: str, text: str, model_id: str) -> str | list[Any]:
    metas = await asyncio.to_thread(ChatAttachmentStore(engine).for_run, tenant_id, run_id)
    return await build_prompt(text, metas, model_id, blob_store())
