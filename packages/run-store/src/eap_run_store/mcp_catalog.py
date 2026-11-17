"""The MCP catalog mirror (migration 0024 `mcp_catalog_entries`): a platform-wide, tenant-free copy of
the official MCP registry. Plain SQL against the table; the API's ORM model is not imported."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from eap_schemas import CatalogEntry

MAX_SEARCH_LIMIT = 100
_COLUMNS = (
    "name, title, description, version, status, is_latest, packages, remotes, licence, fetched_at, hidden,"
    " source, meta"
)
_SOURCES = ("official", "toolhive")
_RANK = "CASE WHEN source = 'toolhive' THEN 0 ELSE 1 END"

_UPSERT = text(
    """
    INSERT INTO mcp_catalog_entries
        (name, title, description, version, status, is_latest, packages, remotes, licence, fetched_at,
         source, meta)
    VALUES
        (:name, :title, :description, :version, :status, :is_latest,
         CAST(:packages AS jsonb), CAST(:remotes AS jsonb), :licence, :fetched_at,
         :source, CAST(:meta AS jsonb))
    ON CONFLICT (name) DO UPDATE SET
        title = EXCLUDED.title, description = EXCLUDED.description, version = EXCLUDED.version,
        status = EXCLUDED.status, is_latest = EXCLUDED.is_latest, packages = EXCLUDED.packages,
        remotes = EXCLUDED.remotes, licence = EXCLUDED.licence, fetched_at = EXCLUDED.fetched_at,
        source = EXCLUDED.source, meta = EXCLUDED.meta
    WHERE mcp_catalog_entries.fetched_at <= EXCLUDED.fetched_at
    """
)


def _escape_like(q: str) -> str:
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _entry(row: Any) -> CatalogEntry:
    return CatalogEntry.model_validate(
        {
            "name": row.name,
            "title": row.title,
            "description": row.description,
            "version": row.version,
            "status": row.status,
            "is_latest": row.is_latest,
            "packages": row.packages,
            "remotes": row.remotes,
            "licence": row.licence,
            "fetched_at": row.fetched_at,
            "hidden": row.hidden,
            "source": row.source,
            "meta": row.meta,
        }
    )


def _check_source(source: str) -> None:
    if source not in _SOURCES:
        raise ValueError(f"unknown catalog source {source!r}")


class CatalogStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def upsert(self, entries: Sequence[CatalogEntry], *, fetched_at: datetime | None = None) -> int:
        """Insert or update entries by name (one transaction), stamping `fetched_at`. `hidden` is an
        operator's choice and survives; everything else is the registry's. Returns the row count."""
        if not entries:
            return 0
        stamp = fetched_at or datetime.now(timezone.utc)
        rows = [
            {
                "name": e.name,
                "title": e.title,
                "description": e.description,
                "version": e.version,
                "status": e.status,
                "is_latest": e.is_latest,
                "packages": json.dumps([p.model_dump(mode="json") for p in e.packages]),
                "remotes": json.dumps(list(e.remotes)),
                "licence": e.licence,
                "fetched_at": stamp,
                "source": e.source,
                "meta": json.dumps(e.meta.model_dump(mode="json")) if e.meta is not None else None,
            }
            for e in entries
        ]
        with self._engine.begin() as conn:
            conn.execute(_UPSERT, rows)
        return len(rows)

    def mark_removed_before(self, fetched_before: datetime, source: str = "official") -> int:
        """Mark the active entries of one source last fetched before the instant as `removed` (never
        deleted); the other source's rows are not touched. Returns how many changed."""
        _check_source(source)
        with self._engine.begin() as conn:
            result = conn.execute(
                text(
                    "UPDATE mcp_catalog_entries SET status = 'removed'"
                    " WHERE status = 'active' AND source = :source AND fetched_at < :t"
                ),
                {"t": fetched_before, "source": source},
            )
        return result.rowcount

    def count_active(self, source: str | None = None) -> int:
        """How many entries are `active` (hidden ones included), of one source or of all."""
        sql, params = "SELECT count(*) FROM mcp_catalog_entries WHERE status = 'active'", {}
        if source is not None:
            _check_source(source)
            sql, params = sql + " AND source = :source", {"source": source}
        with self._engine.connect() as conn:
            return int(conn.execute(text(sql), params).scalar_one())

    def list(
        self,
        q: str | None = None,
        limit: int = 30,
        after: str | None = None,
        *,
        include_hidden: bool = False,
        source: str | None = None,
    ) -> list[CatalogEntry]:
        """Latest, active entries whose name, title or description contain `q` (case-insensitive,
        literal), ToolHive entries first and then official ones, each by name; `after` is the last name
        of the previous page. `source` keeps one source. Hidden entries only with `include_hidden`."""
        clauses = ["is_latest", "status = 'active'"]
        params: dict[str, Any] = {"limit": max(1, min(int(limit), MAX_SEARCH_LIMIT))}
        if not include_hidden:
            clauses.append("NOT hidden")
        if q and q.strip():
            clauses.append(
                "(name ILIKE :q ESCAPE '\\' OR title ILIKE :q ESCAPE '\\' OR description ILIKE :q ESCAPE '\\')"
            )
            params["q"] = f"%{_escape_like(q.strip())}%"
        if source is not None:
            _check_source(source)
            clauses.append("source = :source")
            params["source"] = source
        if after is not None:
            # Keyset on (source rank, name): the cursor entry's own source decides which rank it had.
            clauses.append(
                f"({_RANK}, name) > (CASE WHEN (SELECT source FROM mcp_catalog_entries WHERE name = :after)"
                " = 'toolhive' THEN 0 ELSE 1 END, :after)"
            )
            params["after"] = after
        sql = text(
            f"SELECT {_COLUMNS} FROM mcp_catalog_entries WHERE {' AND '.join(clauses)}"
            f" ORDER BY {_RANK}, name LIMIT :limit"
        )
        with self._engine.connect() as conn:
            return [_entry(r) for r in conn.execute(sql, params)]

    search = list  # the earlier name; the same call

    def get(self, name: str) -> CatalogEntry | None:
        """One entry by exact name, whatever its status or visibility (an installed server keeps resolving)."""
        with self._engine.connect() as conn:
            row = conn.execute(
                text(f"SELECT {_COLUMNS} FROM mcp_catalog_entries WHERE name = :name"), {"name": name}
            ).first()
        return _entry(row) if row else None

    def set_hidden(self, name: str, hidden: bool) -> bool:
        """Hide or show an entry in `search`. False when there is no such entry."""
        with self._engine.begin() as conn:
            result = conn.execute(
                text("UPDATE mcp_catalog_entries SET hidden = :hidden WHERE name = :name"),
                {"hidden": hidden, "name": name},
            )
        return result.rowcount > 0


__all__ = ["CatalogStore", "MAX_SEARCH_LIMIT"]
