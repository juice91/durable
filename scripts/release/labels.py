#!/usr/bin/env python3
"""Release-note labels: the mapping from a pull request's title prefix or changed paths to one label.

Used by three callers, so the rules live in one place:
  - the `labeler.yml` workflow (`labels.py apply --pr N`) labels a pull request that carries no category label;
  - `backfill_labels.py` labels the pull requests merged before the labeler existed;
  - `changelog.py` classifies a pull request that still has no category label when it builds a section.

The categories are the labels `.github/release.yml` groups GitHub's generated notes by:
feature, fix, security, docs, infra, internal. `classify` returns one of them or None (the notes then list
the pull request under "Other changes"). A pull request that already has a category label is never relabelled.

No third-party imports: the workflow runs it with the runner's own Python.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Sequence

CATEGORIES: tuple[str, ...] = ("feature", "fix", "security", "docs", "infra", "internal")

# Conventional-commit type (the word before `(scope)` and `:`) -> label. The first type of a list such as
# `fix(architect),feat(mcp): ...` decides.
PREFIX_LABELS: dict[str, str] = {
    "feat": "feature",
    "feature": "feature",
    "fix": "fix",
    "bugfix": "fix",
    "hotfix": "fix",
    "security": "security",
    "sec": "security",
    "docs": "docs",
    "doc": "docs",
    "ci": "infra",
    "build": "infra",
    "deploy": "infra",
    "infra": "infra",
    "images": "infra",
    "chore": "internal",
    "test": "internal",
    "tests": "internal",
    "refactor": "internal",
    "style": "internal",
    "perf": "internal",
    "spike": "internal",
}
SECURITY_WORDS = re.compile(r"\b(CVE-\d{4}-\d+|CVEs?|vulnerab\w*|security advisory)\b", re.IGNORECASE)
PREFIX = re.compile(r"^\s*(?P<type>[A-Za-z]+)(?:\([^)]*\))?(?:,\s*[A-Za-z]+(?:\([^)]*\))?)*!?\s*:")

# Path rules apply only when the title has no known prefix. A pull request is labelled by paths only when
# every changed file falls in the same category; a mixed change stays unlabelled rather than guessed.
DOC_PATHS = ("docs/", "README", "CHANGELOG", "LICENSE", "NOTICE", "THIRD_PARTY_NOTICES")
INFRA_PATHS = (".github/", "deploy/", "infrastructure/", "scripts/ci/", "scripts/release/", "Dockerfile")
INTERNAL_PATHS = ("tests/", "spikes/", ".planning/")


def _path_category(path: str) -> str | None:
    if path.startswith(INTERNAL_PATHS) or "/tests/" in path or "_tests/" in path:
        return "internal"
    if path.startswith(INFRA_PATHS):
        return "infra"
    if path.startswith(DOC_PATHS) or path.endswith(".md"):
        return "docs"
    return None


def classify(title: str, files: Iterable[str] = ()) -> str | None:
    """The category label for a pull request, from its title prefix, a few security words and its changed paths."""
    match = PREFIX.match(title)
    if match:
        label = PREFIX_LABELS.get(match.group("type").lower())
        if label:
            # A change whose title names a vulnerability is a security change, whatever its type.
            if label not in ("docs", "internal") and SECURITY_WORDS.search(title):
                return "security"
            return label
    if SECURITY_WORDS.search(title):
        return "security"
    paths = [f for f in files if f]
    if paths:
        categories = {_path_category(p) for p in paths}
        if len(categories) == 1:
            return categories.pop()
    return None


def category_of(labels: Iterable[str]) -> str | None:
    """The category among a pull request's existing labels (the first in CATEGORIES order), or None."""
    present = set(labels)
    return next((c for c in CATEGORIES if c in present), None)


def _gh(args: Sequence[str]) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def apply(repo: str, number: int, dry_run: bool = False) -> str | None:
    """Label pull request `number` when it has no category label. The title and files are read through the API
    (never interpolated into a shell), so a hostile title cannot do anything but pick a label from the table."""
    data = json.loads(_gh(["pr", "view", str(number), "--repo", repo, "--json", "title,labels,files"]))
    existing = [label["name"] for label in data.get("labels", [])]
    if category_of(existing):
        print(f"#{number}: already labelled {category_of(existing)}")
        return None
    label = classify(data["title"], [f["path"] for f in data.get("files", [])])
    if label is None:
        print(f"#{number}: no rule matches, left unlabelled")
        return None
    print(f"#{number}: {label}")
    if not dry_run:
        _gh(["pr", "edit", str(number), "--repo", repo, "--add-label", label])
    return label


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("apply", help="label one pull request when it has no category label")
    one.add_argument("--pr", type=int, required=True)
    one.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    one.add_argument("--dry-run", action="store_true")
    check = sub.add_parser("classify", help="print the label for a title (and optional paths)")
    check.add_argument("title")
    check.add_argument("paths", nargs="*")
    args = parser.parse_args(argv)
    if args.command == "classify":
        print(classify(args.title, args.paths) or "")
        return 0
    if not args.repo:
        parser.error("--repo or GITHUB_REPOSITORY is required")
    apply(args.repo, args.pr, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
