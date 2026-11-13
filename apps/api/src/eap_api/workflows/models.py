"""Tables `workflows` and `workflow_versions` (created by Alembic revision 0005_workflows).

Mirrors agents/agent_versions, with tenant-composite keys: a version row can only belong to a
workflow of its own tenant (FK (tenant_id, workflow_id) -> workflows(tenant_id, id)).
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from eap_api.models import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class WorkflowModel(Base):
    __tablename__ = "workflows"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="workflows_pkey"),
        UniqueConstraint("tenant_id", "id", name="uq_workflows_tenant_id_id"),
    )

    id: Mapped[str] = mapped_column(String, default=lambda: str(uuid4()))
    tenant_id: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    draft_spec: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)


class WorkflowVersionModel(Base):
    """An immutable published spec; `version` counts up from 1 per workflow."""

    __tablename__ = "workflow_versions"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="workflow_versions_pkey"),
        UniqueConstraint("workflow_id", "version", name="uq_workflow_version"),
        ForeignKeyConstraint(
            ["tenant_id", "workflow_id"],
            ["workflows.tenant_id", "workflows.id"],
            name="fk_workflow_versions_tenant_workflow",
        ),
    )

    id: Mapped[str] = mapped_column(String, default=lambda: str(uuid4()))
    workflow_id: Mapped[str] = mapped_column(String, nullable=False)
    tenant_id: Mapped[str] = mapped_column(String, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    spec: Mapped[dict] = mapped_column(JSONB, nullable=False)
    spec_hash: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    created_by: Mapped[str] = mapped_column(String, nullable=False)
