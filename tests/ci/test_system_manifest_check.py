"""scripts/ci/system_manifest.py: `--check` passes on a clean tree and fails on each drift case."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from eap_schemas.capability_gaps import GAPS

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ci" / "system_manifest.py"
spec = importlib.util.spec_from_file_location("ci_system_manifest", SCRIPT)
script = importlib.util.module_from_spec(spec)
sys.modules["ci_system_manifest"] = script
spec.loader.exec_module(script)

MANIFEST = ROOT / "docs" / "platform" / "system-manifest.yaml"


@pytest.fixture(scope="module")
def sources():
    return script.collect_sources()


def check(text, sources, **kw):
    return script.run_checks(ROOT, text, sources, **kw)


def test_a_clean_tree_passes(sources):
    assert check(MANIFEST.read_text(encoding="utf-8"), sources) == []


def test_the_command_line_check_passes_on_a_clean_tree():
    done = subprocess.run([sys.executable, str(SCRIPT), "--check", "--base", "HEAD"], cwd=ROOT, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert "in step with the code" in done.stdout


def test_changing_max_steps_without_regenerating_fails(sources, monkeypatch):
    from eap_schemas import workflows

    monkeypatch.setattr(workflows, "MAX_STEPS", workflows.MAX_STEPS + 1)
    problems = check(MANIFEST.read_text(encoding="utf-8"), sources)
    assert any("generated block is stale" in p for p in problems)


def test_a_new_worker_tool_without_a_registry_entry_fails(sources):
    extra = replace(sources, worker_tool_ids=(*sources.worker_tool_ids, "brand.new_tool"))
    problems = check(MANIFEST.read_text(encoding="utf-8"), extra)
    assert any("brand.new_tool" in p and "no capability registry entry" in p for p in problems)


def test_a_hand_written_string_repeating_a_generated_number_fails(sources):
    text = MANIFEST.read_text(encoding="utf-8").replace("A draft changes nothing", "A workflow has at most 50 steps; a draft changes nothing", 1)
    problems = check(text, sources)
    assert any("repeats the generated number '50'" in p for p in problems)


def test_an_unknown_pattern_need_fails(sources):
    text = MANIFEST.read_text(encoding="utf-8").replace("needs: [feature.evals]", "needs: [feature.not_there]", 1)
    problems = check(text, sources)
    assert any("feature.not_there" in p for p in problems)


def test_a_declared_gap_whose_designed_in_is_missing_fails(sources):
    gaps = tuple(
        g.model_copy(update={"designed_in": "docs/no/such/design.md"}) if g.id == "gap.iam_per_user" else g for g in GAPS
    )
    problems = check(MANIFEST.read_text(encoding="utf-8"), replace(sources, gaps=gaps))
    assert any("gap.iam_per_user" in p and "is not a file on main" in p for p in problems)


def test_hand_written_changes_need_a_higher_manifest_version(sources):
    # The base is the committed text with the previous version number (the code supports the current one).
    current = MANIFEST.read_text(encoding="utf-8")
    base = current.replace("manifest_version: 2", "manifest_version: 1", 1)
    edited = base.replace("Control plane.", "The control plane.", 1)
    assert any("bump it" in p for p in check(edited, sources, base_text=base))
    bumped = edited.replace("manifest_version: 1", "manifest_version: 2", 1)
    assert not any("bump it" in p for p in check(bumped, sources, base_text=base))
    assert not any("bump it" in p for p in check(base, sources, base_text=base))  # nothing changed
    assert not any("bump it" in p for p in check(edited, sources, base_text=None))  # no base to compare with


def test_write_regenerates_a_stale_block(sources):
    from eap_schemas.system_manifest import generate, replace_generated

    base = MANIFEST.read_text(encoding="utf-8")
    stale = base.replace("max_steps: 50", "max_steps: 49", 1)
    assert stale != base and check(stale, sources)
    assert replace_generated(stale, generate(sources.providers, sources.gaps)) == base
