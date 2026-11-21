"""Providers of the knowledge domain's services: knowledge bases, KB sources, memory and attachments."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import Engine

from eap_api.deps import (
    get_chat_attachment_store,
    get_database_engine,
    get_knowledge_store,
    get_memory_store,
    get_message_feedback_store,
    get_run_launcher,
    require_run_storage,
)
from eap_api.services.attachments import AttachmentService
from eap_api.services.kb_sources import KbSourceService
from eap_api.services.knowledge import KnowledgeService
from eap_api.services.memory import MemoryService
from eap_observability import current_trace_context
from eap_run_store.kb_sources import ConnectionStore, SourceStore, SyncStore
from eap_run_store.knowledge import KnowledgeStore


def get_knowledge_service(
    store=Depends(get_knowledge_store), stores=Depends(require_run_storage), launcher=Depends(get_run_launcher)
) -> KnowledgeService:
    return KnowledgeService(store, stores[0], launcher, current_trace_context)


KnowledgeServiceDep = Annotated[KnowledgeService, Depends(get_knowledge_service)]


def build_kb_source_service(engine: Engine, run_store, launcher) -> KbSourceService:
    return KbSourceService(
        KnowledgeStore(engine), SourceStore(engine), ConnectionStore(engine), SyncStore(engine), run_store, engine,
        launcher, current_trace_context,
    )


def get_kb_source_service(
    engine: Engine = Depends(get_database_engine), stores=Depends(require_run_storage),
    launcher=Depends(get_run_launcher),
) -> KbSourceService:
    return build_kb_source_service(engine, stores[0], launcher)


KbSourceServiceDep = Annotated[KbSourceService, Depends(get_kb_source_service)]


def get_memory_service(
    memory=Depends(get_memory_store), stores=Depends(require_run_storage), engine: Engine = Depends(get_database_engine)
) -> MemoryService:
    return MemoryService(memory, stores[0], engine)


MemoryServiceDep = Annotated[MemoryService, Depends(get_memory_service)]


def get_attachment_service(
    attachments=Depends(get_chat_attachment_store), feedback=Depends(get_message_feedback_store),
    stores=Depends(require_run_storage), engine: Engine = Depends(get_database_engine),
) -> AttachmentService:
    return AttachmentService(attachments, feedback, stores[0], engine)


AttachmentServiceDep = Annotated[AttachmentService, Depends(get_attachment_service)]


# --- for callers that hold an app but no request (the MCP control endpoint) ---------------------------


def knowledge_service_of(request: Request, launcher=None) -> KnowledgeService:
    """The service for `request` (a real Request, or the MCP context's stand-in with an `app`)."""
    return KnowledgeService(
        get_knowledge_store(request), require_run_storage(request)[0], launcher or get_run_launcher(request),
        current_trace_context,
    )


def kb_source_service_of(request: Request, launcher=None) -> KbSourceService:
    return build_kb_source_service(
        get_database_engine(request), require_run_storage(request)[0], launcher or get_run_launcher(request)
    )
