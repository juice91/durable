"""Versioning and release notes (docs/releasing.md): scripts/release/{bump,labels,changelog,public_notes}.py."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RELEASE = ROOT / "scripts" / "release"
FIXTURES = ROOT / "tests" / "fixtures" / "release"
sys.path.insert(0, str(RELEASE))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"release_{name}", RELEASE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"release_{name}"] = module
    spec.loader.exec_module(module)
    return module


bump = _load("bump")
labels = _load("labels")
changelog = _load("changelog")
public_notes = _load("public_notes")


# ---- bump.py on a fixture tree ---------------------------------------------------------------------

LOCK = """version = 1

[[package]]
name = "alpha"
version = "0.1.0"
source = {{ editable = "packages/alpha" }}

[[package]]
name = "api"
version = "0.1.0"
source = {{ editable = "apps/api" }}

[[package]]
name = "beta"
version = "0.1.0"
source = {{ editable = "plugins/beta" }}

[[package]]
name = "requests"
version = "0.1.0"
source = {{ registry = "https://pypi.org/simple" }}

[[package]]
name = "demo"
version = "0.1.0"
source = {{ virtual = "." }}
"""


def _tree(root: Path, version: str = "0.1.0") -> Path:
    def py(path: str, name: str, extra: str = "") -> None:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            f'[build-system]\nrequires = ["hatchling"]\n\n[project]\nname = "{name}"\nversion = "{version}"\n'
            f'description = "x"\n{extra}\n[tool.other]\nversion = "9.9.9"\n'
        )

    py("pyproject.toml", "demo")
    py("packages/alpha/pyproject.toml", "alpha")
    py("apps/api/pyproject.toml", "api")
    py("plugins/beta/pyproject.toml", "beta")
    # A spike keeps its own version: never touched.
    py("spikes/one/pyproject.toml", "one")
    (root / "apps/studio").mkdir(parents=True, exist_ok=True)
    (root / "apps/studio/package.json").write_text(f'{{\n  "name": "@x/studio",\n  "version": "{version}",\n  "private": true\n}}\n')
    (root / "uv.lock").write_text(LOCK.format().replace("0.1.0", version) + "")
    return root


def test_bump_updates_every_workspace_file_and_nothing_else(tmp_path):
    root = _tree(tmp_path)
    assert bump.check(root) == []
    changed = bump.write(root, "0.2.0")
    assert sorted(changed) == [
        "apps/api/pyproject.toml", "apps/studio/package.json", "packages/alpha/pyproject.toml",
        "plugins/beta/pyproject.toml", "pyproject.toml", "uv.lock",
    ]
    assert bump.check(root) == []
    assert bump.root_version(root) == "0.2.0"
    assert json.loads((root / "apps/studio/package.json").read_text())["version"] == "0.2.0"
    lock = (root / "uv.lock").read_text()
    assert lock.count('version = "0.2.0"') == 4  # the editable and the virtual entries
    assert 'name = "requests"\nversion = "0.1.0"' in lock  # a registry package is not ours
    assert 'version = "0.1.0"' in (root / "spikes/one/pyproject.toml").read_text()
    # [tool.*] tables keep their own `version` keys.
    assert 'version = "9.9.9"' in (root / "pyproject.toml").read_text()


def test_bump_adds_a_missing_package_json_version(tmp_path):
    root = _tree(tmp_path)
    (root / "apps/studio/package.json").write_text('{\n  "name": "@x/studio",\n  "private": true\n}\n')
    assert any("package.json" in p for p in bump.check(root))
    bump.write(root, "0.2.0")
    assert bump.check(root) == []


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (lambda r: (r / "packages/alpha/pyproject.toml").write_text((r / "packages/alpha/pyproject.toml").read_text().replace('"0.1.0"', '"0.1.1"', 1)), "packages/alpha/pyproject.toml"),
        (lambda r: (r / "apps/studio/package.json").write_text((r / "apps/studio/package.json").read_text().replace("0.1.0", "0.0.9")), "apps/studio/package.json"),
        (lambda r: (r / "uv.lock").write_text((r / "uv.lock").read_text().replace('name = "alpha"\nversion = "0.1.0"', 'name = "alpha"\nversion = "0.3.0"')), "uv.lock:alpha"),
        (lambda r: (r / "plugins/beta/pyproject.toml").write_text((r / "plugins/beta/pyproject.toml").read_text().replace('version = "0.1.0"\n', "", 1)), "plugins/beta/pyproject.toml"),
    ],
)
def test_check_detects_drift(tmp_path, mutate, expected):
    root = _tree(tmp_path)
    mutate(root)
    problems = bump.check(root)
    assert any(p.startswith(expected) for p in problems), problems
    assert bump.main(["--check", "--root", str(root)]) == 1


def test_check_detects_a_workspace_member_missing_from_the_lock(tmp_path):
    root = _tree(tmp_path)
    (root / "uv.lock").write_text((root / "uv.lock").read_text().replace('name = "alpha"', 'name = "gone"'))
    assert any("no entry for alpha" in p for p in bump.check(root))


def test_next_version_levels_and_pre_one_policy():
    assert bump.next_version("0.1.0", "patch") == "0.1.1"
    assert bump.next_version("0.1.5", "minor") == "0.2.0"
    assert bump.next_version("1.4.2", "major") == "2.0.0"
    assert bump.next_version("0.9.0", "1.0.0") == "1.0.0"
    with pytest.raises(bump.BumpError):
        bump.next_version("0.9.0", "major")  # declaring 1.0 is an explicit act
    with pytest.raises(bump.BumpError):
        bump.next_version("0.2.0", "0.2.0")
    with pytest.raises(bump.BumpError):
        bump.next_version("0.2.0", "0.1.9")
    with pytest.raises(bump.BumpError):
        bump.next_version("0.2.0", "v0.3.0")


def test_cli_bump_refuses_when_files_already_disagree(tmp_path, capsys):
    root = _tree(tmp_path)
    (root / "apps/api/pyproject.toml").write_text((root / "apps/api/pyproject.toml").read_text().replace("0.1.0", "0.1.2", 1))
    assert bump.main(["minor", "--root", str(root)]) == 2
    assert bump.root_version(root) == "0.1.0"


def test_the_real_repository_agrees_on_one_version():
    """The CI gate (`bump.py --check`): root, apps, packages, plugins, studio and the lock say the same."""
    assert bump.check(ROOT) == [], bump.check(ROOT)


def test_platform_version_keeps_the_sha_suffix():
    from eap_schemas.system_manifest import platform_version

    assert platform_version("0.2.0", "abcdef1234567890") == "0.2.0+abcdef1234567890"
    assert platform_version(bump.root_version(ROOT), "") == f"{bump.root_version(ROOT)}+dev"


# ---- labels.py -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title, files, expected",
    [
        ("feat(studio): Settings as tabs", (), "feature"),
        ("fix(api): idempotent submit", (), "fix"),
        ("fix(architect),feat(mcp): discover tools", (), "fix"),
        ("docs: pluggable IAM design", (), "docs"),
        ("ci: pnpm installs into the temp dir", (), "infra"),
        ("deploy(k8s): project a token", (), "infra"),
        ("test: timing caps", (), "internal"),
        ("chore(export): tooling", (), "internal"),
        ("Fix vulnerable JS dependencies", (), "security"),
        ("images: fix fixable CRITICAL CVEs in the runner images", (), "security"),
        ("feat(auth): reject CVE-2026-1234 style tokens", (), "security"),
        ("docs: explain the CVE process", (), "docs"),
        ("Tidy things", ("docs/a.md", "README.md"), "docs"),
        ("Tidy things", (".github/workflows/x.yml", "deploy/a.sh"), "infra"),
        ("Tidy things", ("tests/a.py",), "internal"),
        ("Tidy things", ("docs/a.md", "apps/api/x.py"), None),
        ("Tidy things", (), None),
        ("Studio parity round 2: schedules", (), None),
    ],
)
def test_label_mapping(title, files, expected):
    assert labels.classify(title, files) == expected


def test_category_of_prefers_the_first_category_and_ignores_other_labels():
    assert labels.category_of(["ci:full", "fix"]) == "fix"
    assert labels.category_of(["ci:full"]) is None
    assert labels.category_of(["internal", "feature"]) == "feature"


def test_the_label_set_matches_the_release_config():
    import yaml

    config = yaml.safe_load((ROOT / ".github" / "release.yml").read_text())["changelog"]
    listed = {label for cat in config["categories"] for label in cat["labels"]} - {"*"}
    assert listed | set(config["exclude"]["labels"]) >= set(labels.CATEGORIES)
    assert "internal" in config["exclude"]["labels"]
    assert {c["title"] for c in config["categories"]} >= {"Features", "Fixes", "Security", "Docs"}


# ---- changelog.py with a recorded PR-list fixture ---------------------------------------------------

REPO = "acme/widget"


def _prs():
    return changelog.load_prs(json.loads((FIXTURES / "prs.json").read_text()))


def test_sections_ordering_and_internal_handling():
    groups = changelog.categorise(_prs())
    public = changelog.render_public("0.2.0", "2026-11-23", groups, REPO)
    internal = changelog.render_internal("0.2.0", "2026-11-23", groups, REPO)
    link = lambda n: f"([#{n}](https://github.com/{REPO}/pull/{n}))"  # noqa: E731
    assert public == "\n".join([
        "## [0.2.0] - 2026-11-23", "",
        "### Features", "",
        f"- Agents as tools: a supervisor calls published agents {link(7)}",
        f"- **studio:** Settings as tabs {link(21)}",
        f"- **workflows:** A run can end as skipped {link(41)}",
        "",
        "### Fixes", "",
        f"- Fix the spacing in a title {link(5)}",
        f"- **api:** Eval run double submit is idempotent {link(12)}",
        f"- **studio:** Dropdowns accept mouse selection {link(42)}",
        "",
        "### Security", "",
        f"- Fix vulnerable JS dependencies (CVE-2026-0001) {link(15)}",
        "",
        "### Documentation", "",
        f"- Pluggable IAM design {link(9)}",
        "",
        "### Infrastructure", "",
        f"- Skip docs-only heavy runs {link(30)}",
        f"- **k8s:** Project the provider token into the pods {link(43)}",
        "",
        "### Other changes", "",
        f"- Tidy things that no rule classifies {link(44)}",
        "",
    ])
    # Internal pull requests never appear in the public section; they are listed on their own.
    assert "internal" not in public.lower() and "layering" not in public and "timing caps" not in public
    assert "release: v0.1.1" not in public + internal
    assert internal == "\n".join([
        "## [0.2.0] - 2026-11-23", "", "### Internal", "",
        f"- Timing caps tolerate a loaded runner {link(31)}",
        f"- API layering: runs routes go through services {link(40)}",
        f"- **export:** Tooling lives in its own directory {link(45)}",
    ]) + "\n"


@pytest.mark.parametrize(
    "title, shown",
    [
        ("feat(studio): Settings as tabs", "**studio:** Settings as tabs"),
        ("fix(architect),feat(mcp): discover tools", "**architect, mcp:** Discover tools"),
        ("docs,tests(kb-sources): the framework", "**kb-sources:** The framework"),
        ("feat(tools): http.request general tool", "**tools:** http.request general tool"),
        ("fix(api): fastmcp-slim[server] is a dependency", "**api:** fastmcp-slim[server] is a dependency"),
        ("ci: skip docs-only runs", "Skip docs-only runs"),
        ("Layering: empty the ratchets", "Layering: empty the ratchets"),
        ("workflow.history: skip items", "workflow.history: skip items"),
        ("Fix  spacing   here", "Fix spacing here"),
    ],
)
def test_display_title(title, shown):
    assert changelog.display_title(title) == shown


def test_build_is_deterministic_and_independent_of_input_order():
    data = json.loads((FIXTURES / "prs.json").read_text())
    first = changelog.render_public("0.2.0", "2026-11-23", changelog.categorise(changelog.load_prs(data)), REPO)
    second = changelog.render_public("0.2.0", "2026-11-23", changelog.categorise(changelog.load_prs(list(reversed(data)))), REPO)
    assert first == second


def test_overrides_classify_titles_no_rule_can_but_never_beat_a_label():
    groups = changelog.categorise(_prs(), {"44": "fix", "21": "internal"})
    assert [p.number for p in groups["fix"]] == [5, 12, 42, 44]
    assert 21 in [p.number for p in groups["feature"]]  # the label wins over the override
    assert None not in groups


def test_an_empty_release_says_so():
    assert "No user-facing changes." in changelog.render_public("0.2.1", "2026-12-01", {}, REPO)


def test_changelog_file_round_trip_and_links(tmp_path):
    groups = changelog.categorise(_prs())
    text = changelog.insert_section("", changelog.render_public("0.2.0", "2026-11-23", groups, REPO), "0.2.0", REPO, changelog.HEADER)
    assert text.index("## [Unreleased]") < text.index("## [0.2.0]")
    assert f"[0.2.0]: https://github.com/{REPO}/releases/tag/v0.2.0" in text
    assert f"[Unreleased]: https://github.com/{REPO}/compare/v0.2.0...HEAD" in text
    second = changelog.insert_section(text, changelog.render_public("0.3.0", "2026-12-01", {}, REPO), "0.3.0", REPO, changelog.HEADER, "0.2.0")
    assert second.index("## [0.3.0]") < second.index("## [0.2.0]")
    assert f"[0.3.0]: https://github.com/{REPO}/compare/v0.2.0...v0.3.0" in second
    assert f"[Unreleased]: https://github.com/{REPO}/compare/v0.3.0...HEAD" in second
    assert changelog.latest_version(second) == "0.3.0"
    body = changelog.extract_section(second, "0.2.0")
    assert body.startswith("### Features") and "## [" not in body and "]: https://" not in body
    with pytest.raises(ValueError):
        changelog.insert_section(second, "x", "0.3.0", REPO, changelog.HEADER)
    with pytest.raises(KeyError):
        changelog.extract_section(second, "9.9.9")


def test_cli_build_from_the_fixture_writes_both_files(tmp_path):
    result = subprocess.run(
        [sys.executable, str(RELEASE / "changelog.py"), "--root", str(tmp_path), "build", "--version", "0.2.0", "--date", "2026-11-23",
         "--repo", REPO, "--prs", str(FIXTURES / "prs.json"), "--write"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    public = (tmp_path / "CHANGELOG.md").read_text()
    internal = (tmp_path / "CHANGELOG.internal.md").read_text()
    assert "### Features" in public and "### Internal" not in public
    assert "### Internal" in internal and "layering" in internal
    assert "layering" not in public


# ---- public notes: replacement rules and the residual-string scan --------------------------------------

PLANTED = "internal-host.corp.example"


@pytest.fixture
def rules(tmp_path):
    replacements = tmp_path / "replacements.txt"
    replacements.write_text(
        "Acme Corp==>Example-Org\n"
        "regex:[ ]*\\(\\[#[0-9]+\\]\\(https://github\\.com/acme/widget/pull/[0-9]+\\)\\)==>\n"
        "regex:https://github\\.com/acme/widget/(compare|releases/tag)/==>https://github.com/public/widget/\\1/\n"
    )
    patterns = tmp_path / "patterns.txt"
    patterns.write_text("internal-host\\.corp\\.example\n@private\\.example\n")
    return replacements, patterns


NOTES = """## [0.2.0] - 2026-11-23

### Features

- Studio: Acme Corp theme ([#7](https://github.com/acme/widget/pull/7))

### Internal

- Tooling for the Acme Corp deploy ([#8](https://github.com/acme/widget/pull/8))

### Fixes

- Fix a crash ([#9](https://github.com/acme/widget/pull/9))

[0.2.0]: https://github.com/acme/widget/releases/tag/v0.2.0
"""


def test_sanitize_strips_internal_applies_replacements_and_drops_private_pr_links(rules):
    replacements, patterns = rules
    clean = public_notes.sanitize(NOTES, public_notes.read_replacements(replacements), public_notes.read_patterns(patterns))
    assert "Internal" not in clean and "Tooling for" not in clean
    assert "Example-Org theme" in clean and "Acme" not in clean
    assert "pull/" not in clean  # private pull request numbers mean nothing publicly
    assert "- Fix a crash\n" in clean
    assert "[0.2.0]: https://github.com/public/widget/releases/tag/v0.2.0" in clean


LINKED = """## [Unreleased]

## [0.5.0] - 2026-01-05

## [0.4.0] - 2026-01-04

## [0.3.0] - 2026-01-03

## [0.2.0] - 2026-01-02

[Unreleased]: https://github.com/public/widget/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/public/widget/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/public/widget/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/public/widget/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/public/widget/releases/tag/v0.2.0
"""


def test_links_never_point_at_a_version_the_public_repository_has_no_tag_for():
    clean = public_notes.sanitize(LINKED, [], [], unpublished={"0.2.0", "0.4.0"})
    assert clean.split("\n\n")[-1].splitlines() == [
        "[Unreleased]: https://github.com/public/widget/compare/v0.5.0...HEAD",
        "[0.5.0]: https://github.com/public/widget/compare/v0.3.0...v0.5.0",
        "[0.3.0]: https://github.com/public/widget/releases/tag/v0.3.0",
    ]
    assert "## [0.4.0] - 2026-01-04" in clean and "## [0.2.0] - 2026-01-02" in clean  # the sections stay
    assert public_notes.sanitize(LINKED, [], []) == LINKED


def test_the_unpublished_file_is_read_by_the_command(rules, tmp_path):
    replacements, patterns = rules
    changelog_file, unpublished = tmp_path / "CHANGELOG.md", tmp_path / "unpublished.txt"
    changelog_file.write_text(LINKED)
    unpublished.write_text("# never tagged\n0.4.0\n")
    argv = ["sanitize", str(changelog_file), "--replacements", str(replacements), "--patterns", str(patterns)]
    assert public_notes.main([*argv, "--unpublished", str(unpublished), "--write"]) == 0
    assert "v0.4.0" not in changelog_file.read_text()
    assert "compare/v0.3.0...v0.5.0" in changelog_file.read_text()


def test_a_planted_internal_string_blocks_the_public_release(rules, tmp_path, capsys):
    replacements, patterns = rules
    changelog_file = tmp_path / "CHANGELOG.md"
    planted = NOTES.replace("Fix a crash", f"Fix a crash on {PLANTED}")
    changelog_file.write_text(planted)
    with pytest.raises(public_notes.Blocked) as blocked:
        public_notes.sanitize(planted, public_notes.read_replacements(replacements), public_notes.read_patterns(patterns))
    assert blocked.value.hits and PLANTED not in str(blocked.value)
    argv = ["sanitize", str(changelog_file), "--replacements", str(replacements), "--patterns", str(patterns)]
    assert public_notes.main([*argv, "--write"]) == 1
    assert changelog_file.read_text() == planted  # nothing was rewritten
    err = capsys.readouterr().err
    assert "BLOCKED" in err and PLANTED not in err  # the log never repeats the string
    # The same hit blocks when only one version's section is checked, and an e-mail pattern blocks too.
    assert public_notes.main([*argv, "--version", "0.2.0"]) == 1
    changelog_file.write_text(NOTES.replace("Fix a crash", "Thanks dev@private.example"))
    assert public_notes.main(argv) == 1


def test_a_clean_changelog_passes_and_is_rewritten(rules, tmp_path):
    replacements, patterns = rules
    path = tmp_path / "CHANGELOG.md"
    path.write_text(NOTES)
    argv = ["sanitize", str(path), "--replacements", str(replacements), "--patterns", str(patterns)]
    assert public_notes.main([*argv, "--write"]) == 0
    assert "Internal" not in path.read_text() and "Acme" not in path.read_text()


def test_a_replacement_that_hides_the_string_means_no_hit(tmp_path):
    replacements = tmp_path / "r.txt"
    replacements.write_text(f"{PLANTED}==>host.example\n")
    patterns = tmp_path / "p.txt"
    patterns.write_text("internal-host\\.corp\\.example\n")
    clean = public_notes.sanitize(f"- see {PLANTED}\n", public_notes.read_replacements(replacements), public_notes.read_patterns(patterns))
    assert clean == "- see host.example\n"


def test_the_real_export_rules_load_and_the_scan_catches_a_planted_name():
    export = ROOT / "deploy" / "export"
    if not export.exists():
        pytest.skip("the export directory is operator-only and absent from the public tree")
    replacements = public_notes.read_replacements(export / "replacements.txt")
    patterns = public_notes.read_patterns(export / "residual-patterns.txt")
    assert replacements and patterns
    # A string the rules rewrite is fine; one they do not rewrite but the scan knows (an operator
    # address) blocks. Built from parts so this file itself carries no scan pattern.
    address = "someone@" + "gmail" + ".com"
    with pytest.raises(public_notes.Blocked):
        public_notes.sanitize(f"- thanks {address}\n", [], patterns)
    assert public_notes.sanitize("- plain note\n", replacements, patterns) == "- plain note\n"


def test_the_real_changelog_is_public_safe_shape():
    path = ROOT / "CHANGELOG.md"
    text = path.read_text()
    assert text.startswith("# Changelog") and "## [Unreleased]" in text
    assert "### Internal" not in text


def test_public_workflow_and_export_wiring():
    export = ROOT / "deploy" / "export"
    if not export.exists():
        pytest.skip("operator-only")
    excluded = (export / "exclude-paths.txt").read_text().split()
    for path in ("CHANGELOG.internal.md", ".github/workflows/release.yml", ".github/workflows/release-tag.yml"):
        assert path in excluded, path
    # The public-release workflow and the notes tooling are exported (they are not on the exclusion list).
    assert ".github/workflows/release-public.yml" not in excluded
    assert not {"scripts/release/changelog.py", "scripts/release/public_notes.py", "scripts/release/bump.py", "scripts/release/labels.py"} & set(excluded)
    script = (export / "run-export.sh").read_text()
    assert "public_notes.py" in script and "residual-patterns.txt" in script and "BLOCKED" in script
    assert script.index("public_notes.py") < script.index("git push")


def test_every_cli_prints_help():
    for name in ("bump", "changelog", "labels", "public_notes", "backfill_labels"):
        if not (RELEASE / f"{name}.py").exists():  # the private label backfill is not exported
            continue
        result = subprocess.run([sys.executable, str(RELEASE / f"{name}.py"), "--help"], capture_output=True, text=True, check=False)
        assert result.returncode == 0, (name, result.stderr)


def test_export_excludes_root_readme_only():
    """The public repo has its own README: only the root file is dropped, package READMEs are still exported."""
    export = ROOT / "deploy" / "export"
    if not export.exists():
        pytest.skip("operator-only")
    excluded = (export / "exclude-paths.txt").read_text().split()
    assert "README.md" in excluded
    assert not [p for p in excluded if p.endswith("README.md") and p != "README.md" and p != "deploy/README.md"]

    filter_repo = shutil.which("git-filter-repo")
    if filter_repo is None:
        pytest.skip("git filter-repo not installed")
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
        def git(*args):
            subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, env=env)
        git("init", "-q")
        for rel in ("README.md", "packages/db/README.md", "plugins/iam-zitadel/README.md"):
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / rel).write_text("x\n")
        git("add", "-A")
        git("commit", "-q", "-m", "init")
        args = ["git", "filter-repo", "--force", "--invert-paths"]
        for p in excluded:
            args += ["--path", p]
        subprocess.run(args, cwd=repo, check=True, capture_output=True, env=env)
        kept = subprocess.run(["git", "ls-files"], cwd=repo, check=True, capture_output=True, text=True).stdout.split()
        assert "README.md" not in kept
        assert "packages/db/README.md" in kept and "plugins/iam-zitadel/README.md" in kept
