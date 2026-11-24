"""Database models for the control-plane API."""

from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)

from eap_schemas import AgentSpec, canonical_spec_hash

# SQLAlchemy 2.x style type annotations
StrAnnotated = Annotated[str, ""]
UUIDAnnotated = Annotated[UUID, ""]


class Base(DeclarativeBase):
    """Base class for all database models."""


class AgentModel(Base):
    """Model for agents table."""

    __tablename__ = "agents"
    __table_args__ = (
        Index("uq_agents_tenant_slug", "tenant_id", "slug", unique=True, postgresql_where=text("slug IS NOT NULL")),
    )

    id: Mapped[UUIDAnnotated] = mapped_column(
        "id",
        String,
        primary_key=True,
        default=lambda: str(uuid4()),
    )
    tenant_id: Mapped[StrAnnotated] = mapped_column("tenant_id", String, nullable=False)
    name: Mapped[StrAnnotated] = mapped_column("name", String, nullable=False)
    draft_spec: Mapped[dict] = mapped_column("draft_spec", JSONB, nullable=True)
    archived: Mapped[bool] = mapped_column("archived", Boolean, default=False)
    # 0025: a platform-seeded agent (the Architect and its drafter): never archived, and the only
    # kind whose tools may use the `architect.*` namespace.
    builtin: Mapped[bool] = mapped_column(
        "builtin", Boolean, nullable=False, default=False, server_default=text("false")
    )
    # 0026: the Architect's short id for a definition it made (NULL for hand-made agents).
    slug: Mapped[str | None] = mapped_column("slug", String, nullable=True)
    # 0033: the optimistic-concurrency token (eap_run_store.revisions): +1 on every write.
    revision: Mapped[int] = mapped_column("revision", Integer, nullable=False, default=1, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(
        "created_at", DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        "updated_at", DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    versions: Mapped[list["AgentVersionModel"]] = relationship(
        "AgentVersionModel",
        back_populates="agent",
        cascade="all, delete-orphan",
        order_by="desc(AgentVersionModel.version)",
    )

    def to_dict(self) -> dict:
        """Convert to dict representation."""
        return {
            "id": str(self.id),
            "tenant_id": self.tenant_id,
            "name": self.name,
            "draft_spec": self.draft_spec,
            "archived": self.archived,
            "builtin": bool(self.builtin),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class AgentVersionModel(Base):
    """Model for agent_versions table."""

    __tablename__ = "agent_versions"
    __table_args__ = (
        UniqueConstraint("agent_id", "version", name="uq_agent_version"),
        # 0009: a restore names an existing version of the same agent as its source.
        ForeignKeyConstraint(
            ["agent_id", "restored_from"],
            ["agent_versions.agent_id", "agent_versions.version"],
            name="fk_agent_versions_restored_from",
        ),
    )

    id: Mapped[UUIDAnnotated] = mapped_column(
        "id",
        String,
        primary_key=True,
        default=lambda: str(uuid4()),
    )
    agent_id: Mapped[UUIDAnnotated] = mapped_column(
        "agent_id", String, ForeignKey("agents.id"), nullable=False
    )
    tenant_id: Mapped[StrAnnotated] = mapped_column("tenant_id", String, nullable=False)
    version: Mapped[int] = mapped_column("version", Integer, nullable=False)
    spec: Mapped[dict] = mapped_column("spec", JSONB, nullable=False)
    spec_hash: Mapped[StrAnnotated] = mapped_column("spec_hash", String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        "created_at", DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    created_by: Mapped[StrAnnotated] = mapped_column("created_by", String, nullable=False)
    # 0036: the principal who published (or restored) this version; NULL for versions from before (legacy).
    published_by: Mapped[str | None] = mapped_column("published_by", Text, nullable=True)
    # The version this one was restored from (POST .../versions/{v}/restore); NULL for a publish.
    restored_from: Mapped[int | None] = mapped_column("restored_from", Integer, nullable=True)
    # 0025: `architect:<run_id>` or `architect-page` for a version the Architect made; NULL otherwise.
    created_via: Mapped[str | None] = mapped_column("created_via", Text, nullable=True)

    agent: Mapped["AgentModel"] = relationship(
        "AgentModel", back_populates="versions", foreign_keys=[agent_id]
    )

    def to_dict(self) -> dict:
        """Convert to dict representation."""
        return {
            "id": str(self.id),
            "agent_id": str(self.agent_id),
            "tenant_id": self.tenant_id,
            "version": self.version,
            "spec": self.spec,
            "spec_hash": self.spec_hash,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "created_by": self.created_by,
            "published_by": self.published_by,
            "restored_from": self.restored_from,
            "created_via": self.created_via,
        }

    @classmethod
    def from_agent_spec(
        cls, agent_id: UUID, tenant_id: str, spec: AgentSpec, version: int, created_by: str
    ) -> "AgentVersionModel":
        """Create a version from an AgentSpec."""
        return cls(
            agent_id=agent_id,
            tenant_id=tenant_id,
            version=version,
            spec=spec.model_dump(mode="json"),
            spec_hash=canonical_spec_hash(spec),
            created_by=created_by,
        )


def create_database_engine(database_url: str):
    """Create a SQLAlchemy engine."""
    return create_engine(database_url)


def get_sessionmaker(engine):
    """Get a sessionmaker for the engine."""
    return sessionmaker(bind=engine)
