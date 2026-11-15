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
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from eap_observability import current_trace_context

from eap_api.launcher import RunLauncher, get_run_launcher
from eap_api.agent_archive import refuse_if_archived
from eap_api.models import AgentModel
from eap_api.request_guard import GuardedJsonRoute
from eap_api.routes.runs import (
    PrincipalDep,
    SessionDep,
    StoresDep,
    _get_run_or_404,
    _run_json,
    launch_recorded_run,
    lock_agent,
    started_response,
)
from eap_api.run_options import RunOptions, resolve_run_options, same_options
from eap_api.schemas import ConversationCreate, ConversationRename, ErrorDetail, TurnCreate
from eap_schemas import ModelRef
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


def turn_in_progress_409(busy: TurnInProgress) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=f"The previous turn (run {busy.run_id}) is still {busy.status}; wait for it to finish",
    )


DIFFERENT_RUN_REQUEST = "run_id is already used by a different run request"


def new_turn_options(
    db: Session,
    tenant_id: str,
    agent: AgentModel,
    *,
    version: int | str | None,
    request_context: dict[str, Any] | None,
    model: ModelRef | None,
    input_text: str,
) -> RunOptions:
    """A new turn's validated options: 409 AGENT_ARCHIVED for an archived agent (a recorded turn
    is replayed, never refused; eap_api.agent_archive), then resolve_run_options (404/409/422/413)."""
    refuse_if_archived(agent)
    return resolve_run_options(
        db, tenant_id, agent, version=version, request_context=request_context, model=model, input_text=input_text
    )


def record_and_launch_turn(
    db: Session,
    launcher: RunLauncher,
    run_store: RunStore,
    tenant_id: str,
    conversation_id: uuid.UUID | str,
    *,
    run_id: uuid.UUID | str,
    new_options: Callable[[], RunOptions] | RunOptions,
    input_text: str,
    same_request: Callable[[dict[str, Any]], bool],
    conflict_detail: str = DIFFERENT_RUN_REQUEST,
    client_tools: list[dict[str, Any]] | None = None,
    client_tool_results: list[dict[str, Any]] | None = None,
    ag_ui_run: Any = None,
) -> tuple[dict[str, Any], bool]:
    """The shared run-start path of a conversation turn (POST /conversations/{id}/runs and the
    AG-UI agent route): record the turn idempotently on `run_id`, then hand it off.

    - `run_id` already recorded for the tenant: no option is validated; `same_request(run)` must
      hold (else 409 `conflict_detail`), and the recorded run is launched again, so a crash
      between insert and enqueue heals on a retry (launch_recorded_run).
    - New: `new_options` (a RunOptions, or a callable building it: new_turn_options, which refuses
      an archived agent) validates the turn, then RunStore.create_turn inserts it with the trace
      context and the extra columns (`client_tools`, `client_tool_results`, `ag_ui_run`: an
      AgUiRunWrite recorded in the turn's transaction). A concurrent insert of the same run_id
      (created False) is compared with `same_request` like a retry.

    Returns (run as it stands after the hand-off, created). Raises 404 for an unknown
    conversation, 409 for another tenant's run_id or a different request, 503 when the enqueue
    fails, and **TurnInProgress** (not mapped: the caller decides; turn_in_progress_409) while the
    latest turn is not terminal.
    """
    tenant_run = run_store.get_run(tenant_id, uuid.UUID(str(run_id)))
    created = False
    if tenant_run is None:
        options = new_options() if callable(new_options) else new_options
        # The request span (FastAPI instrumentation) is current here; (None, None) when telemetry is off.
        trace_id, traceparent = current_trace_context()
        try:
            recorded_id, created = run_store.create_turn(
                tenant_id, conversation_id, options.target.version, input_text, uuid.UUID(str(run_id)),
                trace_id=trace_id, traceparent=traceparent, **options.store_kwargs(),
                client_tools=client_tools, client_tool_results=client_tool_results, ag_ui_run=ag_ui_run,
            )
        except ConversationNotFound:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
        except RunIdConflict:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="run_id is already in use")
        tenant_run = _get_run_or_404(run_store, tenant_id, recorded_id)
    if not created and not same_request(tenant_run):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=conflict_detail)
    return launch_recorded_run(db, launcher, run_store, tenant_id, tenant_run, created), created


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
    # FOR SHARE until the request's transaction ends: an archive (FOR NO KEY UPDATE, like publish)
    # waits for this run to be recorded, or this request sees `archived` (eap_api.agent_archive).
    agent = (
        db.query(AgentModel)
        .filter(AgentModel.id == str(agent_id), AgentModel.tenant_id == tenant_id)
        .with_for_update(read=True)
        .first()
    )
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    if agent.archived and (
        body.conversation_id is None or run_store.get_conversation(tenant_id, body.conversation_id) is None
    ):
        # A new conversation only; a retry of an existing one still answers (eap_api.agent_archive).
        refuse_if_archived(agent)
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
    chat = run_store.chat_usage(principal.tenant_id, [r["id"] for r in runs])
    return JSONResponse(
        content={**_conversation_json(conv), "runs": [_run_json(r, chat.get(str(r["id"]))) for r in runs]}
    )


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
    agent = lock_agent(db, tenant_id, conv["agent_id"])

    # A retry of a recorded turn is compared with the row and relaunched as recorded; only a new
    # turn validates its options (each turn has its own context, model and version or draft).
    def same_request(run: dict[str, Any]) -> bool:
        return (
            run["conversation_id"] == conv["id"]
            and run["input"] == body.input
            and same_options(db, run, version=body.version, request_context=body.request_context, model=body.model)
        )

    try:
        run, created = record_and_launch_turn(
            db, launcher, run_store, tenant_id, conversation_id,
            run_id=body.run_id,
            new_options=lambda: new_turn_options(
                db, tenant_id, agent,
                version=body.version, request_context=body.request_context, model=body.model, input_text=body.input,
            ),
            input_text=body.input,
            same_request=same_request,
        )
    except TurnInProgress as busy:
        raise turn_in_progress_409(busy)
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
