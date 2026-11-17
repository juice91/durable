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
