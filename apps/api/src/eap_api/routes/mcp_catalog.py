"""The MCP catalog: a read-only, platform-wide mirror of the official registry (milestone MCP local
runtime, task 7).

- GET /mcp-catalog?q&limit&after&source   latest active entries, ToolHive first then by name; `q` matches name,
  title, description; `source` is `toolhive` or `official` (absent: both)
- GET /mcp-catalog/{name}          one entry (names contain a slash: `io.github.acme/server`)

Installing an entry is POST /mcp-servers/from-catalog (routes.mcp_servers). The mirror has no tenant;
hiding an entry is an operator's database choice (there is no admin role to put it behind), and a
hidden or removed entry still resolves by name so an installed server keeps its history.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from eap_api.deps import get_database_engine
from eap_api.auth import Principal, current_principal
from eap_api.schemas import ErrorDetail
from eap_run_store.mcp_catalog import MAX_SEARCH_LIMIT, CatalogStore
from eap_schemas import CatalogEntry

PrincipalDep = Annotated[Principal, Depends(current_principal)]

router = APIRouter(tags=["mcp-catalog"], responses={404: {"model": ErrorDetail}})

DEFAULT_LIMIT = 30


def _entry_json(entry: CatalogEntry) -> dict[str, Any]:
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


@router.get("/mcp-catalog")
async def search_catalog(
    request: Request,
    principal: PrincipalDep,
    q: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_SEARCH_LIMIT)] = DEFAULT_LIMIT,
    after: Annotated[str | None, Query(max_length=300)] = None,
    source: Annotated[Literal["toolhive", "official"] | None, Query()] = None,
) -> JSONResponse:
    """`{items: [entry], next_after}`: pass `next_after` as `after` for the next page (null on the last)."""
    del principal  # any authenticated tenant reads the same mirror
    store = CatalogStore(get_database_engine(request))
    entries = await run_in_threadpool(store.search, q, limit, after, source=source)
    next_after = entries[-1].name if len(entries) == limit else None
    return JSONResponse(content={"items": [_entry_json(e) for e in entries], "next_after": next_after})


@router.get("/mcp-catalog/{name:path}")
async def get_catalog_entry(name: str, request: Request, principal: PrincipalDep) -> JSONResponse:
    del principal
    entry = await run_in_threadpool(CatalogStore(get_database_engine(request)).get, name)
    if entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="catalog entry not found")
    return JSONResponse(content=_entry_json(entry))
