"""The hourly mirror of the official MCP registry (`eap.mcp_catalog_mirror`).

The registry is a recorded fake (server.schema.json shape, 2025-12-11) behind an httpx2 MockTransport:
nothing here reaches the real registry."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import httpx2
import pytest
from sqlalchemy import text

from eap_run_store import CatalogStore
from eap_worker import mcp_catalog_mirror as mirror
from eap_worker.mcp_catalog_mirror import (
    DEFAULT_SCHEDULE,
    SCHEDULE_NAME,
    CatalogMirrorConfig,
    CatalogMirrorConfigError,
    apply_catalog_schedule,
    mcp_catalog_mirror,
    parse_entry,
)

TICK = datetime(2026, 11, 17, 15, 0, tzinfo=timezone.utc)
META = "io.modelcontextprotocol.registry/official"


def server(name: str, *, status: str = "active", latest: bool = True, **over) -> dict:
    body = {
        "$schema": "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json",
        "name": name,
        "title": name.split("/")[1].title(),
        "description": f"{name} description",
        "version": "1.2.0",
        "packages": [
            {
                "registryType": "npm",
                "identifier": "@acme/" + name.split("/")[1],
                "version": "1.2.0",
                "transport": {"type": "stdio"},
                "environmentVariables": [
                    {"name": "ACME_TOKEN", "description": "API token", "isRequired": True, "isSecret": True}
                ],
            }
        ],
    }
    body.update(over)
    return {"server": body, "_meta": {META: {"status": status, "isLatest": latest}}}


class FakeRegistry:
    def __init__(self, pages: list[list[dict]]) -> None:
        self.pages = pages
        self.requests: list[str] = []
        self.fail = False

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(str(request.url))
        if self.fail:
            return httpx2.Response(503, text="down")
        cursor = request.url.params.get("cursor")
        i = int(cursor) if cursor else 0
        meta = {"count": len(self.pages[i])}
        if i + 1 < len(self.pages):
            meta["nextCursor"] = str(i + 1)
        return httpx2.Response(200, json={"servers": self.pages[i], "metadata": meta})


@pytest.fixture
def registry(monkeypatch):
    reg = FakeRegistry([])
    monkeypatch.setattr(
        mirror, "_client", lambda: httpx2.AsyncClient(transport=httpx2.MockTransport(reg.handler))
    )
    return reg


@pytest.fixture
def store(worker):
    with worker.engine.begin() as conn:
        conn.execute(text("DELETE FROM mcp_catalog_entries"))
    yield CatalogStore(worker.engine)
    with worker.engine.begin() as conn:
        conn.execute(text("DELETE FROM mcp_catalog_entries"))


def run(at: datetime = TICK, **context):
    from dbos import DBOS, SetWorkflowID

    context.setdefault("min_keep", 1)
    ctx = CatalogMirrorConfig(**context).to_context()
    with SetWorkflowID(f"test-mirror-{uuid.uuid4().hex[:8]}"):
        return DBOS.start_workflow(mcp_catalog_mirror, at, ctx).get_result()


def names(store: CatalogStore) -> list[str]:
    return [e.name for e in store.search(limit=100)]


# ------------------------------------------------------------------------------------------- parsing


def test_parse_entry_maps_the_registry_shape():
    e = parse_entry(server("io.acme/files", remotes=[{"type": "streamable-http", "url": "https://x.example/mcp"}]))
    assert e is not None
    assert (e.name, e.title, e.version, e.status, e.is_latest) == ("io.acme/files", "Files", "1.2.0", "active", True)
    (pkg,) = e.packages
    assert (pkg.registry_type, pkg.identifier, pkg.version, pkg.transport) == ("npm", "@acme/files", "1.2.0", "stdio")
    (var,) = pkg.environment_variables
    assert (var.name, var.required, var.secret) == ("ACME_TOKEN", True, True)
    assert e.remotes == ({"type": "streamable-http", "url": "https://x.example/mcp"},)


def test_parse_entry_accepts_plain_required_and_secret_keys_and_skips_unsupported_packages():
    item = server("io.acme/p")
    item["server"]["packages"] = [
        {"registryType": "pypi", "identifier": "acme", "version": "0.1", "transport": {"type": "streamable-http"},
         "environmentVariables": [{"name": "A", "required": True, "secret": False}]},
        {"registryType": "nuget", "identifier": "Acme", "version": "1", "transport": {"type": "stdio"}},
        {"registryType": "npm", "identifier": "sse-one", "version": "1", "transport": {"type": "sse"}},
        {"registryType": "oci", "identifier": "ghcr.io/acme/x:1", "transport": {"type": "stdio"}},
    ]
    e = parse_entry(item)
    assert [p.registry_type for p in e.packages] == ["pypi", "oci"]
    assert e.packages[0].environment_variables[0].required is True


@pytest.mark.parametrize("item", [{}, {"server": {}}, {"server": {"name": "x"}}, "nope", None])
def test_parse_entry_drops_what_is_not_an_entry(item):
    assert parse_entry(item) is None


# ------------------------------------------------------------------------------------------ config


def test_config_defaults_off_and_bad_values(monkeypatch):
    cfg = CatalogMirrorConfig.from_env({})
    assert cfg.schedule == DEFAULT_SCHEDULE == "0 15 * * * *" and not cfg.off
    assert CatalogMirrorConfig.from_env({"EAP_MCP_CATALOG_SCHEDULE": "off"}).off
    assert CatalogMirrorConfig.from_env({"EAP_MCP_CATALOG_SCHEDULE": " OFF "}).off
    with pytest.raises(CatalogMirrorConfigError):
        CatalogMirrorConfig.from_env({"EAP_MCP_CATALOG_MAX_PAGES": "0"})
    with pytest.raises(CatalogMirrorConfigError):
        CatalogMirrorConfig.from_env({"EAP_MCP_CATALOG_PAGE_SIZE": "abc"})
    with pytest.raises(CatalogMirrorConfigError):
        CatalogMirrorConfig.from_env({"EAP_MCP_CATALOG_URL": "ftp://x"})


# ----------------------------------------------------------------------------------------- workflow


def test_mirror_pages_through_the_registry_and_upserts(worker, registry, store):
    registry.pages = [
        [server("io.acme/a"), server("io.acme/b")],
        [server("io.acme/c")],
    ]
    out = run()
    assert names(store) == ["io.acme/a", "io.acme/b", "io.acme/c"]
    assert len(registry.requests) == 2 and "cursor=1" in registry.requests[1]
    assert registry.requests[0].startswith("https://registry.modelcontextprotocol.io/v0/servers")
    assert out == {"scheduled_at": TICK.isoformat(), "pages": 2, "fetched": 3, "kept": 3, "removed": 0, "complete": True}
    got = store.get("io.acme/a")
    assert got.fetched_at == TICK and got.packages[0].identifier == "@acme/a"


def test_only_latest_active_entries_are_kept(worker, registry, store):
    registry.pages = [
        [
            server("io.acme/live"),
            server("io.acme/old", latest=False),
            server("io.acme/deprecated", status="deprecated"),
            server("io.acme/deleted", status="deleted"),
            {"server": {"name": "broken"}},
        ]
    ]
    out = run()
    assert names(store) == ["io.acme/live"]
    assert store.get("io.acme/old") is None
    assert (out["fetched"], out["kept"]) == (5, 1)


def test_entries_that_vanish_are_marked_removed_not_deleted(worker, registry, store):
    registry.pages = [[server("io.acme/a"), server("io.acme/b"), server("io.acme/c")]]
    run()
    store.set_hidden("io.acme/a", True)
    registry.pages = [[server("io.acme/a", version="2.0.0"), server("io.acme/d"), server("io.acme/c", status="deprecated")]]
    out = run(TICK + timedelta(hours=1))
    assert out["removed"] == 2
    assert store.get("io.acme/b").status == "removed"  # vanished
    assert store.get("io.acme/c").status == "removed"  # no longer active
    a = store.get("io.acme/a")
    assert a.status == "active" and a.version == "2.0.0" and a.hidden is True
    assert names(store) == ["io.acme/d"]  # a is hidden, b and c are removed: only d is offered to tenants
    # an entry that returns is active again
    registry.pages = [[server("io.acme/b"), server("io.acme/d")]]
    run(TICK + timedelta(hours=2))
    assert store.get("io.acme/b").status == "active" and store.get("io.acme/a").status == "removed"


def test_registry_outage_keeps_the_last_mirror(worker, registry, store):
    registry.pages = [[server("io.acme/a"), server("io.acme/b")]]
    run()
    registry.fail = True
    with pytest.raises(Exception):
        run(TICK + timedelta(hours=1))
    assert names(store) == ["io.acme/a", "io.acme/b"]

    # an outage half way through a listing: the pages upserted so far stay, nothing is removed
    registry.fail = False
    registry.pages = [[server("io.acme/a", version="3.0.0")], [server("io.acme/never")]]
    real = registry.handler

    def flaky(request):
        if request.url.params.get("cursor") == "1":
            return httpx2.Response(500)
        return real(request)

    registry.handler = flaky  # type: ignore[method-assign]
    with pytest.raises(Exception):
        run(TICK + timedelta(hours=2))
    assert names(store) == ["io.acme/a", "io.acme/b"]
    assert store.get("io.acme/b").status == "active"


def test_an_empty_registry_answer_never_empties_the_mirror(worker, registry, store):
    registry.pages = [[server("io.acme/a")]]
    run()
    registry.pages = [[]]
    out = run(TICK + timedelta(hours=1))
    assert out["complete"] is False and out["removed"] == 0
    assert names(store) == ["io.acme/a"]


def test_the_page_limit_stops_the_run_without_removing(worker, registry, store):
    registry.pages = [[server("io.acme/a")]]
    run()
    registry.pages = [[server(f"io.acme/p{i}")] for i in range(4)]
    out = run(TICK + timedelta(hours=1), max_pages=2)
    assert out["complete"] is False and out["pages"] == 2 and out["removed"] == 0
    assert store.get("io.acme/a").status == "active"


def test_a_repeated_cursor_is_refused(worker, registry, store):
    def loop(request):
        return httpx2.Response(200, json={"servers": [server("io.acme/a")], "metadata": {"nextCursor": "same"}})

    registry.handler = loop  # type: ignore[method-assign]
    with pytest.raises(Exception, match="cursor"):
        run()


# -------------------------------------------------------------------------------------- schedule


def test_schedule_is_applied_and_off_applies_none(worker):
    from dbos import DBOS

    try:
        cfg = CatalogMirrorConfig.from_env({})
        assert apply_catalog_schedule(cfg) == "applied"
        assert apply_catalog_schedule(cfg) == "applied"  # idempotent: every worker start
        sched = DBOS.get_schedule(SCHEDULE_NAME)
        assert sched is not None and sched["schedule"] == "0 15 * * * *"
        assert sched["workflow_name"] == "eap.mcp_catalog_mirror" and sched["context"] == cfg.to_context()

        assert apply_catalog_schedule(CatalogMirrorConfig.from_env({"EAP_MCP_CATALOG_SCHEDULE": "off"})) == "removed"
        assert DBOS.get_schedule(SCHEDULE_NAME) is None
        assert apply_catalog_schedule(CatalogMirrorConfig.from_env({"EAP_MCP_CATALOG_SCHEDULE": "off"})) == "removed"
        assert DBOS.get_schedule(SCHEDULE_NAME) is None
    finally:
        DBOS.delete_schedule(SCHEDULE_NAME)


def test_worker_start_validates_the_mirror_settings(worker_settings, monkeypatch):
    from eap_worker.app import validate_startup

    monkeypatch.setenv("EAP_MCP_CATALOG_SCHEDULE", "off")
    validate_startup(worker_settings)
    monkeypatch.setenv("EAP_MCP_CATALOG_MAX_PAGES", "-3")
    with pytest.raises(CatalogMirrorConfigError):
        validate_startup(worker_settings)


# ------------------------------------------------------------------------------ bounds and floors


def test_an_oversized_body_is_refused(worker, registry, store):
    big = b'{"servers": [], "pad": "' + b"x" * (mirror.MAX_BODY_BYTES + 10) + b'"}'
    registry.handler = lambda request: httpx2.Response(200, content=big)  # type: ignore[method-assign]
    with pytest.raises(Exception) as info:
        run()
    assert "fetch_page" in str(info.value)  # the step gave up after its retries
    assert names(store) == []


def test_removal_is_skipped_below_the_floor(worker, registry, store):
    registry.pages = [[server(f"io.acme/s{i}") for i in range(10)]]
    run()
    registry.pages = [[server("io.acme/s0"), server("io.acme/s1")]]  # 2 of 10: under half
    out = run(TICK + timedelta(hours=1))
    assert out["removed"] == 0 and out["complete"] is False
    assert store.get("io.acme/s9").status == "active"
    # at or above half it removes
    registry.pages = [[server(f"io.acme/s{i}") for i in range(5)]]
    assert run(TICK + timedelta(hours=2))["removed"] == 5
    # the absolute minimum applies too
    registry.pages = [[server(f"io.acme/s{i}") for i in range(4)]]
    out = run(TICK + timedelta(hours=3), min_keep=20)
    assert out["removed"] == 0 and store.get("io.acme/s4").status == "active"


def test_config_validates_the_cron_and_min_keep():
    with pytest.raises(CatalogMirrorConfigError):
        CatalogMirrorConfig.from_env({"EAP_MCP_CATALOG_SCHEDULE": "not a cron"})
    assert CatalogMirrorConfig.from_env({"EAP_MCP_CATALOG_SCHEDULE": "0 */5 * * * *"}).schedule == "0 */5 * * * *"
    assert CatalogMirrorConfig.from_env({}).min_keep == 20
    with pytest.raises(CatalogMirrorConfigError, match="http"):
        CatalogMirrorConfig.from_env({"EAP_MCP_CATALOG_URL": "ftp://x"})
