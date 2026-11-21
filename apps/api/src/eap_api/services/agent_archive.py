"""The archived-agent rule, independent of the transport (see eap_api.agent_archive for the policy)."""

from __future__ import annotations

from eap_api.services.errors import Conflict

AGENT_ARCHIVED = "Agent is archived; unarchive it to start runs, conversations or publish versions"


def refuse_if_archived(agent: object) -> None:
    """Conflict AGENT_ARCHIVED when `agent` (an AgentModel) is archived."""
    if getattr(agent, "archived", False):
        raise Conflict(AGENT_ARCHIVED)
