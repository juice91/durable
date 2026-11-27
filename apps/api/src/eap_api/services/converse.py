"""Who may chat with an agent: `run` on it, and one narrow exception for the built-in Architect.

Starting or continuing a conversation is a run of the agent. The exception (docs/iam.md, "Viewers and the Architect"): a
person who may read the tenant (any member, a `viewer` included) may converse with the built-in Architect, and with
nothing else, without `run` on it. It is not a grant of `run`: no other route accepts it, and the worker offers such a
person's turn read tools only (eap_worker.read_only). It reaches only the conversations they started: their text must not
enter a thread whose next turn runs with someone else's write tools. API keys, services and runs are unaffected.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from eap_api.models import AgentModel
from eap_api.services.access import Access
from eap_api.services.agents import builtin_role
from eap_api.services.errors import Forbidden
from eap_run_store import RunStore

NOT_YOUR_CONVERSATION = "you may only continue a conversation with the Architect that you started"


def require_converse(access: Access | None, db: Session, tenant_id: str, agent_id: str | uuid.UUID) -> bool:
    """Return when the caller may converse with the agent, else NotFound / Forbidden exactly as for `run`.

    True when only the Architect exception allows it: the caller must then also pass `require_own_conversation` for an
    existing conversation."""
    agent_id = str(agent_id)
    if access is None or access.allowed("run", "agent", agent_id):
        return False
    if _may_consult(access, db, tenant_id, agent_id):
        return True
    access.require("run", "agent", agent_id)  # refuses with the answer `run` gives
    return False


def _may_consult(access: Access, db: Session, tenant_id: str, agent_id: str) -> bool:
    if access.principal.kind != "user":
        return False
    agent = db.query(AgentModel).filter(AgentModel.id == agent_id, AgentModel.tenant_id == tenant_id).first()
    if agent is None or builtin_role(agent) != "architect":
        return False
    return access.allowed("read", "tenant", None) and access.allowed("read", "agent", agent_id)


def require_own_conversation(
    access: Access, run_store: RunStore, tenant_id: str, conversation_id: str | uuid.UUID
) -> None:
    """Forbidden when anyone else (or nobody recorded) started one of its turns."""
    mine = f"user:{access.principal.subject}"
    if run_store.conversation_starters(tenant_id, conversation_id) - {mine}:
        raise Forbidden(NOT_YOUR_CONVERSATION, code="not_your_conversation")
