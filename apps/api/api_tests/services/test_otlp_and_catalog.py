"""OtlpIngestService and McpCatalogService on fake stores."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from eap_api.services.mcp_catalog import McpCatalogService
from eap_api.services.otlp import OtlpIngestService
from eap_run_store.definition_errors import DefinitionNotFound


def test_otlp_stores_spans_for_the_keys_tenant_and_skips_an_empty_batch():
    seen: list[tuple] = []
    spans = SimpleNamespace(insert_external=lambda tenant, key, items: seen.append((tenant, key, items)))
    service = OtlpIngestService(spans)
    service.store_spans("t1", "key1", [])
    assert seen == []
    service.store_spans("t1", "key1", ["s"])
    assert seen == [("t1", "key1", ["s"])]


class FakeCatalog:
    def __init__(self, names: list[str]) -> None:
        self.names = names

    def search(self, q, limit, after, source=None):
        return [SimpleNamespace(name=n) for n in self.names[:limit]]

    def get(self, name):
        return None


def test_a_full_page_names_the_cursor_for_the_next_one(monkeypatch):
    from eap_api.services import mcp_catalog

    monkeypatch.setattr(mcp_catalog, "entry_json", lambda entry: {"name": entry.name})
    service = McpCatalogService(FakeCatalog(["a", "b", "c"]))
    assert service.search(None, 2, None, None) == {"items": [{"name": "a"}, {"name": "b"}], "next_after": "b"}
    assert service.search(None, 5, None, None)["next_after"] is None  # a short page is the last


def test_an_unknown_entry_is_not_found():
    with pytest.raises(DefinitionNotFound) as raised:
        McpCatalogService(FakeCatalog([])).get("io.github.x/y")
    assert raised.value.detail == "catalog entry not found"
