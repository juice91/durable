"""Security and cost guardrails for the GitHub Actions workflows."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = ROOT / ".github" / "workflows"
WORKFLOW_NAMES = ("ci.yml", "ci-heavy.yml", "deploy.yml")
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


def test_deploy_is_serialized_and_uses_the_studio_environment():
    deploy = _load("deploy.yml")
    assert deploy.get("concurrency") == {"group": "deploy-studio", "cancel-in-progress": "false"}
    # GitHub Pro: the `studio` environment carries a main-only deployment branch policy. It is not
    # the approval (required reviewers need Team/Enterprise for private repos); the manual gate is.
    assert deploy["jobs"]["deploy"]["environment"] == "studio"
    assert deploy["jobs"]["deploy"]["if"].count("github.repository_owner") == 2


def test_hosted_ci_never_builds_studio():
    text = (WORKFLOW_DIR / "ci.yml").read_text()
    assert "pnpm build" not in text


def test_no_workflow_lists_issue_comment_or_a_write_permission():
    for name in WORKFLOW_NAMES:
        text = (WORKFLOW_DIR / name).read_text()
        assert "issue_comment" not in text, name
        assert "contents: write" not in text, name
        assert "pull-requests: write" not in text, name


def test_deploy_is_manual_dispatch_only():
    deploy = _load("deploy.yml")
    assert list(deploy["on"]) == ["workflow_dispatch"]
    text = (WORKFLOW_DIR / "deploy.yml").read_text()
    assert "workflow_run:" not in text
    inputs = deploy["on"]["workflow_dispatch"]["inputs"]
    assert inputs["confirm"]["required"] == "true"
    assert inputs["allow_untested"]["default"] == "false"
    assert inputs["allow_untested"]["type"] == "boolean"


def test_deploy_skip_drain_input_exports_the_skip_variable_and_reports_it():
    deploy = _load("deploy.yml")
    skip = deploy["on"]["workflow_dispatch"]["inputs"]["skip_drain"]
    assert skip["type"] == "boolean" and skip["default"] == "false"
    steps = deploy["jobs"]["deploy"]["steps"]
    step = next(s for s in steps if s.get("name") == "Deploy this revision")
    assert step["env"]["SKIP_DRAIN"] == "${{ inputs.skip_drain }}"
    assert '[[ "$SKIP_DRAIN" == true ]]' in step["run"]
    assert "export PYDANTICOS_SKIP_DRAIN=1" in step["run"]
    assert "drain was skipped" in step["run"] and "GITHUB_STEP_SUMMARY" in step["run"]
    assert step["run"].index("PYDANTICOS_SKIP_DRAIN=1") < step["run"].index("deploy/scripts/up.sh")


def test_deploy_runs_only_for_the_owner_on_main():
    job = _load("deploy.yml")["jobs"]["deploy"]
    condition = " ".join(job["if"].split())
    assert condition == "github.ref == 'refs/heads/main' && github.actor == github.repository_owner && github.triggering_actor == github.repository_owner"
    assert job["permissions"] == {"contents": "read", "actions": "read"}


def test_deploy_fails_immediately_unless_confirm_is_deploy():
    steps = _load("deploy.yml")["jobs"]["deploy"]["steps"]
    first = steps[0]
    assert first["env"]["CONFIRM"] == "${{ inputs.confirm }}"
    assert '[[ "$CONFIRM" != deploy ]]' in first["run"]
    assert "exit 1" in first["run"]
    # Nothing that touches the host runs before the confirmation.
    assert not any("uses" in step for step in steps[:1])


def test_deploy_checks_out_exactly_github_sha_and_never_uses_event_fields():
    steps = _load("deploy.yml")["jobs"]["deploy"]["steps"]
    checkout = next(step for step in steps if str(step.get("uses", "")).startswith("actions/checkout@"))
    assert checkout["with"]["ref"] == "${{ github.sha }}"
    assert checkout["with"]["persist-credentials"] == "false"
    for step in steps:
        assert "github.event" not in step.get("run", "")
    # Untrusted input reaches the shell only through env.
    for step in steps:
        assert "inputs." not in step.get("run", "")


def test_deploy_reports_ci_status_and_needs_allow_untested_to_override():
    steps = _load("deploy.yml")["jobs"]["deploy"]["steps"]
    check = next(step for step in steps if step.get("name") == "Report CI status for this commit")
    assert check["env"]["ALLOW_UNTESTED"] == "${{ inputs.allow_untested }}"
    assert check["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert "GITHUB_STEP_SUMMARY" in check["run"]
    assert "'CI' 'CI Heavy'" in check["run"]
    assert '[[ "$ALLOW_UNTESTED" == true ]]' in check["run"]
    assert steps.index(check) < next(i for i, s in enumerate(steps) if "uses" in s)


def test_deploy_secrets_come_from_1password_in_the_deploy_step_only():
    text = (WORKFLOW_DIR / "deploy.yml").read_text()
    assert "up.sh --op-refs deploy/secrets.op.env" in text
    assert "--sops" not in text and "sops" not in text.lower()
    steps = _load("deploy.yml")["jobs"]["deploy"]["steps"]
    sourcing = [step for step in steps if "runner.env" in step.get("run", "")]
    assert [step["name"] for step in sourcing] == ["Deploy this revision"]
    assert "OP_SERVICE_ACCOUNT_TOKEN" in sourcing[0]["run"]
    # The token is never exported at workflow or job level.
    deploy = _load("deploy.yml")
    assert "env" not in deploy and "env" not in deploy["jobs"]["deploy"]


def test_ci_heavy_postgres_is_loopback_and_always_removed():
    text = (WORKFLOW_DIR / "ci-heavy.yml").read_text()
    assert 'docker --context colima-pydanticos rm -fv "$CI_PG_NAME"' in text  # -v: the anonymous data volume goes too
    assert "-p '127.0.0.1::5432'" in text  # loopback only, free port chosen by Docker
    assert "kubectl" not in text
    assert "-n pydanticos" not in text
    steps = _load("ci-heavy.yml")["jobs"]["heavy"]["steps"]
    start = next(step for step in steps if step.get("name") == "Start isolated CI Postgres")
    remove = next(step for step in steps if step.get("name") == "Remove isolated CI Postgres")
    assert 'rm -fv "$CI_PG_NAME"' in start["run"]
    assert remove["if"] == "always()"
    assert 'rm -fv "$CI_PG_NAME"' in remove["run"]


def test_nightly_skip_requires_schedule_and_the_same_sha():
    text = (WORKFLOW_DIR / "ci-heavy.yml").read_text()
    assert (
        '[[ "$EVENT_NAME" == schedule && -r "$marker" && "$(<"$marker")" == "$HEAD_SHA" ]]' in text
    )
    assert "run=false" in text
    record = next(
        step
        for step in _load("ci-heavy.yml")["jobs"]["heavy"]["steps"]
        if step.get("name") == "Record green nightly SHA"
    )
    assert record["if"] == "success() && steps.nightly.outputs.run == 'true' && github.event_name == 'schedule'"


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


def test_ci_heavy_skips_docs_only_pushes():
    push = _load("ci-heavy.yml")["on"]["push"]
    assert push["branches"] == ["main"]
    assert push["paths-ignore"] == ["docs/**", "**/*.md", ".planning/**"]
    assert _load("ci-heavy.yml")["on"]["pull_request"]["paths-ignore"] == push["paths-ignore"]
    # Only the push trigger is path-filtered: the nightly and manual runs always run.
    assert "schedule" in _load("ci-heavy.yml")["on"]
    assert "workflow_dispatch" in _load("ci-heavy.yml")["on"]


def test_ci_heavy_cancels_superseded_runs_of_the_same_ref_only():
    concurrency = _load("ci-heavy.yml")["concurrency"]
    # One group per ref: a newer run cancels an older run of the same branch, and a run of another
    # branch (or main) is never in that group, so it is never cancelled by it.
    assert concurrency["group"] == "ci-heavy-${{ github.ref }}"
    # Only pull request runs are superseded; main, nightly and manual runs always finish.
    assert concurrency["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"


def test_ci_heavy_runs_for_pull_requests_with_a_read_only_token():
    heavy = _load("ci-heavy.yml")
    assert "pull_request" in heavy["on"] and "workflow_dispatch" in heavy["on"] and "schedule" in heavy["on"]
    assert heavy["permissions"] == {"contents": "read"}
    # Checkout, the main-ref fetch, the status report and the ci:full label lookup need nothing beyond these.
    assert heavy["jobs"]["heavy"]["permissions"] == {
        "actions": "read",
        "contents": "read",
        "pull-requests": "read",
    }
    assert "pull_request_target" not in heavy["on"]


def test_ci_heavy_postgres_is_unique_per_run_so_two_runners_do_not_collide():
    heavy = _load("ci-heavy.yml")["jobs"]["heavy"]
    assert heavy["env"]["CI_PG_NAME"] == "pydanticos-ci-pg-${{ github.run_id }}-${{ github.run_attempt }}"
    assert "CI_PG_PORT" not in heavy["env"]  # Docker picks a free host port
    text = (WORKFLOW_DIR / "ci-heavy.yml").read_text()
    assert "54359" not in text
    assert "CI_PG_PORT=${port}" in text and 'GITHUB_ENV' in text
    for step in heavy["steps"]:
        for value in (step.get("env") or {}).values():
            if "127.0.0.1" in value:
                assert "${{ env.CI_PG_PORT }}" in value


def test_ci_heavy_runs_the_api_suite_under_xdist_on_six_workers():
    steps = _load("ci-heavy.yml")["jobs"]["heavy"]["steps"]
    api = next(step for step in steps if step.get("name") == "API suite")
    # The paths come from the selection step (all of apps/api/api_tests on main).
    assert 'uv run pytest "${paths[@]}" -q -n 6' in api["run"]
    # The worker suite stays serial: every worker test shares the one DBOS database.
    worker = next(step for step in steps if step.get("name") == "Nightly worker suite")
    assert "-n" not in worker["run"].split()


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


def test_ci_heavy_does_not_wait_for_hosted_ci():
    steps = _load("ci-heavy.yml")["jobs"]["heavy"]["steps"]
    text = (WORKFLOW_DIR / "ci-heavy.yml").read_text()
    assert not any(step.get("name") == "Require the hosted CI gate" for step in steps)
    assert "sleep 15" not in text and "Timed out waiting for CI" not in text
    report = next(step for step in steps if "hosted CI status" in step.get("name", ""))
    # Information only: no loop, no exit 1, so it can never block or fail the run.
    assert "exit 1" not in report["run"] and "sleep" not in report["run"]
    assert "|| conclusion=unknown" in report["run"]


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
HEAVY_JOB_NAMES = ("heavy",)


def _steps(job: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {step.get("name", step.get("run", "")): step for step in job["steps"]}


def test_ci_job_names_are_unchanged():
    assert tuple(_load("ci.yml")["jobs"]) == CI_JOB_NAMES
    assert tuple(_load("ci-heavy.yml")["jobs"]) == HEAVY_JOB_NAMES
    # No job sets a display name that would rename its check.
    for name in ("ci.yml", "ci-heavy.yml"):
        assert all("name" not in job for job in _load(name)["jobs"].values()), name
    assert _load("ci.yml")["jobs"]["studio-test"]["strategy"]["matrix"]["shard"] == ["1", "2", "3", "4", "5"]
    assert _load("ci.yml")["name"] == "CI" and _load("ci-heavy.yml")["name"] == "CI Heavy"


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


def test_heavy_suites_take_the_selection_and_skip_the_infrastructure_when_empty():
    job = _load("ci-heavy.yml")["jobs"]["heavy"]
    steps = _steps(job)
    assert "scripts/ci/select-tests.sh python" in steps["Select the affected suites"]["run"]
    for name in ("Start isolated CI Postgres", "Install Python dependencies", "Migrate the CI database"):
        assert "steps.select.outputs.any == 'true'" in steps[name]["if"], name
    for name, key in (("Package suites", "packages"), ("Run-store suite", "runstore"), ("API suite", "api")):
        assert f"steps.select.outputs.{key}_run == 'true'" in steps[name]["if"], name
        assert steps[name]["env"]["TEST_PATHS"] == f"${{{{ steps.select.outputs.{key}_paths }}}}", name
    assert "-n 6" in steps["API suite"]["run"]
    assert "No affected Python tests" in steps["No affected Python tests"]["run"]
    # The nightly-only suites and the cleanup are untouched by the selection.
    assert steps["Nightly worker suite"]["if"].endswith("github.event_name == 'schedule'")
    assert steps["Remove isolated CI Postgres"]["if"] == "always()"


def test_selection_reads_labels_and_is_full_by_default_off_pull_requests():
    for name, job_name in (("ci.yml", "python"), ("ci.yml", "studio-test"), ("ci-heavy.yml", "heavy")):
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
