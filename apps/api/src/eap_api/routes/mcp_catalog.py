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

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from eap_api.auth import Principal, current_principal
from eap_api.schemas import ErrorDetail
from eap_api.service_deps.connections import McpCatalogServiceDep
from eap_api.services.mcp_catalog import MAX_SEARCH_LIMIT

PrincipalDep = Annotated[Principal, Depends(current_principal)]

router = APIRouter(tags=["mcp-catalog"], responses={404: {"model": ErrorDetail}})

DEFAULT_LIMIT = 30


@router.get("/mcp-catalog")
async def search_catalog(
    service: McpCatalogServiceDep,
    principal: PrincipalDep,
    q: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_SEARCH_LIMIT)] = DEFAULT_LIMIT,
    after: Annotated[str | None, Query(max_length=300)] = None,
    source: Annotated[Literal["toolhive", "official"] | None, Query()] = None,
) -> JSONResponse:
    """`{items: [entry], next_after}`: pass `next_after` as `after` for the next page (null on the last)."""
    del principal  # any authenticated tenant reads the same mirror
    return JSONResponse(content=await run_in_threadpool(service.search, q, limit, after, source))


@router.get("/mcp-catalog/{name:path}")
async def get_catalog_entry(name: str, service: McpCatalogServiceDep, principal: PrincipalDep) -> JSONResponse:
    del principal
    return JSONResponse(content=await run_in_threadpool(service.get, name))
