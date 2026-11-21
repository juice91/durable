"""Agent memory: working memory, observations and forget-resource (RAG and memory milestone, RM.9).

Working memory (one note per agent and scope key; see the memory store):

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

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, Query, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from eap_api.request_guard import GuardedJsonRoute
from eap_api.routes.conversations import turn_busy_409
from eap_api.routes.runs import PrincipalDep
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
from eap_api.service_deps.knowledge import MemoryServiceDep
from eap_api.services.memory import (  # noqa: F401  (re-exported: tests use these names)
    AUDIT_HASH_SALT_ENV,
    AUDIT_HASH_HEX_CHARS,
    ResourceBusyRefusal,
    resource_audit_hash,
)

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

# GET .../working?missing=null: an absent note is 200 `null` instead of 404 (see the module docstring).
MissingNote = Annotated[Literal["404", "null"], Query(description="'null': answer 200 with a null body when there is no note")]


def _note_response(note: dict[str, Any] | None) -> JSONResponse:
    """The note with its `ETag` (the version); no note (`missing=null`) is 200 `null` with ETag "0"."""
    if note is None:
        return JSONResponse(content=None, headers={"ETag": '"0"'})
    return JSONResponse(content=jsonable_encoder(note), headers={"ETag": f'"{note["version"]}"'})


def _forget_response(forget: Any) -> JSONResponse:
    try:
        return JSONResponse(content=forget())
    except ResourceBusyRefusal as busy:
        return turn_busy_409(busy.message, busy.busy_conversations)


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
    conversation_id: str, principal: PrincipalDep, service: MemoryServiceDep, missing: MissingNote = "404"
) -> JSONResponse:
    """The conversation-scoped note of the conversation's agent. 404 when the conversation is not
    the tenant's, or when it has no note (with `missing=null`: 200 `null`, ETag "0")."""
    return _note_response(service.get_conversation_note(principal.tenant_id, conversation_id, missing))


@router.put(
    "/conversations/{conversation_id}/memory/working",
    response_model=WorkingMemoryResponse,
    responses={**_ETAG_200, **_NOTE_RESPONSES},
)
def put_conversation_working_memory(
    conversation_id: str,
    body: WorkingMemoryPut,
    principal: PrincipalDep,
    service: MemoryServiceDep,
    if_match: IfMatch = None,
) -> JSONResponse:
    """Replace the conversation-scoped note (the agent's latest published `memory.working` must be
    conversation-scoped). 200 with the note and its ETag."""
    return _note_response(service.put_conversation_note(principal.tenant_id, conversation_id, body.content, if_match))


@router.delete(
    "/conversations/{conversation_id}/memory/working",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={409: _NOTE_RESPONSES[409], **_IF_MATCH_422},
)
def delete_conversation_working_memory(
    conversation_id: str, principal: PrincipalDep, service: MemoryServiceDep, if_match: IfMatch = None
) -> Response:
    """Clear the conversation-scoped note; 204 also when there was none."""
    service.delete_conversation_note(principal.tenant_id, conversation_id, if_match)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# -------------------------------------------------------------------------- resource-scoped notes


@router.get("/agents/{agent_id}/memory/working", response_model=WorkingMemoryListResponse)
def list_agent_working_memories(
    agent_id: str,
    principal: PrincipalDep,
    service: MemoryServiceDep,
    scope: Literal["conversation", "resource"] | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> JSONResponse:
    """The agent's notes (optionally one scope), most recently updated first, at most `limit`."""
    return JSONResponse(content=jsonable_encoder(service.list_agent_notes(principal.tenant_id, agent_id, scope, limit)))


@router.get(
    "/agents/{agent_id}/memory/resources/{resource_id}/working",
    response_model=WorkingMemoryResponse | None,
    responses=_ETAG_200,
)
def get_resource_working_memory(
    agent_id: str,
    resource_id: str,
    principal: PrincipalDep,
    service: MemoryServiceDep,
    missing: MissingNote = "404",
) -> JSONResponse:
    """The agent's note for this resource. 404 for another tenant's agent or an invalid resource
    id, and for no note (with `missing=null`: 200 `null`, ETag "0")."""
    return _note_response(service.get_resource_note(principal.tenant_id, agent_id, resource_id, missing))


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
    service: MemoryServiceDep,
    if_match: IfMatch = None,
) -> JSONResponse:
    """Replace the agent's note for this resource (the agent's latest published `memory.working`
    must be resource-scoped). 200 with the note and its ETag."""
    return _note_response(
        service.put_resource_note(principal.tenant_id, agent_id, resource_id, body.content, if_match)
    )


@router.delete(
    "/agents/{agent_id}/memory/resources/{resource_id}/working",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={409: _NOTE_RESPONSES[409], **_IF_MATCH_422},
)
def delete_resource_working_memory(
    agent_id: str,
    resource_id: str,
    principal: PrincipalDep,
    service: MemoryServiceDep,
    if_match: IfMatch = None,
) -> Response:
    """Clear the agent's note for this resource; 204 also when there was none."""
    service.delete_resource_note(principal.tenant_id, agent_id, resource_id, if_match)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ----------------------------------------------------------------------------------- observations


@router.get("/conversations/{conversation_id}/memory/observations", response_model=ObservationListResponse)
def list_observations(
    conversation_id: str,
    principal: PrincipalDep,
    service: MemoryServiceDep,
    include_superseded: bool = False,
) -> JSONResponse:
    """The conversation's observations and reflections ordered by covered turns (active only by
    default), and `covered_to_turn`. Retention markers are listed with `expired: true` and no
    content; they count toward `covered_to_turn` like any active row."""
    return JSONResponse(
        content=jsonable_encoder(service.list_observations(principal.tenant_id, conversation_id, include_superseded))
    )


@router.delete("/conversations/{conversation_id}/memory/observations", response_model=ObservationsDeleted)
def delete_observations(conversation_id: str, principal: PrincipalDep, service: MemoryServiceDep) -> JSONResponse:
    """Delete every observation and reflection of the conversation (its whole history is then
    verbatim again, within the history limits). 200 {deleted}; 0 when there were none."""
    return JSONResponse(content={"deleted": service.delete_observations(principal.tenant_id, conversation_id)})


@router.delete(
    "/conversations/{conversation_id}/memory/observations/{observation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={409: {"model": ErrorDetail, "description": "Not the newest active observation"}},
)
def delete_observation(
    conversation_id: str, observation_id: str, principal: PrincipalDep, service: MemoryServiceDep
) -> Response:
    """Delete the conversation's newest active observation or reflection (and the rows a reflection
    superseded); its turns return to the verbatim history. 409 for any other row. 404 unknown,
    malformed, already deleted, or another conversation's."""
    service.delete_observation(principal.tenant_id, conversation_id, observation_id)
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
    service: MemoryServiceDep,
    include_conversations: bool = False,
) -> JSONResponse:
    """Forget the resource for this agent: its resource-scoped note and the message embeddings
    indexed under the resource, in one transaction; with `include_conversations=true` also the
    agent's conversations of the resource (each as DELETE /conversations/{id}). All-or-nothing:
    409 with Retry-After while one of those conversations has a queued or running turn."""
    return _forget_response(
        lambda: service.forget_agent_resource(
            principal.tenant_id, principal.user_id, agent_id, resource_id, include_conversations
        )
    )


@router.delete("/memory/resources/{resource_id}", response_model=ForgetResourceResponse, responses=_BUSY_RESPONSES)
def forget_resource(
    resource_id: str, principal: PrincipalDep, service: MemoryServiceDep, include_conversations: bool = False
) -> JSONResponse:
    """Forget the resource across every agent of the tenant (as the per-agent route)."""
    return _forget_response(
        lambda: service.forget_resource(principal.tenant_id, principal.user_id, resource_id, include_conversations)
    )


__all__ = ["router"]
