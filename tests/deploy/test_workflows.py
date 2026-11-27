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
# Workflows that need more than read-only contents by design. They skip only the read-only-default, the
# issue_comment / write-permission and the secrets rules (the last still allows the built-in GITHUB_TOKEN);
# test_privileged_workflows_never_check_out_or_run_pr_code holds them to a stricter rule of their own.
PRIVILEGED_WORKFLOWS = {
    "cla.yml": "the CLA bot must comment on pull requests and record signatures; it never checks out or runs pull-request code",
    "labeler.yml": "labels a pull request; runs the base commit's script, never the pull request's code",
    "release.yml": "pushes the release branch and opens the release pull request, from main, by the repository owner",
    "release-tag.yml": "tags the pushed main commit and creates the GitHub Release",
    "release-public.yml": "tags and creates the Release in the public mirror only",
}
# The release family has its own, stricter rules (test_release_workflows_are_least_privilege); the CLA bot rule
# (no checkout, no run step) does not fit workflows that must run the release scripts.
RELEASE_WORKFLOWS = ("labeler.yml", "release.yml", "release-tag.yml", "release-public.yml")
# Operator-only workflows (docs/github-flow.md; excluded from the public export). Each may read exactly the
# secrets listed and, in the review's case, hold pull-requests: write; every other rule above still applies.
OPERATOR_SECRETS = {
    "claude-code-review.yml": {"CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"},
    "project-sync.yml": {"PROJECT_TOKEN"},
}
CLA_SENTENCE = "I have read the CLA Document and I hereby sign the CLA"


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
        if name in PRIVILEGED_WORKFLOWS:
            continue
        assert _load(name).get("permissions") == {"contents": "read"}, name


# The labeler runs on pull_request only, so its guard needs no event-name clause.
PULL_REQUEST_SAME_REPO = "github.event.pull_request.head.repo.full_name == github.repository"
SAME_REPO_ONLY = (
    "github.event_name != 'pull_request' || "
    "github.event.pull_request.head.repo.full_name == github.repository"
)


CI_RUNNER_EXPRESSION = "${{ vars.CI_RUNNER && fromJSON(vars.CI_RUNNER) || 'ubuntu-latest' }}"


def _may_be_self_hosted(job: dict[str, Any]) -> bool:
    runs_on = job.get("runs-on", [])
    # ci.yml picks its runner from the CI_RUNNER variable, so any of its jobs may land on self-hosted.
    return "self-hosted" in runs_on or runs_on == CI_RUNNER_EXPRESSION


def _guarded(job: dict[str, Any]) -> bool:
    return " ".join(job.get("if", "").split()).startswith(f"({SAME_REPO_ONLY})") or " ".join(
        job.get("if", "").split()
    ) in (SAME_REPO_ONLY, PULL_REQUEST_SAME_REPO)


def test_ci_runs_on_defaults_to_hosted_and_reads_only_ci_runner():
    workflow = _load("ci.yml")
    for name, job in workflow["jobs"].items():
        assert job["runs-on"] == CI_RUNNER_EXPRESSION, name
        assert "timeout-minutes" in job, name
    text = (WORKFLOW_DIR / "ci.yml").read_text()
    assert set(re.findall(r"\bvars\.(\w+)", text)) == {"CI_RUNNER"}
    assert text.count("|| 'ubuntu-latest' }}") == len(workflow["jobs"])


def test_self_hosted_workflows_never_run_fork_code():
    for name in WORKFLOW_NAMES:
        workflow = _load(name)
        self_hosted = [job for job in workflow["jobs"].values() if _may_be_self_hosted(job)]
        if not self_hosted:
            continue
        events = workflow.get("on", {})
        # pull_request_target and issue_comment run with secrets or on comment text: never.
        for forbidden in ("pull_request_target", "issue_comment"):
            assert forbidden not in events, name
        if "pull_request" in events:
            # Only CI Heavy, CI and the labeler (when CI_RUNNER is set) run for pull requests, and only for
            # branches of this repository. The labeler runs the base commit's script, not the pull request's.
            assert name in ("ci-heavy.yml", "ci.yml", "compose.yml", "labeler.yml", *OPERATOR_SECRETS), name
            for job in self_hosted:
                assert _guarded(job), name
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
        if name in PRIVILEGED_WORKFLOWS:
            text = text.replace("secrets.GITHUB_TOKEN", "")
        for secret in OPERATOR_SECRETS.get(name, ()):
            text = text.replace(f"secrets.{secret}", "")
        assert not re.search(r"\$\{\{\s*secrets\.", text), name


def test_hosted_ci_never_builds_studio():
    text = (WORKFLOW_DIR / "ci.yml").read_text()
    assert "pnpm build" not in text


def test_no_workflow_lists_issue_comment_or_a_write_permission():
    for name in WORKFLOW_NAMES:
        if name in PRIVILEGED_WORKFLOWS:
            continue
        text = (WORKFLOW_DIR / name).read_text()
        assert "issue_comment" not in text, name
        assert "contents: write" not in text, name
        if name != "claude-code-review.yml":  # the review comments on the pull request
            assert "pull-requests: write" not in text, name


def test_privileged_workflows_never_check_out_or_run_pr_code():
    present = [name for name in PRIVILEGED_WORKFLOWS if name in WORKFLOW_NAMES and name not in RELEASE_WORKFLOWS]
    if not present:
        print(f"no privileged workflow present ({', '.join(PRIVILEGED_WORKFLOWS)}): nothing to check")
        return
    allowed_comments = {"github.event.comment.body == 'recheck'", f"github.event.comment.body == '{CLA_SENTENCE}'"}
    for name in present:
        workflow = _load(name)
        assert PRIVILEGED_WORKFLOWS[name], name
        for job_name, job in workflow["jobs"].items():
            assert not _may_be_self_hosted(job), f"{name}/{job_name}"
            for step in job.get("steps", []):
                assert not str(step.get("uses", "")).startswith("actions/checkout"), f"{name}/{job_name}"
                assert "run" not in step, f"{name}/{job_name}: {step.get('name')}"
                if "uses" in step:
                    assert SHA.fullmatch(step["uses"]), f"{name}/{job_name}: {step['uses']}"
            # Every clause of the job condition is either the pull_request_target event or one of the two
            # exact comment bodies; anything else (a looser match, another event) fails.
            clauses = [c.strip() for c in " ".join(job["if"].split()).split("||")]
            assert clauses, f"{name}/{job_name}"
            for clause in clauses:
                assert clause == "github.event_name == 'pull_request_target'" or clause in allowed_comments, (
                    f"{name}/{job_name}: {clause}"
                )
            assert allowed_comments <= set(clauses), f"{name}/{job_name}"


def test_release_workflows_are_least_privilege():
    """Versioning and releases (docs/releasing.md): the CI_RUNNER runner expression, a read-only default, write scopes
    named per job and only the two a release needs, a pinned checkout without persisted credentials, and
    no trigger that runs pull-request code with a write token."""
    for name in RELEASE_WORKFLOWS:
        if name not in WORKFLOW_NAMES:
            continue  # the public tree leaves the upstream-only ones out
        workflow = _load(name)
        assert PRIVILEGED_WORKFLOWS[name], name
        assert workflow["permissions"] == {"contents": "read"}, name
        events = workflow["on"]
        assert "pull_request_target" not in events and "issue_comment" not in events, name
        if "pull_request" in events:
            assert name == "labeler.yml", name
        text = (WORKFLOW_DIR / name).read_text()
        assert not re.search(r"\$\{\{\s*secrets\.", text), name
        granted = set()
        for job_name, job in workflow["jobs"].items():
            assert job["runs-on"] == CI_RUNNER_EXPRESSION, f"{name}/{job_name}"
            assert "timeout-minutes" in job, f"{name}/{job_name}"
            granted |= {k for k, v in job["permissions"].items() if v == "write"}
            for step in job["steps"]:
                if str(step.get("uses", "")).startswith("actions/checkout"):
                    assert step["with"]["persist-credentials"] == "false", f"{name}/{job_name}"
                if "uses" in step:
                    assert SHA.fullmatch(step["uses"]), f"{name}/{job_name}: {step['uses']}"
        allowed = {"labeler.yml": {"pull-requests"}, "release.yml": {"contents", "pull-requests"},
                   "release-tag.yml": {"contents"}, "release-public.yml": {"contents"}}[name]
        assert granted == allowed, f"{name}: {granted}"


def test_labeler_runs_trusted_code_for_same_repo_pull_requests_only():
    workflow = _load("labeler.yml")
    job = workflow["jobs"]["label"]
    assert job["if"] == "github.event.pull_request.head.repo.full_name == github.repository"
    checkout = job["steps"][0]
    # The base commit, never the pull request's merge ref or head: no pull-request code runs with the write token.
    assert checkout["with"]["ref"] == "${{ github.event.pull_request.base.sha }}"
    assert all("head" not in str(step.get("with", {}).get("ref", "")) for step in job["steps"])


def test_release_workflows_gate_who_and_where():
    if "release.yml" in WORKFLOW_NAMES:  # the private release workflows are not exported
        assert _load("release.yml")["jobs"]["release-pr"]["if"] == (
            "github.ref == 'refs/heads/main' && github.actor == github.repository_owner"
        )
        assert list(_load("release.yml")["on"]) == ["workflow_dispatch"]
        assert _load("release-tag.yml")["on"]["push"]["branches"] == ["main"]
    if "release-public.yml" in WORKFLOW_NAMES:
        # Exported to the public mirror with the rest of the workflows; inert anywhere else.
        assert _load("release-public.yml")["jobs"]["release"]["if"] == "github.repository == 'juice91/durable'"


def test_deploy_records_the_version_it_deployed():
    if "deploy.yml" not in WORKFLOW_NAMES:
        return
    steps = _load("deploy.yml")["jobs"]["deploy"]["steps"]
    record = next(s for s in steps if s.get("name") == "Record the version being deployed")
    assert "GITHUB_STEP_SUMMARY" in record["run"] and "SHA:0:12" in record["run"]
    assert steps.index(record) < next(i for i, s in enumerate(steps) if s.get("name") == "Deploy this revision")


def test_hosted_path_filters_skip_unaffected_jobs():
    ci = _load("ci.yml")
    def condition(job: str) -> str:
        return " ".join(ci["jobs"][job]["if"].split())

    assert condition("changes") == SAME_REPO_ONLY
    for job, output in (("python", "python"), ("studio", "studio"), ("studio-test", "studio"), ("deploy-checks", "deploy")):
        assert condition(job) == f"({SAME_REPO_ONLY}) && needs.changes.outputs.{output} == 'true'", job
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
    assert _load("ci.yml")["jobs"]["changes"]["timeout-minutes"] == "10"
    assert static["timeout-minutes"] == "30"
    assert tests["timeout-minutes"] == "30"


def test_hosted_jobs_cache_the_uv_and_pnpm_stores_on_their_lockfiles():
    jobs = _load("ci.yml")["jobs"]
    for name in ("python", "deploy-checks"):
        uv_step = next(s for s in jobs[name]["steps"] if str(s.get("uses", "")).startswith("astral-sh/setup-uv@"))
        # Hosted runners cache the uv store on the lockfile; self-hosted ones skip the archive round
        # trip (uv's own cache persists there), so the setting is an expression on runner.environment.
        assert uv_step["with"]["enable-cache"] == "${{ runner.environment == 'github-hosted' }}", name
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
CI_JOB_NAMES = ("changes", "python", "studio", "studio-test", "deploy-checks", "site")


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
    assert _load("ci.yml")["jobs"]["studio"]["if"].endswith("needs.changes.outputs.studio == 'true'")


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




HOSTED_RUNNER = re.compile(r"\b(ubuntu|macos|windows)-[\w.]+", re.IGNORECASE)


def test_no_workflow_hard_codes_a_github_hosted_runner():
    # GitHub-hosted runners are blocked for the private repository. A job either targets self-hosted labels or
    # reads the repository variable CI_RUNNER, falling back to ubuntu-latest where it is unset (the public mirror).
    for name in WORKFLOW_NAMES:
        if name in ("cla.yml", "codeql.yml", "pages.yml", "security.yml"):
            continue  # owned by the public repository, where GitHub-hosted runners are the norm
        for job_name, job in (_load(name).get("jobs") or {}).items():
            runs_on = job.get("runs-on")
            assert runs_on, f"{name}:{job_name} has no runs-on"
            labels = runs_on if isinstance(runs_on, list) else [runs_on]
            if "self-hosted" in labels:
                continue
            text = " ".join(str(label) for label in labels)
            assert "vars.CI_RUNNER" in text and "fromJSON(vars.CI_RUNNER)" in text, (
                f"{name}:{job_name} runs-on {runs_on!r} must use the CI_RUNNER fallback expression"
            )
            if HOSTED_RUNNER.search(text):
                assert text.count("ubuntu-latest") == 1 and not re.search(r"macos|windows", text, re.I), (
                    f"{name}:{job_name} hard-codes a hosted runner beyond the fallback"
                )


def test_labeler_skips_when_the_base_commit_has_no_labeler_script():
    # A pull request into a repository whose base predates scripts/release/labels.py (the first sync) must pass,
    # not fail on a missing file: every step after the checkout is guarded on the script being present.
    steps = _load("labeler.yml")["jobs"]["label"]["steps"]
    guard = "hashFiles('scripts/release/labels.py') != ''"
    assert all(step.get("if") == guard for step in steps[1:])


# ---- GitHub flow: Claude PR review and Project sync (operator-only; absent from the public tree) -------------------


def test_claude_review_comments_only_on_ready_same_repo_pull_requests_and_skips_without_a_credential():
    if "claude-code-review.yml" not in WORKFLOW_NAMES:
        return
    workflow = _load("claude-code-review.yml")
    assert workflow["on"]["pull_request"]["types"] == ["opened", "synchronize", "ready_for_review"]
    assert workflow["permissions"] == {"contents": "read"}
    job = workflow["jobs"]["review"]
    assert job["runs-on"] == CI_RUNNER_EXPRESSION
    assert job["timeout-minutes"] == "15"
    assert _guarded(job) and "!github.event.pull_request.draft" in job["if"]
    # Least privilege: contents stay read-only (no pushes or commits), one comment scope, the App token exchange.
    assert job["permissions"] == {"contents": "read", "pull-requests": "write", "id-token": "write"}
    assert workflow["concurrency"]["cancel-in-progress"] == "true"
    steps = job["steps"]
    check = steps[0]
    assert check["id"] == "auth" and "::notice::" in check["run"]
    # Everything after the credential check runs only when one exists; the secrets reach it through env, not run text.
    assert all(step.get("if") == "steps.auth.outputs.present == 'true'" for step in steps[1:])
    assert not re.search(r"\$\{\{\s*secrets\.", check["run"])
    assert next(s for s in steps if s.get("uses", "").startswith("actions/checkout"))["with"]["persist-credentials"] == "false"
    action = next(s for s in steps if s.get("uses", "").startswith("anthropics/claude-code-action@"))
    args = action["with"]["claude_args"]
    tools = re.search(r'--allowedTools "([^"]+)"', args).group(1)
    assert "Edit" not in tools and "Write" not in tools and "Bash(git push" not in tools and "Bash(git commit" not in tools
    assert {"Read", "Grep", "Glob", "Bash(gh pr diff:*)", "Bash(git diff:*)", "Bash(git log:*)"} <= set(tools.split(","))
    assert "AGENTS.md" in action["with"]["prompt"]
    for secret in OPERATOR_SECRETS["claude-code-review.yml"]:
        assert f"secrets.{secret}" in (WORKFLOW_DIR / "claude-code-review.yml").read_text()


def test_project_sync_needs_its_token_and_variable_and_finds_ids_by_name():
    if "project-sync.yml" not in WORKFLOW_NAMES:
        return
    workflow = _load("project-sync.yml")
    assert workflow["on"]["pull_request"]["types"] == ["opened", "ready_for_review", "converted_to_draft"]
    assert workflow["permissions"] == {"contents": "read"}
    job = workflow["jobs"]["sync"]
    assert job["runs-on"] == CI_RUNNER_EXPRESSION and "timeout-minutes" in job
    assert _guarded(job)
    assert job["permissions"] == {"contents": "read", "pull-requests": "read"}
    assert job["env"]["PROJECT_TOKEN"] == "${{ secrets.PROJECT_TOKEN }}"
    assert job["env"]["PROJECT_NUMBER"] == "${{ vars.PROJECT_NUMBER }}"
    # No checkout: nothing from the pull request runs next to the project token.
    assert not any(str(step.get("uses", "")).startswith("actions/checkout") for step in job["steps"])
    run = job["steps"][0]["run"]
    assert '[ -z "$PROJECT_TOKEN" ] || [ -z "$PROJECT_NUMBER" ]' in run and "::notice::Project sync skipped" in run
    assert "In review" in run and "In progress" in run
    assert 'field(name: \\"Status\\")' in run and "options { id name }" in run  # ids are looked up, not hard-coded
    assert not re.search(r"PVT[SF]?_|PVTSSF_", run)
    assert "closingIssuesReferences" in run


def test_github_flow_files_are_operator_only_in_the_export():
    flow = [
        ".github/workflows/claude-code-review.yml",
        ".github/workflows/project-sync.yml",
        ".github/ISSUE_TEMPLATE",
        ".github/pull_request_template.md",
        "docs/github-flow.md",
    ]
    export = ROOT / "deploy" / "export" / "exclude-paths.txt"
    if not export.exists():
        return  # the public tree has no export tooling
    rules = export.read_text().splitlines()
    for path in flow:
        assert path in rules, path
    agents = (ROOT / "AGENTS.md").read_text()
    link = "[docs/github-flow.md](docs/github-flow.md)"


def test_the_site_job_builds_the_website_as_the_pages_workflow_does():
    jobs = _load("ci.yml")["jobs"]
    runs = [step.get("run", "") for step in jobs["site"]["steps"]]
    assert "bash site/build-pages.sh" in runs
    assert "needs.changes.outputs.site == 'true'" in jobs["site"]["if"]
    paths = yaml.load(
        next(step for step in jobs["changes"]["steps"] if step.get("id") == "filter")["with"]["filters"],
        Loader=yaml.BaseLoader,
    )["site"]
    assert {"site/**", "docs/**"} <= set(paths)
