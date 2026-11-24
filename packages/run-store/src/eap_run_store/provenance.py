"""Provenance of the Architect's drafts and publish cards, and the capability snapshots they point at (0037, 0038).

`definition_provenance` is append-only (a trigger refuses UPDATE) and keyed by the definition's `spec_hash`, beside
the definition, never inside it. `capability_snapshots` is content-addressed: one row per registry hash, ids with
status only. Every read of a provenance row filters by tenant; a snapshot has no tenant (the hash is the key) and is
read only through a row of the caller's own tenant. All SQL for both tables lives here."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, PrimaryKeyConstraint, String, delete, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, mapped_column, sessionmaker

from eap_run_store.store import Base

KINDS = ("agent", "workflow")
DEFAULT_SNAPSHOT_RETENTION_DAYS = 400
MAX_LIMIT = 50


class DefinitionProvenanceModel(Base):
    __tablename__ = "definition_provenance"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="definition_provenance_pkey"),
        CheckConstraint("kind IN ('agent', 'workflow')", name="ck_definition_provenance_kind"),
        Index("ix_definition_provenance_def_hash", "tenant_id", "definition_id", "spec_hash"),
    )

    id: Mapped[str] = mapped_column(String, default=lambda: str(uuid4()))
    tenant_id: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    definition_id: Mapped[str] = mapped_column(String, nullable=False)
    spec_hash: Mapped[str] = mapped_column(String, nullable=False)
    version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    plan_id: Mapped[str | None] = mapped_column(String, nullable=True)
    body: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"), default=lambda: datetime.now(timezone.utc)
    )


class CapabilitySnapshotModel(Base):
    __tablename__ = "capability_snapshots"
    __table_args__ = (
        PrimaryKeyConstraint("hash", name="capability_snapshots_pkey"),
        Index("ix_capability_snapshots_created_at", "created_at"),
    )

    hash: Mapped[str] = mapped_column(String)
    body: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()"), default=lambda: datetime.now(timezone.utc)
    )


def _row(m: DefinitionProvenanceModel) -> dict[str, Any]:
    return {
        "id": m.id, "kind": m.kind, "definition_id": m.definition_id, "spec_hash": m.spec_hash, "version": m.version,
        "run_id": m.run_id, "plan_id": m.plan_id, "body": dict(m.body), "created_at": m.created_at,
    }


class ProvenanceStore:
    def __init__(self, engine: Engine) -> None:
        self.Session = sessionmaker(bind=engine, expire_on_commit=False)

    def add(
        self, tenant_id: str, *, kind: str, definition_id: str, spec_hash: str, body: dict[str, Any],
        run_id: str | None = None, plan_id: str | None = None, version: int | None = None,
    ) -> dict[str, Any]:
        """Append one row (never an update)."""
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        row = DefinitionProvenanceModel(
            tenant_id=tenant_id, kind=kind, definition_id=definition_id, spec_hash=spec_hash, version=version,
            run_id=run_id, plan_id=plan_id, body=body,
        )
        with self.Session.begin() as db:
            db.add(row)
        return _row(row)

    def list(
        self, tenant_id: str, kind: str, definition_id: str, *, spec_hash: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        """The definition's rows, newest first; `spec_hash` keeps one version of the spec."""
        stmt = select(DefinitionProvenanceModel).where(
            DefinitionProvenanceModel.tenant_id == tenant_id,
            DefinitionProvenanceModel.kind == kind,
            DefinitionProvenanceModel.definition_id == definition_id,
        )
        if spec_hash:
            stmt = stmt.where(DefinitionProvenanceModel.spec_hash == spec_hash)
        stmt = stmt.order_by(DefinitionProvenanceModel.created_at.desc(), DefinitionProvenanceModel.id.desc()).limit(
            max(1, min(limit, MAX_LIMIT))
        )
        with self.Session() as db:
            return [_row(m) for m in db.scalars(stmt)]

    # --- snapshots ---------------------------------------------------------------------------------------

    def put_snapshot(self, snapshot_hash: str, body: dict[str, Any]) -> None:
        """Store a snapshot once per hash (a repeat is a no-op)."""
        stmt = pg_insert(CapabilitySnapshotModel).values(hash=snapshot_hash, body=body).on_conflict_do_nothing(
            index_elements=["hash"]
        )
        with self.Session.begin() as db:
            db.execute(stmt)

    def get_snapshot(self, snapshot_hash: str) -> dict[str, Any] | None:
        with self.Session() as db:
            found = db.get(CapabilitySnapshotModel, snapshot_hash)
            return dict(found.body) if found is not None else None

    def prune_snapshots(self, days: int = DEFAULT_SNAPSHOT_RETENTION_DAYS) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        with self.Session.begin() as db:
            return db.execute(delete(CapabilitySnapshotModel).where(CapabilitySnapshotModel.created_at < cutoff)).rowcount

    # --- the definition a row is about --------------------------------------------------------------------

    def find_definition(self, tenant_id: str, kind: str, ref: str) -> dict[str, Any] | None:
        """`{id, slug, draft_spec}` of the tenant's agent or workflow named by a slug or a UUID, else None."""
        table = "agents" if kind == "agent" else "workflows"
        if kind not in KINDS:
            return None
        with self.Session() as db:
            row = db.execute(
                text(
                    f"SELECT id, slug, draft_spec FROM {table} "
                    "WHERE tenant_id = :t AND (slug = :r OR CAST(id AS text) = :r) ORDER BY (slug = :r) DESC LIMIT 1"
                ),
                {"t": tenant_id, "r": ref},
            ).mappings().first()
        return None if row is None else {"id": str(row["id"]), "slug": row["slug"], "draft_spec": row["draft_spec"]}

    def version_hash(self, tenant_id: str, kind: str, definition_uuid: str, version: int) -> str | None:
        table, fk = ("agent_versions", "agent_id") if kind == "agent" else ("workflow_versions", "workflow_id")
        with self.Session() as db:
            return db.execute(
                text(f"SELECT spec_hash FROM {table} WHERE tenant_id = :t AND {fk} = :d AND version = :v"),
                {"t": tenant_id, "d": definition_uuid, "v": version},
            ).scalar_one_or_none()

    def run_model(self, tenant_id: str, run_id: str) -> str | None:
        with self.Session() as db:
            return db.execute(
                text("SELECT model_id FROM runs WHERE tenant_id = :t AND CAST(id AS text) = :r"),
                {"t": tenant_id, "r": run_id},
            ).scalar_one_or_none()

    # --- the run's own events (what provenance is derived from) -----------------------------------------

    def run_events(self, tenant_id: str, run_id: str, types: tuple[str, ...]) -> list[dict[str, Any]]:
        """`[{type, event_key, data}]` of the run's events of `types`, in order (tenant-checked)."""
        with self.Session() as db:
            rows = db.execute(
                text(
                    "SELECT type, event_key, data FROM execution_events "
                    "WHERE tenant_id = :t AND run_id = :r AND type = ANY(:types) ORDER BY seq"
                ),
                {"t": tenant_id, "r": run_id, "types": list(types)},
            ).mappings().all()
        return [{"type": r["type"], "event_key": r["event_key"], "data": r["data"] or {}} for r in rows]


__all__ = ["DefinitionProvenanceModel", "CapabilitySnapshotModel", "ProvenanceStore", "KINDS"]
