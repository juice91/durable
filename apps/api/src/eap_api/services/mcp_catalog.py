"""The MCP catalog: a read-only, platform-wide mirror of the official registry (no tenant)."""

from __future__ import annotations

from typing import Any

from eap_run_store.definition_errors import DefinitionNotFound
from eap_run_store.mcp_catalog import MAX_SEARCH_LIMIT, CatalogStore  # noqa: F401  (MAX_SEARCH_LIMIT: the route's bound)
from eap_schemas import CatalogEntry


def entry_json(entry: CatalogEntry) -> dict[str, Any]:
    """The entry as the API shows it. `local` is true when it has a package the platform can run,
    `remote` when it has a Streamable HTTP remote that registers as an external server."""
    remotes = [dict(r) for r in entry.remotes]
    meta = entry.meta
    return {
        "name": entry.name,
        "title": entry.title,
        "description": entry.description,
        "version": entry.version,
        "status": entry.status,
        "licence": entry.licence,
        "packages": [p.model_dump(mode="json") for p in entry.packages],
        "remotes": remotes,
        "local": any(p.registry_type in ("npm", "pypi", "oci") for p in entry.packages),
        "remote": any(str(r.get("type", "")).lower() == "streamable-http" and r.get("url") for r in remotes),
        "fetched_at": entry.fetched_at.isoformat() if entry.fetched_at else None,
        "source": entry.source,
        "tier": meta.tier if meta else None,
        "tags": list(meta.tags) if meta else [],
        "network": meta.network.model_dump(mode="json") if meta and meta.network else None,
        "tool_count": meta.tool_count if meta else 0,
    }


class McpCatalogService:
    def __init__(self, store: CatalogStore) -> None:
        self._store = store

    def search(self, q: str | None, limit: int, after: str | None, source: str | None) -> dict[str, Any]:
        """`{items: [entry], next_after}`: `next_after` is the last name when the page is full, else null."""
        entries = self._store.search(q, limit, after, source=source)
        next_after = entries[-1].name if len(entries) == limit else None
        return {"items": [entry_json(e) for e in entries], "next_after": next_after}

    def get(self, name: str) -> dict[str, Any]:
        entry = self._store.get(name)
        if entry is None:
            raise DefinitionNotFound("catalog entry not found")
        return entry_json(entry)

    def get_entry(self, name: str) -> CatalogEntry | None:
        return self._store.get(name)
