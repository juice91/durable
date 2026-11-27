"""scripts/ci/affected.py: which Python tests a change affects."""

from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ci" / "affected.py"

spec = importlib.util.spec_from_file_location("ci_affected", SCRIPT)
affected = importlib.util.module_from_spec(spec)
sys.modules["ci_affected"] = affected
spec.loader.exec_module(affected)


def _member(root: Path, path: str, name: str, deps=(), group_deps=(), sources=(), tests=("tests",)):
    directory = root / path
    directory.mkdir(parents=True)
    dep_lines = ", ".join(f'"{d}>=1"' for d in deps)
    group_lines = ", ".join(f'"{d}"' for d in group_deps)
    source_lines = "\n".join(f"{s} = {{ workspace = true }}" for s in sources)
    (directory / "pyproject.toml").write_text(
        textwrap.dedent(
            f"""
            [project]
            name = "{name}"
            version = "0.1.0"
            dependencies = [{dep_lines}]
            [dependency-groups]
            dev = ["pytest", {group_lines}]
            [tool.uv.sources]
            {source_lines}
            """
        )
    )
    for test in tests:
        (directory / test).mkdir()
        (directory / test / "test_x.py").write_text("")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    # base <- mid <- top ; base <- lib (dev group only) ; solo has no edges ; Eap_Odd uses an underscore name
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "root"\nversion = "0"\n[tool.uv.workspace]\nmembers = ["packages/*", "apps/api"]\n'
    )
    _member(tmp_path, "packages/base", "base-pkg", tests=("tests",))
    _member(tmp_path, "packages/mid", "mid-pkg", deps=("base_pkg",), sources=("base-pkg",), tests=("mid_tests",))
    _member(tmp_path, "packages/lib", "lib", group_deps=("base-pkg[extra]",), tests=("lib_tests", "tests"))
    _member(tmp_path, "packages/solo", "solo", tests=("solo_tests",))
    _member(tmp_path, "apps/api", "top", deps=("mid-pkg",), tests=("api_tests",))
    return tmp_path


def run(workspace: Path, files, **kwargs):
    return affected.select(workspace, list(files), **kwargs)


def names(result):
    return set(result.members)


def test_a_change_expands_to_transitive_dependents(workspace):
    result = run(workspace, ["packages/base/src/x.py"])
    assert names(result) == {"base-pkg", "mid-pkg", "top", "lib"}
    assert not result.full
    assert result.members["mid-pkg"] == "depends on base-pkg"
    assert result.members["top"] == "depends on mid-pkg"


def test_a_leaf_change_stays_a_leaf(workspace):
    result = run(workspace, ["apps/api/src/y.py"])
    assert names(result) == {"top"}
    assert result.test_paths == ["apps/api/api_tests"]


def test_dependency_group_edges_count(workspace):
    result = run(workspace, ["packages/base/a.py"])
    assert result.members["lib"] == "depends on base-pkg"
    assert set(result.test_paths) >= {"packages/lib/lib_tests", "packages/lib/tests"}


def test_unrelated_members_are_untouched(workspace):
    assert "solo" not in names(run(workspace, ["packages/base/a.py"]))
    assert names(run(workspace, ["packages/solo/a.py"])) == {"solo"}


@pytest.mark.parametrize(
    "path",
    [
        "docs/ci.md",
        "apps/docs/content/a.mdx",
        "README.md",
        "packages/base/README.md",
        "examples/a.py",
        ".planning/x.json",
        "spikes/a.py",
    ],
)
def test_docs_only_changes_select_nothing(workspace, path):
    result = run(workspace, [path])
    assert not result.full and not result.test_paths and not result.members and not result.studio


def test_studio_changes_run_no_python_tests(workspace):
    result = run(workspace, ["apps/studio/src/a.tsx", "pnpm-lock.yaml"])
    assert result.studio and not result.full and result.test_paths == []


@pytest.mark.parametrize(
    "path",
    [
        "pyproject.toml",
        "uv.lock",
        ".github/workflows/ci.yml",
        ".github/workflows/ci-heavy.yml",
        "scripts/ci/affected.py",
        "conftest.py",
        "packages/base/mid_tests/conftest.py",
        "tests/recovery/test_a.py",
        "tests/fixtures/a.json",
        "config/model-prices.json",
        "something-new/file.txt",
    ],
)
def test_full_run_triggers(workspace, path):
    result = run(workspace, [path])
    assert result.full
    assert names(result) == {"base-pkg", "mid-pkg", "lib", "solo", "top"}


def test_a_docs_file_does_not_cancel_a_real_change(workspace):
    result = run(workspace, ["docs/a.md", "packages/solo/a.py"])
    assert names(result) == {"solo"} and not result.full


def test_deploy_files_select_only_the_deploy_checks(workspace):
    for path in ("deploy/k8s/a.yaml", "tests/deploy/test_a.py", ".github/workflows/deploy.yml", ".dockerignore"):
        result = run(workspace, [path])
        assert result.deploy and not result.full and not result.members
        assert result.test_paths == ["tests/deploy"]


def test_ci_workflow_change_also_selects_studio(workspace):
    assert run(workspace, [".github/workflows/ci.yml"]).studio


def test_restrict_to_keeps_only_affected_job_paths(workspace):
    job = ["packages/base/tests", "packages/mid/mid_tests", "packages/solo/solo_tests", "apps/api/api_tests"]
    result = run(workspace, ["packages/mid/src/a.py"], restrict_to=job)
    assert result.test_paths == ["packages/mid/mid_tests", "apps/api/api_tests"]
    assert run(workspace, ["docs/a.md"], restrict_to=job).test_paths == []
    assert run(workspace, ["pyproject.toml"], restrict_to=job).test_paths == job


def test_force_full_and_no_files(workspace):
    assert run(workspace, [], force_full=True).full
    assert run(workspace, []).test_paths == []


def test_the_db_plugin_member_is_global(workspace, monkeypatch):
    monkeypatch.setattr(affected, "GLOBAL_MEMBERS", {"solo"})
    assert run(workspace, ["packages/solo/a.py"]).full


def test_test_only_dependencies_are_one_step(workspace, monkeypatch):
    monkeypatch.setattr(affected, "TEST_ONLY_DEPENDENCIES", {"solo": ("base-pkg",)})
    result = run(workspace, ["packages/base/a.py"])
    assert result.members["solo"] == "its tests import base-pkg"
    # solo's own dependents (none here) are not dragged in, and a change to solo does not run base
    assert names(run(workspace, ["packages/solo/a.py"])) == {"solo"}


def test_cli_json_github_and_summary(workspace, tmp_path):
    summary = tmp_path / "summary.md"
    files = tmp_path / "files.txt"
    files.write_text("packages/solo/a.py\n")
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(workspace), "--files-from", str(files), "--summary", str(summary)],
        capture_output=True, text=True, check=True,
    ).stdout
    data = json.loads(out)
    assert data["test_paths"] == ["packages/solo/solo_tests"] and data["has_python_tests"]
    assert "| solo |" in summary.read_text()
    gh = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(workspace), "--files-from", str(files), "--format", "github"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "has_tests=true" in gh and "paths=packages/solo/solo_tests" in gh


def test_git_diff_uses_the_merge_base(tmp_path):
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True,
                       env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                            "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "HOME": str(tmp_path)})
    git("init", "-q", "-b", "main")
    (tmp_path / "a.txt").write_text("1")
    git("add", "."); git("commit", "-qm", "one")
    git("checkout", "-qb", "feature")
    (tmp_path / "feature.txt").write_text("f")
    git("add", "."); git("commit", "-qm", "feature")
    git("checkout", "-q", "main")
    (tmp_path / "main-only.txt").write_text("m")
    git("add", "."); git("commit", "-qm", "main moved")
    git("checkout", "-q", "feature")
    assert affected.changed_files(tmp_path, "main", "HEAD") == ["feature.txt"]


def test_an_unknown_base_fails_safe_to_a_full_run(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname="r"\nversion="0"\n[tool.uv.workspace]\nmembers=[]\n')
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(tmp_path), "--base", "nope"],
        capture_output=True, text=True, check=True,
    )
    assert json.loads(out.stdout)["full"] is True


# ---- the real repository -------------------------------------------------


def _module_roots(directory: Path) -> set[str]:
    found: set[str] = set()
    for path in directory.rglob("*.py"):
        if any(part in {"node_modules", ".venv"} for part in path.parts):
            continue
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                found.add(node.module.split(".")[0])
    return found


def test_the_real_graph_covers_every_cross_member_import():
    """A member's code and tests may import only members the graph says it depends on (transitively).

    If this fails, add the dependency to the member's pyproject (or to TEST_ONLY_DEPENDENCIES) so the
    affected-test selection cannot miss it.
    """
    members = affected.load_workspace(ROOT)
    module_owner: dict[str, str] = {}
    for member in members.values():
        for source in (ROOT / member.path / "src").glob("*"):
            if source.is_dir():
                module_owner[source.name] = member.name
    for member in members.values():
        closure: set[str] = {member.name}
        queue = [member.name]
        while queue:
            for dep in members[queue.pop()].dependencies:
                if dep not in closure:
                    closure.add(dep)
                    queue.append(dep)
        imported = _module_roots(ROOT / member.path)
        owners = {module_owner[m] for m in imported if m in module_owner}
        # eap_db is global (every change to it runs everything); eap-db's own imports of the API and
        # worker models are covered by SCHEMA_FILE.
        owners -= affected.GLOBAL_MEMBERS | set(member.test_dependencies)
        if member.name in affected.GLOBAL_MEMBERS:
            owners -= {"eap-api", "eap-worker"}
        assert owners <= closure, f"{member.name} imports {sorted(owners - closure)} without depending on it"


def test_an_openfga_plugin_change_reruns_the_api_tests_that_run_its_model():
    result = affected.select(ROOT, ["plugins/iam-openfga/src/eap_iam_openfga/engine.py"])
    assert result.members["eap-api"] == "its tests import eap-iam-openfga"


def test_the_real_workspace_is_parsed():
    members = affected.load_workspace(ROOT)
    assert {"eap-api", "eap-worker", "eap-schemas", "eap-db"} <= set(members)
    assert members["eap-api"].test_dirs == ["apps/api/api_tests"]
    assert "eap-schemas" in members["eap-run-store"].dependencies
    result = affected.select(ROOT, ["packages/pricing/src/eap_pricing/__init__.py"])
    assert {"eap-pricing", "eap-run-store", "eap-api", "eap-worker"} <= set(result.members)
    assert "eap-processors" not in result.members
    # the API alone does not drag in the run store or the knowledge packages
    api = affected.select(ROOT, ["apps/api/src/eap_api/routes.py"])
    assert not api.full and "eap-run-store" not in api.members and "eap-knowledge" not in api.members
    assert affected.select(ROOT, ["apps/api/src/eap_api/workflows/models.py"]).full
    assert affected.select(ROOT, ["apps/worker/src/eap_worker/tickets.py"]).full


def test_every_model_module_eap_db_imports_triggers_a_full_run():
    tree = ast.parse((ROOT / "packages/db/src/eap_db/metadata.py").read_text())
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules |= {a.name for a in node.names}
    owned = [m for m in modules if m.split(".")[0] in {"eap_api", "eap_worker"}]
    assert owned
    for module in owned:
        app = "api" if module.startswith("eap_api") else "worker"
        base = f"apps/{app}/src/{module.replace('.', '/')}"
        path = base + ".py" if (ROOT / (base + ".py")).exists() else base + "/__init__.py"
        assert affected.SCHEMA_FILE.match(path), f"{module} ({path}) is not in SCHEMA_FILE"
