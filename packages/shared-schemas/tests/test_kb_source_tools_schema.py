"""kb source sync (Task 4): the five functional tool declarations, the Architect denylist, and no provider
identifier in the public layers."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from eap_schemas.architect import ARCHITECT_DENIED_TOOL_PREFIXES, is_denied_tool, offer_tool
from eap_schemas.tools import TOOL_DECLARATIONS

IDS = ("kb.source.get", "settings.connection.check", "kb.source.sync", "workflow.run.wait", "workflow.run.result")
ROOT = Path(__file__).resolve().parents[3]


def test_declarations_and_input_models():
    for tool_id in IDS:
        assert TOOL_DECLARATIONS[tool_id].id == tool_id
    assert {t: TOOL_DECLARATIONS[t].side_effect for t in IDS} == {
        "kb.source.get": False, "settings.connection.check": False, "kb.source.sync": True,
        "workflow.run.wait": False, "workflow.run.result": False}
    assert TOOL_DECLARATIONS["kb.source.sync"].external_idempotent is True
    sync = TOOL_DECLARATIONS["kb.source.sync"].input_model
    assert sync(source_id="s", mode="targeted", part_numbers=["LM317"]).part_numbers == ["LM317"]
    with pytest.raises(ValidationError):
        sync(source_id="s", mode="hourly")
    with pytest.raises(ValidationError):
        sync(source_id="s", mode="full", extra=1)
    wait = TOOL_DECLARATIONS["workflow.run.wait"].input_model
    with pytest.raises(ValidationError):
        wait(run_id="r", wait_seconds=0)
    with pytest.raises(ValidationError):
        wait(run_id="r", wait_seconds=301)
    assert wait(run_id="r", wait_seconds=1).wait_seconds == 1 and wait(run_id="r", wait_seconds=300)


def test_architect_denies_the_management_tool_prefixes():
    for prefix in ("kb.", "settings.", "workflow."):
        assert prefix in ARCHITECT_DENIED_TOOL_PREFIXES
    for tool_id in IDS:
        assert is_denied_tool(tool_id)
        decl = TOOL_DECLARATIONS[tool_id]
        assert offer_tool(decl.id, decl.title, decl.description, side_effect=decl.side_effect) is None
    # created agents are still offered ordinary tools
    assert offer_tool("clock.now", "Clock", "now") is not None


PUBLIC_LAYERS = (
    "packages/shared-schemas/src/eap_schemas",
    "apps/api/src",
    "apps/studio/src",
    "apps/worker/src/eap_worker/platform_tools.py",
    "apps/worker/src/eap_worker/kb_source_tools.py",
)
# A hostname such as `https://vendor.example/x.pdf` (a test fixture) is not an identifier: it follows `://` or `.`.
TI_IDENTIFIER = re.compile(r"(?<![.@])\bti\.[a-z_]+")
HOST_PREFIX = re.compile(r"://ti\.")
SUFFIXES = {".py", ".ts", ".tsx", ".json", ".yaml", ".yml", ".md"}
# Not provider identifiers: file names (`foo.ti.ts`), the word "acme_catalog" never matches \bti\.
ALLOWED = ("providers/ti",)


def _files(rel: str):
    path = ROOT / rel
    if not path.exists():
        return []
    if path.is_file():
        return [path]
    return [p for p in path.rglob("*") if p.is_file() and p.suffix in SUFFIXES and "node_modules" not in p.parts]


def test_no_ti_identifier_in_the_public_layers():
    hits = []
    for rel in PUBLIC_LAYERS:
        for path in _files(rel):
            for n, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
                scrubbed = HOST_PREFIX.sub("://", line)
                for allowed in ALLOWED:
                    scrubbed = scrubbed.replace(allowed, "")
                if TI_IDENTIFIER.search(scrubbed):
                    hits.append(f"{path.relative_to(ROOT)}:{n}")
    assert hits == [], hits


GITHUB_IDENTIFIER = re.compile(r"(?<![.@/\w-])github\.[a-z_]+\.[a-z_]+|[\"'`/]github\.[a-z_]+[\"'`]|/github/")


def test_no_github_tool_or_route_in_the_public_layers():
    """The GitHub adapter is a provider behind `kb.*` / `settings.*` / `workflow.*`: no `github.*` tool id and
    no `/github` route in the public layers (the host names github.com and api.github.com are not identifiers)."""
    hits = []
    for rel in PUBLIC_LAYERS:
        for path in _files(rel):
            for n, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
                scrubbed = re.sub(r"(api\.)?github\.com|githubusercontent\.com", "", line)
                if GITHUB_IDENTIFIER.search(scrubbed):
                    hits.append(f"{path.relative_to(ROOT)}:{n}")
    assert hits == [], hits
