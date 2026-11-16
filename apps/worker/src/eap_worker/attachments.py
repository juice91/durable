"""Chat attachments in an agent run (W2.4): a turn's uploaded files become the user prompt's parts.

The API ties each uploaded file to the user message that sends it (`chat_attachments.message_id`);
the worker finds a turn's files through the turn's `ag_ui_runs.input_message_ids`. The bytes are
immutable, so reading them outside a checkpointed step is deterministic on replay.

Text files (plain text, Markdown, CSV, JSON) are inlined as `TextContent` for any model. Images and
PDFs go as `BinaryContent` to a model that takes them (`model_accepts`); for another model the run
fails with a compact error (the API already answers 422 at run start, this is the re-check for a
model changed since).
"""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic_ai import BinaryContent, TextContent

from eap_run_store.chat_attachments import ChatAttachmentStore, is_text_type, model_accepts


class AttachmentUnsupported(ValueError):
    """The run's model does not accept an attached file's media type."""


def build_prompt(text: str, blobs: list[Any], model_id: str) -> str | list[Any]:
    """`text` alone when there are no files, else [text, one part per file] (pure)."""
    if not blobs:
        return text
    provider, _, name = model_id.partition(":")
    parts: list[Any] = [text]
    for blob in blobs:
        meta = blob.meta
        if is_text_type(meta.media_type):
            body = blob.data.decode("utf-8", errors="replace")
            parts.append(TextContent(content=f"Attached file {meta.name}:\n{body}", metadata={"attachment_id": meta.id}))
            continue
        if not model_accepts(provider, name, meta.media_type):
            raise AttachmentUnsupported(f"model {model_id} does not accept {meta.media_type} attachments")
        parts.append(BinaryContent(data=blob.data, media_type=meta.media_type, identifier=meta.id))
    return parts


async def resolve_prompt(engine: Any, tenant_id: str, run_id: str, text: str, model_id: str) -> str | list[Any]:
    blobs = await asyncio.to_thread(ChatAttachmentStore(engine).for_run, tenant_id, run_id)
    return build_prompt(text, blobs, model_id)
