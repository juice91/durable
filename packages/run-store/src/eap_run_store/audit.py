"""The audit log (0036_iam_actor_audit, design sections 6 and 9): one append-only row per mutating call.

`detail` is built from an allowlist of scalar fields (`clean_detail`): ids, names, status codes, from -> to values. It
never holds a request body, a secret value, a key or a token; a value that looks like a credential is dropped even
from an allowed field. Every read is tenant-scoped; the order is `(at DESC, id DESC)` and the cursor is the same
opaque `(timestamp, id)` token as the run list."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import CheckConstraint, DateTime, Index, PrimaryKeyConstraint, String, and_, delete, or_, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, mapped_column, sessionmaker

from eap_run_store.cursor import RunCursor
from eap_run_store.store import Base

OUTCOMES = ("success", "denied", "failed")
AUDIT_OUTCOME_CHECK = "outcome IN ('success', 'denied', 'failed')"
DEFAULT_RETENTION_DAYS = 400
MAX_LIMIT = 200
DETAIL_FIELDS = frozenset({
    "method", "route", "status", "name", "from", "to", "version", "decision", "scope", "role", "step_id", "tool", "count", "actor_email", "via_api_key",
})
MAX_DETAIL_VALUE = 200
_CREDENTIAL = re.compile(r"(pos_[A-Za-z0-9_-]{8,}|Bearer\s+\S+|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}|sk-[A-Za-z0-9]{16,})")


class AuditLogModel(Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="audit_log_pkey"),
        CheckConstraint(AUDIT_OUTCOME_CHECK, name="ck_audit_log_outcome"),
        Index("ix_audit_log_tenant_at", "tenant_id", "at"),
        Index("ix_audit_log_tenant_resource_at", "tenant_id", "resource_kind", "resource_id", "at"),
    )

    id: Mapped[str] = mapped_column(String, default=lambda: str(uuid4()))
    tenant_id: Mapped[str] = mapped_column(String, nullable=False)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"), default=lambda: datetime.now(timezone.utc)
    )
    actor_kind: Mapped[str] = mapped_column(String, nullable=False)
    actor_subject: Mapped[str] = mapped_column(String, nullable=False)
    on_behalf_of: Mapped[str | None] = mapped_column(String, nullable=True)
    action: Mapped[str] = mapped_column(String, nullable=False)
    resource_kind: Mapped[str | None] = mapped_column(String, nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String, nullable=True)
    outcome: Mapped[str] = mapped_column(String, nullable=False)
    request_id: Mapped[str | None] = mapped_column(String, nullable=True)
    run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


@dataclass(frozen=True)
class AuditEntry:
    tenant_id: str
    actor_kind: str
    actor_subject: str
    action: str
    outcome: str
    on_behalf_of: str | None = None
    resource_kind: str | None = None
    resource_id: str | None = None
    request_id: str | None = None
    run_id: str | None = None
    detail: dict[str, Any] | None = None


def clean_detail(detail: dict[str, Any] | None) -> dict[str, Any] | None:
    """The allowlisted scalar fields of `detail`, with anything credential-shaped dropped; None when nothing is left."""
    if not detail:
        return None
    cleaned: dict[str, Any] = {}
    for key, value in detail.items():
        if key not in DETAIL_FIELDS:
            continue
        if isinstance(value, bool) or isinstance(value, (int, float)) or value is None:
            cleaned[key] = value
        elif isinstance(value, str) and not _CREDENTIAL.search(value):
            cleaned[key] = value[:MAX_DETAIL_VALUE]
    return cleaned or None


def _row(model: AuditLogModel) -> dict[str, Any]:
    return {
        "id": model.id,
        "at": model.at,
        "actor_kind": model.actor_kind,
        "actor_subject": model.actor_subject,
        "on_behalf_of": model.on_behalf_of,
        "action": model.action,
        "resource_kind": model.resource_kind,
        "resource_id": model.resource_id,
        "outcome": model.outcome,
        "request_id": model.request_id,
        "run_id": model.run_id,
        "detail": model.detail,
    }


class AuditStore:
    def __init__(self, engine: Engine) -> None:
        self.Session = sessionmaker(bind=engine)

    def record(self, entry: AuditEntry) -> str:
        if entry.outcome not in OUTCOMES:
            raise ValueError(f"unknown audit outcome {entry.outcome!r}")
        row_id = str(uuid4())
        with self.Session() as session:
            session.add(
                AuditLogModel(
                    id=row_id, tenant_id=entry.tenant_id, actor_kind=entry.actor_kind, actor_subject=entry.actor_subject,
                    on_behalf_of=entry.on_behalf_of, action=entry.action, resource_kind=entry.resource_kind,
                    resource_id=entry.resource_id, outcome=entry.outcome, request_id=entry.request_id,
                    run_id=entry.run_id, detail=clean_detail(entry.detail),
                )
            )
            session.commit()
        return row_id

    def list(
        self,
        tenant_id: str,
        *,
        actor: str | None = None,
        action: str | None = None,
        resource_kind: str | None = None,
        resource_id: str | None = None,
        outcome: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        cursor: RunCursor | None = None,
    ) -> tuple[list[dict[str, Any]], RunCursor | None]:
        """A page of the tenant's rows, newest first, and the cursor of the next page (None at the end)."""
        limit = max(1, min(limit, MAX_LIMIT))
        conditions = [AuditLogModel.tenant_id == tenant_id]
        if actor:
            conditions.append(or_(AuditLogModel.actor_subject == actor, AuditLogModel.on_behalf_of == actor))
        if action:
            conditions.append(AuditLogModel.action == action)
        if resource_kind:
            conditions.append(AuditLogModel.resource_kind == resource_kind)
        if resource_id:
            conditions.append(AuditLogModel.resource_id == resource_id)
        if outcome:
            conditions.append(AuditLogModel.outcome == outcome)
        if since:
            conditions.append(AuditLogModel.at >= since)
        if until:
            conditions.append(AuditLogModel.at < until)
        if cursor:
            conditions.append(
                or_(
                    AuditLogModel.at < cursor.created_at,
                    and_(AuditLogModel.at == cursor.created_at, AuditLogModel.id < cursor.id),
                )
            )
        with self.Session() as session:
            rows = session.execute(
                select(AuditLogModel).where(*conditions).order_by(AuditLogModel.at.desc(), AuditLogModel.id.desc()).limit(limit + 1)
            ).scalars().all()
            items = [_row(r) for r in rows[:limit]]
        next_cursor = RunCursor(items[-1]["at"], items[-1]["id"]) if len(rows) > limit else None
        return items, next_cursor

    def prune_batch(self, cutoff: datetime, limit: int = 500) -> int:
        """Delete at most `limit` rows older than `cutoff` (all tenants); the number deleted. Bounded, so a caller
        loops until it returns less than `limit`."""
        with self.Session() as session:
            ids = session.execute(
                select(AuditLogModel.id).where(AuditLogModel.at < cutoff).order_by(AuditLogModel.at).limit(limit)
            ).scalars().all()
            if not ids:
                return 0
            session.execute(delete(AuditLogModel).where(AuditLogModel.id.in_(ids)))
            session.commit()
            return len(ids)


def retention_cutoff(days: int, now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)) - timedelta(days=days)


RETENTION_ENV = "EAP_AUDIT_RETENTION_DAYS"
MAX_RETENTION_DAYS = 36_500


def retention_days_from_env(env: dict[str, str] | Any) -> int | None:
    """EAP_AUDIT_RETENTION_DAYS: whole days (default 400); `off` keeps every row (None). ValueError when invalid."""
    raw = (env.get(RETENTION_ENV) or "").strip().lower()
    if not raw:
        return DEFAULT_RETENTION_DAYS
    if raw == "off":
        return None
    try:
        days = int(raw)
    except ValueError:
        raise ValueError(f"{RETENTION_ENV} must be a whole number of days or 'off', got {raw!r}") from None
    if not 1 <= days <= MAX_RETENTION_DAYS:
        raise ValueError(f"{RETENTION_ENV} must be between 1 and {MAX_RETENTION_DAYS} days or 'off'")
    return days
