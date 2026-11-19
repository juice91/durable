"""Adapters are plug-ins: distributions declare entry points in the group `eap.kb_sources.adapters`."""

import logging
import sys
import textwrap

import pytest

from eap_kb_sources import registry
from eap_kb_sources.errors import SourceError


@pytest.fixture(autouse=True)
def clean_registry(monkeypatch):
    """Each test starts with an undiscovered registry and restores the real one afterwards."""
    saved = registry._snapshot()
    registry._reset()
    yield
    registry._restore(saved)


def make_dist(tmp_path, monkeypatch, name: str, entry_points: str, module_source: str, module: str | None = None):
    """A throwaway installed distribution: a dist-info with entry_points.txt plus its module, on sys.path."""
    module = module or name.replace("-", "_")
    info = tmp_path / f"{module}-1.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n")
    (info / "entry_points.txt").write_text("[eap.kb_sources.adapters]\n" + entry_points + "\n")
    (tmp_path / f"{module}.py").write_text(textwrap.dedent(module_source))
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, module, raising=False)


PLUGIN = """
from eap_kb_sources import register_adapter
from eap_kb_sources.fake import FakeCatalogAdapter

def register():
    register_adapter("product_catalog", "acme", FakeCatalogAdapter)
"""


def test_a_callable_entry_point_is_discovered_with_its_distribution(tmp_path, monkeypatch):
    make_dist(tmp_path, monkeypatch, "eap-kb-sources-acme", "acme = eap_kb_sources_acme:register", PLUGIN, "eap_kb_sources_acme")
    assert registry.adapter_for("product_catalog", "acme") is not None
    by_key = {(a.kind, a.provider): a.distribution for a in registry.list_adapters()}
    assert by_key[("product_catalog", "acme")] == "eap-kb-sources-acme"
    assert by_key[("repository_docs", "github")] == "eap-kb-sources"  # the built-in adapter


def test_a_module_entry_point_registers_by_import(tmp_path, monkeypatch):
    source = PLUGIN + "\nregister()\n"
    make_dist(tmp_path, monkeypatch, "eap-kb-sources-modacme", "acme = eap_kb_sources_modacme", source, "eap_kb_sources_modacme")
    assert ("product_catalog", "acme") in registry.registered()


def test_discovery_runs_once(tmp_path, monkeypatch):
    make_dist(tmp_path, monkeypatch, "eap-kb-sources-acme", "acme = eap_kb_sources_acme:register", PLUGIN, "eap_kb_sources_acme")
    registry.registered()
    calls = []
    monkeypatch.setattr(registry, "entry_points", lambda **kw: calls.append(kw) or [])
    registry.registered()
    registry.adapter_for("product_catalog", "acme")
    assert calls == []


def test_a_broken_plugin_is_skipped_with_a_warning_naming_its_distribution(tmp_path, monkeypatch, caplog):
    make_dist(tmp_path, monkeypatch, "eap-kb-sources-broken", "broken = eap_kb_sources_broken:register",
              "raise RuntimeError('boom')\n", "eap_kb_sources_broken")
    make_dist(tmp_path, monkeypatch, "eap-kb-sources-acme", "acme = eap_kb_sources_acme:register", PLUGIN, "eap_kb_sources_acme")
    with caplog.at_level(logging.WARNING, logger="eap_kb_sources.registry"):
        kinds = registry.registered()
    assert ("product_catalog", "acme") in kinds and ("repository_docs", "github") in kinds
    assert any("eap-kb-sources-broken" in r.getMessage() for r in caplog.records)


def test_a_plugin_whose_register_raises_is_skipped(tmp_path, monkeypatch, caplog):
    source = "def register():\n    raise ValueError('no')\n"
    make_dist(tmp_path, monkeypatch, "eap-kb-sources-bad", "bad = eap_kb_sources_bad:register", source, "eap_kb_sources_bad")
    with caplog.at_level(logging.WARNING, logger="eap_kb_sources.registry"):
        assert registry.registered()
    assert any("eap-kb-sources-bad" in r.getMessage() for r in caplog.records)


def test_a_duplicate_kind_and_provider_raises_a_clear_error(tmp_path, monkeypatch):
    make_dist(tmp_path, monkeypatch, "eap-kb-sources-one", "acme = eap_kb_sources_one:register", PLUGIN, "eap_kb_sources_one")
    make_dist(tmp_path, monkeypatch, "eap-kb-sources-two", "acme = eap_kb_sources_two:register", PLUGIN, "eap_kb_sources_two")
    with pytest.raises(registry.DuplicateAdapterError) as err:
        registry.registered()
    text = str(err.value)
    assert "product_catalog" in text and "acme" in text and "eap-kb-sources-one" in text and "eap-kb-sources-two" in text
    with pytest.raises(registry.DuplicateAdapterError):  # not silently half-loaded on the next call
        registry.adapter_for("product_catalog", "acme")


def test_a_plugin_cannot_replace_the_builtin_github_adapter(tmp_path, monkeypatch):
    source = PLUGIN.replace('"product_catalog", "acme"', '"repository_docs", "github"')
    make_dist(tmp_path, monkeypatch, "eap-kb-sources-evil", "github = eap_kb_sources_evil:register", source, "eap_kb_sources_evil")
    with pytest.raises(registry.DuplicateAdapterError):
        registry.registered()


def test_a_direct_registration_before_discovery_wins_for_tests(monkeypatch):
    sentinel = object()
    registry.register_adapter("repository_docs", "github", lambda: sentinel)
    assert registry.adapter_for("repository_docs", "github") is sentinel
    assert {(a.kind, a.provider): a.distribution for a in registry.list_adapters()}[("repository_docs", "github")] == "local"


def test_unknown_adapter_is_still_source_adapter_not_found():
    with pytest.raises(SourceError) as err:
        registry.adapter_for("product_catalog", "nope")
    assert err.value.code == "source_adapter_not_found"
