"""Reusable scorers (0020_scorers): tenant-scoped named evaluators and their attachment to agents.

`scorers` holds one deterministic evaluator each (eap_schemas.scorers); `agent_scorers` says which
agent runs which scorer on its finished runs, with a `sampling_rate` in (0, 1]. Every read and write
is tenant-scoped: another tenant's scorer is `ScorerNotFound`. The worker reads `attached_to_agent`
and records the results as `score` run events; nothing here scores anything."""

from __future__ import annotations

import zlib
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    delete,
    func,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column, sessionmaker

from eap_run_store.revisions import bump_row
from eap_run_store.store import Base
from eap_schemas.scorers import MAX_SCORERS_PER_AGENT, validate_scorer_evaluator

SCORER_NAME_MAX_CHARS = 100
SCORER_DESCRIPTION_MAX_CHARS = 2000

# Same texts as migration 0020_scorers.
SCORER_NAME_CHECK = "char_length(name) BETWEEN 1 AND 100"
SCORER_EVALUATOR_CHECK = "jsonb_typeof(evaluator) = 'object'"
SAMPLING_RATE_CHECK = "sampling_rate > 0 AND sampling_rate <= 1"


class ScorerNotFound(LookupError):
    """No such scorer for the tenant (another tenant's reads the same)."""


class ScorerNameConflict(ValueError):
    """The tenant already has a scorer of this name."""


class ScorerLimitExceeded(ValueError):
    """The agent already has MAX_SCORERS_PER_AGENT scorers."""


class ScorerModel(Base):
    __tablename__ = "scorers"

    id: Mapped[str] = mapped_column("id", PG_UUID(as_uuid=False), primary_key=True)
    tenant_id: Mapped[str] = mapped_column("tenant_id", String, nullable=False)
    name: Mapped[str] = mapped_column("name", String, nullable=False)
    description: Mapped[str | None] = mapped_column("description", Text, nullable=True)
    evaluator: Mapped[dict] = mapped_column("evaluator", JSONB, nullable=False)
    # 0033: the optimistic-concurrency token (eap_run_store.revisions): +1 on every write.
    revision: Mapped[int] = mapped_column("revision", Integer, nullable=False, default=1, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        "updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_scorers_tenant_id_id"),
        UniqueConstraint("tenant_id", "name", name="uq_scorers_tenant_name"),
        CheckConstraint(SCORER_NAME_CHECK, name="ck_scorers_name"),
        CheckConstraint(SCORER_EVALUATOR_CHECK, name="ck_scorers_evaluator"),
    )


class AgentScorerModel(Base):
    __tablename__ = "agent_scorers"

    tenant_id: Mapped[str] = mapped_column("tenant_id", String, nullable=False)
    agent_id: Mapped[str] = mapped_column("agent_id", String, nullable=False)
    scorer_id: Mapped[str] = mapped_column("scorer_id", PG_UUID(as_uuid=False), nullable=False)
    sampling_rate: Mapped[Decimal] = mapped_column(
        "sampling_rate", Numeric(5, 4), nullable=False, server_default=text("1")
    )
    created_at: Mapped[datetime] = mapped_column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        PrimaryKeyConstraint("agent_id", "scorer_id", name="agent_scorers_pkey"),
        ForeignKeyConstraint(
            ["tenant_id", "scorer_id"],
            ["scorers.tenant_id", "scorers.id"],
            name="fk_agent_scorers_tenant_scorer",
            ondelete="CASCADE",
        ),
        CheckConstraint(SAMPLING_RATE_CHECK, name="ck_agent_scorers_sampling_rate"),
        Index("ix_agent_scorers_tenant_agent", "tenant_id", "agent_id"),
        Index("ix_agent_scorers_tenant_scorer", "tenant_id", "scorer_id"),
    )


@dataclass(frozen=True)
class ScorerRecord:
    id: str
    name: str
    description: str | None
    evaluator: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    agent_ids: tuple[str, ...] = ()
    revision: int = 1


def _record(row: ScorerModel, agent_ids: tuple[str, ...] = ()) -> ScorerRecord:
    return ScorerRecord(
        str(row.id), row.name, row.description, dict(row.evaluator), row.created_at, row.updated_at, agent_ids,
        row.revision,
    )


def sampled(run_id: str, scorer_id: str, rate: float) -> bool:
    """Whether a run is in a scorer's sample: a stable function of (run, scorer), so a replay and a
    retry decide the same. Rate 1 always, otherwise the hash's fraction is below the rate."""
    if rate >= 1:
        return True
    bucket = zlib.crc32(f"{run_id}:{scorer_id}".encode()) / 0x1_0000_0000
    return bucket < rate


class ScorerStore:
    def __init__(self, engine) -> None:
        self.engine = engine
        self.Session = sessionmaker(bind=engine)

    def _get(self, session, tenant_id: str, scorer_id: UUID | str) -> ScorerModel:
        row = session.execute(
            select(ScorerModel).where(ScorerModel.tenant_id == tenant_id, ScorerModel.id == str(scorer_id))
        ).scalar_one_or_none()
        if row is None:
            raise ScorerNotFound(str(scorer_id))
        return row

    def _agents(self, session, tenant_id: str, scorer_ids: list[str]) -> dict[str, list[str]]:
        found: dict[str, list[str]] = {}
        if not scorer_ids:
            return found
        rows = session.execute(
            select(AgentScorerModel.scorer_id, AgentScorerModel.agent_id)
            .where(AgentScorerModel.tenant_id == tenant_id, AgentScorerModel.scorer_id.in_(scorer_ids))
            .order_by(AgentScorerModel.agent_id)
        ).all()
        for scorer_id, agent_id in rows:
            found.setdefault(str(scorer_id), []).append(agent_id)
        return found

    def create(
        self, tenant_id: str, name: str, evaluator: Any, description: str | None = None, scorer_id: UUID | None = None
    ) -> ScorerRecord:
        spec = validate_scorer_evaluator(evaluator)
        row = ScorerModel(
            id=str(scorer_id or uuid4()), tenant_id=tenant_id, name=name, description=description, evaluator=spec
        )
        with self.Session() as session:
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                raise ScorerNameConflict(name) from None
            session.refresh(row)
            return _record(row)

    def get(self, tenant_id: str, scorer_id: UUID | str) -> ScorerRecord:
        with self.Session() as session:
            row = self._get(session, tenant_id, scorer_id)
            agents = self._agents(session, tenant_id, [str(row.id)])
            return _record(row, tuple(agents.get(str(row.id), ())))

    def list(self, tenant_id: str) -> list[ScorerRecord]:
        with self.Session() as session:
            rows = session.execute(
                select(ScorerModel).where(ScorerModel.tenant_id == tenant_id).order_by(ScorerModel.name, ScorerModel.id)
            ).scalars().all()
            agents = self._agents(session, tenant_id, [str(r.id) for r in rows])
            return [_record(r, tuple(agents.get(str(r.id), ()))) for r in rows]

    def update(
        self, tenant_id: str, scorer_id: UUID | str, *, name: str | None = None,
        description: str | None = None, set_description: bool = False, evaluator: Any = None,
        expected_revision: int | None = None,
    ) -> ScorerRecord:
        """RevisionMismatch when `expected_revision` is given and the scorer has moved on."""
        with self.Session() as session:
            row = self._get(session, tenant_id, scorer_id)
            bump_row(session, row, expected_revision)
            if name is not None:
                row.name = name
            if set_description:
                row.description = description
            if evaluator is not None:
                row.evaluator = validate_scorer_evaluator(evaluator)
            row.updated_at = func.now()
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                raise ScorerNameConflict(name or "") from None
            return self.get(tenant_id, scorer_id)

    def delete(self, tenant_id: str, scorer_id: UUID | str) -> None:
        with self.Session() as session:
            result = session.execute(
                delete(ScorerModel).where(ScorerModel.tenant_id == tenant_id, ScorerModel.id == str(scorer_id))
            )
            if result.rowcount == 0:
                raise ScorerNotFound(str(scorer_id))
            session.commit()

    # ------------------------------------------------------------------ attachments

    def attach(
        self, tenant_id: str, agent_id: UUID | str, scorer_id: UUID | str, sampling_rate: Decimal | float = 1
    ) -> None:
        """Attach (or change the sampling rate of) a scorer on an agent. The caller has checked the
        agent belongs to the tenant."""
        with self.Session() as session:
            self._get(session, tenant_id, scorer_id)
            existing = session.get(AgentScorerModel, (str(agent_id), str(scorer_id)))
            if existing is not None:
                existing.sampling_rate = Decimal(str(sampling_rate))
            else:
                count = session.execute(
                    select(func.count()).select_from(AgentScorerModel).where(
                        AgentScorerModel.tenant_id == tenant_id, AgentScorerModel.agent_id == str(agent_id)
                    )
                ).scalar_one()
                if count >= MAX_SCORERS_PER_AGENT:
                    raise ScorerLimitExceeded(str(agent_id))
                session.add(AgentScorerModel(
                    tenant_id=tenant_id, agent_id=str(agent_id), scorer_id=str(scorer_id),
                    sampling_rate=Decimal(str(sampling_rate)),
                ))
            session.commit()

    def detach(self, tenant_id: str, agent_id: UUID | str, scorer_id: UUID | str) -> None:
        with self.Session() as session:
            session.execute(delete(AgentScorerModel).where(
                AgentScorerModel.tenant_id == tenant_id, AgentScorerModel.agent_id == str(agent_id),
                AgentScorerModel.scorer_id == str(scorer_id),
            ))
            session.commit()

    def attached_to_agent(self, tenant_id: str, agent_id: UUID | str) -> list[dict[str, Any]]:
        """`[{id, name, evaluator, sampling_rate}]` of the agent's scorers by name (JSON-safe: the
        worker records it in a step)."""
        with self.Session() as session:
            rows = session.execute(
                select(ScorerModel, AgentScorerModel.sampling_rate)
                .join(AgentScorerModel, (AgentScorerModel.tenant_id == ScorerModel.tenant_id)
                      & (AgentScorerModel.scorer_id == ScorerModel.id))
                .where(AgentScorerModel.tenant_id == tenant_id, AgentScorerModel.agent_id == str(agent_id))
                .order_by(ScorerModel.name, ScorerModel.id)
            ).all()
            return [
                {"id": str(s.id), "name": s.name, "evaluator": dict(s.evaluator), "sampling_rate": float(rate)}
                for s, rate in rows
            ]


__all__ = [
    "AgentScorerModel", "ScorerLimitExceeded", "ScorerModel", "ScorerNameConflict", "ScorerNotFound",
    "ScorerRecord", "ScorerStore", "sampled",
]
