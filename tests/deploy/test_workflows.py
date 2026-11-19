"""Security and cost guardrails for the GitHub Actions workflows."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = ROOT / ".github" / "workflows"
# Every workflow present: the hosted CI here, plus the self-hosted CI Heavy and the deploy workflow in a
# checkout that carries them (tests/deploy/ops covers what is specific to those two).
WORKFLOW_NAMES = tuple(sorted(p.name for p in WORKFLOW_DIR.glob("*.yml")))
SHA = re.compile(r"^[^\s@]+@[0-9a-f]{40}$")
UNTRUSTED_EVENT = re.compile(r"\$\{\{[^}]*github\.event\.", re.IGNORECASE)


def _load(name: str) -> dict[str, Any]:
    # BaseLoader keeps YAML 1.1 from treating the key `on` as a boolean.
    return yaml.load((WORKFLOW_DIR / name).read_text(), Loader=yaml.BaseLoader)


def _values_for_key(value: Any, key: str):
    if isinstance(value, dict):
        for child_key, child in value.items():
            if child_key == key:
                yield child
            yield from _values_for_key(child, key)
    elif isinstance(value, list):
        for child in value:
            yield from _values_for_key(child, key)


def test_the_hosted_ci_workflow_is_present():
    assert "ci.yml" in WORKFLOW_NAMES


def test_every_action_is_pinned_to_a_full_commit_sha():
    for name in WORKFLOW_NAMES:
        workflow = _load(name)
        for action in _values_for_key(workflow, "uses"):
            assert SHA.fullmatch(action), f"{name}: {action}"


def test_workflows_default_to_read_only_contents():
    for name in WORKFLOW_NAMES:
        assert _load(name).get("permissions") == {"contents": "read"}, name


SAME_REPO_ONLY = (
    "github.event_name != 'pull_request' || "
    "github.event.pull_request.head.repo.full_name == github.repository"
)


def test_self_hosted_workflows_never_run_fork_code():
    for name in WORKFLOW_NAMES:
        workflow = _load(name)
        self_hosted = [
            job for job in workflow["jobs"].values() if "self-hosted" in job.get("runs-on", [])
        ]
        if not self_hosted:
            continue
        events = workflow.get("on", {})
        # pull_request_target and issue_comment run with secrets or on comment text: never.
        for forbidden in ("pull_request_target", "issue_comment"):
            assert forbidden not in events, name
        if "pull_request" in events:
            # Only CI Heavy runs for pull requests, and only for branches of this repository.
            assert name == "ci-heavy.yml", name
            for job in self_hosted:
                assert " ".join(job["if"].split()) == SAME_REPO_ONLY, name
        push = events.get("push")
        if isinstance(push, dict):
            assert push.get("branches") == ["main"], name


def test_run_blocks_do_not_interpolate_untrusted_event_fields():
    for name in WORKFLOW_NAMES:
        workflow = _load(name)
        for job in workflow.get("jobs", {}).values():
            for step in job.get("steps", []):
                run = step.get("run", "")
                assert not UNTRUSTED_EVENT.search(run), f"{name}: {step.get('name', run)}"


def test_deploy_credentials_do_not_come_from_github_secrets():
    for name in WORKFLOW_NAMES:
        text = (WORKFLOW_DIR / name).read_text()
        assert not re.search(r"\$\{\{\s*secrets\.", text), name


def test_hosted_ci_never_builds_studio():
    text = (WORKFLOW_DIR / "ci.yml").read_text()
    assert "pnpm build" not in text


def test_no_workflow_lists_issue_comment_or_a_write_permission():
    for name in WORKFLOW_NAMES:
        text = (WORKFLOW_DIR / name).read_text()
        assert "issue_comment" not in text, name
        assert "contents: write" not in text, name
        assert "pull-requests: write" not in text, name


def test_hosted_path_filters_skip_unaffected_jobs():
    ci = _load("ci.yml")
    assert "if" not in ci["jobs"]["changes"]
    assert ci["jobs"]["python"]["if"] == "needs.changes.outputs.python == 'true'"
    assert ci["jobs"]["studio"]["if"] == "needs.changes.outputs.studio == 'true'"
    assert ci["jobs"]["studio-test"]["if"] == "needs.changes.outputs.studio == 'true'"
    assert ci["jobs"]["deploy-checks"]["if"] == "needs.changes.outputs.deploy == 'true'"
    filters = ci["jobs"]["changes"]["steps"][1]["with"]["filters"]
    assert "packages/**" in filters
    assert "apps/studio/**" in filters
    assert ".github/workflows/ci.yml" in filters  # a change to the studio gate runs the studio gate
    assert "deploy/**" in filters
    assert "tests/deploy/**" in filters


# Peeled commit of pnpm/action-setup v6.1.1. The 40-hex value currently in ci.yml is the
# annotated tag object, which has no action.yml.
PNPM_ACTION_COMMIT = "73e46992e05b19abd31f2f3c20e0e7370d762115"
PNPM_ACTION_TAG_OBJECT = "7088e561eb65bb68695d245aa206f005ef30921d"


def test_pnpm_action_setup_is_pinned_to_the_v6_1_1_commit():
    text = (WORKFLOW_DIR / "ci.yml").read_text()
    pinned_to_commit = f"pnpm/action-setup@{PNPM_ACTION_COMMIT}" in text
    pinned_to_tag_object = PNPM_ACTION_TAG_OBJECT in text
    if pinned_to_tag_object or not pinned_to_commit:
        raise AssertionError(
            "pnpm/action-setup must be pinned to commit "
            f"{PNPM_ACTION_COMMIT} (v6.1.1); the annotated tag object is not a commit"
        )


def test_pytest_xdist_is_a_dev_dependency():
    text = (ROOT / "pyproject.toml").read_text()
    assert re.search(r'"pytest-xdist[^"]*"', text)
    assert 'name = "pytest-xdist"' in (ROOT / "uv.lock").read_text()


def test_studio_vitest_is_sharded_in_five_jobs_and_static_checks_run_once():
    jobs = _load("ci.yml")["jobs"]
    static, tests = jobs["studio"], jobs["studio-test"]
    static_runs = [step.get("run", "") for step in static["steps"]]
    assert "pnpm -C apps/studio lint" in static_runs
    assert "pnpm -C apps/studio exec tsc --noEmit" in static_runs
    assert not any("vitest" in run or run.endswith(" test") for run in static_runs)
    assert tests["strategy"]["matrix"]["shard"] == ["1", "2", "3", "4", "5"]
    assert tests["strategy"]["fail-fast"] == "false"
    test_runs = [step.get("run", "") for step in tests["steps"]]
    assert any('vitest run --shard="${SHARD}/5" "${args[@]}"' in run for run in test_runs)
    assert not any("lint" in run or "tsc" in run for run in test_runs)
    assert static["timeout-minutes"] == "30"
    assert tests["timeout-minutes"] == "30"


def test_hosted_jobs_cache_the_uv_and_pnpm_stores_on_their_lockfiles():
    jobs = _load("ci.yml")["jobs"]
    for name in ("python", "deploy-checks"):
        uv_step = next(s for s in jobs[name]["steps"] if str(s.get("uses", "")).startswith("astral-sh/setup-uv@"))
        assert uv_step["with"]["enable-cache"] == "true", name
        assert uv_step["with"]["cache-dependency-glob"] == "uv.lock", name
    for name in ("studio", "studio-test"):
        node = next(s for s in jobs[name]["steps"] if str(s.get("uses", "")).startswith("actions/setup-node@"))
        # setup-node's pnpm cache is keyed on pnpm-lock.yaml (cache-dependency-path defaults to it).
        assert node["with"]["cache"] == "pnpm", name
        assert "cache-dependency-path" not in node["with"], name


def test_studio_test_shards_cache_the_vite_transform_cache_on_lockfile_and_config():
    steps = _load("ci.yml")["jobs"]["studio-test"]["steps"]
    cache = next(s for s in steps if str(s.get("uses", "")).startswith("actions/cache@"))
    assert cache["with"]["path"] == "apps/studio/node_modules/.vite"
    assert "hashFiles('pnpm-lock.yaml', 'apps/studio/vite.config.ts')" in cache["with"]["key"]
    assert "matrix.shard" in cache["with"]["key"]
    # Restored after install (install would not touch .vite) and before vitest runs.
    names = [s.get("run") or s.get("uses") for s in steps]
    assert steps.index(cache) > names.index("pnpm install --frozen-lockfile")
    assert steps.index(cache) < next(i for i, s in enumerate(steps) if "vitest run" in s.get("run", ""))


# ---- CI round 3: pull requests run the affected tests, main runs everything ----------------------

# Branch protection and the deploy gate key on these names; the selection must not rename them.
CI_JOB_NAMES = ("changes", "python", "studio", "studio-test", "deploy-checks")


def _steps(job: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {step.get("name", step.get("run", "")): step for step in job["steps"]}


def test_ci_job_names_are_unchanged():
    assert tuple(_load("ci.yml")["jobs"]) == CI_JOB_NAMES
    # No job sets a display name that would rename its check.
    assert all("name" not in job for job in _load("ci.yml")["jobs"].values())
    assert _load("ci.yml")["jobs"]["studio-test"]["strategy"]["matrix"]["shard"] == ["1", "2", "3", "4", "5"]
    assert _load("ci.yml")["name"] == "CI"


def test_ci_python_job_runs_the_selected_unit_paths():
    job = _load("ci.yml")["jobs"]["python"]
    steps = _steps(job)
    select = steps["Select the affected tests"]
    assert select["id"] == "select"
    assert "scripts/ci/select-tests.sh python" in select["run"]
    for suite in ("packages/shared-schemas/tests", "packages/secrets/secrets_tests"):
        assert suite in select["run"]
    unit = steps["DB-free unit tests"]
    assert unit["if"] == "steps.select.outputs.unit_run == 'true'"
    assert unit["env"]["TEST_PATHS"] == "${{ steps.select.outputs.unit_paths }}"
    assert "-p no:eap_db" in unit["run"]
    assert "No affected Python tests" in steps["No affected Python tests"]["run"]
    # lint-imports always runs: an import-contract break does not depend on the selected tests.
    assert steps["uv run lint-imports"]["run"] == "uv run lint-imports"


def test_ci_studio_tests_use_vitest_changed_on_pull_requests_only():
    job = _load("ci.yml")["jobs"]["studio-test"]
    steps = _steps(job)
    assert steps["Select the Studio tests"]["run"] == "bash scripts/ci/select-tests.sh studio"
    checkout = job["steps"][0]
    assert checkout["with"]["fetch-depth"] == "0"
    shard = steps["Vitest shard ${{ matrix.shard }}/5"]
    assert shard["env"]["VITEST_ARGS"] == "${{ steps.select.outputs.vitest_args }}"
    assert "--shard=" in shard["run"]
    # Lint and tsc stay behind the studio path filter (they never ran for a Python-only change).
    assert _load("ci.yml")["jobs"]["studio"]["if"] == "needs.changes.outputs.studio == 'true'"


def test_selection_reads_labels_and_is_full_by_default_off_pull_requests():
    for name, job_name in (("ci.yml", "python"), ("ci.yml", "studio-test")):
        workflow = _load(name)
        job = workflow["jobs"][job_name]
        assert job["permissions"]["pull-requests"] == "read", (name, job_name)
        assert "pull-requests: write" not in (WORKFLOW_DIR / name).read_text()
        for key in ("EVENT_NAME", "PR_NUMBER", "FULL_INPUT", "GH_TOKEN"):
            assert key in job["env"], (name, job_name, key)
        full = workflow["on"]["workflow_dispatch"]["inputs"]["full"]
        assert full["type"] == "boolean" and full["default"] == "true"
    script = (ROOT / "scripts" / "ci" / "select-tests.sh").read_text()
    assert 'full_label="ci:full"' in script
    # Anything that is not a pull request (push to main, schedule, dispatch) runs the full suites.
    assert re.search(r"\*\)\s+mode=full", script)


def test_selection_scripts_exist_and_do_not_use_untrusted_input():
    script = ROOT / "scripts" / "ci" / "select-tests.sh"
    assert script.exists() and script.stat().st_mode & 0o111
    assert (ROOT / "scripts" / "ci" / "affected.py").exists()
    # PR_NUMBER and the labels are the only pull request data the script reads, never branch or title text.
    assert "PR_TITLE" not in script.read_text() and "HEAD_REF" not in script.read_text()


