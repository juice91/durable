"""CatalogStore: the platform-wide mirror of the MCP registry (0024 `mcp_catalog_entries`)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from eap_run_store import CatalogStore
from eap_schemas import CatalogEntry, CatalogPackage, EnvVarSpec

T0 = datetime(2026, 11, 17, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def prefix(engine):
    p = f"io.test.{uuid.uuid4().hex[:8]}"
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM mcp_catalog_entries"))
    yield p
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM mcp_catalog_entries"))


def entry(prefix: str, short: str, **kw) -> CatalogEntry:
    kw.setdefault("title", short.title())
    kw.setdefault("description", f"The {short} server")
    kw.setdefault("version", "1.0.0")
    return CatalogEntry(name=f"{prefix}/{short}", **kw)


def test_upsert_round_trips_packages_and_keeps_hidden(engine, prefix):
    store = CatalogStore(engine)
    pkg = CatalogPackage(
        registry_type="npm",
        identifier="@x/y",
        version="1.2.3",
        transport="stdio",
        environment_variables=(EnvVarSpec(name="TOKEN", description="d", required=True, secret=True),),
    )
    e = entry(prefix, "alpha", packages=(pkg,), remotes=({"type": "streamable-http", "url": "https://a.example/mcp"},))
    assert store.upsert([e], fetched_at=T0) == 1
    got = store.get(e.name)
    assert got is not None and got.packages == (pkg,) and got.remotes == e.remotes
    assert got.fetched_at == T0 and got.status == "active" and got.is_latest and not got.hidden

    assert store.set_hidden(e.name, True) is True
    store.upsert([entry(prefix, "alpha", version="2.0.0")], fetched_at=T0 + timedelta(hours=1))
    again = store.get(e.name)
    assert again.version == "2.0.0" and again.hidden is True and again.packages == ()
    assert store.set_hidden(f"{prefix}/nope", True) is False
    assert store.get(f"{prefix}/nope") is None


def test_search_matches_name_title_and_description(engine, prefix):
    store = CatalogStore(engine)
    store.upsert(
        [
            entry(prefix, "weather", title="Forecasts", description="Rain and wind"),
            entry(prefix, "plain", title="Zebra Tools", description="Stripes"),
            entry(prefix, "other", title="Other", description="Finds a quokka"),
            entry(prefix, "100%_sure", title="Literal", description="x"),
        ],
        fetched_at=T0,
    )
    names = lambda q: [e.name.split("/")[1] for e in store.search(f"{q}", limit=50) if e.name.startswith(prefix)]  # noqa: E731
    assert names("WEATHER") == ["weather"]  # name, case-insensitive
    assert names("zebra") == ["plain"]  # title
    assert names("quokka") == ["other"]  # description
    assert names("100%_") == ["100%_sure"]  # LIKE wildcards are literals
    assert names("%") == ["100%_sure"]
    assert names(f"{prefix}") == ["100%_sure", "other", "plain", "weather"]  # empty-ish q lists by name


def test_search_pages_by_name_and_hides_removed_and_hidden(engine, prefix):
    store = CatalogStore(engine)
    store.upsert([entry(prefix, c) for c in "abcde"], fetched_at=T0)
    store.set_hidden(f"{prefix}/b", True)
    store.upsert([entry(prefix, "d", status="removed")], fetched_at=T0)
    store.upsert([entry(prefix, "e", is_latest=False)], fetched_at=T0)
    first = store.search(prefix, limit=2)
    assert [e.name.split("/")[1] for e in first] == ["a", "c"]
    rest = store.search(prefix, limit=2, after=first[-1].name)
    assert rest == []
    withh = store.search(prefix, limit=10, include_hidden=True)
    assert [e.name.split("/")[1] for e in withh] == ["a", "b", "c"]
    assert store.get(f"{prefix}/d").status == "removed"  # get still resolves a removed entry


def test_mark_removed_before_only_touches_older_active_rows(engine, prefix):
    store = CatalogStore(engine)
    store.upsert([entry(prefix, "old"), entry(prefix, "gone", status="removed")], fetched_at=T0)
    store.upsert([entry(prefix, "new")], fetched_at=T0 + timedelta(hours=1))
    n = store.mark_removed_before(T0 + timedelta(hours=1))
    assert n == 1
    assert store.get(f"{prefix}/old").status == "removed"
    assert store.get(f"{prefix}/new").status == "active"
    assert store.get(f"{prefix}/gone").fetched_at == T0


def test_an_older_run_never_lowers_the_stamp(engine, prefix):
    store = CatalogStore(engine)
    store.upsert([entry(prefix, "a", version="2.0.0")], fetched_at=T0 + timedelta(hours=1))
    store.upsert([entry(prefix, "a", version="1.0.0")], fetched_at=T0)  # a slow, older run
    got = store.get(f"{prefix}/a")
    assert got.fetched_at == T0 + timedelta(hours=1) and got.version == "2.0.0"
    store.upsert([entry(prefix, "a", version="3.0.0")], fetched_at=T0 + timedelta(hours=1))  # same stamp: ok
    assert store.get(f"{prefix}/a").version == "3.0.0"
    assert store.count_active() == 1
