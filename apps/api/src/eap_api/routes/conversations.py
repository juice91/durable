"""Conversation routes: a tenant-owned thread with one agent whose turns are runs.

- POST /agents/{agent_id}/conversations   create (idempotent on an optional client conversation_id)
- GET  /conversations                     the tenant's conversations, most recently active first,
                                          paged (?agent_id=&limit=&before=)
- GET  /conversations/{id}                one conversation with its runs in turn order
- PATCH /conversations/{id}               rename (title 1–200 characters)
- POST /conversations/{id}/runs           start the next turn
- DELETE /conversations/{id}              delete the conversation and its derived memory (RM.9)

A turn is an ordinary run (same enqueue, same events, same SSE) whose row also carries
conversation_id and turn; the worker builds the turn's message history from the earlier completed
turns. The next turn is refused with 409 while the latest turn is not terminal. The first turn titles an
untitled conversation from its input (first 60 characters, one line), under the conversation lock. Starting a turn is
idempotent on the client run_id exactly like POST /agents/{id}/runs: the row is inserted under the
conversation's lock, then the workflow is enqueued on every attempt, so a crash between the two
heals when the client retries. Another tenant's or an unknown conversation is a 404.

Resource identity (RAG and memory milestone, RM.9): `resource_id` is set once at creation and never
changed. It defaults to the principal's user id (left NULL when that id does not fit the resource
pattern, which turns resource-scoped memory off for the conversation); an integration acting for
its own end users passes `resource_id` explicitly. Reusing a client conversation_id with another
resource_id is a 409 ("... different resource_id"), distinct from the other-tenant or other-agent
409s.

DELETE keeps the turns' runs (the audit and cost record) with their text nulled and events
scrubbed, and deletes the conversation's observations, conversation-scoped working memory and
message embeddings in one transaction (eap_run_store.memory.MemoryStore.delete_conversation).
Resource-scoped working memory stays (DELETE /memory/resources/{rid} forgets it). 204; 404 for an
unknown, another tenant's, malformed or already deleted id (a repeated delete changes nothing);
409 with Retry-After while the latest turn is queued or running.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse

from eap_observability import current_trace_context

from eap_api.launcher import RunLauncher, get_run_launcher
from eap_api.models import AgentModel
from eap_api.request_guard import GuardedJsonRoute
from eap_api.routes.runs import (
    PrincipalDep,
    SessionDep,
    StoresDep,
    _get_run_or_404,
    _run_json,
    launch_recorded_run,
    started_response,
)
from eap_api.run_options import resolve_run_options, same_options
from eap_api.schemas import ConversationCreate, ConversationRename, ErrorDetail, TurnCreate
from eap_run_store import (
    ConversationCursor,
    ConversationIdConflict,
    ConversationNotFound,
    ConversationResourceConflict,
    InvalidRunCursor,
    MemoryStore,
    RunIdConflict,
    RunStore,
    TurnInProgress,
    validate_resource_id,
)
from eap_run_store.cursor import MAX_CURSOR_LENGTH

# How long a client should wait before retrying a delete refused because a turn is still running.
BUSY_RETRY_AFTER_SECONDS = 2

router = APIRouter(
    tags=["conversations"],
    responses={404: {"model": ErrorDetail}, 503: {"model": ErrorDetail}},
    route_class=GuardedJsonRoute,
)


def _conversation_json(conv: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": conv["id"],
        "agent_id": conv["agent_id"],
        "resource_id": conv.get("resource_id"),
        "title": conv["title"],
        "turn_count": conv["turn_count"],
        "last_turn_run_id": conv["last_turn_run_id"],
        "last_turn_status": conv["last_turn_status"],
        "last_turn_display_status": conv["last_turn_display_status"],
        "created_at": conv["created_at"].isoformat(),
        "updated_at": conv["updated_at"].isoformat(),
    }


def default_resource_id(user_id: str) -> str | None:
    """The principal's user id as the default resource id, or None when it does not fit
    RESOURCE_ID_PATTERN (resource-scoped memory is then off for the conversation)."""
    try:
        return validate_resource_id(user_id)
    except ValueError:
        return None


def get_memory_store(request: Request) -> MemoryStore:
    store = getattr(request.app.state, "memory_store", None)
    if store is None:  # built lazily on the API's engine (create_app does not wire it)
        store = request.app.state.memory_store = MemoryStore(request.app.state.database_engine)
    return store


MemoryDep = Annotated[MemoryStore, Depends(get_memory_store)]


def canonical_uuid_or_404(value: str, detail: str) -> str:
    """A path id as a canonical UUID string; a malformed one answers 404 like an unknown one."""
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def turn_busy_409(detail: str, busy: list[str] | None = None) -> JSONResponse:
    """409 for a delete refused while a turn is queued or running (nothing was deleted)."""
    content: dict[str, Any] = {"detail": detail}
    if busy is not None:
        content["busy_conversations"] = busy
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content=content,
        headers={"Retry-After": str(BUSY_RETRY_AFTER_SECONDS)},
    )


def _conversation_or_404(run_store: RunStore, tenant_id: str, conversation_id: uuid.UUID) -> dict[str, Any]:
    conv = run_store.get_conversation(tenant_id, conversation_id)
    if conv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    return conv


@router.post(
    "/agents/{agent_id}/conversations", status_code=status.HTTP_201_CREATED, responses={409: {"model": ErrorDetail}}
)
def create_conversation(
    agent_id: uuid.UUID, body: ConversationCreate, principal: PrincipalDep, db: SessionDep, stores: StoresDep
) -> JSONResponse:
    """201 when created, 200 when the tenant already has this client-supplied conversation_id (with
    the same resource_id; 409 for a different one). `resource_id` defaults to the caller's user id
    and is immutable."""
    run_store, _ = stores
    tenant_id = principal.tenant_id
    agent = db.query(AgentModel).filter(AgentModel.id == str(agent_id), AgentModel.tenant_id == tenant_id).first()
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    resource_id = body.resource_id if body.resource_id is not None else default_resource_id(principal.user_id)
    try:
        conv, created = run_store.create_conversation(
            tenant_id, agent_id, body.title, body.conversation_id, resource_id=resource_id
        )
    except ConversationResourceConflict:  # a subclass of ConversationIdConflict: caught first
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="conversation_id already exists with a different resource_id; resource_id cannot change",
        )
    except ConversationIdConflict:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="conversation_id is already in use")
    if conv["agent_id"] != str(agent_id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="conversation_id belongs to another agent")
    return JSONResponse(
        status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK, content=_conversation_json(conv)
    )


@router.get("/conversations")
def list_conversations(
    principal: PrincipalDep,
    stores: StoresDep,
    agent_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before: Annotated[
        str | None,
        Query(max_length=MAX_CURSOR_LENGTH, description="Opaque cursor: the previous page's next_before, verbatim"),
    ] = None,
) -> JSONResponse:
    """The tenant's conversations (optionally one agent's), ordered by (updated_at, id) descending:
    a turn start moves its conversation to the top, a rename does not. `next_before` is an opaque
    cursor naming the last item's position when a full page came back (pass it back verbatim as
    `before`), else null. A cursor this API did not issue is a 422."""
    run_store, _ = stores
    cursor = None
    if before is not None:
        try:
            cursor = ConversationCursor.decode(before)
        except InvalidRunCursor:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="before is not a valid cursor; pass the previous page's next_before verbatim",
            )
    conversations = run_store.list_conversations(principal.tenant_id, agent_id, limit=limit, before=cursor)
    next_before = (
        ConversationCursor.after(conversations[-1]).encode() if len(conversations) == limit else None
    )
    return JSONResponse(
        content={"items": [_conversation_json(c) for c in conversations], "next_before": next_before}
    )


@router.get("/conversations/{conversation_id}")
def get_conversation(conversation_id: uuid.UUID, principal: PrincipalDep, stores: StoresDep) -> JSONResponse:
    run_store, _ = stores
    conv = _conversation_or_404(run_store, principal.tenant_id, conversation_id)
    runs = run_store.list_turns(principal.tenant_id, conversation_id)
    return JSONResponse(content={**_conversation_json(conv), "runs": [_run_json(r) for r in runs]})


@router.patch("/conversations/{conversation_id}")
def rename_conversation(
    conversation_id: uuid.UUID, body: ConversationRename, principal: PrincipalDep, stores: StoresDep
) -> JSONResponse:
    """Rename: 200 with the conversation; 422 for an empty, over-long or multi-line title; 404 for
    another tenant's or an unknown conversation. The order of the list does not change."""
    run_store, _ = stores
    try:
        conv = run_store.rename_conversation(principal.tenant_id, conversation_id, body.title)
    except ConversationNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    return JSONResponse(content=_conversation_json(conv))


@router.post(
    "/conversations/{conversation_id}/runs",
    status_code=status.HTTP_201_CREATED,
    responses={409: {"model": ErrorDetail}, 413: {"model": ErrorDetail}},
)
def start_turn(
    conversation_id: uuid.UUID,
    body: TurnCreate,
    principal: PrincipalDep,
    db: SessionDep,
    stores: StoresDep,
    launcher: Annotated[RunLauncher, Depends(get_run_launcher)],
) -> JSONResponse:
    """Start the next turn. 201 created, 200 for a retry of the same turn (same run_id, input and
    run options), 409 while the previous turn is not terminal or when run_id is used by a different
    request. `version` ("draft" for a draft test turn), `request_context` and `model` work as on
    POST /agents/{id}/runs, per turn.

    The request's trace id and traceparent are stored on the run; the worker's run span joins that
    trace, and the response carries `trace_id`.
    """
    run_store, _ = stores
    tenant_id = principal.tenant_id
    conv = _conversation_or_404(run_store, tenant_id, conversation_id)
    agent_id = conv["agent_id"]
    agent = db.query(AgentModel).filter(AgentModel.id == agent_id, AgentModel.tenant_id == tenant_id).first()
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    # A retry of a recorded turn is compared with the row (below) and relaunched as recorded; only a
    # new turn validates its options (each turn has its own context, model and version or draft).
    run = run_store.get_run(tenant_id, body.run_id)
    created = False
    if run is None:
        options = resolve_run_options(
            db, tenant_id, agent,
            version=body.version, request_context=body.request_context, model=body.model, input_text=body.input,
        )
        # The request span (FastAPI instrumentation) is current here; (None, None) when telemetry is off.
        trace_id, traceparent = current_trace_context()
        try:
            run_id, created = run_store.create_turn(
                tenant_id, conversation_id, options.target.version, body.input, body.run_id,
                trace_id=trace_id, traceparent=traceparent, **options.store_kwargs(),
            )
        except ConversationNotFound:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
        except RunIdConflict:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="run_id is already in use")
        except TurnInProgress as busy:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"The previous turn (run {busy.run_id}) is still {busy.status}; wait for it to finish",
            )
        run = _get_run_or_404(run_store, tenant_id, run_id)

    if not created and (
        run["conversation_id"] != conv["id"]
        or run["input"] != body.input
        or not same_options(db, run, version=body.version, request_context=body.request_context, model=body.model)
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="run_id is already used by a different run request"
        )

    run = launch_recorded_run(db, launcher, run_store, tenant_id, run, created)
    return started_response(run, created, {"conversation_id": run["conversation_id"], "turn": run["turn"]})


@router.delete(
    "/conversations/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={409: {"model": ErrorDetail, "description": "A turn is still queued or running; retry after it ends"}},
)
def delete_conversation(conversation_id: str, principal: PrincipalDep, memory: MemoryDep) -> Response:
    """Delete the conversation: its runs stay with input, output, messages, request context and
    error nulled and every event scrubbed (usage and cost stay); its observations,
    conversation-scoped working memory and message embeddings are deleted; resource-scoped working
    memory stays. 204; 404 unknown, another tenant's, malformed or already deleted; 409 (with
    Retry-After) while its latest turn is queued or running, nothing deleted."""
    conv_id = canonical_uuid_or_404(conversation_id, "Conversation not found")
    try:
        deleted = memory.delete_conversation(principal.tenant_id, conv_id)
    except TurnInProgress as busy:
        return turn_busy_409(
            f"The latest turn (run {busy.run_id}) is still {busy.status}; nothing was deleted. "
            "Retry when it finishes"
        )
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
