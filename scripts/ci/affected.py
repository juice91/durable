#!/usr/bin/env python3
"""Select the Python tests a change affects, from the uv workspace graph.

Standard library only (no uv import), so it runs before `uv sync` and on any runner.

    python3 scripts/ci/affected.py [--base REF] [--head REF] [--files-from FILE]
        [--restrict-to PATH ...] [--full] [--format json|table|github] [--summary FILE]

The changed files are `git diff <merge-base(base, head)> <head>`. Each file maps to a workspace
member (or to a rule), the set grows to every member that depends on a changed member (transitively,
through [project].dependencies, [dependency-groups], [project.optional-dependencies] and the workspace
entries of [tool.uv.sources]), and the output lists the test directories of the affected members
with the reason for each.

Rules, first match wins:
  * docs/, apps/docs/, examples/, spikes/, .planning/, any *.md       -> nothing to test
  * apps/studio/ and the pnpm files                                    -> Studio only, no Python tests
  * tests/deploy/, deploy/, .dockerignore, other .github/ files        -> the deploy checks only
  * root pyproject.toml, uv.lock, .github/workflows/ci*.yml, scripts/ci/, any conftest.py,
    the rest of tests/ (tests/deploy is deploy-only), packages/db (its pytest plugin runs in every session), and any path no
    rule knows (config/, infrastructure/, scripts/, ...)                -> everything
  * a path inside a workspace member                                   -> that member and its dependents
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

NOTHING_PREFIXES = ("docs/", "apps/docs/", "examples/", "spikes/", ".planning/")
STUDIO_FILES = {"package.json", "pnpm-lock.yaml", "pnpm-workspace.yaml", ".npmrc"}
STUDIO_PREFIXES = ("apps/studio/",)
DEPLOY_PREFIXES = ("tests/deploy/", "deploy/")
DEPLOY_FILES = {".dockerignore"}
EVERYTHING_FILES = {"pyproject.toml", "uv.lock"}
EVERYTHING_PREFIXES = ("scripts/ci/", "tests/")
# Members whose change affects every Python test: eap-db registers a pytest plugin (eap_db.testing)
# that every session loads and that migrates the throwaway schema.
GLOBAL_MEMBERS = {"eap-db"}
# The plugin builds its metadata from the model modules of eap-api and eap-worker (eap_db/metadata.py
# imports eap_api.models, eap_api.*.models and eap_worker.tickets; a test keeps this list in step), so a change to those files
# affects every session too. Other eap-api and eap-worker changes do not.
SCHEMA_FILE = re.compile(
    r"^apps/api/src/eap_api/(?:mcp_servers/|workflows/)?models\.py$|^apps/worker/src/eap_worker/tickets\.py$"
)
# Test-only imports the pyproject files do not declare: the tests of the key import the packages
# of the values, so a change to a value reruns the key's tests. The edge is one step: the key's
# own dependents do not rerun (nothing of the key's code changed), which keeps one cross-import
# in a test from pulling in the whole graph.
TEST_ONLY_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    # The API suite exercises the IAM plug-ins through the identity chain (the key, dev bearer and policy acceptance tests).
    "eap-api": ("eap-worker", "eap-iam-apikey", "eap-iam-cfaccess", "eap-iam-devloopback", "eap-iam-openaccess", "eap-iam-zitadel"),
    "eap-pricing": ("eap-api", "eap-worker"),
    "eap-schemas": ("eap-agent-runtime", "eap-worker"),
}

_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass
class Member:
    name: str
    path: str  # posix, relative to the repository root
    dependencies: set[str] = field(default_factory=set)
    test_dependencies: set[str] = field(default_factory=set)
    test_dirs: list[str] = field(default_factory=list)


def _requirement_name(requirement: str) -> str | None:
    match = _NAME.match(requirement)
    return normalize(match.group(1)) if match else None


def _requirements(data: dict) -> list[str]:
    found: list[str] = list(data.get("project", {}).get("dependencies", []))
    for extra in data.get("project", {}).get("optional-dependencies", {}).values():
        found.extend(extra)
    for group in data.get("dependency-groups", {}).values():
        found.extend(item for item in group if isinstance(item, str))
    return found


def load_workspace(root: Path) -> dict[str, Member]:
    root_data = tomllib.loads((root / "pyproject.toml").read_text())
    workspace = root_data.get("tool", {}).get("uv", {}).get("workspace", {})
    excluded = workspace.get("exclude", [])
    members: dict[str, Member] = {}
    raw: dict[str, set[str]] = {}
    for pattern in workspace.get("members", []):
        for directory in sorted(root.glob(pattern)):
            relative = directory.relative_to(root).as_posix()
            if not (directory / "pyproject.toml").is_file():
                continue
            if any(fnmatch.fnmatch(relative, ex) for ex in excluded):
                continue
            data = tomllib.loads((directory / "pyproject.toml").read_text())
            name = normalize(data["project"]["name"])
            names = {n for r in _requirements(data) if (n := _requirement_name(r))}
            sources = data.get("tool", {}).get("uv", {}).get("sources", {})
            names |= {
                normalize(source)
                for source, spec in sources.items()
                if isinstance(spec, dict) and spec.get("workspace")
            }
            tests = sorted(
                child.relative_to(root).as_posix()
                for child in directory.iterdir()
                if child.is_dir() and (child.name == "tests" or child.name.endswith("_tests"))
            )
            members[name] = Member(name=name, path=relative, test_dirs=tests)
            raw[name] = names
    for name, names in raw.items():
        members[name].dependencies = {n for n in names if n in members and n != name}
    for name, extra in TEST_ONLY_DEPENDENCIES.items():
        if name in members:
            members[name].test_dependencies = {n for n in extra if n in members}
    return members


def reverse_closure(members: dict[str, Member], seeds: dict[str, str]) -> dict[str, str]:
    """Every member that depends, transitively, on a seed -> the reason it is affected."""
    reasons = dict(seeds)
    queue = list(seeds)
    while queue:
        current = queue.pop(0)
        for member in members.values():
            if current in member.dependencies and member.name not in reasons:
                reasons[member.name] = f"depends on {current}"
                queue.append(member.name)
    for member in members.values():
        if member.name not in reasons:
            hit = sorted(member.test_dependencies & set(reasons))
            if hit:
                reasons[member.name] = f"its tests import {hit[0]}"
    return reasons


@dataclass
class Result:
    full: bool = False
    studio: bool = False
    deploy: bool = False
    changed_files: list[str] = field(default_factory=list)
    file_reasons: dict[str, str] = field(default_factory=dict)
    file_kinds: dict[str, str] = field(default_factory=dict)
    members: dict[str, str] = field(default_factory=dict)  # name -> reason
    member_paths: dict[str, str] = field(default_factory=dict)
    test_paths: list[str] = field(default_factory=list)
    restricted_to: list[str] | None = None
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "full": self.full,
            "has_python_tests": bool(self.test_paths),
            "studio": self.studio,
            "deploy": self.deploy,
            "test_paths": self.test_paths,
            "members": [
                {"name": n, "path": self.member_paths[n], "reason": r} for n, r in self.members.items()
            ],
            "changed_files": self.changed_files,
            "file_reasons": self.file_reasons,
            "restricted_to": self.restricted_to,
            "notes": self.notes,
        }


def classify(path: str, members: dict[str, Member]) -> tuple[str, str | None, str]:
    """(kind, member name, reason) for one changed file; kind is nothing|studio|deploy|everything|member."""
    if path.startswith(NOTHING_PREFIXES) or path.endswith(".md"):
        return "nothing", None, "docs, examples or notes: no tests"
    if path.startswith(STUDIO_PREFIXES) or path in STUDIO_FILES:
        return "studio", None, "Studio: no Python tests"
    if path.startswith(DEPLOY_PREFIXES) or path in DEPLOY_FILES:
        return "deploy", None, "deployment files: the deploy checks"
    if path.startswith(".github/") and not re.fullmatch(r"\.github/workflows/ci[^/]*\.yml", path):
        return "deploy", None, "workflow or runner config: the deploy checks"
    if path in EVERYTHING_FILES:
        return "everything", None, f"{path} changes the whole dependency set"
    if re.fullmatch(r"\.github/workflows/ci[^/]*\.yml", path):
        return "everything", None, "CI workflow change"
    if path.startswith("scripts/ci/"):
        return "everything", None, "CI selection script change"
    if path.rsplit("/", 1)[-1] == "conftest.py":
        return "everything", None, "conftest change"
    if path.startswith("tests/"):
        return "everything", None, "shared tests tree"
    if SCHEMA_FILE.match(path):
        return "everything", None, "database model module (eap_db builds its metadata from it)"
    for member in members.values():
        if path == member.path or path.startswith(member.path + "/"):
            if member.name in GLOBAL_MEMBERS:
                return "everything", None, f"{member.name} (its pytest plugin loads in every session)"
            return "member", member.name, f"in {member.path}"
    return "everything", None, "path no rule knows: running everything to be safe"


def select(
    root: Path,
    files: list[str],
    restrict_to: list[str] | None = None,
    force_full: bool = False,
) -> Result:
    members = load_workspace(root)
    result = Result(changed_files=sorted(files), restricted_to=restrict_to)
    result.member_paths = {n: m.path for n, m in members.items()}
    seeds: dict[str, str] = {}
    if force_full:
        result.full = True
        result.notes.append("full run requested")
    for path in result.changed_files:
        kind, member, reason = classify(path, members)
        result.file_reasons[path] = reason
        result.file_kinds[path] = kind
        if re.fullmatch(r"\.github/workflows/ci[^/]*\.yml", path):
            result.studio = True
        if kind == "everything":
            result.full = True
        elif kind == "studio":
            result.studio = True
        elif kind == "deploy":
            result.deploy = True
        elif kind == "member" and member:
            seeds.setdefault(member, f"changed ({path})")
    if result.full:
        result.members = {n: "full run" for n in members}
        result.deploy = True
    else:
        result.members = reverse_closure(members, seeds)
    candidates: list[str] = []
    for name in members:
        if name in result.members:
            candidates.extend(members[name].test_dirs)
    if result.deploy:
        candidates.append("tests/deploy")
    if restrict_to is None:
        result.test_paths = candidates
    else:
        owned = [
            p
            for p in restrict_to
            if any(p == c or p.startswith(c + "/") or c.startswith(p + "/") for c in candidates)
        ]
        result.test_paths = owned
    return result


def table(result: Result) -> str:
    lines = []
    if result.full:
        why = {r for p, r in result.file_reasons.items() if result.file_kinds[p] == "everything"}
        lines.append("Full run: " + "; ".join(sorted(why) or result.notes))
    lines.append(f"Changed files: {len(result.changed_files)}")
    lines.append("")
    lines.append("| Member | Why |")
    lines.append("| --- | --- |")
    for name, reason in result.members.items():
        lines.append(f"| {name} | {reason} |")
    if not result.members:
        lines.append("| (none) | no workspace member affected |")
    lines.append("")
    lines.append("Test paths: " + (" ".join(result.test_paths) if result.test_paths else "(none)"))
    lines.append(f"Studio: {'yes' if result.studio else 'no'}; deploy checks: {'yes' if result.deploy else 'no'}")
    return "\n".join(lines) + "\n"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout


def default_base(root: Path) -> str:
    candidates = []
    if os.environ.get("GITHUB_BASE_REF"):
        candidates.append(f"origin/{os.environ['GITHUB_BASE_REF']}")
    candidates += ["origin/main", "main"]
    for ref in candidates:
        if subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", ref], cwd=root, capture_output=True
        ).returncode == 0:
            return ref
    raise RuntimeError("no base ref found (tried " + ", ".join(candidates) + ")")


def changed_files(root: Path, base: str | None, head: str) -> list[str]:
    base = base or default_base(root)
    merge_base = _git(root, "merge-base", base, head).strip()
    out = _git(root, "diff", "--name-only", "--no-renames", "-z", merge_base, head)
    return [p for p in out.split("\0") if p]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--base", help="base ref (default: origin/$GITHUB_BASE_REF, origin/main, main)")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--files-from", help="read changed files from this file ('-' = stdin) instead of git")
    parser.add_argument("--restrict-to", nargs="*", help="the test paths the calling job runs; the output keeps only the affected ones")
    parser.add_argument("--full", action="store_true", help="force a full run")
    parser.add_argument("--format", choices=("json", "table", "github"), default="json")
    parser.add_argument("--summary", help="append the human table to this file (for $GITHUB_STEP_SUMMARY)")
    args = parser.parse_args(argv)
    root = Path(args.root)
    try:
        if args.files_from:
            text = sys.stdin.read() if args.files_from == "-" else Path(args.files_from).read_text()
            files = [line.strip() for line in text.splitlines() if line.strip()]
        else:
            files = changed_files(root, args.base, args.head)
    except (subprocess.CalledProcessError, RuntimeError) as error:
        # Fail safe: if the change set cannot be computed, run everything.
        print(f"affected.py: cannot compute the change set ({error}); running everything", file=sys.stderr)
        files = []
        args.full = True
    result = select(root, files, args.restrict_to, force_full=args.full)
    if args.summary:
        with open(args.summary, "a") as handle:
            handle.write(table(result))
    if args.format == "json":
        print(json.dumps(result.to_json(), indent=2))
    elif args.format == "table":
        print(table(result), end="")
    else:
        print(f"has_tests={'true' if result.test_paths else 'false'}")
        print(f"full={'true' if result.full else 'false'}")
        print(f"studio={'true' if result.studio else 'false'}")
        print(f"paths={' '.join(result.test_paths)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
