"""GET /api/v1/kb-sources/adapters: the registered adapters (kind, provider, distribution, display data)."""

from __future__ import annotations

from eap_kb_sources import registry
from eap_kb_sources.fake import FakeCatalogAdapter

from .conftest import get_auth_header


class Described(FakeCatalogAdapter):
    kind = "product_catalog"
    provider = "acme"
    display_name = "Acme Parts"
    connection_kind = "api_token"
    default_base_url = "https://api.acme.test"
    config_schema = {"type": "object", "properties": {"page_size": {"type": "integer"}}}


def test_requires_a_tenant_principal(client):
    assert client.get("/api/v1/kb-sources/adapters").status_code in (401, 403)


def test_lists_the_builtin_github_adapter_with_its_display_data(client):
    items = client.get("/api/v1/kb-sources/adapters", headers=get_auth_header("t-adapters", "alice")).json()["items"]
    github = next(i for i in items if i["provider"] == "github")
    assert github["kind"] == "repository_docs" and github["distribution"] == "eap-kb-sources"
    assert github["display_name"] == "GitHub" and github["connection_kind"] == "api_token"
    assert github["base_url"] == "https://api.github.com" and github["config_schema"] is None


def test_a_registered_adapter_is_listed_with_defaults_for_what_it_does_not_describe(client):
    saved = registry._snapshot()
    try:
        registry.register_adapter("product_catalog", "acme", Described)
        registry.register_adapter("product_catalog", "plain", FakeCatalogAdapter)
        items = client.get("/api/v1/kb-sources/adapters", headers=get_auth_header("t-adapters", "alice")).json()["items"]
    finally:
        registry._restore(saved)
    by = {i["provider"]: i for i in items}
    assert by["acme"] == {
        "kind": "product_catalog", "provider": "acme", "distribution": "local", "display_name": "Acme Parts",
        "connection_kind": "api_token", "base_url": "https://api.acme.test", "token_url": None,
        "config_schema": {"type": "object", "properties": {"page_size": {"type": "integer"}}},
    }
    assert by["plain"]["display_name"] == "plain" and by["plain"]["connection_kind"] == "oauth2_client_credentials"
    assert by["plain"]["base_url"] is None and by["plain"]["config_schema"] is None
