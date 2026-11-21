"""Providers of the agent domain's services: conversations (and, below, agents, tools, models)."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from eap_api.deps import MemoryStoreDep, RunLauncherDep, SessionDep, StoresDep
from eap_api.ports import RunLauncher
from eap_api.services.agents import AgentService
from eap_api.services.conversations import ConversationService
from eap_api.services.models import ModelService
from eap_api.services.processors import ProcessorService
from eap_api.services.request_context import RequestContextService
from eap_api.services.tools import ToolService
from eap_run_store import MemoryStore, RunStore


def build_conversation_service(
    db: Session, run_store: RunStore, launcher: RunLauncher, memory: MemoryStore | None = None
) -> ConversationService:
    """A ConversationService without a Request (MCP control and the AG-UI helpers build theirs from the app)."""
    return ConversationService(db, run_store, launcher, memory)


def get_conversation_service(
    db: SessionDep, stores: StoresDep, launcher: RunLauncherDep, memory: MemoryStoreDep
) -> ConversationService:
    return build_conversation_service(db, stores[0], launcher, memory)


ConversationServiceDep = Annotated[ConversationService, Depends(get_conversation_service)]


def get_agent_service(db: SessionDep) -> AgentService:
    return AgentService(db)


AgentServiceDep = Annotated[AgentService, Depends(get_agent_service)]


def get_request_context_service(db: SessionDep) -> RequestContextService:
    return RequestContextService(db)


RequestContextServiceDep = Annotated[RequestContextService, Depends(get_request_context_service)]


def get_tool_catalog() -> ToolService:
    """The tool catalog needs no stores."""
    return ToolService()


ToolCatalogDep = Annotated[ToolService, Depends(get_tool_catalog)]


def get_tool_run_service(stores: StoresDep, launcher: RunLauncherDep) -> ToolService:
    return ToolService(stores[0], launcher)


ToolRunServiceDep = Annotated[ToolService, Depends(get_tool_run_service)]


def get_processor_service(db: SessionDep) -> ProcessorService:
    return ProcessorService(db)


ProcessorServiceDep = Annotated[ProcessorService, Depends(get_processor_service)]


def get_model_service() -> ModelService:
    return ModelService()


ModelServiceDep = Annotated[ModelService, Depends(get_model_service)]
