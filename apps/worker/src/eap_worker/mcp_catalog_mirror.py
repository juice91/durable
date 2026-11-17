"""Hourly mirror of the official MCP registry (MCP local runtime, MR.2): the scheduled DBOS workflow
`eap.mcp_catalog_mirror`.

Studio searches our copy (`mcp_catalog_entries`, platform-wide), never the registry. The workflow
pages through `GET <registry>/v0/servers?cursor=` with `OutboundPolicy.http_client()` (the same
guard every outbound call uses) and keeps the entries that are the latest version and `active`.

    env                          default                                    meaning
    EAP_MCP_CATALOG_SCHEDULE     0 15 * * * *  (hourly, minute 15, UTC)     cron with seconds; `off` removes the schedule
    EAP_MCP_CATALOG_URL          https://registry.modelcontextprotocol.io/v0/servers
    EAP_MCP_CATALOG_PAGE_SIZE    100                                        `limit` per page (the registry caps it at 100)
    EAP_MCP_CATALOG_MAX_PAGES    200                                        pages per run
    EAP_MCP_CATALOG_MIN_KEEP     20                                         removal floor (see below)

Durability and safety:
- Each page is one fetch step and one upsert step, so a crash resumes after the last completed step;
  the upsert is idempotent. Every row a run writes carries `fetched_at = scheduled_at`.
- A failed run never truncates the mirror: pages already upserted stay, and nothing is marked removed.
- Entries that vanish (or are no longer latest and active) are marked `status = 'removed'`, not
  deleted, and only after a run that read the whole registry and kept at least one entry: an
  empty answer or a run stopped by the page limit leaves the mirror as it is. A removed entry that
  returns becomes `active` again; removal is skipped (and logged) when the run kept fewer than half of
  the active entries or fewer than EAP_MCP_CATALOG_MIN_KEEP (a registry that shrank that far is a bad
  answer). `hidden` is an operator's choice and is never overwritten.
- A response body is capped at 8 MB. An older, slower run never lowers `fetched_at` of a row a newer
  run wrote (the upsert only updates rows with an older or equal stamp).
- Several workers apply the same schedule (an upsert by name); DBOS runs one workflow per tick.

Logs and the result carry counts only.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
import json
from urllib.parse import urlsplit

from dbos import DBOS

from eap_run_store import CatalogStore
from eap_schemas import CatalogEntry, CatalogPackage, EnvVarSpec
from eap_secrets.outbound import OutboundPolicy

from eap_worker.cancel_reconcile import valid_cron
from eap_worker.runtime import get_runtime

log = logging.getLogger("eap_worker.mcp_catalog_mirror")

WORKFLOW_NAME = "eap.mcp_catalog_mirror"
SCHEDULE_NAME = "eap.mcp_catalog_mirror"
FETCH_STEP = "eap.mcp_catalog_mirror.fetch_page"
UPSERT_STEP = "eap.mcp_catalog_mirror.upsert"
COUNT_STEP = "eap.mcp_catalog_mirror.count_active"
REMOVE_STEP = "eap.mcp_catalog_mirror.mark_removed"

SCHEDULE_ENV = "EAP_MCP_CATALOG_SCHEDULE"
URL_ENV = "EAP_MCP_CATALOG_URL"
PAGE_SIZE_ENV = "EAP_MCP_CATALOG_PAGE_SIZE"
MAX_PAGES_ENV = "EAP_MCP_CATALOG_MAX_PAGES"
MIN_KEEP_ENV = "EAP_MCP_CATALOG_MIN_KEEP"
DEFAULT_SCHEDULE = "0 15 * * * *"
DEFAULT_URL = "https://registry.modelcontextprotocol.io/v0/servers"
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 100
DEFAULT_MAX_PAGES = 200
MAX_MAX_PAGES = 10_000
DEFAULT_MIN_KEEP = 20
MAX_MIN_KEEP = 1_000_000
MAX_BODY_BYTES = 8 * 1024 * 1024
OFF = "off"
REGISTRY_META_KEY = "io.modelcontextprotocol.registry/official"
SUPPORTED_REGISTRY_TYPES = ("npm", "pypi", "oci")
SUPPORTED_TRANSPORTS = ("stdio", "streamable-http")


class CatalogMirrorConfigError(ValueError):
    """An invalid mirror setting (raised at worker start, before anything is scheduled)."""

    def __init__(self, name: str, problem: str) -> None:
        super().__init__(name, problem)
        self.name = name
        self.problem = problem

    def __str__(self) -> str:
        return f"{self.name}: {self.problem}"


def _bounded_int(name: str, raw: Any, default: int, maximum: int) -> int:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise CatalogMirrorConfigError(name, "must be a positive integer") from None
    if not 1 <= value <= maximum:
        raise CatalogMirrorConfigError(name, f"must be between 1 and {maximum}")
    return value


@dataclass(frozen=True)
class CatalogMirrorConfig:
    schedule: str = DEFAULT_SCHEDULE
    url: str = DEFAULT_URL
    page_size: int = DEFAULT_PAGE_SIZE
    max_pages: int = DEFAULT_MAX_PAGES
    min_keep: int = DEFAULT_MIN_KEEP
    off: bool = False

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "CatalogMirrorConfig":
        env = os.environ if environ is None else environ
        raw_schedule = (env.get(SCHEDULE_ENV) or "").strip()
        url = (env.get(URL_ENV) or "").strip() or DEFAULT_URL
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise CatalogMirrorConfigError(URL_ENV, "must be an http(s) URL")
        off = raw_schedule.lower() == OFF
        if not off and raw_schedule and not valid_cron(raw_schedule):
            # Before launch: DBOS.apply_schedules would only reject it after recovery has started.
            raise CatalogMirrorConfigError(SCHEDULE_ENV, "must be a valid cron expression (seconds first) or 'off'")
        config = cls(
            schedule=raw_schedule or DEFAULT_SCHEDULE,
            url=url,
            page_size=_bounded_int(PAGE_SIZE_ENV, env.get(PAGE_SIZE_ENV), DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE),
            max_pages=_bounded_int(MAX_PAGES_ENV, env.get(MAX_PAGES_ENV), DEFAULT_MAX_PAGES, MAX_MAX_PAGES),
            min_keep=_bounded_int(MIN_KEEP_ENV, env.get(MIN_KEEP_ENV), DEFAULT_MIN_KEEP, MAX_MIN_KEEP),
            off=off,
        )
        return config

    def to_context(self) -> dict[str, Any]:
        return {"url": self.url, "page_size": self.page_size, "max_pages": self.max_pages, "min_keep": self.min_keep}

    @classmethod
    def from_context(cls, context: Any) -> "CatalogMirrorConfig":
        ctx = context if isinstance(context, Mapping) else {}
        url = ctx.get("url")
        return cls(
            url=url if isinstance(url, str) and url else DEFAULT_URL,
            page_size=_bounded_int(PAGE_SIZE_ENV, ctx.get("page_size"), DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE),
            max_pages=_bounded_int(MAX_PAGES_ENV, ctx.get("max_pages"), DEFAULT_MAX_PAGES, MAX_MAX_PAGES),
            min_keep=_bounded_int(MIN_KEEP_ENV, ctx.get("min_keep"), DEFAULT_MIN_KEEP, MAX_MIN_KEEP),
        )


# --------------------------------------------------------------------------------------- parsing


def _env_var(raw: Any) -> EnvVarSpec | None:
    if not isinstance(raw, Mapping) or not isinstance(raw.get("name"), str) or not raw["name"]:
        return None
    description = raw.get("description")
    return EnvVarSpec(
        name=raw["name"][:200],
        description=description if isinstance(description, str) else None,
        required=bool(raw.get("isRequired", raw.get("required", False))),
        secret=bool(raw.get("isSecret", raw.get("secret", False))),
    )


def _package(raw: Any) -> CatalogPackage | None:
    """A runnable package (npm, pypi or oci over stdio or streamable-http), else None."""
    if not isinstance(raw, Mapping):
        return None
    registry_type, identifier = raw.get("registryType"), raw.get("identifier")
    transport = raw.get("transport")
    transport_type = transport.get("type") if isinstance(transport, Mapping) else transport
    if registry_type not in SUPPORTED_REGISTRY_TYPES or not isinstance(identifier, str) or not identifier:
        return None
    if transport_type not in SUPPORTED_TRANSPORTS:
        return None
    version = raw.get("version")
    env_vars = raw.get("environmentVariables")
    return CatalogPackage(
        registry_type=registry_type,
        identifier=identifier,
        version=version if isinstance(version, str) and version else None,
        transport=transport_type,
        environment_variables=tuple(
            v for v in (_env_var(x) for x in (env_vars if isinstance(env_vars, list) else ())) if v
        ),
    )


def parse_entry(item: Any) -> CatalogEntry | None:
    """One registry list item (`{server, _meta}`, or the bare server object) as a CatalogEntry, with
    the registry's status and isLatest. None when it is not an entry (no name or version)."""
    if not isinstance(item, Mapping):
        return None
    server = item.get("server") if isinstance(item.get("server"), Mapping) else item
    name, version = server.get("name"), server.get("version")
    if not isinstance(name, str) or not name or not isinstance(version, str) or not version:
        return None
    meta = item.get("_meta") or server.get("_meta") or {}
    official = meta.get(REGISTRY_META_KEY) if isinstance(meta, Mapping) else None
    official = official if isinstance(official, Mapping) else {}
    title, description = server.get("title"), server.get("description")
    packages = server.get("packages")
    remotes = server.get("remotes")
    return CatalogEntry(
        name=name,
        title=title if isinstance(title, str) else None,
        description=description if isinstance(description, str) else None,
        version=version,
        status=str(official.get("status") or "active"),
        is_latest=bool(official.get("isLatest", True)),
        packages=tuple(p for p in (_package(x) for x in (packages if isinstance(packages, list) else ())) if p),
        remotes=tuple(r for r in (remotes if isinstance(remotes, list) else ()) if isinstance(r, dict)),
    )


# ----------------------------------------------------------------------------------------- steps


def _client() -> Any:
    return OutboundPolicy.from_env().http_client(timeout=30.0)


def _store() -> CatalogStore:
    return CatalogStore(get_runtime().engine)


@DBOS.step(name=FETCH_STEP, retries_allowed=True, max_attempts=3)
async def fetch_page_step(url: str, page_size: int, cursor: str | None) -> dict[str, Any]:
    """One registry page. Output: the kept entries (latest and active) as JSON, how many items the page
    had, and the next cursor. A non-200 answer or malformed JSON raises (the step retries)."""
    params: dict[str, Any] = {"limit": page_size}
    if cursor:
        params["cursor"] = cursor
    async with _client() as client:
        async with client.stream("GET", url, params=params, headers={"accept": "application/json"}) as response:
            response.raise_for_status()
            buffer = bytearray()
            async for chunk in response.aiter_bytes():
                buffer.extend(chunk)
                if len(buffer) > MAX_BODY_BYTES:
                    raise ValueError(f"registry answer is larger than {MAX_BODY_BYTES} bytes")
    body = json.loads(bytes(buffer))
    items = body.get("servers") if isinstance(body, Mapping) else None
    if not isinstance(items, list):
        raise ValueError("registry answer has no servers list")
    kept = []
    for item in items:
        entry = parse_entry(item)
        if entry is not None and entry.is_latest and entry.status == "active":
            kept.append(entry.model_dump(mode="json"))
    meta = body.get("metadata")
    next_cursor = meta.get("nextCursor") if isinstance(meta, Mapping) else None
    return {
        "entries": kept,
        "fetched": len(items),
        "next_cursor": next_cursor if isinstance(next_cursor, str) and next_cursor else None,
    }


@DBOS.step(name=UPSERT_STEP, retries_allowed=True, max_attempts=3)
async def upsert_entries_step(entries: list[dict[str, Any]], fetched_at_iso: str) -> int:
    parsed = [CatalogEntry.model_validate(e) for e in entries]
    return await asyncio.to_thread(_store().upsert, parsed, fetched_at=datetime.fromisoformat(fetched_at_iso))


@DBOS.step(name=COUNT_STEP, retries_allowed=True, max_attempts=3)
async def count_active_step() -> int:
    return await asyncio.to_thread(_store().count_active)


@DBOS.step(name=REMOVE_STEP, retries_allowed=True, max_attempts=3)
async def mark_removed_step(fetched_at_iso: str) -> int:
    return await asyncio.to_thread(_store().mark_removed_before, datetime.fromisoformat(fetched_at_iso))


def as_utc(when: datetime) -> datetime:
    return when.replace(tzinfo=timezone.utc) if when.tzinfo is None else when.astimezone(timezone.utc)


@DBOS.workflow(name=WORKFLOW_NAME)
async def mcp_catalog_mirror(scheduled_at: datetime, context: Any) -> dict[str, Any]:
    """The scheduled mirror run (DBOS passes the tick time and the schedule context)."""
    config = CatalogMirrorConfig.from_context(context)
    at = as_utc(scheduled_at).isoformat()
    cursors: set[str] = set()
    cursor: str | None = None
    pages = fetched = kept = 0
    complete = False
    while pages < config.max_pages:
        page = await fetch_page_step(config.url, config.page_size, cursor)
        pages += 1
        fetched += page["fetched"]
        if page["entries"]:
            kept += await upsert_entries_step(page["entries"], at)
        cursor = page["next_cursor"]
        if cursor is None:
            complete = True
            break
        if cursor in cursors:
            raise RuntimeError("the registry repeated a page cursor")
        cursors.add(cursor)
    removed = 0
    if not complete:
        log.warning("mcp catalog mirror stopped at the page limit (%d); nothing marked removed", config.max_pages)
    elif kept == 0:
        complete = False
        log.warning("mcp catalog mirror: the registry returned no latest active entry; mirror left as it is")
    else:
        active = await count_active_step()
        if kept < config.min_keep or kept * 2 < active:
            log.warning(
                "mcp catalog mirror: kept %d of %d active entries (floor: half of them and at least %d); "
                "nothing marked removed", kept, active, config.min_keep,
            )
            complete = False
        else:
            removed = await mark_removed_step(at)
    log.info("mcp catalog mirrored pages=%d fetched=%d kept=%d removed=%d", pages, fetched, kept, removed)
    return {"scheduled_at": at, "pages": pages, "fetched": fetched, "kept": kept, "removed": removed, "complete": complete}


def apply_catalog_schedule(config: CatalogMirrorConfig | None = None) -> str:
    """Register (or replace) the mirror schedule after DBOS.launch(). Returns `applied`, or `removed`
    when EAP_MCP_CATALOG_SCHEDULE=off (the shared schedule is deleted)."""
    config = config or CatalogMirrorConfig.from_env()
    if config.off:
        DBOS.delete_schedule(SCHEDULE_NAME)
        log.info("mcp catalog mirror off: schedule removed")
        return "removed"
    DBOS.apply_schedules(
        [
            {
                "schedule_name": SCHEDULE_NAME,
                "workflow_fn": mcp_catalog_mirror,
                "schedule": config.schedule,
                "context": config.to_context(),
            }
        ]
    )
    log.info("mcp catalog mirror scheduled: schedule=%s", config.schedule)
    return "applied"
