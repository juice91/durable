"""The catalog entry's network block travels into the package document and the runtime spec (no database)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from eap_api.mcp_servers import managed
from eap_schemas import CatalogPackage

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "toolhive_network_entries.json").read_text())
PKG = CatalogPackage(registry_type="npm", identifier="@x/y", version="1.0.0", transport="stdio")


def meta_of(name: str) -> dict:
    """The Task 1 meta shape for an upstream ToolHive entry: network {allow_host, allow_port, insecure_allow_all}."""
    entry = next(e for e in FIXTURE if e["name"].endswith("/" + name))
    inner = next(iter(entry["_meta"]["io.modelcontextprotocol.registry/publisher-provided"]["io.github.stacklok"].values()))
    outbound = (inner.get("permissions") or {}).get("network", {}).get("outbound")
    if outbound is None:
        return {"network": None}
    return {"network": {
        "allow_host": outbound.get("allow_host", []),
        "allow_port": outbound.get("allow_port", []),
        "insecure_allow_all": outbound.get("insecure_allow_all", False),
    }}


def test_playwright_network_is_carried_into_the_package_document() -> None:
    _, document = managed.package_document(PKG, meta_of("playwright"))
    assert document["network"] == {"allow_host": [], "allow_port": [443], "insecure_allow_all": True}


def test_an_entry_with_listed_hosts_carries_them() -> None:
    _, document = managed.package_document(PKG, meta_of("osv"))
    assert document["network"] == {"allow_host": ["api.osv.dev"], "allow_port": [443], "insecure_allow_all": False}


def test_an_empty_block_is_kept_as_no_network_and_a_missing_one_is_absent() -> None:
    _, empty = managed.package_document(PKG, meta_of("memory"))
    assert empty["network"] == {"allow_host": [], "allow_port": [], "insecure_allow_all": False}
    _, none = managed.package_document(PKG, None)
    assert "network" not in none
    _, no_block = managed.package_document(PKG, {"network": None})
    assert "network" not in no_block
    _, junk = managed.package_document(PKG, {"network": "everything"})
    assert "network" not in junk


def test_build_spec_reads_the_network_from_the_package_document() -> None:
    _, document = managed.package_document(PKG, meta_of("playwright"))
    row = SimpleNamespace(tenant_id="t", server_id="s", image="img", package=document)
    spec = managed.build_spec(row, SimpleNamespace(name="srv"), {})
    assert spec.network == document["network"]
    _, plain = managed.package_document(PKG)
    row.package = plain
    assert managed.build_spec(row, SimpleNamespace(name="srv"), {}).network is None


# ---- OCI identifiers that carry their own tag or digest (ToolHive entries have no separate version) ----


def test_oci_identifier_with_a_tag_installs_without_a_separate_version() -> None:
    from eap_api.mcp_servers.managed import package_document
    from eap_schemas.mcp_catalog import CatalogPackage

    package = CatalogPackage(registry_type="oci", identifier="mcr.microsoft.com/playwright/mcp:v0.0.83", transport="stdio")
    image, document = package_document(package)
    assert image == "mcr.microsoft.com/playwright/mcp:v0.0.83"
    assert document["version"] == "v0.0.83" and document["identifier"] == "mcr.microsoft.com/playwright/mcp:v0.0.83"


def test_oci_identifier_with_a_digest_installs_without_a_separate_version() -> None:
    from eap_api.mcp_servers.managed import package_document
    from eap_schemas.mcp_catalog import CatalogPackage

    digest = "sha256:" + "a" * 64
    package = CatalogPackage(registry_type="oci", identifier=f"ghcr.io/stacklok/fetch@{digest}", transport="streamable-http")
    image, document = package_document(package)
    assert image == f"ghcr.io/stacklok/fetch@{digest}" and document["version"] == digest


def test_oci_identifier_without_tag_or_version_is_refused() -> None:
    import pytest
    from eap_api.mcp_servers.managed import package_document
    from eap_mcp_runtime.images import UnsupportedPackage
    from eap_schemas.mcp_catalog import CatalogPackage

    with pytest.raises(UnsupportedPackage, match="no version"):
        package_document(CatalogPackage(registry_type="oci", identifier="localhost:5000/tool", transport="stdio"))
    with pytest.raises(UnsupportedPackage, match="no version"):
        package_document(CatalogPackage(registry_type="npm", identifier="@scope/tool", transport="stdio"))
