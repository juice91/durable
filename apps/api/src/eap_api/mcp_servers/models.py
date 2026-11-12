"""Tables `mcp_servers` and `mcp_server_secrets` (created by Alembic revision 0004_mcp_servers).

Both are tenant-owned with tenant-composite keys, like the rest of the schema: a secret row can only
belong to a server of its own tenant (FK (tenant_id, server_id) -> mcp_servers(tenant_id, id)).
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    LargeBinary,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    text,
)
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
    )

    id: Mapped[str] = mapped_column(String, default=lambda: str(uuid4()))
    tenant_id: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    transport: Mapped[str] = mapped_column(
        String, nullable=False, default=TRANSPORT_STREAMABLE_HTTP, server_default=text("'streamable_http'")
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[str | None] = mapped_column(String, nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String, nullable=True)


class McpServerSecretModel(Base):
    """One encrypted header value. `ciphertext` is AES-GCM (eap_secrets) bound to
    (tenant_id, server_id, header_name); `key_id` names the platform key that sealed it."""

    __tablename__ = "mcp_server_secrets"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "server_id", "header_name", name="mcp_server_secrets_pkey"),
        ForeignKeyConstraint(
            ["tenant_id", "server_id"],
            ["mcp_servers.tenant_id", "mcp_servers.id"],
            name="fk_mcp_server_secrets_tenant_server",
            ondelete="CASCADE",
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String, nullable=False)
    server_id: Mapped[str] = mapped_column(String, nullable=False)
    header_name: Mapped[str] = mapped_column(String, nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_id: Mapped[str] = mapped_column(String, nullable=False)

    def __repr__(self) -> str:
        return f"McpServerSecretModel(server_id={self.server_id!r}, header_name={self.header_name!r})"
