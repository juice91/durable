"""Chat attachments and message feedback (0021_chat_attachments).

Attachments: files the user attaches in the chat composer, stored as bytes in `chat_attachments`
(the same "bytes live in the tenant's Postgres rows" approach as knowledge documents). An upload is
free-standing; `link` ties it to the turn (`run_id`) and the user message (`message_id`) that
references it. Every read and write is tenant-scoped: another tenant's id is "not found".

Feedback: one thumbs rating and optional comment per (tenant, message, user) on an assistant
message, tied to the run that produced it. Setting again replaces the rating.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable
from uuid import uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    delete,
    func,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, mapped_column, sessionmaker

from eap_run_store.store import Base

DEFAULT_MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_ATTACHMENTS_PER_MESSAGE = 5
ATTACHMENT_NAME_MAX_CHARS = 255
FEEDBACK_RATINGS = ("up", "down")
FEEDBACK_COMMENT_MAX_CHARS = 2000

# Same texts as migration 0021_chat_attachments.
ATTACHMENT_SIZE_CHECK = "size >= 0"
FEEDBACK_RATING_CHECK = "rating IN ('up', 'down')"
FEEDBACK_COMMENT_CHECK = "comment IS NULL OR char_length(comment) <= 2000"

IMAGE_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
PDF_TYPE = "application/pdf"
# Text types are read as text by the worker (any model); they are never sent as binary.
TEXT_TYPES = frozenset({"text/plain", "text/markdown", "text/csv", "application/json"})
ALLOWED_MEDIA_TYPES = IMAGE_TYPES | {PDF_TYPE} | TEXT_TYPES


def max_attachment_bytes() -> int:
    """EAP_CHAT_ATTACHMENT_MAX_BYTES: the largest attachment (default 10 MiB)."""
    raw = os.environ.get("EAP_CHAT_ATTACHMENT_MAX_BYTES", "").strip()
    try:
        value = int(raw) if raw else DEFAULT_MAX_ATTACHMENT_BYTES
    except ValueError:
        return DEFAULT_MAX_ATTACHMENT_BYTES
    return value if value > 0 else DEFAULT_MAX_ATTACHMENT_BYTES


def is_text_type(media_type: str) -> bool:
    return media_type in TEXT_TYPES


def model_accepts(provider: str, name: str, media_type: str) -> bool:
    """Whether a model takes this media type as binary input. Text types are always fine (the
    worker inlines them). Real providers take images; PDFs only where the provider reads them.
    Scripted `test:` models: only `echo` and `history` take binary (so a test can see the input)."""
    if is_text_type(media_type):
        return True
    if provider == "test":
        return name in ("echo", "history", "vision")
    if media_type in IMAGE_TYPES:
        return True
    if media_type == PDF_TYPE:
        return provider in ("anthropic", "openai", "google-gla", "google-vertex", "google", "litellm", "bedrock")
    return False


class ChatAttachmentModel(Base):
    __tablename__ = "chat_attachments"

    id: Mapped[str] = mapped_column("id", PG_UUID(as_uuid=False), nullable=False)
    tenant_id: Mapped[str] = mapped_column("tenant_id", String, nullable=False)
    name: Mapped[str] = mapped_column("name", String, nullable=False)
    media_type: Mapped[str] = mapped_column("media_type", String, nullable=False)
    size: Mapped[int] = mapped_column("size", Integer, nullable=False)
    sha256: Mapped[str] = mapped_column("sha256", String, nullable=False)
    data: Mapped[bytes] = mapped_column("data", LargeBinary, nullable=False)
    message_id: Mapped[str | None] = mapped_column("message_id", String, nullable=True)
    created_by: Mapped[str] = mapped_column("created_by", String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        PrimaryKeyConstraint("id", name="chat_attachments_pkey"),
        CheckConstraint(ATTACHMENT_SIZE_CHECK, name="ck_chat_attachments_size"),
        Index("ix_chat_attachments_tenant_message", "tenant_id", "message_id"),
    )


class MessageFeedbackModel(Base):
    __tablename__ = "message_feedback"

    id: Mapped[str] = mapped_column("id", PG_UUID(as_uuid=False), nullable=False)
    tenant_id: Mapped[str] = mapped_column("tenant_id", String, nullable=False)
    run_id: Mapped[str] = mapped_column("run_id", String, nullable=False)
    message_id: Mapped[str] = mapped_column("message_id", String, nullable=False)
    rating: Mapped[str] = mapped_column("rating", String, nullable=False)
    comment: Mapped[str | None] = mapped_column("comment", Text, nullable=True)
    created_by: Mapped[str] = mapped_column("created_by", String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        "updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        PrimaryKeyConstraint("id", name="message_feedback_pkey"),
        ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["runs.tenant_id", "runs.id"],
            name="fk_message_feedback_tenant_run",
            ondelete="CASCADE",
        ),
        UniqueConstraint("tenant_id", "message_id", "created_by", name="uq_message_feedback_message_user"),
        CheckConstraint(FEEDBACK_RATING_CHECK, name="ck_message_feedback_rating"),
        CheckConstraint(FEEDBACK_COMMENT_CHECK, name="ck_message_feedback_comment"),
        Index("ix_message_feedback_tenant_run", "tenant_id", "run_id"),
    )


@dataclass(frozen=True)
class AttachmentMeta:
    id: str
    name: str
    media_type: str
    size: int
    message_id: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "media_type": self.media_type, "size": self.size}


@dataclass(frozen=True)
class AttachmentBlob:
    meta: AttachmentMeta
    data: bytes


@dataclass(frozen=True)
class FeedbackRecord:
    message_id: str
    run_id: str
    rating: str
    comment: str | None
    created_by: str
    created_at: datetime
    updated_at: datetime

    def as_json(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "run_id": self.run_id,
            "rating": self.rating,
            "comment": self.comment,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


def _meta(row: ChatAttachmentModel) -> AttachmentMeta:
    return AttachmentMeta(
        id=str(row.id), name=row.name, media_type=row.media_type, size=row.size, message_id=row.message_id,
    )


class ChatAttachmentStore:
    def __init__(self, engine: Engine) -> None:
        self.Session = sessionmaker(bind=engine, expire_on_commit=False)

    def create(self, tenant_id: str, *, name: str, media_type: str, data: bytes, created_by: str) -> AttachmentMeta:
        row = ChatAttachmentModel(
            id=str(uuid4()), tenant_id=tenant_id, name=name, media_type=media_type, size=len(data),
            sha256=hashlib.sha256(data).hexdigest(), data=data, created_by=created_by,
        )
        with self.Session() as session, session.begin():
            session.add(row)
        return _meta(row)

    def get(self, tenant_id: str, attachment_id: str) -> AttachmentBlob | None:
        try:
            with self.Session() as session:
                row = session.execute(
                    select(ChatAttachmentModel).where(
                        ChatAttachmentModel.tenant_id == tenant_id, ChatAttachmentModel.id == attachment_id
                    )
                ).scalar_one_or_none()
        except Exception as exc:  # noqa: BLE001 - a malformed uuid is "not found"
            if "invalid input syntax" in str(exc).lower():
                return None
            raise
        return None if row is None else AttachmentBlob(_meta(row), row.data)

    def metas(self, tenant_id: str, ids: Iterable[str]) -> dict[str, AttachmentMeta]:
        wanted = [i for i in dict.fromkeys(ids) if _is_uuid(i)]
        if not wanted:
            return {}
        with self.Session() as session:
            rows = session.execute(
                select(ChatAttachmentModel).where(
                    ChatAttachmentModel.tenant_id == tenant_id, ChatAttachmentModel.id.in_(wanted)
                )
            ).scalars().all()
        return {str(r.id): _meta(r) for r in rows}

    def link(self, tenant_id: str, ids: Iterable[str], *, message_id: str) -> None:
        """Tie uploads to the user message that sends them (idempotent for the same message)."""
        wanted = [i for i in ids if _is_uuid(i)]
        if not wanted:
            return
        with self.Session() as session, session.begin():
            session.execute(
                update(ChatAttachmentModel)
                .where(
                    ChatAttachmentModel.tenant_id == tenant_id,
                    ChatAttachmentModel.id.in_(wanted),
                    (ChatAttachmentModel.message_id.is_(None)) | (ChatAttachmentModel.message_id == message_id),
                )
                .values(message_id=message_id)
            )

    def for_run(self, tenant_id: str, run_id: str) -> list[AttachmentBlob]:
        """The bytes of the attachments of a turn's user messages (its `ag_ui_runs.input_message_ids`)."""
        from eap_run_store.ag_ui import AgUiRunModel

        with self.Session() as session:
            ids_rows = session.execute(
                select(AgUiRunModel.input_message_ids)
                .where(AgUiRunModel.tenant_id == tenant_id, AgUiRunModel.run_id == run_id)
                .order_by(AgUiRunModel.from_seq, AgUiRunModel.created_at)
            ).all()
            message_ids = [str(m) for row in ids_rows[:1] for m in (row[0] or [])]
            if not message_ids:
                return []
            rows = session.execute(
                select(ChatAttachmentModel)
                .where(ChatAttachmentModel.tenant_id == tenant_id, ChatAttachmentModel.message_id.in_(message_ids))
                .order_by(ChatAttachmentModel.created_at, ChatAttachmentModel.id)
            ).scalars().all()
        return [AttachmentBlob(_meta(r), r.data) for r in rows]

    def for_messages(self, tenant_id: str, message_ids: Iterable[str]) -> dict[str, list[AttachmentMeta]]:
        wanted = list(dict.fromkeys(message_ids))
        if not wanted:
            return {}
        with self.Session() as session:
            rows = session.execute(
                select(ChatAttachmentModel)
                .where(ChatAttachmentModel.tenant_id == tenant_id, ChatAttachmentModel.message_id.in_(wanted))
                .order_by(ChatAttachmentModel.created_at, ChatAttachmentModel.id)
            ).scalars().all()
        out: dict[str, list[AttachmentMeta]] = {}
        for row in rows:
            out.setdefault(str(row.message_id), []).append(_meta(row))
        return out


def _is_uuid(value: str) -> bool:
    import uuid

    try:
        uuid.UUID(str(value))
        return True
    except ValueError:
        return False


def _feedback(row: MessageFeedbackModel) -> FeedbackRecord:
    return FeedbackRecord(
        message_id=row.message_id, run_id=row.run_id, rating=row.rating, comment=row.comment,
        created_by=row.created_by, created_at=row.created_at, updated_at=row.updated_at,
    )


class MessageFeedbackStore:
    def __init__(self, engine: Engine) -> None:
        self.Session = sessionmaker(bind=engine, expire_on_commit=False)

    def set(
        self, tenant_id: str, run_id: str, message_id: str, *, rating: str, comment: str | None, created_by: str
    ) -> FeedbackRecord:
        stmt = pg_insert(MessageFeedbackModel).values(
            id=str(uuid4()), tenant_id=tenant_id, run_id=run_id, message_id=message_id, rating=rating,
            comment=comment, created_by=created_by,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_message_feedback_message_user",
            set_={"rating": rating, "comment": comment, "run_id": run_id, "updated_at": func.now()},
        ).returning(MessageFeedbackModel)
        with self.Session() as session, session.begin():
            row = session.execute(stmt).scalar_one()
            return _feedback(row)

    def delete(self, tenant_id: str, message_id: str, *, created_by: str) -> bool:
        with self.Session() as session, session.begin():
            result = session.execute(
                delete(MessageFeedbackModel).where(
                    MessageFeedbackModel.tenant_id == tenant_id,
                    MessageFeedbackModel.message_id == message_id,
                    MessageFeedbackModel.created_by == created_by,
                )
            )
            return (result.rowcount or 0) > 0

    def for_runs(self, tenant_id: str, run_ids: Iterable[str], *, created_by: str | None = None) -> list[FeedbackRecord]:
        wanted = list(dict.fromkeys(run_ids))
        if not wanted:
            return []
        stmt = select(MessageFeedbackModel).where(
            MessageFeedbackModel.tenant_id == tenant_id, MessageFeedbackModel.run_id.in_(wanted)
        )
        if created_by is not None:
            stmt = stmt.where(MessageFeedbackModel.created_by == created_by)
        with self.Session() as session:
            rows = session.execute(stmt.order_by(MessageFeedbackModel.created_at)).scalars().all()
        return [_feedback(r) for r in rows]
