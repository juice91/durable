"""Trace feedback (0019_trace_feedback): a rating, optional score and comment on a trace (a run) or
one of its spans, with who and when. Append-only; every read and write is tenant-scoped, and the
composite FK to `runs` keeps another tenant's run unreachable."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Text,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, mapped_column, sessionmaker

from eap_run_store.store import Base

FEEDBACK_RATINGS = ("up", "down")
FEEDBACK_COMMENT_MAX_CHARS = 2000
FEEDBACK_SPAN_ID_MAX_CHARS = 200
FEEDBACK_SCORE_LIMIT = 1_000_000

# Same texts as migration 0019_trace_feedback.
FEEDBACK_RATING_CHECK = "rating IN ('up', 'down')"
FEEDBACK_COMMENT_CHECK = "comment IS NULL OR char_length(comment) <= 2000"
FEEDBACK_SPAN_CHECK = "span_id IS NULL OR char_length(span_id) BETWEEN 1 AND 200"
FEEDBACK_SCORE_CHECK = "score IS NULL OR score BETWEEN -1000000 AND 1000000"


class TraceFeedbackModel(Base):
    __tablename__ = "trace_feedback"

    id: Mapped[str] = mapped_column("id", PG_UUID(as_uuid=False), nullable=False)
    tenant_id: Mapped[str] = mapped_column("tenant_id", String, nullable=False)
    run_id: Mapped[str] = mapped_column("run_id", String, nullable=False)
    span_id: Mapped[str | None] = mapped_column("span_id", String, nullable=True)
    rating: Mapped[str] = mapped_column("rating", String, nullable=False)
    score: Mapped[Decimal | None] = mapped_column("score", Numeric(20, 6), nullable=True)
    comment: Mapped[str | None] = mapped_column("comment", Text, nullable=True)
    created_by: Mapped[str] = mapped_column("created_by", String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        PrimaryKeyConstraint("id", name="trace_feedback_pkey"),
        ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["runs.tenant_id", "runs.id"],
            name="fk_trace_feedback_tenant_run",
            ondelete="CASCADE",
        ),
        CheckConstraint(FEEDBACK_RATING_CHECK, name="ck_trace_feedback_rating"),
        CheckConstraint(FEEDBACK_COMMENT_CHECK, name="ck_trace_feedback_comment"),
        CheckConstraint(FEEDBACK_SPAN_CHECK, name="ck_trace_feedback_span"),
        CheckConstraint(FEEDBACK_SCORE_CHECK, name="ck_trace_feedback_score"),
        Index("ix_trace_feedback_tenant_run_created", "tenant_id", "run_id", "created_at"),
    )


@dataclass(frozen=True)
class TraceFeedbackRecord:
    id: str
    run_id: str
    span_id: str | None
    rating: str
    score: float | None
    comment: str | None
    created_by: str
    created_at: datetime

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "trace_id": self.run_id,
            "span_id": self.span_id,
            "rating": self.rating,
            "score": self.score,
            "comment": self.comment,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat(),
        }


def _record(row: TraceFeedbackModel) -> TraceFeedbackRecord:
    return TraceFeedbackRecord(
        id=str(row.id),
        run_id=str(row.run_id),
        span_id=row.span_id,
        rating=row.rating,
        score=None if row.score is None else float(row.score),
        comment=row.comment,
        created_by=row.created_by,
        created_at=row.created_at,
    )


class TraceFeedbackStore:
    def __init__(self, engine: Engine) -> None:
        self.Session = sessionmaker(bind=engine, expire_on_commit=False)

    def add(
        self,
        tenant_id: str,
        run_id: UUID | str,
        *,
        rating: str,
        created_by: str,
        span_id: str | None = None,
        score: float | None = None,
        comment: str | None = None,
    ) -> TraceFeedbackRecord:
        row = TraceFeedbackModel(
            id=str(uuid4()),
            tenant_id=tenant_id,
            run_id=str(run_id),
            span_id=span_id,
            rating=rating,
            score=None if score is None else Decimal(str(score)),
            comment=comment,
            created_by=created_by,
        )
        with self.Session() as session, session.begin():
            session.add(row)
            session.flush()
            session.refresh(row)
            return _record(row)

    def list(
        self, tenant_id: str, run_id: UUID | str, *, span_id: str | None = None, limit: int = 200
    ) -> list[TraceFeedbackRecord]:
        """The trace's feedback, newest first (one span's when `span_id` is given)."""
        stmt = select(TraceFeedbackModel).where(
            TraceFeedbackModel.tenant_id == tenant_id, TraceFeedbackModel.run_id == str(run_id)
        )
        if span_id is not None:
            stmt = stmt.where(TraceFeedbackModel.span_id == span_id)
        stmt = stmt.order_by(TraceFeedbackModel.created_at.desc(), TraceFeedbackModel.id.desc()).limit(limit)
        with self.Session() as session:
            return [_record(r) for r in session.execute(stmt).scalars()]
