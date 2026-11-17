"""Tenant secrets (0027_tenant_secrets): encrypted values agents reference as `secret:<name>`.

Values are write-only: `names` never returns one, only `resolve` decrypts, and it is called by the
worker right before a value is used (and by tests). Ciphertext is AES-GCM under the platform key with
`tenant_secret_context(tenant, name)` as associated data, so a row copied to another tenant or name
fails to decrypt. Nothing here logs, formats or raises with a value."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, LargeBinary, PrimaryKeyConstraint, String, delete, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Mapped, mapped_column, sessionmaker

from eap_schemas.secrets import SECRET_NAME_PATTERN, SECRET_VALUE_MAX_CHARS, is_secret_name
from eap_secrets import Keyring, tenant_secret_context

from eap_run_store.store import Base


class TenantSecretModel(Base):
    __tablename__ = "tenant_secrets"

    tenant_id: Mapped[str] = mapped_column("tenant_id", String, nullable=False)
    name: Mapped[str] = mapped_column("name", String, nullable=False)
    ciphertext: Mapped[bytes] = mapped_column("ciphertext", LargeBinary, nullable=False)
    key_id: Mapped[str] = mapped_column("key_id", String, nullable=False)
    created_by: Mapped[str] = mapped_column("created_by", String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        "updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "name", name="tenant_secrets_pkey"),
        CheckConstraint(f"name ~ '{SECRET_NAME_PATTERN}'", name="ck_tenant_secrets_name"),
    )


class SecretStore:
    """Per-tenant secret values. Every method is scoped by `tenant_id`."""

    def __init__(self, engine: Any, ring: Keyring | None) -> None:
        """`ring` may be None for callers that only list or delete names (put and resolve then fail)."""
        self.engine = engine
        self.ring = ring
        self.Session = sessionmaker(bind=engine)

    def put(self, tenant_id: str, name: str, value: str, created_by: str) -> None:
        """Store or replace a secret (ValueError for a bad name or an empty or over-long value; the
        message never contains the value)."""
        if not is_secret_name(name):
            raise ValueError("invalid secret name")
        if not isinstance(value, str) or not 1 <= len(value) <= SECRET_VALUE_MAX_CHARS:
            raise ValueError(f"secret value must be 1 to {SECRET_VALUE_MAX_CHARS} characters")
        if self.ring is None:
            raise RuntimeError("no keyring")
        sealed = self.ring.encrypt(value, context=tenant_secret_context(tenant_id, name))
        stmt = pg_insert(TenantSecretModel).values(
            tenant_id=tenant_id, name=name, ciphertext=sealed.ciphertext, key_id=sealed.key_id, created_by=created_by
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["tenant_id", "name"],
            set_={"ciphertext": sealed.ciphertext, "key_id": sealed.key_id, "updated_at": func.now()},
        )
        with self.Session.begin() as db:
            db.execute(stmt)

    def names(self, tenant_id: str) -> list[dict[str, Any]]:
        """`[{name, created_at, updated_at}]` by name; never a value."""
        with self.Session() as db:
            rows = db.execute(
                select(TenantSecretModel.name, TenantSecretModel.created_at, TenantSecretModel.updated_at)
                .where(TenantSecretModel.tenant_id == tenant_id)
                .order_by(TenantSecretModel.name)
            ).all()
        return [{"name": n, "created_at": c, "updated_at": u} for n, c, u in rows]

    def delete(self, tenant_id: str, name: str) -> bool:
        """True when a secret was removed."""
        with self.Session.begin() as db:
            result = db.execute(
                delete(TenantSecretModel).where(TenantSecretModel.tenant_id == tenant_id, TenantSecretModel.name == name)
            )
        return result.rowcount > 0

    def resolve(self, tenant_id: str, name: str) -> str | None:
        """The decrypted value, or None when this tenant has no such secret."""
        with self.Session() as db:
            row = db.execute(
                select(TenantSecretModel.ciphertext, TenantSecretModel.key_id)
                .where(TenantSecretModel.tenant_id == tenant_id, TenantSecretModel.name == name)
            ).first()
        if row is None:
            return None
        if self.ring is None:
            raise RuntimeError("no keyring")
        return self.ring.decrypt(bytes(row[0]), row[1], context=tenant_secret_context(tenant_id, name))


__all__ = ["SecretStore", "TenantSecretModel"]
