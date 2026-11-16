"""Materialised spans (0022_parity_round_2): the ORM table. Filled by a worker step when a run finishes."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, PrimaryKeyConstraint, String, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from eap_run_store.store import Base


class SpanModel(Base):
    __tablename__ = "spans"

    tenant_id: Mapped[str] = mapped_column("tenant_id", String, nullable=False)
    run_id: Mapped[str] = mapped_column("run_id", PG_UUID(as_uuid=False), nullable=False)
    span_id: Mapped[str] = mapped_column("span_id", String, nullable=False)
    parent_id: Mapped[str | None] = mapped_column("parent_id", String, nullable=True)
    kind: Mapped[str] = mapped_column("kind", String, nullable=False)
    name: Mapped[str] = mapped_column("name", String, nullable=False)
    start: Mapped[datetime] = mapped_column("start", DateTime(timezone=True), nullable=False)
    end: Mapped[datetime | None] = mapped_column("end", DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column("status", String, nullable=False)
    agent_id: Mapped[str | None] = mapped_column("agent_id", String, nullable=True)
    workflow_id: Mapped[str | None] = mapped_column("workflow_id", String, nullable=True)
    model: Mapped[str | None] = mapped_column("model", String, nullable=True)

    __table_args__ = (
        PrimaryKeyConstraint("run_id", "span_id", name="spans_pkey"),
        Index("ix_spans_tenant_start", "tenant_id", text("start DESC")),
    )
