"""Knowledge-base source sync stores (0029_kb_sources): tenant connections (`settings_connections`),
sources bound to a knowledge base, sync history, raw records, the structured entity store and the
entity document ledger between discovery and `eap.kb_ingest`.

Plain, tenant-scoped SQL: every method takes `tenant_id` first and never reads or writes another
tenant's row. `Connection.credential_refs` holds `secret:<name>` reference strings, never values.
Payloads are provider JSON; nothing here logs or formats one."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    Integer,
    ForeignKeyConstraint,
    Index,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from eap_schemas.secrets import is_secret_ref

from eap_run_store.revisions import REVISION_SET, explain_empty_update, guard_sql
from eap_run_store.store import Base

CONNECTION_KINDS = ("oauth2_client_credentials", "api_token")
CHECK_STATUSES = ("ok", "error")
SOURCE_KINDS = ("product_catalog", "repository_docs")
SYNC_MODES = ("targeted", "incremental", "full")
SYNC_STATUSES = ("running", "completed", "incomplete", "failed", "cancelled", "needs_attention")
RECORD_STATUSES = ("active", "removed")
VALUE_TYPES = ("numeric", "text", "boolean", "range")
DOCUMENT_STATUSES = ("discovered", "queued", "failed", "removed")

MAX_CHECKPOINT_BYTES = 8 * 1024
MAX_COUNTERS_BYTES = 8 * 1024
MAX_BOUNDED_ERRORS = 50
MAX_BOUNDED_ERRORS_BYTES = 32 * 1024
MAX_PAYLOAD_BYTES = 256 * 1024
MAX_DOCUMENT_KEY_CHARS = 256

# Same texts as migrations 0029_kb_sources and 0030_kb_source_kinds.
PROVIDER_CHECK = "provider ~ '^[a-z][a-z0-9_]{0,63}$'"
CONNECTION_KIND_CHECK = "kind IN ('oauth2_client_credentials', 'api_token')"
CONNECTION_STATUS_CHECK = "last_status IS NULL OR last_status IN ('ok', 'error')"
SOURCE_KIND_CHECK = "kind IN ('product_catalog', 'repository_docs')"
CHECKPOINT_CHECK = "octet_length(last_checkpoint::text) <= 8192"
SYNC_MODE_CHECK = "mode IN ('targeted', 'incremental', 'full')"
SYNC_STATUS_CHECK = "status IN ('running', 'completed', 'incomplete', 'failed', 'cancelled', 'needs_attention')"
RECORD_STATUS_CHECK = "status IN ('active', 'removed')"
PARAMETER_TYPE_CHECK = "value_type IN ('numeric', 'text', 'boolean', 'range')"
PARAMETER_CONFIDENCE_CHECK = "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)"
DOCUMENT_STATUS_CHECK = "status IN ('discovered', 'queued', 'failed', 'removed')"
DOCUMENT_KEY_CHECK = "char_length(document_key) BETWEEN 1 AND 256"


def _ts(name: str, *, default: bool = True) -> Mapped[datetime]:
    return mapped_column(
        name, DateTime(timezone=True), nullable=False, server_default=text("now()") if default else None
    )


# ---------------------------------------------------------------------------------------- models


class SettingsConnectionModel(Base):
    __tablename__ = "settings_connections"

    tenant_id: Mapped[str] = mapped_column("tenant_id", String, nullable=False)
    id: Mapped[str] = mapped_column("id", String, nullable=False)
    name: Mapped[str] = mapped_column("name", String, nullable=False)
    kind: Mapped[str] = mapped_column("kind", String, nullable=False)
    base_url: Mapped[str | None] = mapped_column("base_url", Text, nullable=True)
    config: Mapped[dict] = mapped_column("config", JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    credential_refs: Mapped[dict] = mapped_column(
        "credential_refs", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    enabled: Mapped[bool] = mapped_column("enabled", Boolean, nullable=False, server_default=text("true"))
    last_checked_at: Mapped[datetime | None] = mapped_column("last_checked_at", DateTime(timezone=True), nullable=True)
    last_status: Mapped[str | None] = mapped_column("last_status", String, nullable=True)
    last_error_code: Mapped[str | None] = mapped_column("last_error_code", String, nullable=True)
    created_at: Mapped[datetime] = _ts("created_at")
    updated_at: Mapped[datetime] = _ts("updated_at")
    # 0033: the optimistic-concurrency token (eap_run_store.revisions): +1 on every write.
    revision: Mapped[int] = mapped_column("revision", Integer, nullable=False, default=1, server_default=text("1"))

    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "id", name="settings_connections_pkey"),
        UniqueConstraint("tenant_id", "name", name="uq_settings_connections_tenant_name"),
        CheckConstraint(CONNECTION_KIND_CHECK, name="ck_settings_connections_kind"),
        CheckConstraint(CONNECTION_STATUS_CHECK, name="ck_settings_connections_status"),
    )


class KbSourceModel(Base):
    __tablename__ = "kb_sources"

    tenant_id: Mapped[str] = mapped_column("tenant_id", String, nullable=False)
    id: Mapped[str] = mapped_column("id", String, nullable=False)
    knowledge_base_id: Mapped[str] = mapped_column("knowledge_base_id", PG_UUID(as_uuid=False), nullable=False)
    connection_id: Mapped[str] = mapped_column("connection_id", String, nullable=False)
    name: Mapped[str] = mapped_column("name", String, nullable=False)
    kind: Mapped[str] = mapped_column("kind", String, nullable=False)
    provider: Mapped[str] = mapped_column("provider", String, nullable=False)
    config: Mapped[dict] = mapped_column("config", JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    enabled: Mapped[bool] = mapped_column("enabled", Boolean, nullable=False, server_default=text("true"))
    last_successful_sync_at: Mapped[datetime | None] = mapped_column(
        "last_successful_sync_at", DateTime(timezone=True), nullable=True
    )
    last_checkpoint: Mapped[dict] = mapped_column(
        "last_checkpoint", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    last_source_revision: Mapped[str | None] = mapped_column("last_source_revision", Text, nullable=True)
    created_at: Mapped[datetime] = _ts("created_at")
    updated_at: Mapped[datetime] = _ts("updated_at")
    # 0033: the optimistic-concurrency token (eap_run_store.revisions): +1 on every write.
    revision: Mapped[int] = mapped_column("revision", Integer, nullable=False, default=1, server_default=text("1"))

    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "id", name="kb_sources_pkey"),
        UniqueConstraint("tenant_id", "knowledge_base_id", "name", name="uq_kb_sources_tenant_kb_name"),
        ForeignKeyConstraint(
            ["tenant_id", "knowledge_base_id"], ["knowledge_bases.tenant_id", "knowledge_bases.id"],
            name="fk_kb_sources_tenant_kb", ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "connection_id"], ["settings_connections.tenant_id", "settings_connections.id"],
            name="fk_kb_sources_tenant_connection", ondelete="RESTRICT",
        ),
        CheckConstraint(SOURCE_KIND_CHECK, name="ck_kb_sources_kind"),
        CheckConstraint(PROVIDER_CHECK, name="ck_kb_sources_provider"),
        CheckConstraint(CHECKPOINT_CHECK, name="ck_kb_sources_checkpoint_size"),
        Index("ix_kb_sources_tenant_kb", "tenant_id", "knowledge_base_id"),
        Index("ix_kb_sources_tenant_connection", "tenant_id", "connection_id"),
    )


class KbSourceSyncModel(Base):
    __tablename__ = "kb_source_syncs"

    tenant_id: Mapped[str] = mapped_column("tenant_id", String, nullable=False)
    id: Mapped[str] = mapped_column("id", String, nullable=False)
    source_id: Mapped[str] = mapped_column("source_id", String, nullable=False)
    # runs.id of the kb_source_sync run (= its DBOS workflow id). No foreign key: the history may
    # outlive the run row.
    run_id: Mapped[str] = mapped_column("run_id", String, nullable=False)
    parent_run_id: Mapped[str | None] = mapped_column("parent_run_id", Text, nullable=True)
    mode: Mapped[str] = mapped_column("mode", String, nullable=False)
    request_hash: Mapped[str] = mapped_column("request_hash", String, nullable=False)
    status: Mapped[str] = mapped_column("status", String, nullable=False, server_default=text("'running'"))
    checkpoint: Mapped[dict] = mapped_column("checkpoint", JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    counters: Mapped[dict] = mapped_column("counters", JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    bounded_errors: Mapped[list] = mapped_column(
        "bounded_errors", JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    started_at: Mapped[datetime] = _ts("started_at")
    completed_at: Mapped[datetime | None] = mapped_column("completed_at", DateTime(timezone=True), nullable=True)

    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "id", name="kb_source_syncs_pkey"),
        UniqueConstraint("tenant_id", "run_id", name="uq_kb_source_syncs_tenant_run"),
        ForeignKeyConstraint(
            ["tenant_id", "source_id"], ["kb_sources.tenant_id", "kb_sources.id"],
            name="fk_kb_source_syncs_tenant_source", ondelete="CASCADE",
        ),
        CheckConstraint(SYNC_MODE_CHECK, name="ck_kb_source_syncs_mode"),
        CheckConstraint(SYNC_STATUS_CHECK, name="ck_kb_source_syncs_status"),
        Index("ix_kb_source_syncs_source_started", "tenant_id", "source_id", "started_at"),
    )


class KbSourceRecordModel(Base):
    __tablename__ = "kb_source_records"

    tenant_id: Mapped[str] = mapped_column("tenant_id", String, nullable=False)
    id: Mapped[str] = mapped_column("id", String, nullable=False)
    source_id: Mapped[str] = mapped_column("source_id", String, nullable=False)
    external_id: Mapped[str] = mapped_column("external_id", String, nullable=False)
    record_type: Mapped[str] = mapped_column("record_type", String, nullable=False)
    source_updated_at: Mapped[datetime | None] = mapped_column(
        "source_updated_at", DateTime(timezone=True), nullable=True
    )
    content_hash: Mapped[str] = mapped_column("content_hash", String, nullable=False)
    raw_payload: Mapped[dict] = mapped_column("raw_payload", JSONB, nullable=False)
    observed_at: Mapped[datetime] = mapped_column("observed_at", DateTime(timezone=True), nullable=False)
    # Beyond the brief's column list: `removed` marks a record the provider no longer lists (set only after
    # a complete full walk by RecordStore.mark_removed_except).
    status: Mapped[str] = mapped_column("status", String, nullable=False, server_default=text("'active'"))

    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "id", name="kb_source_records_pkey"),
        UniqueConstraint(
            "tenant_id", "source_id", "external_id", "record_type", name="uq_kb_source_records_tenant_source_ext_type"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "source_id"], ["kb_sources.tenant_id", "kb_sources.id"],
            name="fk_kb_source_records_tenant_source", ondelete="CASCADE",
        ),
        CheckConstraint(RECORD_STATUS_CHECK, name="ck_kb_source_records_status"),
    )


class KbEntityModel(Base):
    __tablename__ = "kb_entities"

    tenant_id: Mapped[str] = mapped_column("tenant_id", String, nullable=False)
    id: Mapped[str] = mapped_column("id", String, nullable=False)
    source_id: Mapped[str] = mapped_column("source_id", String, nullable=False)
    entity_type: Mapped[str] = mapped_column("entity_type", String, nullable=False)
    canonical_name: Mapped[str] = mapped_column("canonical_name", String, nullable=False)
    lifecycle_status: Mapped[str | None] = mapped_column("lifecycle_status", String, nullable=True)
    attributes: Mapped[dict] = mapped_column("attributes", JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = _ts("created_at")
    updated_at: Mapped[datetime] = _ts("updated_at")

    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "id", name="kb_entities_pkey"),
        UniqueConstraint(
            "tenant_id", "source_id", "entity_type", "canonical_name", name="uq_kb_entities_tenant_source_type_name"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "source_id"], ["kb_sources.tenant_id", "kb_sources.id"],
            name="fk_kb_entities_tenant_source", ondelete="CASCADE",
        ),
    )


class KbEntityIdentifierModel(Base):
    __tablename__ = "kb_entity_identifiers"

    tenant_id: Mapped[str] = mapped_column("tenant_id", String, nullable=False)
    entity_id: Mapped[str] = mapped_column("entity_id", String, nullable=False)
    scheme: Mapped[str] = mapped_column("scheme", String, nullable=False)
    value: Mapped[str] = mapped_column("value", String, nullable=False)
    is_primary: Mapped[bool] = mapped_column("is_primary", Boolean, nullable=False, server_default=text("false"))

    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "entity_id", "scheme", "value", name="kb_entity_identifiers_pkey"),
        ForeignKeyConstraint(
            ["tenant_id", "entity_id"], ["kb_entities.tenant_id", "kb_entities.id"],
            name="fk_kb_entity_identifiers_tenant_entity", ondelete="CASCADE",
        ),
        Index("ix_kb_entity_identifiers_scheme_value", "tenant_id", "scheme", "value"),
    )


class KbEntityParameterModel(Base):
    __tablename__ = "kb_entity_parameters"

    tenant_id: Mapped[str] = mapped_column("tenant_id", String, nullable=False)
    id: Mapped[str] = mapped_column("id", String, nullable=False)
    entity_id: Mapped[str] = mapped_column("entity_id", String, nullable=False)
    canonical_name: Mapped[str] = mapped_column("canonical_name", String, nullable=False)
    source_name: Mapped[str] = mapped_column("source_name", String, nullable=False)
    value_type: Mapped[str] = mapped_column("value_type", String, nullable=False)
    numeric_value: Mapped[float | None] = mapped_column("numeric_value", Float(53), nullable=True)
    text_value: Mapped[str | None] = mapped_column("text_value", Text, nullable=True)
    unit: Mapped[str | None] = mapped_column("unit", String, nullable=True)
    normalized_value: Mapped[float | None] = mapped_column("normalized_value", Float(53), nullable=True)
    normalized_unit: Mapped[str | None] = mapped_column("normalized_unit", String, nullable=True)
    min_value: Mapped[float | None] = mapped_column("min_value", Float(53), nullable=True)
    typ_value: Mapped[float | None] = mapped_column("typ_value", Float(53), nullable=True)
    max_value: Mapped[float | None] = mapped_column("max_value", Float(53), nullable=True)
    test_conditions: Mapped[str | None] = mapped_column("test_conditions", Text, nullable=True)
    source_path: Mapped[str | None] = mapped_column("source_path", Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column("confidence", Float(53), nullable=True)

    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "id", name="kb_entity_parameters_pkey"),
        UniqueConstraint(
            "tenant_id", "entity_id", "canonical_name", "source_name", name="uq_kb_entity_parameters_entity_name"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "entity_id"], ["kb_entities.tenant_id", "kb_entities.id"],
            name="fk_kb_entity_parameters_tenant_entity", ondelete="CASCADE",
        ),
        CheckConstraint(PARAMETER_TYPE_CHECK, name="ck_kb_entity_parameters_type"),
        CheckConstraint(PARAMETER_CONFIDENCE_CHECK, name="ck_kb_entity_parameters_confidence"),
        Index("ix_kb_entity_parameters_canonical", "tenant_id", "canonical_name", "normalized_value"),
    )


class KbEntityDocumentModel(Base):
    __tablename__ = "kb_entity_documents"

    tenant_id: Mapped[str] = mapped_column("tenant_id", String, nullable=False)
    entity_id: Mapped[str] = mapped_column("entity_id", String, nullable=False)
    document_key: Mapped[str] = mapped_column("document_key", String, nullable=False)
    kb_document_id: Mapped[str | None] = mapped_column("kb_document_id", PG_UUID(as_uuid=False), nullable=True)
    relationship: Mapped[str] = mapped_column("relationship", String, nullable=False)
    source_url: Mapped[str] = mapped_column("source_url", Text, nullable=False)
    source_revision: Mapped[str | None] = mapped_column("source_revision", Text, nullable=True)
    content_hash: Mapped[str | None] = mapped_column("content_hash", String, nullable=True)
    etag: Mapped[str | None] = mapped_column("etag", Text, nullable=True)
    last_modified: Mapped[str | None] = mapped_column("last_modified", Text, nullable=True)
    status: Mapped[str] = mapped_column("status", String, nullable=False, server_default=text("'discovered'"))
    error_code: Mapped[str | None] = mapped_column("error_code", String, nullable=True)
    discovered_at: Mapped[datetime] = _ts("discovered_at")
    queued_at: Mapped[datetime | None] = mapped_column("queued_at", DateTime(timezone=True), nullable=True)

    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "entity_id", "document_key", name="kb_entity_documents_pkey"),
        ForeignKeyConstraint(
            ["tenant_id", "entity_id"], ["kb_entities.tenant_id", "kb_entities.id"],
            name="fk_kb_entity_documents_tenant_entity", ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "kb_document_id"], ["kb_documents.tenant_id", "kb_documents.id"],
            name="fk_kb_entity_documents_tenant_document", ondelete="CASCADE",
        ),
        CheckConstraint(DOCUMENT_STATUS_CHECK, name="ck_kb_entity_documents_status"),
        CheckConstraint(DOCUMENT_KEY_CHECK, name="ck_kb_entity_documents_key"),
        Index("ix_kb_entity_documents_pending", "tenant_id", "status", "discovered_at"),
    )


# ------------------------------------------------------------------------------------ value types


@dataclass(frozen=True)
class Connection:
    id: str
    tenant_id: str
    name: str
    kind: str
    base_url: str | None
    config: dict[str, Any]
    credential_refs: dict[str, str]
    enabled: bool
    last_checked_at: datetime | None
    last_status: str | None
    last_error_code: str | None
    created_at: datetime
    updated_at: datetime
    revision: int = 1


@dataclass(frozen=True)
class Source:
    id: str
    tenant_id: str
    knowledge_base_id: str
    connection_id: str
    name: str
    kind: str
    provider: str
    config: dict[str, Any]
    enabled: bool
    last_successful_sync_at: datetime | None
    last_checkpoint: dict[str, Any]
    last_source_revision: str | None
    created_at: datetime
    updated_at: datetime
    revision: int = 1


@dataclass(frozen=True)
class Sync:
    id: str
    tenant_id: str
    source_id: str
    run_id: str
    parent_run_id: str | None
    mode: str
    request_hash: str
    status: str
    checkpoint: dict[str, Any]
    counters: dict[str, Any]
    bounded_errors: list[Any]
    started_at: datetime
    completed_at: datetime | None


@dataclass(frozen=True)
class SourceRecord:
    id: str
    tenant_id: str
    source_id: str
    external_id: str
    record_type: str
    source_updated_at: datetime | None
    content_hash: str
    raw_payload: dict[str, Any]
    observed_at: datetime
    status: str


@dataclass(frozen=True)
class Entity:
    id: str
    tenant_id: str
    source_id: str
    entity_type: str
    canonical_name: str
    lifecycle_status: str | None
    attributes: dict[str, Any]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class EntityDocument:
    tenant_id: str
    entity_id: str
    document_key: str
    kb_document_id: str | None
    relationship: str
    source_url: str
    source_revision: str | None
    content_hash: str | None
    etag: str | None
    last_modified: str | None
    status: str
    error_code: str | None
    discovered_at: datetime
    queued_at: datetime | None


class ConnectionNameConflict(Exception):
    """The tenant already has a connection with this name."""


class ConnectionInUse(Exception):
    """A source still references the connection."""


class SourceNameConflict(Exception):
    """The knowledge base already has a source with this name."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _size(value: Any) -> int:
    return len(canonical_json(value).encode("utf-8"))


def _constraint(exc: IntegrityError) -> str:
    return str(getattr(getattr(exc.orig, "diag", None), "constraint_name", "") or "")


def _build(cls: type, row: Any):
    data = dict(row._mapping)
    out = {}
    for f in fields(cls):
        value = data[f.name]
        out[f.name] = str(value) if isinstance(value, uuid.UUID) else value
    return cls(**out)


def _cols(cls: type, prefix: str = "") -> str:
    return ", ".join(f"{prefix}{f.name}" for f in fields(cls))


API_TOKEN_REF_KEYS = frozenset({"token"})


def _check_refs(credential_refs: dict[str, str], kind: str | None = None) -> None:
    """References are `secret:<name>`; an `api_token` connection has at most one, named `token` (none:
    the provider is used without credentials)."""
    for key, ref in credential_refs.items():
        if not isinstance(key, str) or not key or is_secret_ref(ref) is None:
            raise ValueError("credential_refs maps logical names to 'secret:<name>' references")
    if kind == "api_token" and not set(credential_refs) <= API_TOKEN_REF_KEYS:
        raise ValueError("an api_token connection takes only the 'token' reference")


def _check_kind(kind: str) -> None:
    if kind not in CONNECTION_KINDS:
        raise ValueError(f"unknown connection kind {kind!r}")


# --------------------------------------------------------------------------------- connections


class ConnectionStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def create(
        self,
        tenant_id: str,
        *,
        name: str,
        kind: str,
        base_url: str | None,
        config: dict[str, Any],
        credential_refs: dict[str, str],
        enabled: bool = True,
    ) -> Connection:
        """ValueError for an unknown kind or a reference that is not `secret:<name>`;
        ConnectionNameConflict for a duplicate name."""
        _check_kind(kind)
        _check_refs(credential_refs, kind)
        try:
            with self._engine.begin() as conn:
                row = conn.execute(
                    text(
                        "INSERT INTO settings_connections (tenant_id, id, name, kind, base_url, config,"
                        " credential_refs, enabled) VALUES (:t, :id, :name, :kind, :url, CAST(:config AS jsonb),"
                        " CAST(:refs AS jsonb), :enabled) RETURNING " + _cols(Connection)
                    ),
                    {
                        "t": tenant_id, "id": str(uuid.uuid4()), "name": name, "kind": kind, "url": base_url,
                        "config": json.dumps(config), "refs": json.dumps(credential_refs), "enabled": enabled,
                    },
                ).one()
        except IntegrityError as exc:
            if _constraint(exc) == "uq_settings_connections_tenant_name":
                raise ConnectionNameConflict(name) from exc
            raise
        return _build(Connection, row)

    def get(self, tenant_id: str, connection_id: str) -> Connection | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                text(f"SELECT {_cols(Connection)} FROM settings_connections WHERE tenant_id = :t AND id = :id"),
                {"t": tenant_id, "id": connection_id},
            ).first()
        return _build(Connection, row) if row else None

    def list(self, tenant_id: str) -> list[Connection]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(f"SELECT {_cols(Connection)} FROM settings_connections WHERE tenant_id = :t ORDER BY name, id"),
                {"t": tenant_id},
            ).all()
        return [_build(Connection, r) for r in rows]

    def update(
        self,
        tenant_id: str,
        connection_id: str,
        *,
        name: str | None = None,
        base_url: str | None = None,
        config: dict[str, Any] | None = None,
        credential_refs: dict[str, str] | None = None,
        enabled: bool | None = None,
        expected_revision: int | None = None,
    ) -> Connection | None:
        """Change the given fields (None = keep); the row, or None when it is not this tenant's.
        RevisionMismatch when `expected_revision` is given and the row has moved on."""
        if credential_refs is not None:
            _check_refs(credential_refs)
        sets, params = ["updated_at = now()", REVISION_SET], {"t": tenant_id, "id": connection_id}
        for column, value in (("name", name), ("base_url", base_url), ("enabled", enabled)):
            if value is not None:
                sets.append(f"{column} = :{column}")
                params[column] = value
        if config is not None:
            sets.append("config = CAST(:config AS jsonb)")
            params["config"] = json.dumps(config)
        if credential_refs is not None:
            sets.append("credential_refs = CAST(:refs AS jsonb)")
            params["refs"] = json.dumps(credential_refs)
        try:
            with self._engine.begin() as conn:
                row = conn.execute(
                    text(
                        f"UPDATE settings_connections SET {', '.join(sets)} WHERE tenant_id = :t AND id = :id"
                        f"{guard_sql(expected_revision, params)} RETURNING {_cols(Connection)}"
                    ),
                    params,
                ).first()
                if row is None:
                    explain_empty_update(conn, "settings_connections", tenant_id, connection_id, expected_revision)
        except IntegrityError as exc:
            if _constraint(exc) == "uq_settings_connections_tenant_name":
                raise ConnectionNameConflict(name) from exc
            raise
        return _build(Connection, row) if row else None

    def record_check(self, tenant_id: str, connection_id: str, status: str, error_code: str | None) -> bool:
        """Persist a connection check (`ok` | `error`); False when the connection is not this tenant's."""
        if status not in CHECK_STATUSES:
            raise ValueError("status must be 'ok' or 'error'")
        with self._engine.begin() as conn:
            result = conn.execute(
                text(
                    "UPDATE settings_connections SET last_status = :s, last_error_code = :e, last_checked_at = now(),"
                    " updated_at = now() WHERE tenant_id = :t AND id = :id"
                ),
                {"t": tenant_id, "id": connection_id, "s": status, "e": error_code if status == "error" else None},
            )
        return result.rowcount == 1

    def delete(self, tenant_id: str, connection_id: str) -> bool:
        """ConnectionInUse while a source references it; False when it is not this tenant's."""
        try:
            with self._engine.begin() as conn:
                result = conn.execute(
                    text("DELETE FROM settings_connections WHERE tenant_id = :t AND id = :id"),
                    {"t": tenant_id, "id": connection_id},
                )
        except IntegrityError as exc:
            if _constraint(exc) == "fk_kb_sources_tenant_connection":
                raise ConnectionInUse(connection_id) from exc
            raise
        return result.rowcount == 1


# --------------------------------------------------------------------------------------- sources


class SourceStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def create(
        self,
        tenant_id: str,
        *,
        knowledge_base_id: str,
        connection_id: str,
        name: str,
        kind: str,
        provider: str,
        config: dict[str, Any] | None = None,
        enabled: bool = True,
    ) -> Source:
        """ValueError when the knowledge base or connection is not this tenant's (composite FKs), or the
        kind or provider is invalid; SourceNameConflict for a duplicate name in the knowledge base."""
        if kind not in SOURCE_KINDS:
            raise ValueError(f"unknown source kind {kind!r}")
        try:
            with self._engine.begin() as conn:
                row = conn.execute(
                    text(
                        "INSERT INTO kb_sources (tenant_id, id, knowledge_base_id, connection_id, name, kind,"
                        " provider, config, enabled) VALUES (:t, :id, CAST(:kb AS uuid), :c, :name, :kind,"
                        " :provider, CAST(:config AS jsonb), :enabled) RETURNING " + _cols(Source)
                    ),
                    {
                        "t": tenant_id, "id": str(uuid.uuid4()), "kb": str(knowledge_base_id), "c": connection_id,
                        "name": name, "kind": kind, "provider": provider, "config": json.dumps(config or {}),
                        "enabled": enabled,
                    },
                ).one()
        except IntegrityError as exc:
            constraint = _constraint(exc)
            if constraint == "uq_kb_sources_tenant_kb_name":
                raise SourceNameConflict(name) from exc
            if constraint.startswith("fk_kb_sources_") or constraint == "ck_kb_sources_provider":
                raise ValueError("knowledge base or connection not found, or invalid provider") from exc
            raise
        return _build(Source, row)

    def get(self, tenant_id: str, source_id: str) -> Source | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                text(f"SELECT {_cols(Source)} FROM kb_sources WHERE tenant_id = :t AND id = :id"),
                {"t": tenant_id, "id": source_id},
            ).first()
        return _build(Source, row) if row else None

    def list_for_kb(self, tenant_id: str, knowledge_base_id: str) -> list[Source]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"SELECT {_cols(Source)} FROM kb_sources WHERE tenant_id = :t"
                    " AND knowledge_base_id = CAST(:kb AS uuid) ORDER BY name, id"
                ),
                {"t": tenant_id, "kb": str(knowledge_base_id)},
            ).all()
        return [_build(Source, r) for r in rows]

    def update(
        self,
        tenant_id: str,
        source_id: str,
        *,
        name: str | None = None,
        config: dict[str, Any] | None = None,
        enabled: bool | None = None,
        expected_revision: int | None = None,
    ) -> Source | None:
        """RevisionMismatch when `expected_revision` is given and the source has moved on."""
        sets, params = ["updated_at = now()", REVISION_SET], {"t": tenant_id, "id": source_id}
        if name is not None:
            sets.append("name = :name")
            params["name"] = name
        if config is not None:
            sets.append("config = CAST(:config AS jsonb)")
            params["config"] = json.dumps(config)
        if enabled is not None:
            sets.append("enabled = :enabled")
            params["enabled"] = enabled
        try:
            with self._engine.begin() as conn:
                row = conn.execute(
                    text(
                        f"UPDATE kb_sources SET {', '.join(sets)} WHERE tenant_id = :t AND id = :id"
                        f"{guard_sql(expected_revision, params)} RETURNING {_cols(Source)}"
                    ),
                    params,
                ).first()
                if row is None:
                    explain_empty_update(conn, "kb_sources", tenant_id, source_id, expected_revision)
        except IntegrityError as exc:
            if _constraint(exc) == "uq_kb_sources_tenant_kb_name":
                raise SourceNameConflict(name) from exc
            raise
        return _build(Source, row) if row else None

    def set_checkpoint(
        self,
        tenant_id: str,
        source_id: str,
        checkpoint: dict[str, Any],
        *,
        last_source_revision: str | None = None,
        completed: bool,
    ) -> bool:
        """Store the adapter's checkpoint (ValueError above 8 KiB of JSON). `last_source_revision` None keeps
        the stored one; `completed` also stamps `last_successful_sync_at`. False when the source is not this
        tenant's."""
        if _size(checkpoint) > MAX_CHECKPOINT_BYTES:
            raise ValueError(f"checkpoint exceeds {MAX_CHECKPOINT_BYTES} bytes")
        with self._engine.begin() as conn:
            result = conn.execute(
                text(
                    "UPDATE kb_sources SET last_checkpoint = CAST(:cp AS jsonb),"
                    " last_source_revision = COALESCE(:rev, last_source_revision),"
                    " last_successful_sync_at = CASE WHEN :done THEN now() ELSE last_successful_sync_at END,"
                    " updated_at = now() WHERE tenant_id = :t AND id = :id"
                ),
                {"t": tenant_id, "id": source_id, "cp": canonical_json(checkpoint), "rev": last_source_revision,
                 "done": completed},
            )
        return result.rowcount == 1

    def delete(self, tenant_id: str, source_id: str) -> bool:
        """Deletes the source with its syncs, records, entities and document ledger (cascade)."""
        with self._engine.begin() as conn:
            result = conn.execute(
                text("DELETE FROM kb_sources WHERE tenant_id = :t AND id = :id"), {"t": tenant_id, "id": source_id}
            )
        return result.rowcount == 1


# ----------------------------------------------------------------------------------------- syncs


class SyncStore:
    """One row per sync run: progress while it runs, the bounded outcome when it ends."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def create(
        self,
        tenant_id: str,
        id: str,
        source_id: str,
        run_id: str,
        mode: str,
        request_hash: str,
        *,
        parent_run_id: str | None = None,
    ) -> Sync:
        """Idempotent by `(tenant_id, run_id)`: a replay returns the existing row. ValueError for an unknown
        mode or a source that is not this tenant's."""
        try:
            with self._engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO kb_source_syncs (tenant_id, id, source_id, run_id, parent_run_id, mode,"
                        " request_hash) VALUES (:t, :id, :s, :run, :parent, :mode, :hash) ON CONFLICT DO NOTHING"
                    ),
                    {"t": tenant_id, "id": id, "s": source_id, "run": str(run_id),
                     "parent": str(parent_run_id) if parent_run_id else None, "mode": mode, "hash": request_hash},
                )
        except IntegrityError as exc:
            raise ValueError("invalid sync (unknown mode or source)") from exc
        got = self.get_by_run(tenant_id, str(run_id))
        if got is None:  # the id was taken by another run
            raise ValueError("sync id already used")
        return got

    def get_by_run(self, tenant_id: str, run_id: str) -> Sync | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                text(f"SELECT {_cols(Sync)} FROM kb_source_syncs WHERE tenant_id = :t AND run_id = :run"),
                {"t": tenant_id, "run": str(run_id)},
            ).first()
        return _build(Sync, row) if row else None

    def list_for_source(self, tenant_id: str, source_id: str, limit: int) -> list[Sync]:
        """Newest first."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"SELECT {_cols(Sync)} FROM kb_source_syncs WHERE tenant_id = :t AND source_id = :s"
                    " ORDER BY started_at DESC, id DESC LIMIT :n"
                ),
                {"t": tenant_id, "s": source_id, "n": max(0, int(limit))},
            ).all()
        return [_build(Sync, r) for r in rows]

    def update_progress(
        self,
        tenant_id: str,
        run_id: str,
        *,
        checkpoint: dict[str, Any],
        counters: dict[str, Any],
        bounded_errors: list[Any],
    ) -> bool:
        _check_sync_payload(checkpoint, counters, bounded_errors)
        with self._engine.begin() as conn:
            result = conn.execute(
                text(
                    "UPDATE kb_source_syncs SET checkpoint = CAST(:cp AS jsonb), counters = CAST(:c AS jsonb),"
                    " bounded_errors = CAST(:e AS jsonb) WHERE tenant_id = :t AND run_id = :run"
                ),
                {"t": tenant_id, "run": str(run_id), "cp": canonical_json(checkpoint), "c": canonical_json(counters),
                 "e": json.dumps(bounded_errors)},
            )
        return result.rowcount == 1

    def finish(
        self,
        tenant_id: str,
        run_id: str,
        status: str,
        *,
        counters: dict[str, Any],
        bounded_errors: list[Any],
        completed_at: datetime,
    ) -> bool:
        if status not in SYNC_STATUSES or status == "running":
            raise ValueError(f"unknown final sync status {status!r}")
        _check_sync_payload({}, counters, bounded_errors)
        with self._engine.begin() as conn:
            result = conn.execute(
                text(
                    "UPDATE kb_source_syncs SET status = :s, counters = CAST(:c AS jsonb),"
                    " bounded_errors = CAST(:e AS jsonb), completed_at = :at WHERE tenant_id = :t AND run_id = :run"
                ),
                {"t": tenant_id, "run": str(run_id), "s": status, "c": canonical_json(counters),
                 "e": json.dumps(bounded_errors), "at": completed_at},
            )
        return result.rowcount == 1


def _check_sync_payload(checkpoint: dict, counters: dict, bounded_errors: list) -> None:
    if _size(checkpoint) > MAX_CHECKPOINT_BYTES or _size(counters) > MAX_COUNTERS_BYTES:
        raise ValueError("checkpoint or counters too large")
    if len(bounded_errors) > MAX_BOUNDED_ERRORS or _size(bounded_errors) > MAX_BOUNDED_ERRORS_BYTES:
        raise ValueError(f"at most {MAX_BOUNDED_ERRORS} errors, {MAX_BOUNDED_ERRORS_BYTES} bytes")


# -------------------------------------------------------------------------------------- records


class RecordStore:
    """Raw provider payloads, one row per (source, external_id, record_type)."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def upsert(
        self,
        tenant_id: str,
        source_id: str,
        external_id: str,
        record_type: str,
        payload: dict[str, Any],
        *,
        source_updated_at: datetime | None = None,
        observed_at: datetime,
    ) -> Literal["inserted", "updated", "unchanged"]:
        """By hash of the canonical JSON: the same hash on an active row is `unchanged` (only `observed_at`
        moves), a new hash or a removed row coming back is `updated`. ValueError for a payload above 256 KiB
        or a source that is not this tenant's."""
        body = canonical_json(payload)
        if len(body.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise ValueError(f"payload exceeds {MAX_PAYLOAD_BYTES} bytes")
        params = {
            "t": tenant_id, "id": str(uuid.uuid4()), "s": source_id, "ext": external_id, "type": record_type,
            "src_at": source_updated_at, "hash": hashlib.sha256(body.encode("utf-8")).hexdigest(), "payload": body,
            "at": observed_at,
        }
        key = "tenant_id = :t AND source_id = :s AND external_id = :ext AND record_type = :type"
        try:
            with self._engine.begin() as conn:
                inserted = conn.execute(
                    text(
                        "INSERT INTO kb_source_records (tenant_id, id, source_id, external_id, record_type,"
                        " source_updated_at, content_hash, raw_payload, observed_at) VALUES (:t, :id, :s, :ext,"
                        " :type, :src_at, :hash, CAST(:payload AS jsonb), :at) ON CONFLICT DO NOTHING RETURNING 1"
                    ),
                    params,
                ).first()
                if inserted is not None:
                    return "inserted"
                changed = conn.execute(
                    text(
                        "UPDATE kb_source_records SET raw_payload = CAST(:payload AS jsonb), content_hash = :hash,"
                        " source_updated_at = :src_at, observed_at = :at, status = 'active'"
                        f" WHERE {key} AND (content_hash <> :hash OR status <> 'active') RETURNING 1"
                    ),
                    params,
                ).first()
                if changed is not None:
                    return "updated"
                conn.execute(
                    text(f"UPDATE kb_source_records SET observed_at = :at WHERE {key} AND observed_at < :at"), params
                )
                return "unchanged"
        except IntegrityError as exc:
            if _constraint(exc) == "fk_kb_source_records_tenant_source":
                raise ValueError("source not found") from exc
            raise

    def is_unchanged(
        self, tenant_id: str, source_id: str, external_id: str, record_type: str, payload: dict[str, Any]
    ) -> bool:
        """Read-only: an ACTIVE record with exactly this payload (by canonical-JSON hash) is stored. An
        oversized payload cannot be stored, so it is never unchanged."""
        body = canonical_json(payload)
        if len(body.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            return False
        with self._engine.connect() as conn:
            return conn.execute(
                text(
                    "SELECT 1 FROM kb_source_records WHERE tenant_id = :t AND source_id = :s AND external_id = :ext"
                    " AND record_type = :type AND status = 'active' AND content_hash = :hash"
                ),
                {"t": tenant_id, "s": source_id, "ext": external_id, "type": record_type,
                 "hash": hashlib.sha256(body.encode("utf-8")).hexdigest()},
            ).first() is not None

    def get(self, tenant_id: str, source_id: str, external_id: str, record_type: str) -> SourceRecord | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    f"SELECT {_cols(SourceRecord)} FROM kb_source_records WHERE tenant_id = :t AND source_id = :s"
                    " AND external_id = :ext AND record_type = :type"
                ),
                {"t": tenant_id, "s": source_id, "ext": external_id, "type": record_type},
            ).first()
        return _build(SourceRecord, row) if row else None

    def mark_removed_except(self, tenant_id: str, source_id: str, seen_external_ids: Iterable[str]) -> int:
        """Mark every active record of the source (any type) whose `external_id` is not in the set as
        `removed`; returns how many changed. Only for a COMPLETE full walk: a partial one would remove
        records that were merely not reached. ValueError on an empty set."""
        seen = sorted(set(seen_external_ids))
        if not seen:
            raise ValueError("refusing to remove every record of a source")
        with self._engine.begin() as conn:
            result = conn.execute(
                text(
                    "UPDATE kb_source_records SET status = 'removed' WHERE tenant_id = :t AND source_id = :s"
                    " AND status = 'active' AND external_id <> ALL(CAST(:seen AS text[]))"
                ),
                {"t": tenant_id, "s": source_id, "seen": seen},
            )
        return result.rowcount

    def mark_removed_unobserved_since(self, tenant_id: str, source_id: str, since: datetime) -> int:
        """Mark `removed` every active record of the source whose `external_id` has NO record (of any type)
        observed at or after `since`: the key set of a walk that started at `since`, without listing it (so it
        stays small however large the catalogue is). Only for a COMPLETE full walk. ValueError when nothing
        was observed since (an empty or failed walk must not remove every record)."""
        with self._engine.begin() as conn:
            seen = conn.execute(
                text(
                    "SELECT 1 FROM kb_source_records WHERE tenant_id = :t AND source_id = :s AND observed_at >= :since"
                    " LIMIT 1"
                ),
                {"t": tenant_id, "s": source_id, "since": since},
            ).first()
            if seen is None:
                raise ValueError("refusing to remove every record of a source: the walk observed nothing")
            result = conn.execute(
                text(
                    "UPDATE kb_source_records r SET status = 'removed' WHERE r.tenant_id = :t AND r.source_id = :s"
                    " AND r.status = 'active' AND NOT EXISTS (SELECT 1 FROM kb_source_records o WHERE"
                    " o.tenant_id = r.tenant_id AND o.source_id = r.source_id AND o.external_id = r.external_id"
                    " AND o.observed_at >= :since)"
                ),
                {"t": tenant_id, "s": source_id, "since": since},
            )
        return result.rowcount


# --------------------------------------------------------------------------------------- entities

_PARAM_FIELDS = (
    "canonical_name", "source_name", "value_type", "numeric_value", "text_value", "unit", "normalized_value",
    "normalized_unit", "min_value", "typ_value", "max_value", "test_conditions", "source_path", "confidence",
)


class EntityStore:
    """The structured store: entities, their identifiers and parameters, and the document ledger."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def upsert_entity(
        self,
        tenant_id: str,
        source_id: str,
        *,
        entity_type: str,
        canonical_name: str,
        lifecycle_status: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> Entity:
        """By `(source, entity_type, canonical_name)`; lifecycle and attributes are overwritten. ValueError for a
        source that is not this tenant's."""
        try:
            with self._engine.begin() as conn:
                row = conn.execute(
                    text(
                        "INSERT INTO kb_entities (tenant_id, id, source_id, entity_type, canonical_name,"
                        " lifecycle_status, attributes) VALUES (:t, :id, :s, :type, :name, :life,"
                        " CAST(:attrs AS jsonb)) ON CONFLICT (tenant_id, source_id, entity_type, canonical_name)"
                        " DO UPDATE SET lifecycle_status = EXCLUDED.lifecycle_status, attributes = EXCLUDED.attributes,"
                        " updated_at = now() RETURNING " + _cols(Entity)
                    ),
                    {"t": tenant_id, "id": str(uuid.uuid4()), "s": source_id, "type": entity_type,
                     "name": canonical_name, "life": lifecycle_status, "attrs": canonical_json(attributes or {})},
                ).one()
        except IntegrityError as exc:
            raise ValueError("source not found") from exc
        return _build(Entity, row)

    def get_entity(self, tenant_id: str, entity_id: str) -> Entity | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                text(f"SELECT {_cols(Entity)} FROM kb_entities WHERE tenant_id = :t AND id = :id"),
                {"t": tenant_id, "id": entity_id},
            ).first()
        return _build(Entity, row) if row else None

    def find_by_identifier(self, tenant_id: str, source_id: str, scheme: str, value: str) -> Entity | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    f"SELECT {_cols(Entity, 'e.')} FROM kb_entities e JOIN kb_entity_identifiers i"
                    " ON i.tenant_id = e.tenant_id AND i.entity_id = e.id"
                    " WHERE e.tenant_id = :t AND e.source_id = :s AND i.scheme = :scheme AND i.value = :v LIMIT 1"
                ),
                {"t": tenant_id, "s": source_id, "scheme": scheme, "v": value},
            ).first()
        return _build(Entity, row) if row else None

    def set_identifiers(self, tenant_id: str, entity_id: str, identifiers: Sequence[tuple[str, str, bool]]) -> None:
        """Replace the entity's identifiers with `(scheme, value, is_primary)` rows, atomically. ValueError for
        a duplicate row or an entity that is not this tenant's (nothing changes)."""
        try:
            with self._engine.begin() as conn:
                conn.execute(
                    text("DELETE FROM kb_entity_identifiers WHERE tenant_id = :t AND entity_id = :e"),
                    {"t": tenant_id, "e": entity_id},
                )
                if identifiers:
                    conn.execute(
                        text(
                            "INSERT INTO kb_entity_identifiers (tenant_id, entity_id, scheme, value, is_primary)"
                            " VALUES (:t, :e, :scheme, :value, :primary)"
                        ),
                        [{"t": tenant_id, "e": entity_id, "scheme": s, "value": v, "primary": p}
                         for s, v, p in identifiers],
                    )
        except IntegrityError as exc:
            raise ValueError("invalid identifiers or entity not found") from exc

    def list_identifiers(self, tenant_id: str, entity_id: str) -> list[tuple[str, str, bool]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT scheme, value, is_primary FROM kb_entity_identifiers"
                    " WHERE tenant_id = :t AND entity_id = :e ORDER BY scheme, value"
                ),
                {"t": tenant_id, "e": entity_id},
            ).all()
        return [(r[0], r[1], r[2]) for r in rows]

    def replace_parameters(self, tenant_id: str, entity_id: str, rows: Sequence[dict[str, Any]]) -> None:
        """Replace the entity's parameters, atomically. Each row needs `canonical_name`, `source_name`,
        `value_type`; the rest of `_PARAM_FIELDS` is optional. ValueError for a bad row, a duplicate
        `(canonical_name, source_name)` or an entity that is not this tenant's (nothing changes)."""
        params = []
        for row in rows:
            unknown = set(row) - set(_PARAM_FIELDS)
            if unknown or not all(k in row for k in ("canonical_name", "source_name", "value_type")):
                raise ValueError("invalid parameter row")
            params.append({"t": tenant_id, "e": entity_id, "id": str(uuid.uuid4()),
                           **{k: row.get(k) for k in _PARAM_FIELDS}})
        try:
            with self._engine.begin() as conn:
                conn.execute(
                    text("DELETE FROM kb_entity_parameters WHERE tenant_id = :t AND entity_id = :e"),
                    {"t": tenant_id, "e": entity_id},
                )
                if params:
                    conn.execute(
                        text(
                            "INSERT INTO kb_entity_parameters (tenant_id, id, entity_id, "
                            + ", ".join(_PARAM_FIELDS)
                            + ") VALUES (:t, :id, :e, "
                            + ", ".join(f":{f}" for f in _PARAM_FIELDS)
                            + ")"
                        ),
                        params,
                    )
        except IntegrityError as exc:
            raise ValueError("invalid parameters or entity not found") from exc

    def list_parameters(self, tenant_id: str, entity_id: str) -> list[dict[str, Any]]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"SELECT {', '.join(_PARAM_FIELDS)} FROM kb_entity_parameters"
                    " WHERE tenant_id = :t AND entity_id = :e ORDER BY canonical_name, source_name"
                ),
                {"t": tenant_id, "e": entity_id},
            ).all()
        return [dict(r._mapping) for r in rows]

    # -- documents: the ledger between discovery and eap.kb_ingest

    def upsert_document(
        self,
        tenant_id: str,
        entity_id: str,
        *,
        document_key: str,
        relationship: str,
        source_url: str,
        source_revision: str | None = None,
    ) -> bool:
        """Record a discovered document; True when new, False when the ledger already has the key (status and
        hashes are left as they are). ValueError for a key outside 1..256 characters or an entity that is not
        this tenant's."""
        try:
            with self._engine.begin() as conn:
                row = conn.execute(
                    text(
                        "INSERT INTO kb_entity_documents (tenant_id, entity_id, document_key, relationship,"
                        " source_url, source_revision) VALUES (:t, :e, :k, :rel, :url, :rev)"
                        " ON CONFLICT DO NOTHING RETURNING 1"
                    ),
                    {"t": tenant_id, "e": entity_id, "k": document_key, "rel": relationship, "url": source_url,
                     "rev": source_revision},
                ).first()
        except IntegrityError as exc:
            raise ValueError("invalid document key or entity not found") from exc
        return row is not None

    def get_document(self, tenant_id: str, entity_id: str, document_key: str) -> EntityDocument | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    f"SELECT {_cols(EntityDocument)} FROM kb_entity_documents"
                    " WHERE tenant_id = :t AND entity_id = :e AND document_key = :k"
                ),
                {"t": tenant_id, "e": entity_id, "k": document_key},
            ).first()
        return _build(EntityDocument, row) if row else None

    def mark_document_queued(
        self,
        tenant_id: str,
        entity_id: str,
        document_key: str,
        *,
        content_hash: str,
        etag: str | None = None,
        last_modified: str | None = None,
        source_revision: str | None = None,
        kb_document_id: str | None = None,
    ) -> bool:
        """The document is in the knowledge base and its ingest is queued. `etag`/`last_modified` are the
        provider's validators for the next conditional request; `source_revision` and `kb_document_id` None keep
        the stored value. Clears a previous error. False when the ledger has no such row for this tenant."""
        with self._engine.begin() as conn:
            result = conn.execute(
                text(
                    "UPDATE kb_entity_documents SET status = 'queued', content_hash = :h, etag = :etag,"
                    " last_modified = :lm, source_revision = COALESCE(:rev, source_revision),"
                    " kb_document_id = COALESCE(CAST(:doc AS uuid), kb_document_id), error_code = NULL,"
                    " queued_at = now() WHERE tenant_id = :t AND entity_id = :e AND document_key = :k"
                ),
                {"t": tenant_id, "e": entity_id, "k": document_key, "h": content_hash, "etag": etag,
                 "lm": last_modified, "rev": source_revision, "doc": kb_document_id},
            )
        return result.rowcount == 1

    def list_documents(self, tenant_id: str, entity_id: str, relationship: str) -> list[EntityDocument]:
        """Every ledger row of the entity with this relationship, by key."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"SELECT {_cols(EntityDocument)} FROM kb_entity_documents"
                    " WHERE tenant_id = :t AND entity_id = :e AND relationship = :rel ORDER BY document_key"
                ),
                {"t": tenant_id, "e": entity_id, "rel": relationship},
            ).all()
        return [_build(EntityDocument, r) for r in rows]

    def mark_document_removed(self, tenant_id: str, entity_id: str, document_key: str) -> bool:
        """The provider no longer has the document (status `removed`; the hashes stay for history)."""
        with self._engine.begin() as conn:
            result = conn.execute(
                text(
                    "UPDATE kb_entity_documents SET status = 'removed', error_code = NULL"
                    " WHERE tenant_id = :t AND entity_id = :e AND document_key = :k"
                ),
                {"t": tenant_id, "e": entity_id, "k": document_key},
            )
        return result.rowcount == 1

    def mark_document_failed(self, tenant_id: str, entity_id: str, document_key: str, error_code: str) -> bool:
        with self._engine.begin() as conn:
            result = conn.execute(
                text(
                    "UPDATE kb_entity_documents SET status = 'failed', error_code = :c"
                    " WHERE tenant_id = :t AND entity_id = :e AND document_key = :k"
                ),
                {"t": tenant_id, "e": entity_id, "k": document_key, "c": error_code},
            )
        return result.rowcount == 1

    def pending_documents(self, tenant_id: str, source_id: str, limit: int) -> list[EntityDocument]:
        """Documents of the source's entities still `discovered` (not queued, not failed), oldest first."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"SELECT {_cols(EntityDocument, 'd.')} FROM kb_entity_documents d JOIN kb_entities e"
                    " ON e.tenant_id = d.tenant_id AND e.id = d.entity_id"
                    " WHERE d.tenant_id = :t AND e.source_id = :s AND d.status = 'discovered'"
                    " ORDER BY d.discovered_at, d.document_key LIMIT :n"
                ),
                {"t": tenant_id, "s": source_id, "n": max(0, int(limit))},
            ).all()
        return [_build(EntityDocument, r) for r in rows]
