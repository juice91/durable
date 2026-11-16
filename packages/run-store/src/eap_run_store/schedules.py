"""Workflow schedules (0022_parity_round_2): the ORM table. Store methods arrive with the schedules task."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Index, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from eap_run_store.store import Base


class WorkflowScheduleModel(Base):
    __tablename__ = "workflow_schedules"

    id: Mapped[str] = mapped_column("id", PG_UUID(as_uuid=False), primary_key=True)
    tenant_id: Mapped[str] = mapped_column("tenant_id", String, nullable=False)
    workflow_id: Mapped[str] = mapped_column("workflow_id", String, nullable=False)
    name: Mapped[str] = mapped_column("name", String, nullable=False)
    cron: Mapped[str] = mapped_column("cron", String, nullable=False)
    timezone: Mapped[str] = mapped_column("timezone", String, nullable=False, server_default=text("'UTC'"))
    input: Mapped[Any] = mapped_column("input", JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    active: Mapped[bool] = mapped_column("active", Boolean, nullable=False, server_default=text("true"))
    last_error: Mapped[str | None] = mapped_column("last_error", Text, nullable=True)
    last_fired_at: Mapped[datetime | None] = mapped_column("last_fired_at", DateTime(timezone=True), nullable=True)
    next_fire_at: Mapped[datetime] = mapped_column("next_fire_at", DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        "updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "workflow_id", "name", name="uq_workflow_schedules_tenant_workflow_name"),
        Index("ix_workflow_schedules_active_next_fire", "active", "next_fire_at"),
    )
