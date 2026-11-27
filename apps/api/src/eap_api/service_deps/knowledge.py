"""Providers of the knowledge domain's services: knowledge bases, KB sources, memory and attachments."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy import Engine

from eap_api.deps import (
    get_chat_attachment_store,
    get_database_engine,
    get_knowledge_store,
    get_memory_store,
    get_run_launcher,
    get_message_feedback_store,
    database_engine_of,
    knowledge_store_of,
    require_run_storage,
    run_launcher_of,
    run_stores_of,
)
from eap_api.service_deps.iam import AccessDep
from eap_api.services.access import Access
from eap_api.services.attachments import AttachmentService
from eap_api.services.kb_sources import KbSourceService
from eap_api.services.knowledge import KnowledgeService
from eap_api.services.memory import MemoryService
from eap_run_store.kb_sources import ConnectionStore, SourceStore, SyncStore
from eap_run_store.knowledge import KnowledgeStore


def get_knowledge_service(
    access: AccessDep, store=Depends(get_knowledge_store), stores=Depends(require_run_storage),
    launcher=Depends(get_run_launcher),
) -> KnowledgeService:
    return KnowledgeService(store, stores[0], launcher, access)


KnowledgeServiceDep = Annotated[KnowledgeService, Depends(get_knowledge_service)]


def build_kb_source_service(engine: Engine, run_store, launcher, access: Access | None = None) -> KbSourceService:
    return KbSourceService(
        KnowledgeStore(engine), SourceStore(engine), ConnectionStore(engine), SyncStore(engine), run_store, engine,
        launcher, access,
    )


def get_kb_source_service(
    access: AccessDep, engine: Engine = Depends(get_database_engine), stores=Depends(require_run_storage),
    launcher=Depends(get_run_launcher),
) -> KbSourceService:
    return build_kb_source_service(engine, stores[0], launcher, access)


KbSourceServiceDep = Annotated[KbSourceService, Depends(get_kb_source_service)]


def get_memory_service(
    access: AccessDep, memory=Depends(get_memory_store), stores=Depends(require_run_storage),
    engine: Engine = Depends(get_database_engine),
) -> MemoryService:
    return MemoryService(memory, stores[0], engine, access)


MemoryServiceDep = Annotated[MemoryService, Depends(get_memory_service)]


def get_attachment_service(
    access: AccessDep, attachments=Depends(get_chat_attachment_store), feedback=Depends(get_message_feedback_store),
    stores=Depends(require_run_storage), engine: Engine = Depends(get_database_engine),
) -> AttachmentService:
    return AttachmentService(attachments, feedback, stores[0], engine, access)


AttachmentServiceDep = Annotated[AttachmentService, Depends(get_attachment_service)]


# --- for callers that hold an app but no request (the MCP control endpoint) ---------------------------


def knowledge_service_of(app, launcher=None) -> KnowledgeService:
    """The service for `app`; `launcher` is the one the caller resolved (a dependency override included)."""
    return KnowledgeService(
        knowledge_store_of(app), run_stores_of(app)[0], launcher or run_launcher_of(app),
    )


def kb_source_service_of(app, launcher=None) -> KbSourceService:
    return build_kb_source_service(
        database_engine_of(app), run_stores_of(app)[0], launcher or run_launcher_of(app)
    )
