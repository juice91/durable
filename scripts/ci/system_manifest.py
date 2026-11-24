#!/usr/bin/env python3
"""Keep docs/platform/system-manifest.yaml in step with the code (system-aware Architect, phase B).

    uv run python scripts/ci/system_manifest.py --write   regenerate the generated block (developers run it)
    uv run python scripts/ci/system_manifest.py --check   fail on any drift (CI runs it; no database needed)

`--check` fails when: the generated block differs from what the code and the capability registry produce now;
a `patterns[].needs` id or a known gap's entry does not resolve in the registry; a declared gap's `designed_in`
is missing; a hand-written string repeats a generated number or id; a tool registered in the worker or an MCP
control tool has no registry entry; the hand-written sections changed without a higher `manifest_version` than
the base commit's; or the rendered Platform layer is over its token budget.

The design is docs/superpowers/specs/2026-11-22-architect-system-context-design.md, section 3.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from eap_schemas.capabilities import ControlToolDecl, collect_declared
from eap_schemas.capability_gaps import GAPS, GapDecl
from eap_schemas.system_manifest import (
    MANIFEST_RELATIVE_PATH,
    LoadedManifest,
    generate,
    handwritten_problems,
    parse_manifest,
    platform_layer_over_budget,
    registry_problems,
    replace_generated,
)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASE = "origin/main"


@dataclass(frozen=True)
class Sources:
    """What lives outside eap_schemas: the providers (API), the worker's registered tools and the MCP control tools."""

    providers: Sequence[str]
    worker_tool_ids: Sequence[str] = ()
    control_tools: Sequence[ControlToolDecl] = ()
    gaps: tuple[GapDecl, ...] = field(default=GAPS)


def collect_sources() -> Sources:
    """Read the providers, worker tools and control tools from the code (imports the API and the worker)."""
    from eap_api import control_catalog
    from eap_api.mcp_control.server import build_server
    from eap_api.model_discovery import PROVIDERS
    from eap_runtime._default_registry import default_registry
    from eap_worker.platform_tools import register_platform_tools

    registry = default_registry()
    register_platform_tools(registry)
    build_server()  # registers every control tool; control_catalog records them
    return Sources(
        providers=tuple(p.id for p in PROVIDERS),
        worker_tool_ids=tuple(sorted(registry.tool_ids())),
        control_tools=control_catalog.declared(),
    )


def _git_show(root: Path, ref: str) -> str | None:
    try:
        done = subprocess.run(
            ["git", "-C", str(root), "show", f"{ref}:{MANIFEST_RELATIVE_PATH}"], capture_output=True, text=True, check=False
        )
    except OSError:
        return None
    return done.stdout if done.returncode == 0 else None


def version_problem(current: LoadedManifest, base_text: str | None) -> str | None:
    """CI check 4: hand-written sections changed since the base commit without a higher `manifest_version`."""
    if base_text is None:
        return None  # no base to compare with (a new file, a shallow checkout)
    try:
        base = parse_manifest(base_text)
    except ValueError:
        return None  # the base predates this format
    if current.handwritten_hash == base.handwritten_hash:
        return None
    if current.manifest_version > base.manifest_version:
        return None
    return (
        f"the hand-written sections changed but manifest_version is still {current.manifest_version}"
        f" (the base has {base.manifest_version}): bump it"
    )


def run_checks(
    root: Path, text: str, sources: Sources, *, base_text: str | None = None,
    path_exists: Callable[[str], bool] | None = None,
) -> list[str]:
    """Every problem with the manifest text, empty when it is in step with the code."""
    exists = path_exists or (lambda p: (root / p).is_file())
    problems: list[str] = []
    try:
        loaded = parse_manifest(text)
    except Exception as exc:  # a schema error is a problem report, not a crash
        return [f"the manifest does not parse: {exc}"]
    manifest = loaded.manifest

    fresh = replace_generated(text, generate(sources.providers, sources.gaps))
    if fresh != text:
        problems.append("the generated block is stale: run `uv run python scripts/ci/system_manifest.py --write`")

    entries = collect_declared(sources.control_tools)
    ids = {e.id for e in entries}
    problems += registry_problems(manifest, ids, sources.gaps, exists)
    problems += handwritten_problems(manifest)
    problems += [f"worker tool {t!r} has no capability registry entry" for t in sorted(sources.worker_tool_ids) if f"tool.{t}" not in ids]
    problems += [
        f"MCP control tool {c.name!r} has no capability registry entry" for c in sources.control_tools if f"control.{c.name}" not in ids
    ]
    problem = version_problem(loaded, base_text)
    if problem:
        problems.append(problem)
    over = platform_layer_over_budget(manifest)
    if over:
        problems.append(over)
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="regenerate the generated block")
    mode.add_argument("--check", action="store_true", help="fail on any drift")
    parser.add_argument("--base", default=DEFAULT_BASE, help=f"git ref whose manifest_version the check compares (default {DEFAULT_BASE})")
    args = parser.parse_args(argv)

    path = ROOT / MANIFEST_RELATIVE_PATH
    text = path.read_text(encoding="utf-8")
    sources = collect_sources()
    if args.write:
        fresh = replace_generated(text, generate(sources.providers, sources.gaps))
        if fresh == text:
            print(f"{MANIFEST_RELATIVE_PATH}: already up to date")
        else:
            path.write_text(fresh, encoding="utf-8")
            print(f"{MANIFEST_RELATIVE_PATH}: generated block rewritten")
        return 0
    problems = run_checks(ROOT, text, sources, base_text=_git_show(ROOT, args.base))
    for problem in problems:
        print(f"system manifest: {problem}", file=sys.stderr)
    if problems:
        return 1
    print(f"{MANIFEST_RELATIVE_PATH}: in step with the code")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
