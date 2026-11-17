import pytest
from pydantic import ValidationError

from eap_schemas import CatalogEntry, CatalogPackage, EnvVarSpec, McpServerSpec


def test_catalog_entry_parses_registry_shape():
    e = CatalogEntry.model_validate({
        "name": "io.example/echo", "version": "1.0.0", "unknown": 1,
        "packages": [{"registry_type": "npm", "identifier": "@x/echo", "version": "1.0.0",
                      "transport": "stdio",
                      "environment_variables": [{"name": "KEY", "required": True, "secret": True}]}],
    })
    assert e.packages[0].environment_variables[0] == EnvVarSpec(name="KEY", required=True, secret=True)
    assert e.status == "active" and e.is_latest and not e.hidden


def test_package_registry_type_is_closed():
    with pytest.raises(ValidationError):
        CatalogPackage(registry_type="gem", identifier="x")


def test_external_server_needs_url_managed_must_not_have_one():
    assert McpServerSpec(name="a", url="https://x").runtime == "external"
    assert McpServerSpec(name="a", runtime="managed", catalog_name="io.example/echo").url is None
    with pytest.raises(ValidationError):
        McpServerSpec(name="a")
    with pytest.raises(ValidationError):
        McpServerSpec(name="a", runtime="managed", url="https://x")
