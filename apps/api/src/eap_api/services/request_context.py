"""Request context of an agent: the context fields of a version (or the draft) and a context check that starts no run.

Validation is exactly the run start's (`eap_api.services.run_options.validate_context`). An unknown agent or
another tenant's is `NotFound`; no published version or no draft is `Conflict`.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session

from eap_api.services.agents import AgentService
from eap_api.services.errors import Conflict, InvalidInput
from eap_api.services.run_filters import plain_refusals
from eap_api.services.run_options import DRAFT, resolve_version, validate_context, version_row
from eap_schemas import AgentSpec
from eap_schemas.request_context import context_json_schema


@plain_refusals
class RequestContextService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.agents = AgentService(db)

    def _spec(self, tenant_id: str, agent_id: uuid.UUID, requested: int | str | None) -> tuple[uuid.UUID, int | None, AgentSpec]:
        """(agent id, version, spec) whose context fields a context is checked against: a published version
        (latest when None) or, for `"draft"`, the draft as a parsed spec (version None). Only the spec's shape
        is needed here, so the draft is not held to the publish policy."""
        agent = self.agents.tenant_agent(tenant_id, agent_id)
        if requested == DRAFT:
            if agent.draft_spec is None:
                raise Conflict("Agent has no draft")
            try:
                return agent.id, None, AgentSpec.model_validate(agent.draft_spec)
            except ValidationError:
                raise InvalidInput("The draft is not a valid agent spec; fix it in the editor") from None
        assert requested is None or isinstance(requested, int)
        version = resolve_version(self.db, agent.id, requested)
        row = version_row(self.db, agent.id, version)
        assert row is not None  # versions are immutable and never deleted
        return agent.id, version, AgentSpec.model_validate(row.spec)

    def fields(self, tenant_id: str, agent_id: uuid.UUID, requested: int | str | None) -> dict[str, Any]:
        """{"agent_id", "version" (null for the draft), "draft", "fields": [...], "schema": {...}}."""
        found_id, resolved, spec = self._spec(tenant_id, agent_id, requested)
        return {
            "agent_id": str(found_id),
            "version": resolved,
            "draft": resolved is None,
            "fields": [f.model_dump(mode="json") for f in spec.context_fields],
            "schema": context_json_schema(spec.context_fields),
        }

    def validate(
        self, tenant_id: str, agent_id: uuid.UUID, requested: int | str | None, request_context: dict[str, Any] | None
    ) -> dict[str, Any]:
        """The accepted context (declared fields only); InvalidInput with the field errors otherwise."""
        _, _, spec = self._spec(tenant_id, agent_id, requested)
        return {"valid": True, "request_context": validate_context(spec, request_context)}
