#!/usr/bin/env python3
"""One platform version. The root pyproject.toml `version` is the source of truth (SemVer, 0.x before 1.0).

    scripts/release/bump.py patch|minor|major|X.Y.Z   write the new version everywhere
    scripts/release/bump.py --check                    exit 1 when any file disagrees with the root version
    scripts/release/bump.py --current [--stdin]        print the root version (or the one in a pyproject on stdin)

Every workspace pyproject (apps, packages, plugins; never spikes), apps/studio/package.json and the editable and
virtual package entries of uv.lock carry the same version, so `uv sync --frozen` keeps working after a bump.
`platform_version()` (eap_schemas.system_manifest) appends `+<build sha>` at image build time; that suffix is not
part of this file's version. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
PYPROJECT_GLOBS = ("apps/*/pyproject.toml", "packages/*/pyproject.toml", "plugins/*/pyproject.toml")
PACKAGE_JSON = "apps/studio/package.json"
LOCK = "uv.lock"
# `version = "x"` is the first such line after the `[project]` header and before the next table header.
PROJECT_TABLE = re.compile(r"(?ms)^\[project\]\s*$(?P<body>.*?)(?=^\[|\Z)")
PY_VERSION = re.compile(r'(?m)^(version\s*=\s*)"([^"]*)"')
JSON_VERSION = re.compile(r'(?m)^(\s*"version"\s*:\s*)"([^"]*)"')
# A uv.lock entry for a workspace member: `name`, `version`, then a `source` that is editable or virtual.
LOCK_ENTRY = re.compile(
    r'(?m)^(?P<head>name = "(?P<name>[^"]+)"\nversion = )"(?P<version>[^"]*)"(?P<tail>\nsource = \{ (?:editable|virtual) = )'
)


class BumpError(Exception):
    pass


def parse(version: str) -> tuple[int, int, int]:
    match = SEMVER.match(version)
    if not match:
        raise BumpError(f"{version!r} is not a SemVer version of the form X.Y.Z")
    return int(match[1]), int(match[2]), int(match[3])


def next_version(current: str, level: str) -> str:
    major, minor, patch = parse(current)
    if level == "patch":
        return f"{major}.{minor}.{patch + 1}"
    if level == "minor":
        return f"{major}.{minor + 1}.0"
    if level == "major":
        if major == 0:
            raise BumpError("the platform is pre-1.0: pass the explicit version 1.0.0 to declare it stable")
        return f"{major + 1}.0.0"
    parse(level)
    if parse(level) <= (major, minor, patch):
        raise BumpError(f"{level} is not greater than the current version {current}")
    return level


def _project_version(text: str) -> str | None:
    table = PROJECT_TABLE.search(text)
    match = PY_VERSION.search(table["body"]) if table else None
    return match[2] if match else None


def targets(root: Path) -> list[Path]:
    """Every file that carries the platform version, relative paths sorted, root pyproject first."""
    found = [root / "pyproject.toml"]
    for pattern in PYPROJECT_GLOBS:
        found += sorted(root.glob(pattern))
    for extra in (PACKAGE_JSON, LOCK):
        if (root / extra).exists():
            found.append(root / extra)
    return found


def read_versions(root: Path) -> dict[str, str | None]:
    """Relative path (uv.lock entries as `uv.lock:<package>`) -> the version found there, None when absent."""
    versions: dict[str, str | None] = {}
    for path in targets(root):
        rel = path.relative_to(root).as_posix()
        text = path.read_text()
        if rel == LOCK:
            for entry in LOCK_ENTRY.finditer(text):
                versions[f"{LOCK}:{entry['name']}"] = entry["version"]
        elif rel == PACKAGE_JSON:
            match = JSON_VERSION.search(text)
            versions[rel] = match[2] if match else None
        else:
            versions[rel] = _project_version(text)
    return versions


def root_version(root: Path = ROOT) -> str:
    version = _project_version((root / "pyproject.toml").read_text())
    if not version:
        raise BumpError("the root pyproject.toml has no [project] version")
    parse(version)
    return version


def check(root: Path = ROOT) -> list[str]:
    """Problems found: a file whose version differs from the root's, or that has none."""
    expected = root_version(root)
    problems = []
    for rel, found in read_versions(root).items():
        if found != expected:
            problems.append(f"{rel}: {found or 'no version'} (expected {expected})")
    # A workspace package whose pyproject exists but whose lock entry vanished would hide drift; the lock
    # names every member, so compare the sets.
    lock = root / LOCK
    if lock.exists():
        locked = {k.split(":", 1)[1] for k in read_versions(root) if k.startswith(f"{LOCK}:")}
        for path in targets(root):
            rel = path.relative_to(root).as_posix()
            if rel.endswith("pyproject.toml") and rel != "pyproject.toml":
                match = re.search(r'(?m)^name\s*=\s*"([^"]+)"', PROJECT_TABLE.search(path.read_text())["body"])
                if match and match[1] not in locked:
                    problems.append(f"{LOCK}: no entry for {match[1]} ({rel})")
    return problems


def write(root: Path, version: str) -> list[str]:
    """Write `version` into every target. Returns the relative paths that changed."""
    changed = []
    for path in targets(root):
        rel = path.relative_to(root).as_posix()
        text = path.read_text()
        if rel == LOCK:
            new = LOCK_ENTRY.sub(lambda m: f'{m["head"]}"{version}"{m["tail"]}', text)
        elif rel == PACKAGE_JSON:
            if JSON_VERSION.search(text):
                new = JSON_VERSION.sub(lambda m: f'{m[1]}"{version}"', text, count=1)
            else:
                new = re.sub(r'(?m)^(\s*"name"\s*:\s*"[^"]*",?)\n', lambda m: f'{m[1]}\n  "version": "{version}",\n', text, count=1)
                json.loads(new)
        else:
            table = PROJECT_TABLE.search(text)
            if not table or not PY_VERSION.search(table["body"]):
                raise BumpError(f"{rel}: no [project] version line to update")
            body = PY_VERSION.sub(lambda m: f'{m[1]}"{version}"', table["body"], count=1)
            new = text[: table.start("body")] + body + text[table.end("body") :]
        if new != text:
            path.write_text(new)
            changed.append(rel)
    return changed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("level", nargs="?", help="major, minor, patch, or an explicit X.Y.Z")
    parser.add_argument("--check", action="store_true", help="fail when any file disagrees with the root version")
    parser.add_argument("--current", action="store_true", help="print the root version")
    parser.add_argument("--stdin", action="store_true", help="with --current: read a pyproject.toml from stdin instead (the version a commit had)")
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.current and args.stdin:
            version = _project_version(sys.stdin.read())
            if not version:
                raise BumpError("no [project] version on stdin")
            print(version)
            return 0
        if args.current:
            print(root_version(args.root))
            return 0
        if args.check:
            problems = check(args.root)
            for problem in problems:
                print(f"version drift: {problem}", file=sys.stderr)
            if not problems:
                print(f"all versions agree: {root_version(args.root)}")
            return 1 if problems else 0
        if not args.level:
            parser.error("a level (major, minor, patch) or an explicit version is required")
        current = root_version(args.root)
        problems = check(args.root)
        if problems:
            raise BumpError("versions already disagree; fix that first:\n  " + "\n  ".join(problems))
        version = next_version(current, args.level)
        changed = write(args.root, version)
        print(f"{current} -> {version} ({len(changed)} files)")
        for rel in changed:
            print(f"  {rel}")
        return 0
    except BumpError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
