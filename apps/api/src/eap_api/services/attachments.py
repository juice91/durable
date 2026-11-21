"""Chat attachments (metadata) and message feedback (W2.4): the business logic behind `eap_api.routes.attachments`.

The attachment bytes are streamed by `eap_api.attachments_shared` (a transport concern); this service owns the
rows: the tenant-scoped metadata lookup and the per-(tenant, message, user) feedback.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from eap_api.services.refusals import refusal
from eap_run_store import AgUiRunModel, RunStore
from eap_run_store.chat_attachments import (  # noqa: F401  (re-exported for the route)
    FEEDBACK_COMMENT_MAX_CHARS,
    AttachmentMeta,
    ChatAttachmentStore,
    MessageFeedbackStore,
    max_attachment_bytes,
)

_MESSAGE_ID = re.compile(r"^(?P<prefix>.+)-m(?P<n>\d+)$")


class AttachmentService:
    def __init__(
        self, attachments: ChatAttachmentStore, feedback: MessageFeedbackStore, run_store: RunStore, engine: Any
    ) -> None:
        self.attachments = attachments
        self.feedback = feedback
        self.run_store = run_store
        self.engine = engine

    def get_attachment(self, tenant_id: str, attachment_id: str) -> AttachmentMeta:
        meta = self.attachments.get(tenant_id, attachment_id)
        if meta is None:
            raise refusal(404, "Attachment not found")
        return meta

    # ------------------------------------------------------------------ message feedback

    def _turn_for_message(self, tenant_id: str, conversation_id: str, message_id: str) -> str:
        """The native run id of the turn whose assistant message `message_id` is (404 otherwise)."""
        run_store = self.run_store
        if run_store.get_conversation(tenant_id, conversation_id) is None:
            raise refusal(404, "Conversation not found")
        found = _MESSAGE_ID.match(message_id)
        if found is None:
            raise refusal(404, "Message not found")
        prefix = found.group("prefix")
        turns = run_store.list_turns(tenant_id, conversation_id)
        ids = [str(t["id"]) for t in turns]
        if prefix in ids:
            return prefix
        with Session(self.engine) as session:
            row = session.execute(
                select(AgUiRunModel.run_id).where(
                    AgUiRunModel.tenant_id == tenant_id,
                    AgUiRunModel.external_run_id == prefix,
                    AgUiRunModel.run_id.in_(ids or [""]),
                )
            ).first()
        if row is None:
            raise refusal(404, "Message not found")
        return str(row[0])

    def set_feedback(
        self, tenant_id: str, user_id: str, conversation_id: str, message_id: str, *, rating: str, comment: str | None
    ) -> dict[str, Any]:
        run_id = self._turn_for_message(tenant_id, conversation_id, message_id)
        comment = (comment or "").strip() or None
        record = self.feedback.set(
            tenant_id, run_id, message_id, rating=rating, comment=comment, created_by=user_id,
        )
        return record.as_json()

    def clear_feedback(self, tenant_id: str, user_id: str, conversation_id: str, message_id: str) -> None:
        self._turn_for_message(tenant_id, conversation_id, message_id)
        # Idempotent: deleting feedback that does not exist writes nothing.
        self.feedback.delete(tenant_id, message_id, created_by=user_id)

    def conversation_feedback(self, tenant_id: str, user_id: str, conversation_id: str) -> dict[str, Any]:
        """{message_id: {rating, comment, updated_at}} of one user on a conversation."""
        ids = [str(t["id"]) for t in self.run_store.list_turns(tenant_id, conversation_id)]
        rows = self.feedback.for_runs(tenant_id, ids, created_by=user_id)
        return {
            r.message_id: {"rating": r.rating, "comment": r.comment, "updated_at": r.updated_at.isoformat()}
            for r in rows
        }

    def list_feedback(self, tenant_id: str, conversation_id: str, viewer: str | None) -> dict[str, Any]:
        """The thread's feedback of `viewer` (None: no one, so none)."""
        if self.run_store.get_conversation(tenant_id, conversation_id) is None:
            raise refusal(404, "Conversation not found")
        if viewer is None:
            return {"feedback": {}}
        return {"feedback": self.conversation_feedback(tenant_id, viewer, conversation_id)}
