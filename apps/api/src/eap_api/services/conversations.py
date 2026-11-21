"""Conversations: a tenant-owned thread with one agent whose turns are runs (the business logic behind
the conversation routes and the AG-UI agent route).

Every lookup is tenant-scoped: an unknown conversation, another tenant's or a malformed id is `NotFound`.
Refusals answer the bodies the routes always had (`{"detail": <message>}`, see `plain_refusals`).
A new turn is recorded idempotently on the client run_id under the conversation's lock, then handed to
the worker on every attempt (`launch_recorded_run`), so a crash between the two heals on a retry.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from eap_api.models import AgentModel
from eap_api.ports import RunLauncher
from eap_api.services.agent_archive import refuse_if_archived
from eap_api.services.errors import Conflict, InvalidInput, NotFound, Unavailable
from eap_api.services.run_filters import BAD_CURSOR, plain_refusals
from eap_api.services.run_options import RunOptions, resolve_run_options, same_options
from eap_api.services.runs import get_run_or_404, launch_recorded_run, lock_agent, run_json
from eap_observability.context import current_trace_context
from eap_run_store.cursor import MAX_CURSOR_LENGTH  # noqa: F401 - re-exported for the route's query limit
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
from eap_schemas import ModelRef, ModelSettings

CONVERSATION_NOT_FOUND = "Conversation not found"
AGENT_NOT_FOUND = "Agent not found"
DIFFERENT_RUN_REQUEST = "run_id is already used by a different run request"
CLONE_SUFFIX = " (copy)"
TITLE_MAX_LENGTH = 200


class TurnBusyRefusal(Conflict):
    """A delete refused while the conversation's latest turn is queued or running (nothing was deleted)."""


def default_resource_id(user_id: str) -> str | None:
    """The principal's user id as the default resource id, or None when it does not fit
    RESOURCE_ID_PATTERN (resource-scoped memory is then off for the conversation)."""
    try:
        return validate_resource_id(user_id)
    except ValueError:
        return None


def canonical_uuid_or_404(value: str, detail: str) -> str:
    """A path id as a canonical UUID string; a malformed one is refused like an unknown one."""
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise NotFound(detail) from None


def clone_title(title: str | None) -> str:
    """"<title> (copy)", within the 200-character title limit; an untitled thread reads
    "Untitled conversation (copy)" (the Studio's label for it)."""
    base = (title or "").strip() or "Untitled conversation"
    return base[: TITLE_MAX_LENGTH - len(CLONE_SUFFIX)] + CLONE_SUFFIX


def turn_in_progress_message(busy: TurnInProgress) -> str:
    return f"The previous turn (run {busy.run_id}) is still {busy.status}; wait for it to finish"


def conversation_json(conv: dict[str, Any]) -> dict[str, Any]:
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


@plain_refusals
class ConversationService:
    """Conversations and their turns. `memory` is needed only by `delete`."""

    def __init__(
        self, db: Session, run_store: RunStore, launcher: RunLauncher, memory: MemoryStore | None = None
    ) -> None:
        self.db = db
        self.run_store = run_store
        self.launcher = launcher
        self.memory = memory

    # -- reads and thread edits ------------------------------------------------------------------

    def require_conversation(self, tenant_id: str, conversation_id: uuid.UUID | str) -> dict[str, Any]:
        conv = self.run_store.get_conversation(tenant_id, conversation_id)
        if conv is None:
            raise NotFound(CONVERSATION_NOT_FOUND)
        return conv

    def create(
        self,
        tenant_id: str,
        user_id: str,
        agent_id: uuid.UUID,
        *,
        title: str | None,
        conversation_id: uuid.UUID | None,
        resource_id: str | None,
    ) -> tuple[dict[str, Any], bool]:
        """`(conversation as the API shows it, created)`: created is False when the tenant already has this
        client-supplied conversation_id (with the same resource_id; 409 for a different one).
        `resource_id` defaults to the caller's user id and is immutable."""
        tenant_agent = (
            self.db.query(AgentModel)
            .filter(AgentModel.id == str(agent_id), AgentModel.tenant_id == tenant_id)
            # FOR SHARE until the request's transaction ends: an archive (FOR NO KEY UPDATE, like publish)
            # waits for this run to be recorded, or this request sees `archived` (eap_api.agent_archive).
            .with_for_update(read=True)
            .first()
        )
        if tenant_agent is None:
            raise NotFound(AGENT_NOT_FOUND)
        if tenant_agent.archived and (
            conversation_id is None or self.run_store.get_conversation(tenant_id, conversation_id) is None
        ):
            # A new conversation only; a retry of an existing one still answers (eap_api.agent_archive).
            refuse_if_archived(tenant_agent)
        resource = resource_id if resource_id is not None else default_resource_id(user_id)
        try:
            conv, created = self.run_store.create_conversation(
                tenant_id, agent_id, title, conversation_id, resource_id=resource
            )
        except ConversationResourceConflict:  # a subclass of ConversationIdConflict: caught first
            raise Conflict(
                "conversation_id already exists with a different resource_id; resource_id cannot change"
            ) from None
        except ConversationIdConflict:
            raise Conflict("conversation_id is already in use") from None
        if conv["agent_id"] != str(agent_id):
            raise Conflict("conversation_id belongs to another agent")
        return conversation_json(conv), created

    def list(
        self, tenant_id: str, *, agent_id: uuid.UUID | None, q: str | None, limit: int, before: str | None
    ) -> dict[str, Any]:
        """The tenant's conversations, ordered by (updated_at, id) descending, with `next_before`, the
        opaque cursor of the last item when a full page came back. A cursor this API did not issue is 422."""
        cursor = None
        if before is not None:
            try:
                cursor = ConversationCursor.decode(before)
            except InvalidRunCursor:
                raise InvalidInput(BAD_CURSOR) from None
        conversations = self.run_store.list_conversations(
            tenant_id, agent_id, limit=limit, before=cursor, title_contains=q
        )
        next_before = ConversationCursor.after(conversations[-1]).encode() if len(conversations) == limit else None
        return {"items": [conversation_json(c) for c in conversations], "next_before": next_before}

    def get(self, tenant_id: str, conversation_id: uuid.UUID) -> dict[str, Any]:
        """The conversation with its runs in turn order."""
        conv = self.require_conversation(tenant_id, conversation_id)
        runs = self.run_store.list_turns(tenant_id, conversation_id)
        chat = self.run_store.chat_usage(tenant_id, [r["id"] for r in runs])
        return {**conversation_json(conv), "runs": [run_json(r, chat.get(str(r["id"]))) for r in runs]}

    def rename(self, tenant_id: str, conversation_id: uuid.UUID, title: str) -> dict[str, Any]:
        try:
            conv = self.run_store.rename_conversation(tenant_id, conversation_id, title)
        except ConversationNotFound:
            raise NotFound(CONVERSATION_NOT_FOUND) from None
        return conversation_json(conv)

    def clone(self, tenant_id: str, conversation_id: str) -> dict[str, Any]:
        """Clone the thread into a NEW conversation on the same agent and resource_id, titled
        "<title> (copy)" (terminal turns copied, see RunStore.clone_conversation). NotFound for an unknown,
        another tenant's, malformed or deleted conversation; AGENT_ARCHIVED for an archived agent."""
        conv_id = canonical_uuid_or_404(conversation_id, CONVERSATION_NOT_FOUND)
        conv = self.require_conversation(tenant_id, uuid.UUID(conv_id))
        refuse_if_archived(lock_agent(self.db, tenant_id, conv["agent_id"]))
        try:
            cloned = self.run_store.clone_conversation(tenant_id, conv_id, clone_title(conv["title"]))
        except ConversationNotFound:
            raise NotFound(CONVERSATION_NOT_FOUND) from None
        return conversation_json(cloned)

    def delete(self, tenant_id: str, conversation_id: str) -> None:
        """Delete the conversation and its derived memory (the turns' runs stay, scrubbed). NotFound for an
        unknown, another tenant's, malformed or already deleted id; TurnBusyRefusal while the latest turn
        is queued or running (nothing deleted)."""
        if self.memory is None:
            raise Unavailable("memory store is not configured")
        conv_id = canonical_uuid_or_404(conversation_id, CONVERSATION_NOT_FOUND)
        try:
            deleted = self.memory.delete_conversation(tenant_id, conv_id)
        except TurnInProgress as busy:
            raise TurnBusyRefusal(
                f"The latest turn (run {busy.run_id}) is still {busy.status}; nothing was deleted. "
                "Retry when it finishes"
            ) from None
        if not deleted:
            raise NotFound(CONVERSATION_NOT_FOUND)

    # -- turns -----------------------------------------------------------------------------------

    def new_turn_options(
        self,
        tenant_id: str,
        agent: AgentModel,
        *,
        version: int | str | None,
        request_context: dict[str, Any] | None,
        model: ModelRef | None,
        input_text: str,
        model_settings: ModelSettings | None = None,
    ) -> RunOptions:
        """A new turn's validated options: Conflict AGENT_ARCHIVED for an archived agent (a recorded turn
        is replayed, never refused), then resolve_run_options (NotFound / Conflict / InvalidInput /
        PayloadTooLarge)."""
        refuse_if_archived(agent)
        return resolve_run_options(
            self.db, tenant_id, agent, version=version, request_context=request_context, model=model,
            input_text=input_text, model_settings=model_settings,
        )

    def record_and_launch_turn(
        self,
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
          hold (else Conflict `conflict_detail`), and the recorded run is launched again, so a crash
          between insert and enqueue heals on a retry (launch_recorded_run).
        - New: `new_options` (a RunOptions, or a callable building it: `new_turn_options`) validates the
          turn, then RunStore.create_turn inserts it with the trace context and the extra columns
          (`client_tools`, `client_tool_results`, `ag_ui_run`: an AgUiRunWrite recorded in the turn's
          transaction). A concurrent insert of the same run_id (created False) is compared with
          `same_request` like a retry.

        Returns (run as it stands after the hand-off, created). Raises NotFound (unknown conversation),
        Conflict (another tenant's run_id or a different request), Unavailable (the enqueue failed) and
        **TurnInProgress** (not a ServiceError: the caller decides, see `turn_in_progress_message`) while
        the latest turn is not terminal."""
        tenant_run = self.run_store.get_run(tenant_id, uuid.UUID(str(run_id)))
        created = False
        if tenant_run is None:
            options = new_options() if callable(new_options) else new_options
            # The request span (FastAPI instrumentation) is current here; (None, None) when telemetry is off.
            trace_id, traceparent = current_trace_context()
            try:
                recorded_id, created = self.run_store.create_turn(
                    tenant_id, conversation_id, options.target.version, input_text, uuid.UUID(str(run_id)),
                    trace_id=trace_id, traceparent=traceparent, **options.store_kwargs(),
                    client_tools=client_tools, client_tool_results=client_tool_results, ag_ui_run=ag_ui_run,
                )
            except ConversationNotFound:
                raise NotFound(CONVERSATION_NOT_FOUND) from None
            except RunIdConflict:
                raise Conflict("run_id is already in use") from None
            tenant_run = get_run_or_404(self.run_store, tenant_id, recorded_id)
        if not created and not same_request(tenant_run):
            raise Conflict(conflict_detail)
        return launch_recorded_run(self.db, self.launcher, self.run_store, tenant_id, tenant_run, created), created

    def start_turn(
        self,
        tenant_id: str,
        conversation_id: uuid.UUID,
        *,
        run_id: uuid.UUID,
        input_text: str,
        version: int | str | None,
        request_context: dict[str, Any] | None,
        model: ModelRef | None,
        model_settings: ModelSettings | None,
    ) -> tuple[dict[str, Any], bool]:
        """Start the next turn: `(run, created)`; created is False for a retry of the same turn (same
        run_id, input and run options). Conflict while the previous turn is not terminal or when run_id
        is used by a different request."""
        conv = self.require_conversation(tenant_id, conversation_id)
        agent = lock_agent(self.db, tenant_id, conv["agent_id"])

        # A retry of a recorded turn is compared with the row and relaunched as recorded; only a new
        # turn validates its options (each turn has its own context, model and version or draft).
        def same_request(run: dict[str, Any]) -> bool:
            return (
                run["conversation_id"] == conv["id"]
                and run["input"] == input_text
                and same_options(
                    self.db, run, version=version, request_context=request_context, model=model,
                    model_settings=model_settings,
                )
            )

        try:
            return self.record_and_launch_turn(
                tenant_id, conversation_id,
                run_id=run_id,
                new_options=lambda: self.new_turn_options(
                    tenant_id, agent, version=version, request_context=request_context, model=model,
                    input_text=input_text, model_settings=model_settings,
                ),
                input_text=input_text,
                same_request=same_request,
            )
        except TurnInProgress as busy:
            raise Conflict(turn_in_progress_message(busy)) from None
