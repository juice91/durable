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


def test_self_hosted_workflows_never_run_for_pull_requests():
    for name in WORKFLOW_NAMES:
        workflow = _load(name)
        if not any("self-hosted" in job.get("runs-on", []) for job in workflow["jobs"].values()):
            continue
        events = workflow.get("on", {})
        # A fork has no separate event: pull_request / pull_request_target is how its code arrives.
        for forbidden in ("pull_request", "pull_request_target", "issue_comment"):
            assert forbidden not in events, name
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
    assert 'docker --context colima-pydanticos rm -f "$CI_PG_NAME"' in text
    assert '-p "127.0.0.1:${CI_PG_PORT}:5432"' in text
    assert "CI_PG_PORT: '54359'" in text
    assert "kubectl" not in text
    assert "-n pydanticos" not in text
    steps = _load("ci-heavy.yml")["jobs"]["heavy"]["steps"]
    start = next(step for step in steps if step.get("name") == "Start isolated CI Postgres")
    remove = next(step for step in steps if step.get("name") == "Remove isolated CI Postgres")
    assert 'rm -f "$CI_PG_NAME"' in start["run"]
    assert remove["if"] == "always()"
    assert 'rm -f "$CI_PG_NAME"' in remove["run"]


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
    # Only the push trigger is path-filtered: the nightly and manual runs always run.
    assert "schedule" in _load("ci-heavy.yml")["on"]
    assert "workflow_dispatch" in _load("ci-heavy.yml")["on"]


def test_ci_heavy_cancels_superseded_runs_of_the_same_ref_only():
    concurrency = _load("ci-heavy.yml")["concurrency"]
    # One group per ref: a newer run cancels an older run of the same branch, and a run of another
    # branch (or main) is never in that group, so it is never cancelled by it.
    assert concurrency["group"] == "ci-heavy-${{ github.ref }}"
    assert concurrency["cancel-in-progress"] == "true"


def test_ci_heavy_runs_the_api_suite_under_xdist_on_six_workers():
    steps = _load("ci-heavy.yml")["jobs"]["heavy"]["steps"]
    api = next(step for step in steps if step.get("name") == "API suite")
    assert api["run"] == "uv run pytest apps/api/api_tests -q -n 6"
    # The worker suite stays serial: every worker test shares the one DBOS database.
    worker = next(step for step in steps if step.get("name") == "Nightly worker suite")
    assert "-n" not in worker["run"].split()


def test_pytest_xdist_is_a_dev_dependency():
    text = (ROOT / "pyproject.toml").read_text()
    assert re.search(r'"pytest-xdist[^"]*"', text)
    assert 'name = "pytest-xdist"' in (ROOT / "uv.lock").read_text()


def test_studio_vitest_is_sharded_in_three_jobs_and_static_checks_run_once():
    jobs = _load("ci.yml")["jobs"]
    static, tests = jobs["studio"], jobs["studio-test"]
    static_runs = [step.get("run", "") for step in static["steps"]]
    assert "pnpm -C apps/studio lint" in static_runs
    assert "pnpm -C apps/studio exec tsc --noEmit" in static_runs
    assert not any("vitest" in run or run.endswith(" test") for run in static_runs)
    assert tests["strategy"]["matrix"]["shard"] == ["1", "2", "3"]
    assert tests["strategy"]["fail-fast"] == "false"
    test_runs = [step.get("run", "") for step in tests["steps"]]
    assert "pnpm -C apps/studio exec vitest run --shard=${{ matrix.shard }}/3" in test_runs
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
