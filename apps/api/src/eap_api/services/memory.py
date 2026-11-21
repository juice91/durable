"""Agent memory: working memory, observations and forget-resource (RAG and memory milestone, RM.9).

The business logic behind `eap_api.routes.memory`; the rules are documented there. Every call is scoped by the
tenant id; another tenant's agent, conversation or observation, a malformed id and an unknown id all refuse
with the same 404 message, so existence never leaks. Refusals keep the bodies the routes always answered
(`refusal`); a forget refused for a busy resource raises `ResourceBusyRefusal` (the route adds `Retry-After`).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import uuid
from typing import Any, Literal

from pydantic import ValidationError
from sqlalchemy.orm import Session

from eap_api.models import AgentModel, AgentVersionModel
from eap_api.services.errors import Conflict
from eap_api.services.refusals import refusal
from eap_run_store import ConversationNotFound, ResourceBusy, RunStore, WorkingMemoryVersionConflict, validate_resource_id
from eap_run_store.memory import MemoryStore, Observation, ObservationNotLatest, WorkingMemory
from eap_schemas.memory import WorkingMemoryConfig, WorkingMemoryInvalid

CONVERSATION_NOT_FOUND = "Conversation not found"
AGENT_NOT_FOUND = "Agent not found"
RESOURCE_NOT_FOUND = "Resource not found"
WORKING_MEMORY_NOT_FOUND = "Working memory not found"
OBSERVATION_NOT_FOUND = "Observation not found"
log = logging.getLogger("eap_api")

# Bare N, "N" or W/"N" only: quotes must be balanced, and W/ needs a quoted version.
_IF_MATCH = re.compile(r'^\s*(?:W/"(\d{1,9})"|"(\d{1,9})"|(\d{1,9}))\s*$')

AUDIT_HASH_SALT_ENV = "EAP_AUDIT_HASH_SALT"
AUDIT_HASH_HEX_CHARS = 16


class ResourceBusyRefusal(Conflict):
    """A forget refused while a turn is queued or running in one of the resource's conversations."""

    def __init__(self, busy_conversations: list[str]) -> None:
        super().__init__(
            f"a turn is still queued or running in {len(busy_conversations)} conversation(s) of this "
            "resource; nothing was deleted. Retry when the turns finish"
        )
        self.busy_conversations = busy_conversations


def parse_if_match(value: str | None) -> int | None:
    """The note version an `If-Match` header names (bare `N`, `"N"` or `W/"N"`); 422 for anything else."""
    if value is None:
        return None
    match = _IF_MATCH.match(value)
    if match is None:
        raise refusal(422, 'If-Match must be the note version (a number, optionally quoted), e.g. If-Match: "3"')
    return int(next(g for g in match.groups() if g is not None))


def canonical_uuid_or_404(value: str, detail: str) -> str:
    """A path id as a canonical UUID string; a malformed one is refused like an unknown one (404)."""
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise refusal(404, detail) from None


def resource_audit_hash(resource_id: str) -> str:
    """The short stable hash an audit record carries instead of a resource id: the first 16 hex
    characters of HMAC-SHA256 keyed with EAP_AUDIT_HASH_SALT (read per call), or of plain SHA-256
    when the salt is unset or blank."""
    data = resource_id.encode("utf-8")
    salt = os.environ.get(AUDIT_HASH_SALT_ENV, "")
    if salt.strip():
        digest = hmac.new(salt.encode("utf-8"), data, hashlib.sha256).hexdigest()
    else:
        digest = hashlib.sha256(data).hexdigest()
    return digest[:AUDIT_HASH_HEX_CHARS]


def note_json(note: WorkingMemory) -> dict[str, Any]:
    return {
        "agent_id": note.agent_id,
        "scope": note.scope,
        "scope_key": note.scope_key,
        "format": note.format,
        "content": note.content,
        "version": note.version,
        "updated_by_run_id": note.updated_by_run_id,
        "created_at": note.created_at,
        "updated_at": note.updated_at,
    }


def _obs_json(obs: Observation) -> dict[str, Any]:
    return {
        "id": obs.id,
        "agent_id": obs.agent_id,
        "conversation_id": obs.conversation_id,
        "kind": obs.kind,
        "covers_from_turn": obs.covers_from_turn,
        "covers_to_turn": obs.covers_to_turn,
        "content": obs.content,
        "approx_tokens": obs.approx_tokens,
        "run_id": obs.run_id,
        "superseded_by": obs.superseded_by,
        "created_at": obs.created_at,
        "expired": obs.is_marker,
    }


def _version_conflict(exc: WorkingMemoryVersionConflict) -> Exception:
    detail = (
        "working memory does not exist (version 0)"
        if exc.actual == 0
        else f"working memory is at version {exc.actual}"
    )
    return refusal(409, f"{detail}, not the If-Match version {exc.expected}; reload it and apply the edit again")


def _audit_forget(
    tenant_id: str,
    user_id: str | None,
    resource_id: str,
    agent_id: str | None,
    include_conversations: bool,
    outcome: str,
    **counts: int,
) -> None:
    """One audit record per forget call: who, which tenant, the resource (as resource_audit_hash,
    never the raw id) and agent (or all), the outcome and the counts. Ids, hashes and numbers only,
    never memory content. The tenant also comes from the request's log context (bound at
    authentication), so the record reaches Loki under it."""
    resource_hash = resource_audit_hash(resource_id)
    attrs = {
        "eap.audit": "memory_forget",
        "eap.tenant_id": tenant_id,
        "eap.user_id": user_id,
        "eap.resource_hash": resource_hash,
        "eap.agent_id": agent_id or "*",
        "eap.include_conversations": include_conversations,
        "eap.outcome": outcome,
        **{f"eap.{k}": v for k, v in counts.items()},
    }
    log.info(
        "memory forget %s: tenant=%s user=%s resource_hash=%s agent=%s include_conversations=%s%s",
        outcome, tenant_id, user_id, resource_hash, agent_id or "*", include_conversations,
        "".join(f" {k}={v}" for k, v in counts.items()),
        extra=attrs,
    )


class MemoryService:
    """Working memory notes, observations and the forget of a resource (tenant-scoped)."""

    def __init__(self, memory: MemoryStore, run_store: RunStore, engine: Any) -> None:
        self.memory = memory
        self.run_store = run_store
        self.engine = engine

    # ------------------------------------------------------------------ lookups

    def _conversation_or_404(self, tenant_id: str, conversation_id: str) -> dict[str, Any]:
        conv_id = canonical_uuid_or_404(conversation_id, CONVERSATION_NOT_FOUND)
        conv = self.run_store.get_conversation(tenant_id, conv_id)
        if conv is None:
            raise refusal(404, CONVERSATION_NOT_FOUND)
        return conv

    def _agent_id_or_404(self, tenant_id: str, agent_id: str) -> str:
        aid = canonical_uuid_or_404(agent_id, AGENT_NOT_FOUND)
        with Session(self.engine) as db:
            agent = db.query(AgentModel).filter(AgentModel.id == aid, AgentModel.tenant_id == tenant_id).first()
            if agent is None:
                raise refusal(404, AGENT_NOT_FOUND)
            return str(agent.id)

    @staticmethod
    def _resource_or_404(resource_id: str) -> str:
        try:
            validate_resource_id(resource_id)
        except ValueError:
            raise refusal(404, RESOURCE_NOT_FOUND) from None
        return resource_id

    def _working_config(self, tenant_id: str, agent_id: str) -> WorkingMemoryConfig:
        """`memory.working` of the agent's latest published version, or 422."""
        with Session(self.engine) as db:
            row = (
                db.query(AgentVersionModel)
                .filter(AgentVersionModel.agent_id == agent_id, AgentVersionModel.tenant_id == tenant_id)
                .order_by(AgentVersionModel.version.desc())
                .first()
            )
            memory = (row.spec or {}).get("memory") if row is not None else None
        working = memory.get("working") if isinstance(memory, dict) else None
        if working is None:
            raise refusal(
                422,
                "the agent's latest published version has no working memory (memory.working); "
                "publish a version with working memory to edit it",
            )
        try:
            return WorkingMemoryConfig.model_validate(working)
        except ValidationError:  # pragma: no cover - a published spec was validated at publish
            raise refusal(422, "the agent's latest published working memory config is invalid; publish a new version") from None

    # ------------------------------------------------------------------ notes (shared)

    def _get_note(self, tenant_id: str, agent_id: str, scope: str, key: str, missing: str) -> dict[str, Any] | None:
        note = self.memory.get_working_memory(tenant_id, agent_id, scope, key)
        if note is None:
            if missing == "null":
                return None
            raise refusal(404, WORKING_MEMORY_NOT_FOUND)
        return note_json(note)

    def _put_note(
        self, tenant_id: str, agent_id: str, scope: Literal["conversation", "resource"], key: str,
        content: Any, if_match: str | None,
    ) -> dict[str, Any]:
        expected = parse_if_match(if_match)
        config = self._working_config(tenant_id, agent_id)
        if config.scope != scope:
            where = (
                "/agents/{agent_id}/memory/resources/{resource_id}/working"
                if config.scope == "resource"
                else "/conversations/{conversation_id}/memory/working"
            )
            raise refusal(422, f"this agent's working memory is {config.scope}-scoped; edit it at {where}")
        memory = self.memory
        try:
            if config.json_schema is not None:
                if not isinstance(content, dict):
                    raise WorkingMemoryInvalid("the content must be a JSON object (the agent uses a JSON schema)")
                write = memory.update_working_memory(
                    tenant_id, agent_id, scope=scope, scope_key=key,
                    content=content, json_schema=config.json_schema, expected_version=expected,
                )
            else:
                if not isinstance(content, str):
                    raise WorkingMemoryInvalid("the content must be a string (the agent uses a Markdown template)")
                write = memory.update_working_memory(
                    tenant_id, agent_id, scope=scope, scope_key=key, content=content, expected_version=expected,
                )
        except WorkingMemoryInvalid as exc:  # path-only message: never a value or a model-written key
            raise refusal(422, str(exc)) from None
        except WorkingMemoryVersionConflict as exc:
            raise _version_conflict(exc) from None
        except ConversationNotFound:  # deleted between the check and the write
            raise refusal(404, CONVERSATION_NOT_FOUND) from None
        # The row as this write stored it (RETURNING, same transaction): never a later writer's version,
        # and never a 404 because a concurrent delete came in after the commit.
        assert write.note is not None  # no ledger key here, so never a replay
        return note_json(write.note)

    def _delete_note(self, tenant_id: str, agent_id: str, scope: str, key: str, if_match: str | None) -> None:
        expected = parse_if_match(if_match)
        try:
            self.memory.delete_working_memory(tenant_id, agent_id, scope, key, expected_version=expected)
        except WorkingMemoryVersionConflict as exc:
            if exc.actual != 0:
                raise _version_conflict(exc) from None
            # The note is already gone (a retried or raced delete): the caller wanted it gone, so 204
            # whatever If-Match said.

    # ------------------------------------------------------------------ conversation-scoped notes

    def get_conversation_note(self, tenant_id: str, conversation_id: str, missing: str = "404") -> dict[str, Any] | None:
        conv = self._conversation_or_404(tenant_id, conversation_id)
        return self._get_note(tenant_id, conv["agent_id"], "conversation", conv["id"], missing)

    def put_conversation_note(
        self, tenant_id: str, conversation_id: str, content: Any, if_match: str | None
    ) -> dict[str, Any]:
        conv = self._conversation_or_404(tenant_id, conversation_id)
        return self._put_note(tenant_id, conv["agent_id"], "conversation", conv["id"], content, if_match)

    def delete_conversation_note(self, tenant_id: str, conversation_id: str, if_match: str | None) -> None:
        conv = self._conversation_or_404(tenant_id, conversation_id)
        self._delete_note(tenant_id, conv["agent_id"], "conversation", conv["id"], if_match)

    # ------------------------------------------------------------------ resource-scoped notes

    def list_agent_notes(self, tenant_id: str, agent_id: str, scope: str | None, limit: int) -> dict[str, Any]:
        aid = self._agent_id_or_404(tenant_id, agent_id)
        notes = self.memory.list_working_memories(tenant_id, aid, scope=scope, limit=limit)
        return {"items": [note_json(n) for n in notes]}

    def get_resource_note(
        self, tenant_id: str, agent_id: str, resource_id: str, missing: str = "404"
    ) -> dict[str, Any] | None:
        aid = self._agent_id_or_404(tenant_id, agent_id)
        return self._get_note(tenant_id, aid, "resource", self._resource_or_404(resource_id), missing)

    def put_resource_note(
        self, tenant_id: str, agent_id: str, resource_id: str, content: Any, if_match: str | None
    ) -> dict[str, Any]:
        aid = self._agent_id_or_404(tenant_id, agent_id)
        rid = self._resource_or_404(resource_id)
        return self._put_note(tenant_id, aid, "resource", rid, content, if_match)

    def delete_resource_note(self, tenant_id: str, agent_id: str, resource_id: str, if_match: str | None) -> None:
        aid = self._agent_id_or_404(tenant_id, agent_id)
        rid = self._resource_or_404(resource_id)
        self._delete_note(tenant_id, aid, "resource", rid, if_match)

    # ------------------------------------------------------------------ observations

    def list_observations(self, tenant_id: str, conversation_id: str, include_superseded: bool) -> dict[str, Any]:
        conv = self._conversation_or_404(tenant_id, conversation_id)
        rows = self.memory.list_observations(
            tenant_id, conv["id"], agent_id=conv["agent_id"],
            include_superseded=include_superseded, include_markers=True,
        )
        covered = max((o.covers_to_turn for o in rows if o.superseded_by is None), default=0)
        return {"items": [_obs_json(o) for o in rows], "covered_to_turn": covered}

    def delete_observations(self, tenant_id: str, conversation_id: str) -> int:
        conv = self._conversation_or_404(tenant_id, conversation_id)
        return self.memory.delete_observations(tenant_id, conv["id"], agent_id=conv["agent_id"])

    def delete_observation(self, tenant_id: str, conversation_id: str, observation_id: str) -> None:
        conv = self._conversation_or_404(tenant_id, conversation_id)
        oid = canonical_uuid_or_404(observation_id, OBSERVATION_NOT_FOUND)
        # The store matches the row by tenant, conversation and id: a row of another conversation (or
        # tenant) deletes nothing and answers 404. The conversation was loaded in the tenant above, and
        # its observations are its own agent's.
        try:
            deleted = self.memory.delete_observation(tenant_id, conv["id"], oid, only_latest=True)
        except ObservationNotLatest:
            raise refusal(
                409,
                "only the newest active observation (expired ones included) can be deleted (deleting an "
                "older one would hide its turns from the model); delete the newer ones first, or delete "
                "all observations",
            ) from None
        if not deleted:
            raise refusal(404, OBSERVATION_NOT_FOUND)

    # ------------------------------------------------------------------ forget a resource

    def forget_agent_resource(
        self, tenant_id: str, user_id: str | None, agent_id: str, resource_id: str, include_conversations: bool
    ) -> dict[str, Any]:
        aid = self._agent_id_or_404(tenant_id, agent_id)
        rid = self._resource_or_404(resource_id)
        return self._forget(tenant_id, user_id, rid, aid, include_conversations)

    def forget_resource(
        self, tenant_id: str, user_id: str | None, resource_id: str, include_conversations: bool
    ) -> dict[str, Any]:
        rid = self._resource_or_404(resource_id)
        return self._forget(tenant_id, user_id, rid, None, include_conversations)

    def _forget(
        self, tenant_id: str, user_id: str | None, resource_id: str, agent_id: str | None, include_conversations: bool
    ) -> dict[str, Any]:
        # TODO(RBAC): any principal of the tenant may forget any resource today (dev auth has no roles).
        # When RBAC lands, allow only resource_id == the principal's user id, or an admin.
        try:
            result = self.memory.forget_resource(
                tenant_id, resource_id, agent_id=agent_id, include_conversations=include_conversations
            )
        except ResourceBusy as busy:
            _audit_forget(
                tenant_id, user_id, resource_id, agent_id, include_conversations, "busy",
                busy=len(busy.busy_conversations),
            )
            raise ResourceBusyRefusal(list(busy.busy_conversations)) from None
        _audit_forget(
            tenant_id, user_id, resource_id, agent_id, include_conversations, "done",
            working_memories=result.working_memories,
            message_embeddings=result.message_embeddings,
            conversations=len(result.conversations),
            busy=len(result.busy_conversations),
        )
        return {
            "resource_id": resource_id,
            "agent_id": agent_id,
            "working_memories_deleted": result.working_memories,
            "message_embeddings_deleted": result.message_embeddings,
            "conversations_deleted": list(result.conversations),
            "busy_conversations": list(result.busy_conversations),
        }
