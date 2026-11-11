"""A fake external ticket service backed by its own table in eap_app.

It stands in for a real third-party API that honours an `Idempotency-Key`: a repeated key returns
the original ticket instead of creating a second one. Every call (including deduplicated ones) is
counted, so cross-process crash tests can tell "called twice" from "created twice".
"""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import (
    Column,
    DateTime,
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

fake_external_tickets = Table(
    "fake_external_tickets",
    metadata,
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
    Column("idempotency_key", String, nullable=False, index=True),
    Column("called_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)


def create_ticket_tables(engine: Engine) -> None:
    metadata.create_all(engine)


class FakeExternalTicketService:
    """TicketService (eap_runtime protocol) that persists to Postgres and dedupes by idempotency key."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def create(self, title: str, body: str, *, idempotency_key: str) -> dict[str, Any]:
        with self.engine.begin() as conn:
            conn.execute(fake_external_ticket_calls.insert().values(idempotency_key=idempotency_key))
            ticket_id = "TCK-" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:12].upper()
            conn.execute(
                insert(fake_external_tickets)
                .values(idempotency_key=idempotency_key, ticket_id=ticket_id, title=title, body=body)
                .on_conflict_do_nothing(index_elements=["idempotency_key"])
            )
            row = conn.execute(
                select(fake_external_tickets).where(fake_external_tickets.c.idempotency_key == idempotency_key)
            ).one()
        return {"ticket_id": row.ticket_id, "title": row.title, "body": row.body, "idempotency_key": idempotency_key}

    # --- inspection helpers for tests -------------------------------------------------------

    def call_count(self, key_prefix: str) -> int:
        with self.engine.connect() as conn:
            return conn.execute(
                select(func.count())
                .select_from(fake_external_ticket_calls)
                .where(fake_external_ticket_calls.c.idempotency_key.startswith(key_prefix))
            ).scalar_one()

    def ticket_count(self, key_prefix: str) -> int:
        with self.engine.connect() as conn:
            return conn.execute(
                select(func.count())
                .select_from(fake_external_tickets)
                .where(fake_external_tickets.c.idempotency_key.startswith(key_prefix))
            ).scalar_one()
