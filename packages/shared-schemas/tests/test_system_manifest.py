"""The system manifest (phase B): loads, hashes, resolves generated values, renders under budget, stays free of
hard-coded numbers in the Architect's seed text, and the version has the `<version>+<sha>` form."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from eap_schemas import system_manifest as sm
from eap_schemas.architect_seed import ARCHITECT_INSTRUCTIONS
from eap_schemas.capabilities import collect_declared
from eap_schemas.system_manifest import (
    LoadedManifest,
    generate,
    generated_numbers,
    handwritten_problems,
    load_manifest,
    parse_manifest,
    platform_layer_over_budget,
    platform_version,
    registry_problems,
    render_platform_layer,
    replace_generated,
)

REPO = Path(__file__).resolve().parents[3]
PROVIDERS = ("anthropic", "cloudflare", "litellm", "openai")


def text() -> str:
    return (REPO / sm.MANIFEST_RELATIVE_PATH).read_text(encoding="utf-8")


def with_generated(raw: str) -> str:
    return replace_generated(raw, generate(PROVIDERS))


def test_the_committed_manifest_loads_and_its_generated_block_matches_the_code():
    raw = text()
    loaded = load_manifest(REPO / sm.MANIFEST_RELATIVE_PATH)
    assert loaded.manifest_version == 2
    assert loaded.manifest.generated.limits["workflow"]["max_steps"] > 0
    # the providers come from the API, so only the rest of the block is compared here
    block = yaml.safe_load(raw)["generated"]
    fresh = generate(block["providers"])
    assert yaml.safe_load(with_generated(raw))["generated"] == yaml.safe_load(replace_generated(raw, fresh))["generated"]
    assert block == yaml.safe_load(yaml.safe_dump(fresh))


def test_the_hash_is_stable_across_two_runs_and_changes_with_a_generated_value(monkeypatch):
    raw = text()
    first, second = parse_manifest(raw), parse_manifest(raw)
    assert first.manifest_hash == second.manifest_hash and re.fullmatch(r"[0-9a-f]{64}", first.manifest_hash)
    assert parse_manifest(with_generated(raw)).manifest_hash == parse_manifest(with_generated(raw)).manifest_hash
    from eap_schemas import workflows

    monkeypatch.setattr(workflows, "MAX_STEPS", workflows.MAX_STEPS + 1)
    changed = parse_manifest(with_generated(raw))
    monkeypatch.undo()
    assert changed.manifest_hash != parse_manifest(with_generated(raw)).manifest_hash
    assert changed.handwritten_hash == parse_manifest(with_generated(raw)).handwritten_hash  # only the generated part moved


def test_the_hand_written_hash_changes_with_the_hand_written_text():
    raw = text()
    edited = raw.replace("Control plane.", "The control plane.", 1)
    assert edited != raw
    assert parse_manifest(edited).handwritten_hash != parse_manifest(raw).handwritten_hash


def test_the_platform_layer_renders_under_its_budget():
    loaded = parse_manifest(text())
    assert platform_layer_over_budget(loaded.manifest) is None
    assert render_platform_layer(loaded.manifest).startswith("A durable platform")


def test_a_platform_layer_over_budget_is_reported():
    manifest = parse_manifest(text()).manifest
    tight = manifest.model_copy(update={"generated": manifest.generated.model_copy(update={"context_budget": {**manifest.generated.context_budget, "platform": 10}})})
    assert "over its budget of 10" in platform_layer_over_budget(tight)


def test_a_placeholder_resolves_to_the_generated_value_and_an_unknown_one_is_refused():
    manifest = parse_manifest(text()).manifest
    steps = manifest.generated.limits["workflow"]["max_steps"]
    assert manifest.resolve("at most {{limits.workflow.max_steps}} steps") == f"at most {steps} steps"
    with pytest.raises(ValueError):
        manifest.resolve("{{limits.workflow.nope}}")
    with pytest.raises(ValueError):
        manifest.resolve("{{limits.workflow}}")


def test_hand_written_text_may_not_repeat_a_generated_number_or_id():
    manifest = parse_manifest(text()).manifest
    assert handwritten_problems(manifest) == []
    steps = manifest.generated.limits["workflow"]["max_steps"]
    bad = parse_manifest(text().replace("A draft changes nothing", f"At most {steps} steps; a draft changes nothing", 1)).manifest
    assert any(str(steps) in p for p in handwritten_problems(bad))
    kib = parse_manifest(text().replace("A draft changes nothing", "Outputs stop at 32 KiB; a draft changes nothing", 1)).manifest
    assert "32" in generated_numbers(kib.generated) and handwritten_problems(kib)
    with_id = parse_manifest(text().replace("A draft changes nothing", "Use http.request; a draft changes nothing", 1)).manifest
    assert any("http.request" in p for p in handwritten_problems(with_id))
    placeholder = parse_manifest(text().replace("A draft changes nothing", "Up to {{limits.workflow.max_steps}} steps; a draft changes nothing", 1)).manifest
    assert handwritten_problems(placeholder) == []


def test_an_unknown_key_or_an_undeclared_relationship_end_is_refused():
    with pytest.raises(ValueError):
        parse_manifest(text().replace("manifest_version: 2", "manifest_version: 2\nsurprise: true", 1))
    with pytest.raises(ValueError):
        parse_manifest(text().replace("target: worker, via: \"a durable queue", "target: nowhere, via: \"a durable queue", 1))


def test_patterns_and_gaps_must_resolve_in_the_registry(tmp_path):
    manifest = parse_manifest(text()).manifest
    ids = {e.id for e in collect_declared()}
    from eap_schemas.capability_gaps import GAPS

    assert registry_problems(manifest, ids, GAPS, lambda p: (REPO / p).is_file()) == []
    broken = parse_manifest(text().replace("needs: [feature.evals]", "needs: [feature.no_such_thing]", 1)).manifest
    assert any("feature.no_such_thing" in p for p in registry_problems(broken, ids, GAPS, lambda p: True))


def test_platform_version_has_the_version_plus_sha_form(monkeypatch):
    monkeypatch.delenv(sm.BUILD_SHA_ENV, raising=False)
    local = platform_version()
    assert re.fullmatch(r"\d+(\.\d+)*\+dev", local)
    assert platform_version("0.1.0", "a1b2c3d") == "0.1.0+a1b2c3d"
    monkeypatch.setenv(sm.BUILD_SHA_ENV, "a1b2c3d4e5f6")
    assert re.fullmatch(r"\d+(\.\d+)*\+a1b2c3d4e5f6", platform_version())
    assert platform_version("0.1.0", "") == "0.1.0+dev"
    assert platform_version("0.1.0", "bad sha/../x") == "0.1.0+badshax"


def test_the_manifest_path_follows_the_environment(monkeypatch, tmp_path):
    copy = tmp_path / "m.yaml"
    copy.write_text(text(), encoding="utf-8")
    monkeypatch.setenv(sm.MANIFEST_PATH_ENV, str(copy))
    assert sm.default_manifest_path() == copy
    assert isinstance(load_manifest(), LoadedManifest)
    assert sm.default_manifest_path("x.yaml") == Path("x.yaml")


# ------------------------------------------------------------------ the seed text carries no generated numbers


def test_architect_instructions_contain_none_of_the_generated_numbers_or_any_number_at_all():
    generated = parse_manifest(text()).manifest.generated
    flat = " ".join(ARCHITECT_INSTRUCTIONS.split())
    numbers = {str(s).replace(",", "") for scalar in (generated.limits, generated.context_budget) for s in _flat(scalar)}
    for n in sorted(numbers):
        if n.isdigit():
            assert not re.search(rf"(?<![\w.]){re.escape(n)}(?![\w])", flat.replace(",", "")), f"the seed text repeats {n}"
    for kib in ("32 KiB", "4 KiB", "2 MiB", "1048576", "50,000", "{MAX_STEPS}"):
        assert kib not in flat
    assert not re.search(r"(?<![\w$.])\d+(?![\w])", flat.replace("RE2", "")), re.findall(r"(?<![\w$.])\d+(?![\w])", flat)
    assert "list_workflow_step_kinds" in flat and "get_capability" in flat


def _flat(node):
    if isinstance(node, dict):
        for v in node.values():
            yield from _flat(v)
    else:
        yield node


def test_the_manifest_states_the_step_naming_convention():
    manifest = parse_manifest(text()).manifest
    rules = " ".join(manifest.lifecycle.rules)
    assert "<subject>_gate" in rules and "ret_<reason>" in rules and "40 characters" in rules
    for example in ("check_gate", "login_gate", "richtext_gate", "ret_posted", "ret_dry", "ret_check_failed", "fetch_rss", "post_thumb"):
        assert example in rules
    assert handwritten_problems(manifest) == []
    assert platform_layer_over_budget(manifest) is None
