#!/usr/bin/env python3
"""Build and read CHANGELOG.md (Keep a Changelog layout) from the pull requests merged since the last tag.

    changelog.py build --version 0.2.0 [--date YYYY-MM-DD] [--write]   next section from merged PRs (stdout, or the files)
    changelog.py section --version 0.2.0 [--internal]                  one version's body (the GitHub Release text)
    changelog.py latest                                                the newest version heading in CHANGELOG.md

Two files, so the public export can leave the internal one out by path:
  CHANGELOG.md           Features, Fixes, Security, Documentation, Infrastructure, Other changes (public)
  CHANGELOG.internal.md  the `Internal` pull requests (tests, refactors, export tooling): upstream only

`build` reads merged pull requests through `gh` (or `--prs FILE`, a recorded `gh pr list --json
number,title,labels,mergedAt,mergeCommit` result, which is how the tests run it offline). Output is deterministic:
fixed section order, pull requests by number, the date from `--date`. A pull request with no category label is
classified from its title prefix (labels.py); one that still matches nothing is listed under "Other changes".
Standard library only.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import labels as label_rules  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
CHANGELOG = "CHANGELOG.md"
INTERNAL = "CHANGELOG.internal.md"
# Public sections in display order; None collects what no rule classified.
SECTIONS: tuple[tuple[str | None, str], ...] = (
    ("feature", "Features"),
    ("fix", "Fixes"),
    ("security", "Security"),
    ("docs", "Documentation"),
    ("infra", "Infrastructure"),
    (None, "Other changes"),
)
INTERNAL_HEADING = "Internal"
HEADER = """# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html); see `docs/releasing.md`.

## [Unreleased]
"""
INTERNAL_HEADER = """# Internal changelog

Pull requests labelled `internal` (tests, refactors, tooling). Upstream only: the public export leaves this file out.

## [Unreleased]
"""
VERSION_HEADING = re.compile(r"(?m)^## \[(\d+\.\d+\.\d+)\][^\n]*$")
LINK_REF = re.compile(r"(?m)^\[[^\]]+\]: \S+$\n?")
TITLE_PREFIX = re.compile(r"^(?P<types>[A-Za-z]+(?:\([^)]*\))?(?:,\s*[A-Za-z]+(?:\([^)]*\))?)*)!?:\s*(?P<rest>.+)$")
TYPE_SCOPE = re.compile(r"([A-Za-z]+)(?:\(([^)]*)\))?")


@dataclass(frozen=True)
class PullRequest:
    number: int
    title: str
    labels: tuple[str, ...] = ()
    merge_commit: str = ""

    @property
    def category(self) -> str | None:
        return label_rules.category_of(self.labels)


def load_prs(raw: Iterable[Mapping]) -> list[PullRequest]:
    prs = []
    for item in raw:
        commit = item.get("mergeCommit") or {}
        names = tuple(sorted(label["name"] if isinstance(label, dict) else str(label) for label in item.get("labels", [])))
        prs.append(PullRequest(int(item["number"]), str(item["title"]), names, commit.get("oid", "") if isinstance(commit, dict) else ""))
    return sorted(prs, key=lambda pr: pr.number)


def categorise(prs: Iterable[PullRequest], overrides: Mapping[str, str] | None = None) -> dict[str | None, list[PullRequest]]:
    """Group by category: the label wins, then an override, then the title-prefix rule. Release PRs are left out."""
    groups: dict[str | None, list[PullRequest]] = {}
    for pr in prs:
        if pr.title.lower().startswith("release:"):
            continue
        category = pr.category or (overrides or {}).get(str(pr.number)) or label_rules.classify(pr.title)
        groups.setdefault(category, []).append(pr)
    return groups


def display_title(title: str) -> str:
    """`feat(studio): Settings as tabs` -> `**studio:** Settings as tabs`, `ci: x` -> `X`, `fix(a),feat(b): x` ->
    `**a, b:** X`: the section heading already says what kind of change it is. A title without a conventional
    prefix is kept, whitespace collapsed. A first word that looks like an identifier (`http.request`) keeps its case."""
    title = " ".join(title.split())
    match = TITLE_PREFIX.match(title)
    if not match:
        return title
    parts = TYPE_SCOPE.findall(match["types"])
    if not all(kind.lower() in label_rules.PREFIX_LABELS for kind, _ in parts):
        return title
    scopes = list(dict.fromkeys(scope for _, scope in parts if scope))
    rest = match["rest"]
    first = rest.split()[0]
    if first.isalpha() and first.islower():
        rest = rest[0].upper() + rest[1:]
    return f"**{', '.join(scopes)}:** {rest}" if scopes else rest


def entry(pr: PullRequest, repo: str) -> str:
    return f"- {display_title(pr.title)} ([#{pr.number}](https://github.com/{repo}/pull/{pr.number}))"


def render_public(version: str, date: str, groups: Mapping[str | None, Sequence[PullRequest]], repo: str) -> str:
    lines = [f"## [{version}] - {date}", ""]
    empty = True
    for key, heading in SECTIONS:
        prs = groups.get(key, [])
        if prs:
            empty = False
            lines += [f"### {heading}", "", *(entry(pr, repo) for pr in prs), ""]
    if empty:
        lines += ["No user-facing changes.", ""]
    return "\n".join(lines)


def render_internal(version: str, date: str, groups: Mapping[str | None, Sequence[PullRequest]], repo: str) -> str:
    prs = groups.get("internal", [])
    lines = [f"## [{version}] - {date}", "", f"### {INTERNAL_HEADING}", ""]
    lines += [entry(pr, repo) for pr in prs] or ["No internal changes."]
    return "\n".join(lines) + "\n"


def _links(text: str, repo: str, previous: str | None) -> str:
    """Rewrite the reference links at the bottom: one per version heading, newest first, plus [Unreleased]."""
    versions = VERSION_HEADING.findall(text)
    body = LINK_REF.sub("", text).rstrip("\n")
    refs = []
    if versions:
        refs.append(f"[Unreleased]: https://github.com/{repo}/compare/v{versions[0]}...HEAD")
    for index, version in enumerate(versions):
        older = versions[index + 1] if index + 1 < len(versions) else previous if index == 0 and previous else None
        if older and older != version:
            refs.append(f"[{version}]: https://github.com/{repo}/compare/v{older}...v{version}")
        else:
            refs.append(f"[{version}]: https://github.com/{repo}/releases/tag/v{version}")
    return body + "\n\n" + "\n".join(refs) + "\n"


def insert_section(text: str, section: str, version: str, repo: str, header: str, previous: str | None = None) -> str:
    """Put `section` directly under `## [Unreleased]`. A version that is already there is an error."""
    text = text or header
    if f"## [{version}]" in text:
        raise ValueError(f"version {version} is already in the changelog")
    marker = re.search(r"(?m)^## \[Unreleased\][^\n]*\n", text)
    if not marker:
        text = text.rstrip("\n") + "\n\n## [Unreleased]\n"
        marker = re.search(r"(?m)^## \[Unreleased\][^\n]*\n", text)
    head, tail = text[: marker.end()], text[marker.end() :]
    return _links(head.rstrip("\n") + "\n\n" + section.rstrip("\n") + "\n\n" + tail.lstrip("\n"), repo, previous)


def extract_section(text: str, version: str) -> str:
    """The body of `## [version]`, without its heading and the reference links."""
    headings = list(VERSION_HEADING.finditer(text))
    for index, heading in enumerate(headings):
        if heading[1] == version:
            end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
            return LINK_REF.sub("", text[heading.end() : end]).strip("\n") + "\n"
    raise KeyError(f"version {version} is not in the changelog")


def latest_version(text: str) -> str | None:
    match = VERSION_HEADING.search(text)
    return match[1] if match else None


def _run(command: Sequence[str], cwd: Path = ROOT) -> str:
    return subprocess.run(command, check=True, capture_output=True, text=True, cwd=cwd).stdout


def last_tag(root: Path = ROOT) -> str | None:
    try:
        return _run(["git", "describe", "--tags", "--abbrev=0", "--match", "v[0-9]*"], root).strip() or None
    except subprocess.CalledProcessError:
        return None


def fetch_prs(repo: str, since_tag: str | None, root: Path = ROOT) -> list[PullRequest]:
    """Merged pull requests on main whose merge commit is not reachable from `since_tag`."""
    raw = json.loads(
        _run(["gh", "pr", "list", "--repo", repo, "--state", "merged", "--base", "main", "--limit", "5000", "--json", "number,title,labels,mergedAt,mergeCommit"], root)
    )
    prs = load_prs(raw)
    if since_tag is None:
        return prs
    new_commits = set(_run(["git", "rev-list", f"{since_tag}..HEAD"], root).split())
    return [pr for pr in prs if pr.merge_commit in new_commits]


def _repo(args: argparse.Namespace) -> str:
    if args.repo:
        return args.repo
    url = _run(["git", "remote", "get-url", "origin"]).strip()
    match = re.search(r"github\.com[:/](.+?)(?:\.git)?$", url)
    if not match:
        raise SystemExit("cannot work out the repository; pass --repo OWNER/NAME")
    return match[1]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build")
    build.add_argument("--version", required=True)
    build.add_argument("--date", default=datetime.datetime.now(datetime.UTC).date().isoformat())
    build.add_argument("--repo")
    build.add_argument("--prs", type=Path, help="a recorded `gh pr list --json ...` file instead of the API")
    build.add_argument("--since-tag", help="default: the newest v* tag, or the whole history when there is none")
    build.add_argument("--previous", help="previous version for the compare link (default: derived from --since-tag)")
    build.add_argument("--overrides", type=Path, help="JSON {pr number: label} for pull requests no rule classifies")
    build.add_argument("--write", action="store_true", help="update CHANGELOG.md and CHANGELOG.internal.md")
    show = sub.add_parser("section")
    show.add_argument("--version", required=True)
    show.add_argument("--internal", action="store_true", help="read CHANGELOG.internal.md instead")
    sub.add_parser("latest")
    args = parser.parse_args(argv)
    root: Path = args.root
    if args.command == "latest":
        version = latest_version((root / CHANGELOG).read_text())
        print(version or "")
        return 0 if version else 1
    if args.command == "section":
        try:
            sys.stdout.write(extract_section((root / (INTERNAL if args.internal else CHANGELOG)).read_text(), args.version))
        except (KeyError, FileNotFoundError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        return 0
    if not re.fullmatch(r"\d+\.\d+\.\d+", args.version):
        print(f"error: {args.version!r} is not X.Y.Z", file=sys.stderr)
        return 2
    repo = _repo(args)
    since = args.since_tag if args.since_tag is not None else (None if args.prs else last_tag(root))
    prs = load_prs(json.loads(args.prs.read_text())) if args.prs else fetch_prs(repo, since, root)
    overrides = json.loads(args.overrides.read_text()) if args.overrides else {}
    groups = categorise(prs, overrides)
    public = render_public(args.version, args.date, groups, repo)
    internal = render_internal(args.version, args.date, groups, repo)
    previous = args.previous or (since[1:] if since and since.startswith("v") else None)
    if not args.write:
        sys.stdout.write(public + "\n" + internal)
        return 0
    for name, section, header in ((CHANGELOG, public, HEADER), (INTERNAL, internal, INTERNAL_HEADER)):
        path = root / name
        path.write_text(insert_section(path.read_text() if path.exists() else "", section, args.version, repo, header, previous))
    print(f"wrote {args.version}: {sum(len(v) for v in groups.values())} pull requests")
    return 0


if __name__ == "__main__":
    sys.exit(main())
