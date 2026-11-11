"""A fake external ticket service backed by its own table in eap_app.

It stands in for a real third-party API that honours an `Idempotency-Key`: a repeated key returns
the original ticket instead of creating a second one. Keys are scoped per tenant. Every call (including deduplicated ones) is
counted, so cross-process crash tests can tell "called twice" from "created twice".
"""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import (
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine

metadata = MetaData()

# The fake service is "external": it has no foreign key into our runs table. It is still tenant-aware
# (as a real multi-tenant SaaS API would be per account): dedupe, storage and inspection are scoped by
# tenant_id, so the same idempotency key under two tenants creates two tickets.
fake_external_tickets = Table(
    "fake_external_tickets",
    metadata,
    Column("tenant_id", String, primary_key=True),
    Column("idempotency_key", String, primary_key=True),
    Column("ticket_id", String, nullable=False, unique=True),
    Column("title", Text, nullable=False),
    Column("body", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

fake_external_ticket_calls = Table(
    "fake_external_ticket_calls",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("tenant_id", String, nullable=False),
    Column("idempotency_key", String, nullable=False, index=True),
    Column("called_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Index("ix_fake_external_ticket_calls_tenant_key", "tenant_id", "idempotency_key"),
)


def create_ticket_tables(engine: Engine) -> None:
    metadata.create_all(engine)


def _ticket_id(tenant_id: str, idempotency_key: str) -> str:
    digest = hashlib.sha256(f"{tenant_id}\x00{idempotency_key}".encode()).hexdigest()[:12].upper()
    return "TCK-" + digest


class FakeExternalTicketService:
    """Postgres-backed fake external ticket API that dedupes by (tenant_id, idempotency key).

    Use `for_tenant(tenant_id)` to get the per-run TicketService (eap_runtime protocol).
    """

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def for_tenant(self, tenant_id: str) -> "TenantTicketService":
        return TenantTicketService(self, tenant_id)

    def create(self, title: str, body: str, *, idempotency_key: str, tenant_id: str) -> dict[str, Any]:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        with self.engine.begin() as conn:
            conn.execute(
                fake_external_ticket_calls.insert().values(tenant_id=tenant_id, idempotency_key=idempotency_key)
            )
            conn.execute(
                insert(fake_external_tickets)
                .values(
                    tenant_id=tenant_id,
                    idempotency_key=idempotency_key,
                    ticket_id=_ticket_id(tenant_id, idempotency_key),
                    title=title,
                    body=body,
                )
                .on_conflict_do_nothing(index_elements=["tenant_id", "idempotency_key"])
            )
            row = conn.execute(
                select(fake_external_tickets).where(
                    fake_external_tickets.c.tenant_id == tenant_id,
                    fake_external_tickets.c.idempotency_key == idempotency_key,
                )
            ).one()
        return {"ticket_id": row.ticket_id, "title": row.title, "body": row.body, "idempotency_key": idempotency_key}

    # --- inspection helpers for tests (tenant-scoped) ---------------------------------------

    def call_count(self, tenant_id: str, key_prefix: str) -> int:
        with self.engine.connect() as conn:
            return conn.execute(
                select(func.count())
                .select_from(fake_external_ticket_calls)
                .where(
                    fake_external_ticket_calls.c.tenant_id == tenant_id,
                    fake_external_ticket_calls.c.idempotency_key.startswith(key_prefix, autoescape=True),
                )
            ).scalar_one()

    def ticket_count(self, tenant_id: str, key_prefix: str) -> int:
        with self.engine.connect() as conn:
            return conn.execute(
                select(func.count())
                .select_from(fake_external_tickets)
                .where(
                    fake_external_tickets.c.tenant_id == tenant_id,
                    fake_external_tickets.c.idempotency_key.startswith(key_prefix, autoescape=True),
                )
            ).scalar_one()


class TenantTicketService:
    """The TicketService a run sees: the fake external service bound to the run's tenant."""

    def __init__(self, service: FakeExternalTicketService, tenant_id: str) -> None:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        self.service = service
        self.tenant_id = tenant_id

    def create(self, title: str, body: str, *, idempotency_key: str) -> dict[str, Any]:
        return self.service.create(title, body, idempotency_key=idempotency_key, tenant_id=self.tenant_id)
