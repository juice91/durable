"""Providers of the agent domain's services: conversations (and, below, agents, tools, models)."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.orm import Session

from eap_api.deps import MemoryStoreDep, RunLauncherDep, SessionDep, StoresDep
from eap_api.ports import RunLauncher
from eap_api.services.conversations import ConversationService
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
