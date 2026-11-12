"""Conversation routes: a tenant-owned thread with one agent whose turns are runs.

- POST /agents/{agent_id}/conversations   create (idempotent on an optional client conversation_id)
- GET  /conversations                     the tenant's conversations, most recent first (?agent_id=)
- GET  /conversations/{id}                one conversation with its runs in turn order
- POST /conversations/{id}/runs           start the next turn

A turn is an ordinary run (same enqueue, same events, same SSE) whose row also carries
conversation_id and turn; the worker builds the turn's message history from the earlier completed
turns. The next turn is refused with 409 while the latest turn is not terminal. Starting a turn is
idempotent on the client run_id exactly like POST /agents/{id}/runs: the row is inserted under the
conversation's lock, then the workflow is enqueued on every attempt, so a crash between the two
heals when the client retries. Another tenant's or an unknown conversation is a 404.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse

from eap_observability import current_trace_context

from eap_api.launcher import RunLauncher, get_run_launcher
from eap_api.models import AgentModel
from eap_api.routes.runs import (
    PrincipalDep,
    SessionDep,
    StoresDep,
    _get_run_or_404,
    _run_json,
    check_payload_size,
    launch_recorded_run,
    resolve_version,
    started_response,
)
from eap_api.schemas import ConversationCreate, ErrorDetail, TurnCreate
from eap_run_store import ConversationIdConflict, ConversationNotFound, RunIdConflict, RunStore, TurnInProgress

router = APIRouter(tags=["conversations"], responses={404: {"model": ErrorDetail}, 503: {"model": ErrorDetail}})


def _conversation_json(conv: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": conv["id"],
        "agent_id": conv["agent_id"],
        "title": conv["title"],
        "turn_count": conv["turn_count"],
        "created_at": conv["created_at"].isoformat(),
        "updated_at": conv["updated_at"].isoformat(),
    }


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
    """201 when created, 200 when the tenant already has this client-supplied conversation_id."""
    run_store, _ = stores
    tenant_id = principal.tenant_id
    agent = db.query(AgentModel).filter(AgentModel.id == str(agent_id), AgentModel.tenant_id == tenant_id).first()
    if agent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    try:
        conv, created = run_store.create_conversation(tenant_id, agent_id, body.title, body.conversation_id)
    except ConversationIdConflict:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="conversation_id is already in use")
    if conv["agent_id"] != str(agent_id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="conversation_id belongs to another agent")
    return JSONResponse(
        status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK, content=_conversation_json(conv)
    )


@router.get("/conversations")
def list_conversations(principal: PrincipalDep, stores: StoresDep, agent_id: uuid.UUID | None = None) -> JSONResponse:
    run_store, _ = stores
    return JSONResponse(
        content=[_conversation_json(c) for c in run_store.list_conversations(principal.tenant_id, agent_id)]
    )


@router.get("/conversations/{conversation_id}")
def get_conversation(conversation_id: uuid.UUID, principal: PrincipalDep, stores: StoresDep) -> JSONResponse:
    run_store, _ = stores
    conv = _conversation_or_404(run_store, principal.tenant_id, conversation_id)
    runs = run_store.list_turns(principal.tenant_id, conversation_id)
    return JSONResponse(content={**_conversation_json(conv), "runs": [_run_json(r) for r in runs]})


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
    """Start the next turn. 201 created, 200 for a retry of the same turn (same run_id and input),
    409 while the previous turn is not terminal or when run_id is used by a different request.

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

    version_num = resolve_version(db, agent_id, body.version)
    check_payload_size(db, agent_id, version_num, body.input)
    # The request span (FastAPI instrumentation) is current here; (None, None) when telemetry is off.
    trace_id, traceparent = current_trace_context()

    try:
        run_id, created = run_store.create_turn(
            tenant_id, conversation_id, version_num, body.input, body.run_id,
            trace_id=trace_id, traceparent=traceparent,
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
        or (body.version is not None and run["agent_version"] != body.version)
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="run_id is already used by a different run request"
        )

    launch_recorded_run(db, launcher, tenant_id, run)
    return started_response(run, created, {"conversation_id": run["conversation_id"], "turn": run["turn"]})
