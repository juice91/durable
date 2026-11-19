"""Tables `mcp_servers` and `mcp_server_secrets` (created by Alembic revision 0004_mcp_servers).

Both are tenant-owned with tenant-composite keys, like the rest of the schema: a secret row can only
belong to a server of its own tenant (FK (tenant_id, server_id) -> mcp_servers(tenant_id, id)).
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
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
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from eap_api.models import Base

TRANSPORT_STREAMABLE_HTTP = "streamable_http"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class McpServerModel(Base):
    __tablename__ = "mcp_servers"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="mcp_servers_pkey"),
        UniqueConstraint("tenant_id", "id", name="uq_mcp_servers_tenant_id_id"),
        UniqueConstraint("tenant_id", "name", name="uq_mcp_servers_tenant_name"),
        CheckConstraint("transport = 'streamable_http'", name="ck_mcp_servers_transport"),
        CheckConstraint("last_status IS NULL OR last_status IN ('ok', 'error')", name="ck_mcp_servers_last_status"),
        CheckConstraint("runtime IN ('external', 'managed')", name="ck_mcp_servers_runtime"),
        CheckConstraint("(runtime = 'external') = (url IS NOT NULL)", name="ck_mcp_servers_runtime_url"),
    )

    id: Mapped[str] = mapped_column(String, default=lambda: str(uuid4()))
    tenant_id: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    # NULL for a managed server: its endpoint lives on mcp_runtimes (0024).
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    runtime: Mapped[str] = mapped_column(Text, nullable=False, default="external", server_default=text("'external'"))
    transport: Mapped[str] = mapped_column(
        String, nullable=False, default=TRANSPORT_STREAMABLE_HTTP, server_default=text("'streamable_http'")
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[str | None] = mapped_column(String, nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    # 0033: the optimistic-concurrency token (eap_run_store.revisions): +1 on every write.
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))


class McpServerSecretModel(Base):
    """One encrypted header value. `ciphertext` is AES-GCM (eap_secrets) bound to
    (tenant_id, server_id, header_name); `key_id` names the platform key that sealed it."""

    __tablename__ = "mcp_server_secrets"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "server_id", "kind", "header_name", name="mcp_server_secrets_pkey"),
        CheckConstraint("kind IN ('header', 'env')", name="ck_mcp_server_secrets_kind"),
        ForeignKeyConstraint(
            ["tenant_id", "server_id"],
            ["mcp_servers.tenant_id", "mcp_servers.id"],
            name="fk_mcp_server_secrets_tenant_server",
            ondelete="CASCADE",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String, nullable=False)
    server_id: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False, default="header", server_default=text("'header'"))
    header_name: Mapped[str] = mapped_column(String, nullable=False)  # also an env variable's name when kind = 'env'
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_id: Mapped[str] = mapped_column(String, nullable=False)

    def __repr__(self) -> str:
        return f"McpServerSecretModel(server_id={self.server_id!r}, header_name={self.header_name!r})"


class McpCatalogEntryModel(Base):
    """Platform-wide, read-only mirror of the official MCP registry (0024); no tenant."""

    __tablename__ = "mcp_catalog_entries"
    __table_args__ = (
        PrimaryKeyConstraint("name", name="mcp_catalog_entries_pkey"),
        Index("ix_mcp_catalog_entries_latest_status", "is_latest", "status"),
        Index("ix_mcp_catalog_entries_name_lower", func.lower(text("name"))),
        Index("ix_mcp_catalog_entries_source", "source", "status"),
        CheckConstraint("source IN ('official', 'toolhive')", name="ck_mcp_catalog_entries_source"),
    )

    name: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active", server_default=text("'active'"))
    is_latest: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    packages: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))
    remotes: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))
    licence: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    hidden: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    source: Mapped[str] = mapped_column(Text, nullable=False, default="official", server_default=text("'official'"))
    meta: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class McpRuntimeModel(Base):
    """A managed server's runtime (one pod per tenant per server). Deleted with its server."""

    __tablename__ = "mcp_runtimes"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="mcp_runtimes_pkey"),
        UniqueConstraint("tenant_id", "server_id", name="uq_mcp_runtimes_tenant_server"),
        ForeignKeyConstraint(
            ["tenant_id", "server_id"],
            ["mcp_servers.tenant_id", "mcp_servers.id"],
            name="fk_mcp_runtimes_tenant_server",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "state IN ('pending', 'starting', 'running', 'idle', 'stopping', 'stopped', 'failed')", name="ck_mcp_runtimes_state"
        ),
        Index("ix_mcp_runtimes_state_last_used", "state", "last_used_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, default=uuid4)
    tenant_id: Mapped[str] = mapped_column(String, nullable=False)
    server_id: Mapped[str] = mapped_column(String, nullable=False)
    catalog_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    package: Mapped[dict] = mapped_column(JSONB, nullable=False)
    image: Mapped[str] = mapped_column(Text, nullable=False)
    namespace: Mapped[str] = mapped_column(Text, nullable=False)
    cr_name: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="pending", server_default=text("'pending'"))
    endpoint: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)
