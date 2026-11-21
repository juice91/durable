"""Conversation routes: a tenant-owned thread with one agent whose turns are runs.

- POST /agents/{agent_id}/conversations   create (idempotent on an optional client conversation_id)
- GET  /conversations                     the tenant's conversations, most recently active first,
                                          paged (?agent_id=&q=&limit=&before=)
- GET  /conversations/{id}                one conversation with its runs in turn order
- PATCH /conversations/{id}               rename (title 1–200 characters)
- POST /conversations/{id}/runs           start the next turn
- POST /conversations/{id}/clone          copy the thread into a new conversation (W1.2)
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
message embeddings in one transaction (the memory store's delete_conversation).
Resource-scoped working memory stays (DELETE /memory/resources/{rid} forgets it). 204; 404 for an
unknown, another tenant's, malformed or already deleted id (a repeated delete changes nothing);
409 with Retry-After while the latest turn is queued or running.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Response, status
from fastapi.responses import JSONResponse

from eap_api.models import AgentModel
from eap_api.request_guard import GuardedJsonRoute
from eap_api.routes.runs import PrincipalDep, started_response
from eap_api.schemas import ConversationCreate, ConversationRename, ErrorDetail, TurnCreate
from eap_api.service_deps.agents import ConversationServiceDep, build_conversation_service
from eap_api.service_errors import to_http_exception
from eap_api.services.conversations import (  # noqa: F401 - re-exported: the AG-UI and memory routes import them from here
    CLONE_SUFFIX,
    DIFFERENT_RUN_REQUEST,
    TITLE_MAX_LENGTH,
    MAX_CURSOR_LENGTH,
    TurnBusyRefusal,
    TurnInProgress,
    canonical_uuid_or_404 as _canonical_uuid_or_404,
    clone_title,
    default_resource_id,
    turn_in_progress_message,
)
from eap_api.services.errors import ServiceError
from eap_api.services.run_options import RunOptions
from eap_schemas import ModelRef, ModelSettings

# How long a client should wait before retrying a delete refused because a turn is still running.
BUSY_RETRY_AFTER_SECONDS = 2

router = APIRouter(
    tags=["conversations"],
    responses={404: {"model": ErrorDetail}, 503: {"model": ErrorDetail}},
    route_class=GuardedJsonRoute,
)


def canonical_uuid_or_404(value: str, detail: str) -> str:
    """A path id as a canonical UUID string; a malformed one answers 404 like an unknown one."""
    try:
        return _canonical_uuid_or_404(value, detail)
    except ServiceError as exc:
        raise to_http_exception(exc) from None


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


def turn_in_progress_409(busy: TurnInProgress) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=turn_in_progress_message(busy))


def new_turn_options(
    db: Any,
    tenant_id: str,
    agent: AgentModel,
    *,
    version: int | str | None,
    request_context: dict[str, Any] | None,
    model: ModelRef | None,
    input_text: str,
    model_settings: ModelSettings | None = None,
) -> RunOptions:
    """A new turn's validated options (ConversationService.new_turn_options) with the HTTP refusals:
    409 AGENT_ARCHIVED for an archived agent, then 404/409/422/413."""
    service = build_conversation_service(db, None, None)  # type: ignore[arg-type] - options need only the session
    try:
        return service.new_turn_options(
            tenant_id, agent, version=version, request_context=request_context, model=model,
            input_text=input_text, model_settings=model_settings,
        )
    except ServiceError as exc:
        raise to_http_exception(exc) from None


def record_and_launch_turn(
    db: Any,
    launcher: Any,
    run_store: Any,
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
    """ConversationService.record_and_launch_turn with the HTTP refusals (404, 409, 503); it still raises
    **TurnInProgress** while the latest turn is not terminal (not mapped: the caller decides; turn_in_progress_409)."""
    service = build_conversation_service(db, run_store, launcher)
    try:
        return service.record_and_launch_turn(
            tenant_id, conversation_id, run_id=run_id, new_options=new_options, input_text=input_text,
            same_request=same_request, conflict_detail=conflict_detail, client_tools=client_tools,
            client_tool_results=client_tool_results, ag_ui_run=ag_ui_run,
        )
    except ServiceError as exc:
        raise to_http_exception(exc) from None


@router.post(
    "/agents/{agent_id}/conversations", status_code=status.HTTP_201_CREATED, responses={409: {"model": ErrorDetail}}
)
def create_conversation(
    agent_id: uuid.UUID, body: ConversationCreate, principal: PrincipalDep, service: ConversationServiceDep
) -> JSONResponse:
    """201 when created, 200 when the tenant already has this client-supplied conversation_id (with
    the same resource_id; 409 for a different one). `resource_id` defaults to the caller's user id
    and is immutable."""
    conv, created = service.create(
        principal.tenant_id, principal.user_id, agent_id,
        title=body.title, conversation_id=body.conversation_id, resource_id=body.resource_id,
    )
    return JSONResponse(status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK, content=conv)


@router.get("/conversations")
def list_conversations(
    principal: PrincipalDep,
    service: ConversationServiceDep,
    agent_id: uuid.UUID | None = None,
    q: Annotated[
        str | None, Query(min_length=1, max_length=200, description="Case-insensitive title substring")
    ] = None,
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
    return JSONResponse(
        content=service.list(principal.tenant_id, agent_id=agent_id, q=q, limit=limit, before=before)
    )


@router.get("/conversations/{conversation_id}")
def get_conversation(
    conversation_id: uuid.UUID, principal: PrincipalDep, service: ConversationServiceDep
) -> JSONResponse:
    return JSONResponse(content=service.get(principal.tenant_id, conversation_id))


@router.patch("/conversations/{conversation_id}")
def rename_conversation(
    conversation_id: uuid.UUID, body: ConversationRename, principal: PrincipalDep, service: ConversationServiceDep
) -> JSONResponse:
    """Rename: 200 with the conversation; 422 for an empty, over-long or multi-line title; 404 for
    another tenant's or an unknown conversation. The order of the list does not change."""
    return JSONResponse(content=service.rename(principal.tenant_id, conversation_id, body.title))


@router.post(
    "/conversations/{conversation_id}/clone",
    status_code=status.HTTP_201_CREATED,
    responses={409: {"model": ErrorDetail}},
)
def clone_conversation(
    conversation_id: str, principal: PrincipalDep, service: ConversationServiceDep
) -> JSONResponse:
    """Clone the thread into a NEW conversation on the same agent and resource_id, titled
    "<title> (copy)". The terminal turns (completed, failed, needs_attention) are copied in order
    with their messages and events, renumbered from 1; a queued or running turn is not copied, and
    the source is not changed. The copies carry no usage or cost (the source keeps the record), and
    derived memory (observations, conversation working memory, recall index) is not copied. 201
    with the new conversation (the create/get shape, `runs` excluded); 404 for an unknown, another
    tenant's, malformed or deleted conversation; 409 AGENT_ARCHIVED for an archived agent."""
    return JSONResponse(
        status_code=status.HTTP_201_CREATED, content=service.clone(principal.tenant_id, conversation_id)
    )


@router.post(
    "/conversations/{conversation_id}/runs",
    status_code=status.HTTP_201_CREATED,
    responses={409: {"model": ErrorDetail}, 413: {"model": ErrorDetail}},
)
def start_turn(
    conversation_id: uuid.UUID, body: TurnCreate, principal: PrincipalDep, service: ConversationServiceDep
) -> JSONResponse:
    """Start the next turn. 201 created, 200 for a retry of the same turn (same run_id, input and
    run options), 409 while the previous turn is not terminal or when run_id is used by a different
    request. `version` ("draft" for a draft test turn), `request_context` and `model` work as on
    POST /agents/{id}/runs, per turn.

    The request's trace id and traceparent are stored on the run; the worker's run span joins that
    trace, and the response carries `trace_id`.
    """
    run, created = service.start_turn(
        principal.tenant_id, conversation_id, run_id=body.run_id, input_text=body.input, version=body.version,
        request_context=body.request_context, model=body.model, model_settings=body.model_settings,
    )
    return started_response(run, created, {"conversation_id": run["conversation_id"], "turn": run["turn"]})


@router.delete(
    "/conversations/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={409: {"model": ErrorDetail, "description": "A turn is still queued or running; retry after it ends"}},
)
def delete_conversation(conversation_id: str, principal: PrincipalDep, service: ConversationServiceDep) -> Response:
    """Delete the conversation: its runs stay with input, output, messages, request context and
    error nulled and every event scrubbed (usage and cost stay); its observations,
    conversation-scoped working memory and message embeddings are deleted; resource-scoped working
    memory stays. 204; 404 unknown, another tenant's, malformed or already deleted; 409 (with
    Retry-After) while its latest turn is queued or running, nothing deleted."""
    try:
        service.delete(principal.tenant_id, conversation_id)
    except TurnBusyRefusal as busy:
        return turn_busy_409(busy.message)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
