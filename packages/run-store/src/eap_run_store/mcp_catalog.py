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
_COLUMNS = "name, title, description, version, status, is_latest, packages, remotes, licence, fetched_at, hidden"

_UPSERT = text(
    """
    INSERT INTO mcp_catalog_entries
        (name, title, description, version, status, is_latest, packages, remotes, licence, fetched_at)
    VALUES
        (:name, :title, :description, :version, :status, :is_latest,
         CAST(:packages AS jsonb), CAST(:remotes AS jsonb), :licence, :fetched_at)
    ON CONFLICT (name) DO UPDATE SET
        title = EXCLUDED.title, description = EXCLUDED.description, version = EXCLUDED.version,
        status = EXCLUDED.status, is_latest = EXCLUDED.is_latest, packages = EXCLUDED.packages,
        remotes = EXCLUDED.remotes, licence = EXCLUDED.licence, fetched_at = EXCLUDED.fetched_at
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
        }
    )


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
            }
            for e in entries
        ]
        with self._engine.begin() as conn:
            conn.execute(_UPSERT, rows)
        return len(rows)

    def mark_removed_before(self, fetched_before: datetime) -> int:
        """Mark the active entries last fetched before the instant as `removed` (never deleted).
        Returns how many changed."""
        with self._engine.begin() as conn:
            result = conn.execute(
                text(
                    "UPDATE mcp_catalog_entries SET status = 'removed'"
                    " WHERE status = 'active' AND fetched_at < :t"
                ),
                {"t": fetched_before},
            )
        return result.rowcount

    def search(
        self, q: str | None = None, limit: int = 30, after: str | None = None, *, include_hidden: bool = False
    ) -> list[CatalogEntry]:
        """Latest, active entries whose name, title or description contain `q` (case-insensitive,
        literal), by name; `after` is the last name of the previous page. Hidden entries only with
        `include_hidden`."""
        clauses = ["is_latest", "status = 'active'"]
        params: dict[str, Any] = {"limit": max(1, min(int(limit), MAX_SEARCH_LIMIT))}
        if not include_hidden:
            clauses.append("NOT hidden")
        if q and q.strip():
            clauses.append(
                "(name ILIKE :q ESCAPE '\\' OR title ILIKE :q ESCAPE '\\' OR description ILIKE :q ESCAPE '\\')"
            )
            params["q"] = f"%{_escape_like(q.strip())}%"
        if after is not None:
            clauses.append("name > :after")
            params["after"] = after
        sql = text(
            f"SELECT {_COLUMNS} FROM mcp_catalog_entries WHERE {' AND '.join(clauses)} ORDER BY name LIMIT :limit"
        )
        with self._engine.connect() as conn:
            return [_entry(r) for r in conn.execute(sql, params)]

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
