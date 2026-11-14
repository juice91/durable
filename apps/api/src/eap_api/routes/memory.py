"""Agent memory: working memory, observations and forget-resource (RAG and memory milestone, RM.9).

Working memory (one note per agent and scope key; see eap_run_store.memory):

- GET    /conversations/{cid}/memory/working[?missing=null]     the conversation-scoped note of the
- PUT    /conversations/{cid}/memory/working                    conversation's agent
- DELETE /conversations/{cid}/memory/working
- GET    /agents/{aid}/memory/working?scope=&limit=             the agent's notes, newest first
- GET    /agents/{aid}/memory/resources/{rid}/working[?missing=null]  the agent's note for one resource
- PUT    /agents/{aid}/memory/resources/{rid}/working           (scope `resource`, keyed by
- DELETE /agents/{aid}/memory/resources/{rid}/working           agent + resource id)

Observations (conversation scope only):

- GET    /conversations/{cid}/memory/observations?include_superseded=
- DELETE /conversations/{cid}/memory/observations               all of them: 200 {deleted}
- DELETE /conversations/{cid}/memory/observations/{oid}         the newest active one only

Forget a resource (its notes, its message embeddings, the key names in its `memory_updated` events
and its working memory ledger rows; with `include_conversations=true` its conversations too, as
DELETE /conversations/{id}):

- DELETE /agents/{aid}/memory/resources/{rid}?include_conversations=   one agent's
- DELETE /memory/resources/{rid}?include_conversations=                every agent of the tenant

Rules:

- Tenant scope. The tenant is the principal's; every store call filters by it in SQL. Another
  tenant's agent, conversation or observation, a malformed id and an unknown id all answer 404 with
  the same message, so existence never leaks. The store's observation methods take the agent only
  as a filter, so these routes first load the conversation in the tenant and pass its own agent.
- Working memory reads and writes address one scope explicitly: the conversation routes the
  `conversation` scope of that conversation's agent, the resource routes the `resource` scope of
  (agent, resource id). Reads and deletes work whatever the agent's config says (a note left over
  after a config change can still be seen and cleared).
- PUT validates against the agent's **latest published version**: `memory.working` must be set
  (otherwise 422: there is nothing to validate against, and the runtime would never read the
  note), its `scope` must be the route's scope (otherwise 422 naming the right route), and the
  content must be a string in template mode or an object valid against `json_schema` in schema
  mode (WorkingMemoryInvalid: 422 whose message names a path and an expected type, never a value
  or a model-written key). `read_only` only stops the model's tool; a person may still edit.
  Edits are stored as sent (they are the user's own words): `memory.redact` applies to what the
  model writes, in the worker.
- `If-Match: <version>` (bare `N`, `"N"` or `W/"N"`; anything else is 422) on PUT and DELETE: 409 when the note's
  version differs; `If-Match: 0` on PUT means "create only". Without it the write is
  unconditional. GET and PUT answer `ETag: "<version>"`.
- Deleting one observation is allowed only for the conversation's newest active row (highest
  `covers_to_turn`, not superseded): coverage is the highest active `covers_to_turn`, so deleting
  an older one would silently hide its turns from both the history and the observations. Older
  rows answer 409; clear them with DELETE .../observations (all), or delete the newer ones first.
  The check runs in the store under the conversation lock the observer's insert takes.
  Retention markers (RM.15: rows whose text aged out, kept so their turns stay covered) are listed
  with `expired: true` and `content: ""` and count as rows here: when a marker is the newest, the
  visible rows before it answer 409 (deleting one would leave a hole under the marker's coverage),
  and the marker itself may be deleted (its turns return to the verbatim history and are observed
  again), after which the row before it is the newest.
- Conflicts are 409 with distinct messages: a version conflict, a not-newest observation, and a
  busy resource (a queued or running turn in one of its conversations: nothing deleted,
  `Retry-After` set, `busy_conversations` listed).
- Responses and errors never carry memory content except the note or observation being read.
- The forget audit record never carries the raw resource id (it may be an email address): it logs
  `eap.resource_hash`, `resource_audit_hash(resource_id)`, the first 16 hex characters of
  HMAC-SHA256(key=EAP_AUDIT_HASH_SALT, msg=resource_id) when that per-deployment salt is set, else
  of plain SHA-256(resource_id). An operator who knows a resource id recomputes it to find the
  record; set the salt so the hash of a guessable id (an email) cannot be looked up offline.
- A missing note: GET answers 404 (WORKING_MEMORY_NOT_FOUND) by default; with `?missing=null` it
  answers 200 with the JSON body `null` and `ETag: "0"` (version 0: PUT with `If-Match: 0` creates
  it). For clients that probe a note on every load (Studio's chat), so an absent note is not a
  failed request in the browser's console. A missing conversation, agent or resource id is 404
  either way.
- Deletes are idempotent: deleting a missing note answers 204; a forget of a resource with nothing
  left answers 200 with zero counts; deleting an observation that is gone answers 404.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, HTTPException, Query, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from eap_api.models import AgentModel, AgentVersionModel
from eap_api.request_guard import GuardedJsonRoute
from eap_api.routes.conversations import MemoryDep, canonical_uuid_or_404, turn_busy_409
from eap_api.auth import Principal
from eap_api.routes.runs import PrincipalDep, SessionDep, StoresDep
from eap_api.schemas import (
    BusyDetail,
    ErrorDetail,
    ForgetResourceResponse,
    ObservationListResponse,
    ObservationsDeleted,
    UnprocessableDetail,
    WorkingMemoryListResponse,
    WorkingMemoryPut,
    WorkingMemoryResponse,
)
from eap_run_store import ConversationNotFound, ResourceBusy, WorkingMemoryVersionConflict, validate_resource_id
from eap_run_store.memory import MemoryStore, Observation, ObservationNotLatest, WorkingMemory
from eap_schemas.memory import WorkingMemoryConfig, WorkingMemoryInvalid

CONVERSATION_NOT_FOUND = "Conversation not found"
AGENT_NOT_FOUND = "Agent not found"
RESOURCE_NOT_FOUND = "Resource not found"
log = logging.getLogger("eap_api")
WORKING_MEMORY_NOT_FOUND = "Working memory not found"
OBSERVATION_NOT_FOUND = "Observation not found"

router = APIRouter(
    tags=["memory"],
    route_class=GuardedJsonRoute,
    responses={404: {"model": ErrorDetail}},
)

IfMatch = Annotated[
    str | None,
    Header(
        alias="If-Match",
        max_length=64,
        description='The note version the edit is based on: 3, "3" or W/"3"; 0 = must not exist yet',
    ),
]

# Bare N, "N" or W/"N" only: quotes must be balanced, and W/ needs a quoted version.
_IF_MATCH = re.compile(r'^\s*(?:W/"(\d{1,9})"|"(\d{1,9})"|(\d{1,9}))\s*$')


# ----------------------------------------------------------------------------------- helpers


def _if_match(value: str | None) -> int | None:
    if value is None:
        return None
    match = _IF_MATCH.match(value)
    if match is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail='If-Match must be the note version (a number, optionally quoted), e.g. If-Match: "3"',
        )
    return int(next(g for g in match.groups() if g is not None))


def _conversation_or_404(stores: StoresDep, tenant_id: str, conversation_id: str) -> dict[str, Any]:
    conv_id = canonical_uuid_or_404(conversation_id, CONVERSATION_NOT_FOUND)
    conv = stores[0].get_conversation(tenant_id, conv_id)
    if conv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=CONVERSATION_NOT_FOUND)
    return conv


def _agent_or_404(db: SessionDep, tenant_id: str, agent_id: str) -> AgentModel:
    aid = canonical_uuid_or_404(agent_id, AGENT_NOT_FOUND)
    agent = db.query(AgentModel).filter(AgentModel.id == aid, AgentModel.tenant_id == tenant_id).first()
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=AGENT_NOT_FOUND)
    return agent


def _resource_or_404(resource_id: str) -> str:
    try:
        validate_resource_id(resource_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=RESOURCE_NOT_FOUND)
    return resource_id


def _working_config(db: SessionDep, tenant_id: str, agent_id: str) -> WorkingMemoryConfig:
    """`memory.working` of the agent's latest published version, or 422."""
    row = (
        db.query(AgentVersionModel)
        .filter(AgentVersionModel.agent_id == agent_id, AgentVersionModel.tenant_id == tenant_id)
        .order_by(AgentVersionModel.version.desc())
        .first()
    )
    memory = (row.spec or {}).get("memory") if row is not None else None
    working = memory.get("working") if isinstance(memory, dict) else None
    if working is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "the agent's latest published version has no working memory (memory.working); "
                "publish a version with working memory to edit it"
            ),
        )
    try:
        return WorkingMemoryConfig.model_validate(working)
    except ValidationError:  # pragma: no cover - a published spec was validated at publish
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="the agent's latest published working memory config is invalid; publish a new version",
        )


def _note_json(note: WorkingMemory) -> dict[str, Any]:
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


def _note_response(note: WorkingMemory) -> JSONResponse:
    return JSONResponse(content=jsonable_encoder(_note_json(note)), headers={"ETag": f'"{note.version}"'})


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


def _version_conflict(exc: WorkingMemoryVersionConflict) -> HTTPException:
    detail = (
        "working memory does not exist (version 0)"
        if exc.actual == 0
        else f"working memory is at version {exc.actual}"
    )
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=f"{detail}, not the If-Match version {exc.expected}; reload it and apply the edit again",
    )


# GET .../working?missing=null: an absent note is 200 `null` instead of 404 (see the module docstring).
MissingNote = Annotated[Literal["404", "null"], Query(description="'null': answer 200 with a null body when there is no note")]


def _get_note(
    memory: MemoryStore, tenant_id: str, agent_id: str, scope: str, key: str, missing: str = "404"
) -> JSONResponse:
    note = memory.get_working_memory(tenant_id, agent_id, scope, key)
    if note is None:
        if missing == "null":
            return JSONResponse(content=None, headers={"ETag": '"0"'})
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=WORKING_MEMORY_NOT_FOUND)
    return _note_response(note)


def _put_note(
    memory: MemoryStore,
    db: SessionDep,
    tenant_id: str,
    agent_id: str,
    scope: Literal["conversation", "resource"],
    key: str,
    body: WorkingMemoryPut,
    if_match: str | None,
) -> JSONResponse:
    expected = _if_match(if_match)
    config = _working_config(db, tenant_id, agent_id)
    if config.scope != scope:
        where = (
            "/agents/{agent_id}/memory/resources/{resource_id}/working"
            if config.scope == "resource"
            else "/conversations/{conversation_id}/memory/working"
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"this agent's working memory is {config.scope}-scoped; edit it at {where}",
        )
    try:
        if config.json_schema is not None:
            if not isinstance(body.content, dict):
                raise WorkingMemoryInvalid("the content must be a JSON object (the agent uses a JSON schema)")
            write = memory.update_working_memory(
                tenant_id, agent_id, scope=scope, scope_key=key,
                content=body.content, json_schema=config.json_schema, expected_version=expected,
            )
        else:
            if not isinstance(body.content, str):
                raise WorkingMemoryInvalid("the content must be a string (the agent uses a Markdown template)")
            write = memory.update_working_memory(
                tenant_id, agent_id, scope=scope, scope_key=key, content=body.content, expected_version=expected,
            )
    except WorkingMemoryInvalid as exc:  # path-only message: never a value or a model-written key
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc))
    except WorkingMemoryVersionConflict as exc:
        raise _version_conflict(exc)
    except ConversationNotFound:  # deleted between the route's check and the write
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=CONVERSATION_NOT_FOUND)
    # The row as this write stored it (RETURNING, same transaction): never a later writer's version,
    # and never a 404 because a concurrent delete came in after the commit.
    assert write.note is not None  # no ledger key here, so never a replay
    return _note_response(write.note)


def _delete_note(
    memory: MemoryStore, tenant_id: str, agent_id: str, scope: str, key: str, if_match: str | None
) -> Response:
    expected = _if_match(if_match)
    try:
        memory.delete_working_memory(tenant_id, agent_id, scope, key, expected_version=expected)
    except WorkingMemoryVersionConflict as exc:
        if exc.actual != 0:
            raise _version_conflict(exc)
        # The note is already gone (a retried or raced delete): the caller wanted it gone, so 204
        # whatever If-Match said.
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _forget(
    memory: MemoryStore, principal: Principal, resource_id: str, agent_id: str | None, include_conversations: bool
) -> JSONResponse:
    # TODO(RBAC): any principal of the tenant may forget any resource today (dev auth has no roles).
    # When RBAC lands, allow only resource_id == principal.user_id, or an admin.
    tenant_id = principal.tenant_id
    try:
        result = memory.forget_resource(
            tenant_id, resource_id, agent_id=agent_id, include_conversations=include_conversations
        )
    except ResourceBusy as busy:
        _audit_forget(principal, resource_id, agent_id, include_conversations, "busy", busy=len(busy.busy_conversations))
        return turn_busy_409(
            f"a turn is still queued or running in {len(busy.busy_conversations)} conversation(s) of this "
            "resource; nothing was deleted. Retry when the turns finish",
            list(busy.busy_conversations),
        )
    _audit_forget(
        principal, resource_id, agent_id, include_conversations, "done",
        working_memories=result.working_memories,
        message_embeddings=result.message_embeddings,
        conversations=len(result.conversations),
        busy=len(result.busy_conversations),
    )
    return JSONResponse(
        content={
            "resource_id": resource_id,
            "agent_id": agent_id,
            "working_memories_deleted": result.working_memories,
            "message_embeddings_deleted": result.message_embeddings,
            "conversations_deleted": list(result.conversations),
            "busy_conversations": list(result.busy_conversations),
        }
    )


AUDIT_HASH_SALT_ENV = "EAP_AUDIT_HASH_SALT"
AUDIT_HASH_HEX_CHARS = 16


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


def _audit_forget(
    principal: Principal,
    resource_id: str,
    agent_id: str | None,
    include_conversations: bool,
    outcome: str,
    **counts: int,
) -> None:
    """One audit record per forget call: who, which tenant, the resource (as resource_audit_hash,
    never the raw id) and agent (or all), the outcome and the counts. Ids, hashes and numbers only,
    never memory content. The tenant also comes from
    the request's log context (bound at authentication), so the record reaches Loki under it."""
    resource_hash = resource_audit_hash(resource_id)
    attrs = {
        "eap.audit": "memory_forget",
        "eap.tenant_id": principal.tenant_id,
        "eap.user_id": principal.user_id,
        "eap.resource_hash": resource_hash,
        "eap.agent_id": agent_id or "*",
        "eap.include_conversations": include_conversations,
        "eap.outcome": outcome,
        **{f"eap.{k}": v for k, v in counts.items()},
    }
    log.info(
        "memory forget %s: tenant=%s user=%s resource_hash=%s agent=%s include_conversations=%s%s",
        outcome, principal.tenant_id, principal.user_id, resource_hash, agent_id or "*", include_conversations,
        "".join(f" {k}={v}" for k, v in counts.items()),
        extra=attrs,
    )


_NOTE_RESPONSES: dict[int | str, dict[str, Any]] = {
    409: {"model": ErrorDetail, "description": "If-Match does not match the note's version"},
    422: {
        "model": UnprocessableDetail,
        "description": "Invalid content, no working memory config, wrong scope, or a malformed If-Match",
    },
}
_ETAG_200: dict[int | str, dict[str, Any]] = {
    200: {
        "description": "The note",
        "headers": {"ETag": {"description": 'The note version, quoted ("3"); send it back as If-Match', "schema": {"type": "string"}}},
    },
}
_IF_MATCH_422: dict[int | str, dict[str, Any]] = {
    422: {"model": UnprocessableDetail, "description": "A malformed If-Match"},
}
_ETAG_200 = {**_ETAG_200, 422: {"model": UnprocessableDetail, "description": "Request validation failed"}}
_BUSY_RESPONSES: dict[int | str, dict[str, Any]] = {
    409: {"model": BusyDetail, "description": "A turn of the resource is queued or running; retry after it ends"},
}


# ---------------------------------------------------------------------- conversation-scoped notes


# `| None`: with `?missing=null` an absent note is a 200 JSON `null` (OpenAPI shows the nullable body).
@router.get(
    "/conversations/{conversation_id}/memory/working", response_model=WorkingMemoryResponse | None, responses=_ETAG_200
)
def get_conversation_working_memory(
    conversation_id: str, principal: PrincipalDep, stores: StoresDep, memory: MemoryDep, missing: MissingNote = "404"
) -> JSONResponse:
    """The conversation-scoped note of the conversation's agent. 404 when the conversation is not
    the tenant's, or when it has no note (with `missing=null`: 200 `null`, ETag "0")."""
    conv = _conversation_or_404(stores, principal.tenant_id, conversation_id)
    return _get_note(memory, principal.tenant_id, conv["agent_id"], "conversation", conv["id"], missing)


@router.put(
    "/conversations/{conversation_id}/memory/working",
    response_model=WorkingMemoryResponse,
    responses={**_ETAG_200, **_NOTE_RESPONSES},
)
def put_conversation_working_memory(
    conversation_id: str,
    body: WorkingMemoryPut,
    principal: PrincipalDep,
    db: SessionDep,
    stores: StoresDep,
    memory: MemoryDep,
    if_match: IfMatch = None,
) -> JSONResponse:
    """Replace the conversation-scoped note (the agent's latest published `memory.working` must be
    conversation-scoped). 200 with the note and its ETag."""
    conv = _conversation_or_404(stores, principal.tenant_id, conversation_id)
    return _put_note(memory, db, principal.tenant_id, conv["agent_id"], "conversation", conv["id"], body, if_match)


@router.delete(
    "/conversations/{conversation_id}/memory/working",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={409: _NOTE_RESPONSES[409], **_IF_MATCH_422},
)
def delete_conversation_working_memory(
    conversation_id: str, principal: PrincipalDep, stores: StoresDep, memory: MemoryDep, if_match: IfMatch = None
) -> Response:
    """Clear the conversation-scoped note; 204 also when there was none."""
    conv = _conversation_or_404(stores, principal.tenant_id, conversation_id)
    return _delete_note(memory, principal.tenant_id, conv["agent_id"], "conversation", conv["id"], if_match)


# -------------------------------------------------------------------------- resource-scoped notes


@router.get("/agents/{agent_id}/memory/working", response_model=WorkingMemoryListResponse)
def list_agent_working_memories(
    agent_id: str,
    principal: PrincipalDep,
    db: SessionDep,
    memory: MemoryDep,
    scope: Literal["conversation", "resource"] | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> JSONResponse:
    """The agent's notes (optionally one scope), most recently updated first, at most `limit`."""
    agent = _agent_or_404(db, principal.tenant_id, agent_id)
    notes = memory.list_working_memories(principal.tenant_id, agent.id, scope=scope, limit=limit)
    return JSONResponse(content=jsonable_encoder({"items": [_note_json(n) for n in notes]}))


@router.get(
    "/agents/{agent_id}/memory/resources/{resource_id}/working",
    response_model=WorkingMemoryResponse | None,
    responses=_ETAG_200,
)
def get_resource_working_memory(
    agent_id: str,
    resource_id: str,
    principal: PrincipalDep,
    db: SessionDep,
    memory: MemoryDep,
    missing: MissingNote = "404",
) -> JSONResponse:
    """The agent's note for this resource. 404 for another tenant's agent or an invalid resource
    id, and for no note (with `missing=null`: 200 `null`, ETag "0")."""
    agent = _agent_or_404(db, principal.tenant_id, agent_id)
    return _get_note(memory, principal.tenant_id, agent.id, "resource", _resource_or_404(resource_id), missing)


@router.put(
    "/agents/{agent_id}/memory/resources/{resource_id}/working",
    response_model=WorkingMemoryResponse,
    responses={**_ETAG_200, **_NOTE_RESPONSES},
)
def put_resource_working_memory(
    agent_id: str,
    resource_id: str,
    body: WorkingMemoryPut,
    principal: PrincipalDep,
    db: SessionDep,
    memory: MemoryDep,
    if_match: IfMatch = None,
) -> JSONResponse:
    """Replace the agent's note for this resource (the agent's latest published `memory.working`
    must be resource-scoped). 200 with the note and its ETag."""
    agent = _agent_or_404(db, principal.tenant_id, agent_id)
    rid = _resource_or_404(resource_id)
    return _put_note(memory, db, principal.tenant_id, agent.id, "resource", rid, body, if_match)


@router.delete(
    "/agents/{agent_id}/memory/resources/{resource_id}/working",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={409: _NOTE_RESPONSES[409], **_IF_MATCH_422},
)
def delete_resource_working_memory(
    agent_id: str,
    resource_id: str,
    principal: PrincipalDep,
    db: SessionDep,
    memory: MemoryDep,
    if_match: IfMatch = None,
) -> Response:
    """Clear the agent's note for this resource; 204 also when there was none."""
    agent = _agent_or_404(db, principal.tenant_id, agent_id)
    rid = _resource_or_404(resource_id)
    return _delete_note(memory, principal.tenant_id, agent.id, "resource", rid, if_match)


# ----------------------------------------------------------------------------------- observations


@router.get("/conversations/{conversation_id}/memory/observations", response_model=ObservationListResponse)
def list_observations(
    conversation_id: str,
    principal: PrincipalDep,
    stores: StoresDep,
    memory: MemoryDep,
    include_superseded: bool = False,
) -> JSONResponse:
    """The conversation's observations and reflections ordered by covered turns (active only by
    default), and `covered_to_turn`. Retention markers are listed with `expired: true` and no
    content; they count toward `covered_to_turn` like any active row."""
    conv = _conversation_or_404(stores, principal.tenant_id, conversation_id)
    rows = memory.list_observations(
        principal.tenant_id,
        conv["id"],
        agent_id=conv["agent_id"],
        include_superseded=include_superseded,
        include_markers=True,
    )
    covered = max((o.covers_to_turn for o in rows if o.superseded_by is None), default=0)
    return JSONResponse(content=jsonable_encoder({"items": [_obs_json(o) for o in rows], "covered_to_turn": covered}))


@router.delete("/conversations/{conversation_id}/memory/observations", response_model=ObservationsDeleted)
def delete_observations(
    conversation_id: str, principal: PrincipalDep, stores: StoresDep, memory: MemoryDep
) -> JSONResponse:
    """Delete every observation and reflection of the conversation (its whole history is then
    verbatim again, within the history limits). 200 {deleted}; 0 when there were none."""
    conv = _conversation_or_404(stores, principal.tenant_id, conversation_id)
    deleted = memory.delete_observations(principal.tenant_id, conv["id"], agent_id=conv["agent_id"])
    return JSONResponse(content={"deleted": deleted})


@router.delete(
    "/conversations/{conversation_id}/memory/observations/{observation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={409: {"model": ErrorDetail, "description": "Not the newest active observation"}},
)
def delete_observation(
    conversation_id: str, observation_id: str, principal: PrincipalDep, stores: StoresDep, memory: MemoryDep
) -> Response:
    """Delete the conversation's newest active observation or reflection (and the rows a reflection
    superseded); its turns return to the verbatim history. 409 for any other row. 404 unknown,
    malformed, already deleted, or another conversation's."""
    conv = _conversation_or_404(stores, principal.tenant_id, conversation_id)
    oid = canonical_uuid_or_404(observation_id, OBSERVATION_NOT_FOUND)
    # The store matches the row by tenant, conversation and id: a row of another conversation (or
    # tenant) deletes nothing and answers 404. The conversation was loaded in the tenant above, and
    # its observations are its own agent's.
    try:
        deleted = memory.delete_observation(principal.tenant_id, conv["id"], oid, only_latest=True)
    except ObservationNotLatest:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "only the newest active observation (expired ones included) can be deleted (deleting an "
                "older one would hide its turns from the model); delete the newer ones first, or delete "
                "all observations"
            ),
        )
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=OBSERVATION_NOT_FOUND)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ------------------------------------------------------------------------------- forget resource


@router.delete(
    "/agents/{agent_id}/memory/resources/{resource_id}",
    response_model=ForgetResourceResponse,
    responses=_BUSY_RESPONSES,
)
def forget_agent_resource(
    agent_id: str,
    resource_id: str,
    principal: PrincipalDep,
    db: SessionDep,
    memory: MemoryDep,
    include_conversations: bool = False,
) -> JSONResponse:
    """Forget the resource for this agent: its resource-scoped note and the message embeddings
    indexed under the resource, in one transaction; with `include_conversations=true` also the
    agent's conversations of the resource (each as DELETE /conversations/{id}). All-or-nothing:
    409 with Retry-After while one of those conversations has a queued or running turn."""
    agent = _agent_or_404(db, principal.tenant_id, agent_id)
    rid = _resource_or_404(resource_id)
    return _forget(memory, principal, rid, agent.id, include_conversations)


@router.delete("/memory/resources/{resource_id}", response_model=ForgetResourceResponse, responses=_BUSY_RESPONSES)
def forget_resource(
    resource_id: str, principal: PrincipalDep, memory: MemoryDep, include_conversations: bool = False
) -> JSONResponse:
    """Forget the resource across every agent of the tenant (as the per-agent route)."""
    rid = _resource_or_404(resource_id)
    return _forget(memory, principal, rid, None, include_conversations)


__all__ = ["router"]
