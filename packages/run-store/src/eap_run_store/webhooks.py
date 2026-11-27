"""Inbound webhooks (0037_webhook_triggers): the trigger table, its audit events, the delivery log and `WebhookStore`.

All SQL of the feature lives here. Every read and write is scoped by `tenant_id` except `get_trigger_by_id`, which the
hook route and the `webhook` identity plug-in use: the public URL carries only the trigger id, and the row selects the
tenant (design 4.2 step 2). Nothing here stores or returns a secret value (a trigger holds `secret:<name>` references),
a request body, a signature or a header other than a sanitised label.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID, insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column, sessionmaker

from eap_run_store.revisions import bump_row
from eap_run_store.store import Base, RunModel

TRIGGER_STATUSES = ("disabled", "pending_enable", "enabled", "archived")
PRESET_NAMES = ("github", "standard_webhooks", "custom")
# A delivery claim older than this (still `processing`) is taken over by the next attempt.
CLAIM_LEASE_SECONDS = 60
LIVE_CLAIM_OUTCOMES = ("processing", "accepted")
TERMINAL_RUN_STATUSES = ("completed", "failed", "needs_attention", "skipped")
LAST_DELIVERY_THROTTLE_SECONDS = 60


class WebhookTriggerModel(Base):
    __tablename__ = "webhook_triggers"

    id: Mapped[str] = mapped_column("id", PG_UUID(as_uuid=False), primary_key=True)
    tenant_id: Mapped[str] = mapped_column("tenant_id", String, nullable=False)
    name: Mapped[str] = mapped_column("name", String, nullable=False)
    description: Mapped[str] = mapped_column("description", String, nullable=False, server_default=text("''"))
    workflow_id: Mapped[str] = mapped_column("workflow_id", String, nullable=False)
    workflow_version: Mapped[int | None] = mapped_column("workflow_version", Integer, nullable=True)
    preset: Mapped[str] = mapped_column("preset", String, nullable=False)
    preset_config: Mapped[Any] = mapped_column("preset_config", JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    secrets: Mapped[Any] = mapped_column("secrets", JSONB, nullable=False)
    tolerance_seconds: Mapped[int] = mapped_column("tolerance_seconds", Integer, nullable=False, server_default=text("300"))
    actions: Mapped[Any] = mapped_column("actions", JSONB, nullable=False)
    limits: Mapped[Any] = mapped_column("limits", JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    status: Mapped[str] = mapped_column("status", String, nullable=False, server_default=text("'disabled'"))
    config_hash: Mapped[str] = mapped_column("config_hash", String, nullable=False)
    approved_config_hash: Mapped[str | None] = mapped_column("approved_config_hash", String, nullable=True)
    enable_requested_by: Mapped[str | None] = mapped_column("enable_requested_by", String, nullable=True)
    enable_requested_at: Mapped[datetime | None] = mapped_column("enable_requested_at", DateTime(timezone=True), nullable=True)
    enabled_by: Mapped[str | None] = mapped_column("enabled_by", String, nullable=True)
    enabled_at: Mapped[datetime | None] = mapped_column("enabled_at", DateTime(timezone=True), nullable=True)
    last_delivery_at: Mapped[datetime | None] = mapped_column("last_delivery_at", DateTime(timezone=True), nullable=True)
    last_outcome: Mapped[str | None] = mapped_column("last_outcome", String, nullable=True)
    revision: Mapped[int] = mapped_column("revision", Integer, nullable=False, default=1, server_default=text("1"))
    created_by: Mapped[str] = mapped_column("created_by", String, nullable=False)
    created_at: Mapped[datetime] = mapped_column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()"))
    archived_at: Mapped[datetime | None] = mapped_column("archived_at", DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_webhook_triggers_tenant_name"),
        CheckConstraint("name ~ '^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$'", name="ck_webhook_triggers_name"),
        CheckConstraint("preset IN ('github', 'standard_webhooks', 'custom')", name="ck_webhook_triggers_preset"),
        CheckConstraint(
            "status IN ('disabled', 'pending_enable', 'enabled', 'archived')", name="ck_webhook_triggers_status"
        ),
        CheckConstraint(
            "jsonb_typeof(secrets) = 'array' AND jsonb_array_length(secrets) BETWEEN 1 AND 2",
            name="ck_webhook_triggers_secrets",
        ),
        CheckConstraint("tolerance_seconds BETWEEN 60 AND 900", name="ck_webhook_triggers_tolerance"),
        Index("ix_webhook_triggers_workflow", "tenant_id", "workflow_id", postgresql_where=text("status <> 'archived'")),
    )


class WebhookTriggerEventModel(Base):
    __tablename__ = "webhook_trigger_events"

    id: Mapped[str] = mapped_column("id", PG_UUID(as_uuid=False), primary_key=True)
    tenant_id: Mapped[str] = mapped_column("tenant_id", String, nullable=False)
    trigger_id: Mapped[str] = mapped_column("trigger_id", PG_UUID(as_uuid=False), nullable=False)
    at: Mapped[datetime] = mapped_column("at", DateTime(timezone=True), nullable=False, server_default=text("now()"))
    actor: Mapped[str] = mapped_column("actor", String, nullable=False)
    kind: Mapped[str] = mapped_column("kind", String, nullable=False)
    detail: Mapped[Any] = mapped_column("detail", JSONB, nullable=False, server_default=text("'{}'::jsonb"))

    __table_args__ = (Index("ix_webhook_trigger_events_trigger_at", "tenant_id", "trigger_id", "at"),)


class WebhookDeliveryModel(Base):
    __tablename__ = "webhook_deliveries"

    id: Mapped[str] = mapped_column("id", PG_UUID(as_uuid=False), primary_key=True)
    tenant_id: Mapped[str] = mapped_column("tenant_id", String, nullable=False)
    trigger_id: Mapped[str] = mapped_column("trigger_id", PG_UUID(as_uuid=False), nullable=False)
    received_at: Mapped[datetime] = mapped_column("received_at", DateTime(timezone=True), nullable=False, server_default=text("now()"))
    claimed_at: Mapped[datetime] = mapped_column("claimed_at", DateTime(timezone=True), nullable=False, server_default=text("now()"))
    state: Mapped[str] = mapped_column("state", String, nullable=False, server_default=text("'processing'"))
    outcome: Mapped[str] = mapped_column("outcome", String, nullable=False, server_default=text("'processing'"))
    reason: Mapped[str | None] = mapped_column("reason", String, nullable=True)
    dedupe_key: Mapped[str | None] = mapped_column("dedupe_key", String, nullable=True)
    key_kind: Mapped[str | None] = mapped_column("key_kind", String, nullable=True)
    provider_delivery_id: Mapped[str | None] = mapped_column("provider_delivery_id", String, nullable=True)
    event_label: Mapped[str | None] = mapped_column("event_label", String, nullable=True)
    body_sha256: Mapped[str | None] = mapped_column("body_sha256", String, nullable=True)
    body_bytes: Mapped[int | None] = mapped_column("body_bytes", Integer, nullable=True)
    content_type: Mapped[str | None] = mapped_column("content_type", String, nullable=True)
    timestamp_skew_seconds: Mapped[int | None] = mapped_column("timestamp_skew_seconds", Integer, nullable=True)
    secret_slot: Mapped[int | None] = mapped_column("secret_slot", Integer, nullable=True)
    run_id: Mapped[str | None] = mapped_column("run_id", PG_UUID(as_uuid=False), nullable=True)

    __table_args__ = (
        CheckConstraint("state IN ('processing', 'done')", name="ck_webhook_deliveries_state"),
        Index("ix_webhook_deliveries_run", "tenant_id", "run_id", postgresql_where=text("run_id IS NOT NULL")),
        Index("ix_webhook_deliveries_trigger_received", "tenant_id", "trigger_id", text("received_at DESC")),
        Index(
            "uq_webhook_deliveries_live_claim", "tenant_id", "trigger_id", "dedupe_key", unique=True,
            postgresql_where=text("outcome IN ('processing', 'accepted')"),
        ),
    )


_LOCAL_LOCKS: dict[str, threading.Lock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()
_HOLDERS = threading.BoundedSemaphore(4)
_LOCK_POLL_SECONDS = 0.02


def _local_lock(trigger_id: str) -> threading.Lock:
    with _LOCAL_LOCKS_GUARD:
        return _LOCAL_LOCKS.setdefault(trigger_id, threading.Lock())


class TriggerNameConflict(ValueError):
    """The tenant already has a trigger with this name."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def trigger_dict(row: WebhookTriggerModel) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "tenant_id": row.tenant_id,
        "name": row.name,
        "description": row.description,
        "workflow_id": row.workflow_id,
        "workflow_version": row.workflow_version,
        "preset": row.preset,
        "preset_config": dict(row.preset_config or {}),
        "secrets": [dict(s) for s in (row.secrets or [])],
        "tolerance_seconds": row.tolerance_seconds,
        "actions": dict(row.actions or {}),
        "limits": dict(row.limits or {}),
        "status": row.status,
        "config_hash": row.config_hash,
        "approved_config_hash": row.approved_config_hash,
        "enable_requested_by": row.enable_requested_by,
        "enable_requested_at": row.enable_requested_at,
        "enabled_by": row.enabled_by,
        "enabled_at": row.enabled_at,
        "last_delivery_at": row.last_delivery_at,
        "last_outcome": row.last_outcome,
        "revision": row.revision,
        "created_by": row.created_by,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "archived_at": row.archived_at,
    }


def _delivery_dict(row: WebhookDeliveryModel) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "trigger_id": str(row.trigger_id),
        "received_at": row.received_at,
        "state": row.state,
        "outcome": row.outcome,
        "reason": row.reason,
        "key_kind": row.key_kind,
        "provider_delivery_id": row.provider_delivery_id,
        "event_label": row.event_label,
        "body_sha256": row.body_sha256,
        "body_bytes": row.body_bytes,
        "content_type": row.content_type,
        "timestamp_skew_seconds": row.timestamp_skew_seconds,
        "secret_slot": row.secret_slot,
        "run_id": row.run_id,
    }


# Fields of a trigger an update may set; anything else is refused by the service before it gets here.
_EDITABLE = frozenset({
    "name", "description", "workflow_id", "workflow_version", "preset", "preset_config", "secrets",
    "tolerance_seconds", "actions", "limits", "config_hash",
})


class WebhookStore:
    def __init__(self, engine: Any) -> None:
        self.engine = engine
        self.Session = sessionmaker(bind=engine)

    # ------------------------------------------------------------------------------------------ triggers

    def create_trigger(self, tenant_id: str, created_by: str, values: dict[str, Any]) -> dict[str, Any]:
        """Insert a `disabled` trigger. TriggerNameConflict when the tenant already has the name."""
        row = WebhookTriggerModel(id=str(uuid.uuid4()), tenant_id=tenant_id, created_by=created_by, status="disabled", **values)
        with self.Session() as session:
            session.add(row)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                if "uq_webhook_triggers_tenant_name" in str(exc.orig):
                    raise TriggerNameConflict(values.get("name", "")) from None
                raise
            session.refresh(row)
            return trigger_dict(row)

    def get_trigger(self, tenant_id: str, trigger_id: str) -> dict[str, Any] | None:
        with self.Session() as session:
            row = session.execute(
                select(WebhookTriggerModel).where(WebhookTriggerModel.tenant_id == tenant_id, WebhookTriggerModel.id == str(trigger_id))
            ).scalar_one_or_none()
            return None if row is None else trigger_dict(row)

    def get_trigger_by_id(self, trigger_id: str) -> dict[str, Any] | None:
        """The row of a public URL's trigger id, whatever its tenant: the row selects the tenant."""
        with self.Session() as session:
            row = session.execute(select(WebhookTriggerModel).where(WebhookTriggerModel.id == str(trigger_id))).scalar_one_or_none()
            return None if row is None else trigger_dict(row)

    def list_triggers(self, tenant_id: str, *, include_archived: bool = False, workflow_id: str | None = None) -> list[dict[str, Any]]:
        query = select(WebhookTriggerModel).where(WebhookTriggerModel.tenant_id == tenant_id)
        if not include_archived:
            query = query.where(WebhookTriggerModel.status != "archived")
        if workflow_id is not None:
            query = query.where(WebhookTriggerModel.workflow_id == workflow_id)
        with self.Session() as session:
            rows = session.execute(query.order_by(WebhookTriggerModel.name)).scalars().all()
            return [trigger_dict(r) for r in rows]

    def triggers_using_secret(self, tenant_id: str, secret_name: str) -> list[dict[str, Any]]:
        """`[{trigger_id, name}]` of the tenant's live triggers that reference `secret:<secret_name>`."""
        ref = f"secret:{secret_name}"
        with self.Session() as session:
            rows = session.execute(
                select(WebhookTriggerModel.id, WebhookTriggerModel.name).where(
                    WebhookTriggerModel.tenant_id == tenant_id,
                    WebhookTriggerModel.status != "archived",
                    WebhookTriggerModel.secrets.contains([{"ref": ref}]),
                ).order_by(WebhookTriggerModel.name)
            ).all()
        return [{"trigger_id": str(i), "name": n} for i, n in rows]

    def update_trigger(
        self, tenant_id: str, trigger_id: str, changes: dict[str, Any], expected_revision: int | None = None, **state: Any,
    ) -> dict[str, Any] | None:
        """Apply `changes` (EDITABLE fields) and `state` (status and approval columns), bump the revision in the same
        transaction, guarded by `expected_revision` (RevisionMismatch when stale). None when there is no such trigger.
        A name collision raises TriggerNameConflict."""
        bad = set(changes) - _EDITABLE
        if bad:
            raise ValueError(f"not editable: {sorted(bad)}")
        with self.Session() as session:
            row = session.execute(
                select(WebhookTriggerModel)
                .where(WebhookTriggerModel.tenant_id == tenant_id, WebhookTriggerModel.id == str(trigger_id))
                .with_for_update()
            ).scalar_one_or_none()
            if row is None:
                return None
            for key, value in {**changes, **state}.items():
                setattr(row, key, value)
            row.updated_at = _now()
            try:
                bump_row(session, row, expected_revision)
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                if "uq_webhook_triggers_tenant_name" in str(exc.orig):
                    raise TriggerNameConflict(str(changes.get("name", ""))) from None
                raise
            except Exception:
                session.rollback()
                raise
            session.refresh(row)
            return trigger_dict(row)

    def touch_secret_matched(self, tenant_id: str, trigger_id: str, slot: int) -> None:
        """Record that secret slot `slot` verified a delivery (`last_matched_at`), at most once a minute; no revision bump."""
        now = _now()
        with self.Session.begin() as session:
            row = session.execute(
                select(WebhookTriggerModel.secrets).where(WebhookTriggerModel.tenant_id == tenant_id, WebhookTriggerModel.id == str(trigger_id))
            ).first()
            if row is None or slot >= len(row[0]):
                return
            secrets = [dict(s) for s in row[0]]
            last = secrets[slot].get("last_matched_at")
            if last:
                try:
                    if now - datetime.fromisoformat(last) < timedelta(seconds=LAST_DELIVERY_THROTTLE_SECONDS):
                        return
                except ValueError:
                    pass
            secrets[slot]["last_matched_at"] = now.isoformat()
            session.execute(
                update(WebhookTriggerModel)
                .where(WebhookTriggerModel.tenant_id == tenant_id, WebhookTriggerModel.id == str(trigger_id))
                .values(secrets=secrets)
            )

    def note_delivery(self, tenant_id: str, trigger_id: str, outcome: str) -> None:
        """`last_delivery_at` and `last_outcome` for the list; no revision bump (a delivery is not an edit)."""
        with self.Session.begin() as session:
            session.execute(
                update(WebhookTriggerModel)
                .where(WebhookTriggerModel.tenant_id == tenant_id, WebhookTriggerModel.id == str(trigger_id))
                .values(last_delivery_at=_now(), last_outcome=outcome)
            )

    # ------------------------------------------------------------------------------------------ audit events

    def add_event(self, tenant_id: str, trigger_id: str, actor: str, kind: str, detail: dict[str, Any] | None = None) -> None:
        with self.Session.begin() as session:
            session.add(WebhookTriggerEventModel(
                id=str(uuid.uuid4()), tenant_id=tenant_id, trigger_id=str(trigger_id), actor=actor[:200], kind=kind,
                detail=detail or {},
            ))

    def list_events(self, tenant_id: str, trigger_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self.Session() as session:
            rows = session.execute(
                select(WebhookTriggerEventModel)
                .where(WebhookTriggerEventModel.tenant_id == tenant_id, WebhookTriggerEventModel.trigger_id == str(trigger_id))
                .order_by(WebhookTriggerEventModel.at.desc(), WebhookTriggerEventModel.id)
                .limit(limit)
            ).scalars().all()
            return [
                {"id": str(r.id), "at": r.at, "actor": r.actor, "kind": r.kind, "detail": dict(r.detail or {})} for r in rows
            ]

    # ------------------------------------------------------------------------------------------ deliveries

    def claim_delivery(
        self, tenant_id: str, trigger_id: str, dedupe_key: str, key_kind: str, **diagnostics: Any,
    ) -> tuple[str, dict[str, Any]]:
        """Claim the dedupe key (design 4.4): ('claimed', row) for a new claim, ('duplicate', row) when a finished claim
        exists, ('busy', row) while another attempt's claim is younger than the lease, and ('claimed', row) again for a
        stale `processing` claim this attempt takes over."""
        values = dict(
            id=str(uuid.uuid4()), tenant_id=tenant_id, trigger_id=str(trigger_id), dedupe_key=dedupe_key, key_kind=key_kind,
            state="processing", outcome="processing", **diagnostics,
        )
        stmt = (
            pg_insert(WebhookDeliveryModel).values(**values)
            .on_conflict_do_nothing(
                index_elements=["tenant_id", "trigger_id", "dedupe_key"],
                index_where=text("outcome IN ('processing', 'accepted')"),
            )
            .returning(WebhookDeliveryModel.id)
        )
        with self.Session.begin() as session:
            inserted = session.execute(stmt).first()
            if inserted is not None:
                return "claimed", self._delivery(session, tenant_id, inserted[0])
            existing = session.execute(
                select(WebhookDeliveryModel).where(
                    WebhookDeliveryModel.tenant_id == tenant_id, WebhookDeliveryModel.trigger_id == str(trigger_id),
                    WebhookDeliveryModel.dedupe_key == dedupe_key, WebhookDeliveryModel.outcome.in_(LIVE_CLAIM_OUTCOMES),
                ).with_for_update()
            ).scalar_one_or_none()
            if existing is None:  # released between the conflict and this read: the sender's retry claims it
                return "busy", {}
            if existing.outcome == "accepted":
                return "duplicate", _delivery_dict(existing) | {"run_id": existing.run_id}
            if _now() - existing.claimed_at < timedelta(seconds=CLAIM_LEASE_SECONDS):
                return "busy", _delivery_dict(existing)
            existing.claimed_at = _now()  # a crashed attempt's claim: take it over
            session.flush()
            return "claimed", _delivery_dict(existing)

    @staticmethod
    def _delivery(session: Any, tenant_id: str, delivery_id: str) -> dict[str, Any]:
        row = session.execute(
            select(WebhookDeliveryModel).where(WebhookDeliveryModel.tenant_id == tenant_id, WebhookDeliveryModel.id == str(delivery_id))
        ).scalar_one()
        return _delivery_dict(row)

    def finish_delivery(
        self, tenant_id: str, delivery_id: str, outcome: str, *, reason: str | None = None, run_id: str | None = None,
    ) -> None:
        with self.Session.begin() as session:
            session.execute(
                update(WebhookDeliveryModel)
                .where(WebhookDeliveryModel.tenant_id == tenant_id, WebhookDeliveryModel.id == str(delivery_id))
                .values(state="done", outcome=outcome, reason=reason, run_id=None if run_id is None else str(run_id))
            )

    def record_delivery(self, tenant_id: str, trigger_id: str, outcome: str, reason: str | None = None, **diagnostics: Any) -> str:
        """A finished delivery row with no dedupe key (a `stale` or `body_invalid` outcome: the signature was valid)."""
        delivery_id = str(uuid.uuid4())
        with self.Session.begin() as session:
            session.add(WebhookDeliveryModel(
                id=delivery_id, tenant_id=tenant_id, trigger_id=str(trigger_id), state="done", outcome=outcome, reason=reason,
                **diagnostics,
            ))
        return delivery_id

    def list_deliveries(
        self, tenant_id: str, *, trigger_id: str | None = None, outcome: str | None = None, since: datetime | None = None,
        limit: int = 100, trigger_ids: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        """`trigger_ids`: restrict to these triggers (what the caller may read); None: no restriction."""
        query = select(WebhookDeliveryModel).where(WebhookDeliveryModel.tenant_id == tenant_id)
        if trigger_ids is not None:
            query = query.where(WebhookDeliveryModel.trigger_id.in_([str(i) for i in trigger_ids]))
        if trigger_id is not None:
            query = query.where(WebhookDeliveryModel.trigger_id == str(trigger_id))
        if outcome is not None:
            query = query.where(WebhookDeliveryModel.outcome == outcome)
        if since is not None:
            query = query.where(WebhookDeliveryModel.received_at >= since)
        with self.Session() as session:
            rows = session.execute(query.order_by(WebhookDeliveryModel.received_at.desc(), WebhookDeliveryModel.id).limit(min(limit, 200))).scalars().all()
            return [_delivery_dict(r) for r in rows]

    def get_delivery(self, tenant_id: str, delivery_id: str) -> dict[str, Any] | None:
        with self.Session() as session:
            row = session.execute(
                select(WebhookDeliveryModel).where(WebhookDeliveryModel.tenant_id == tenant_id, WebhookDeliveryModel.id == str(delivery_id))
            ).scalar_one_or_none()
            return None if row is None else _delivery_dict(row)

    def delete_expired_deliveries(self, cutoff: datetime, batch: int = 1000) -> int:
        """Delete finished delivery rows older than `cutoff` (retention; bounded batch)."""
        with self.Session.begin() as session:
            ids = session.execute(
                select(WebhookDeliveryModel.id)
                .where(WebhookDeliveryModel.received_at < cutoff, WebhookDeliveryModel.state == "done")
                .limit(batch)
            ).scalars().all()
            if not ids:
                return 0
            return session.execute(text("DELETE FROM webhook_deliveries WHERE id = ANY(CAST(:ids AS uuid[]))"), {"ids": ids}).rowcount

    # ------------------------------------------------------------------------------------------ run counts

    @contextmanager
    def serialize_starts(self, trigger_id: str) -> Iterator[None]:
        """Serialize the limit checks and the run creation of one trigger, so a burst of concurrent deliveries cannot all
        read the same count and exceed `max_concurrent_runs` or the daily cap.

        Three layers, so a waiter never sits on a database connection: a per-trigger in-process lock, a process-wide cap
        on the connections held for this (`_HOLDERS`: the run creation under the lock needs another connection from
        the same pool, so holders must never be able to take the whole pool), and a session-level advisory lock for
        other API replicas, taken with `pg_try_advisory_lock` in a short polling loop."""
        key = f"webhook-start:{trigger_id}"
        with _local_lock(trigger_id), _HOLDERS:
            conn = self.engine.connect()
            try:
                while not conn.execute(text("SELECT pg_try_advisory_lock(hashtextextended(:k, 0))"), {"k": key}).scalar():
                    conn.rollback()
                    time.sleep(_LOCK_POLL_SECONDS)
                conn.rollback()
                try:
                    yield
                finally:
                    conn.execute(text("SELECT pg_advisory_unlock(hashtextextended(:k, 0))"), {"k": key})
                    conn.commit()
            finally:
                conn.close()

    def count_active_runs(self, tenant_id: str, trigger_id: str) -> int:
        with self.Session() as session:
            return int(session.execute(
                select(func.count()).select_from(RunModel).where(
                    RunModel.tenant_id == tenant_id, RunModel.trigger_id == str(trigger_id),
                    RunModel.status.not_in(TERMINAL_RUN_STATUSES),
                )
            ).scalar_one())

    def count_runs_since(self, tenant_id: str, trigger_id: str, since: datetime) -> int:
        with self.Session() as session:
            return int(session.execute(
                select(func.count()).select_from(RunModel).where(
                    RunModel.tenant_id == tenant_id, RunModel.trigger_id == str(trigger_id), RunModel.created_at >= since,
                )
            ).scalar_one())

    def get_run_status(self, tenant_id: str, run_id: str) -> str | None:
        with self.Session() as session:
            return session.execute(
                select(RunModel.status).where(RunModel.tenant_id == tenant_id, RunModel.id == str(run_id))
            ).scalar_one_or_none()


__all__ = [
    "CLAIM_LEASE_SECONDS", "PRESET_NAMES", "TRIGGER_STATUSES", "TriggerNameConflict", "WebhookDeliveryModel",
    "WebhookStore", "WebhookTriggerEventModel", "WebhookTriggerModel", "trigger_dict",
]
