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
        assert "secrets." not in text.lower(), name


def test_deploy_is_approval_gated_and_serialized():
    deploy = _load("deploy.yml")
    assert deploy.get("concurrency") == {"group": "deploy-studio", "cancel-in-progress": "false"}
    deploy_job = deploy["jobs"]["deploy"]
    assert deploy_job.get("environment") == "studio"


def test_hosted_ci_never_builds_studio():
    text = (WORKFLOW_DIR / "ci.yml").read_text()
    assert "pnpm build" not in text


def test_no_workflow_lists_issue_comment_or_a_write_permission():
    for name in WORKFLOW_NAMES:
        text = (WORKFLOW_DIR / name).read_text()
        assert "issue_comment" not in text, name
        assert "contents: write" not in text, name
        assert "pull-requests: write" not in text, name


def test_workflow_run_deploys_only_a_successful_main_push_sha():
    deploy = _load("deploy.yml")
    trigger = deploy["on"]["workflow_run"]
    assert trigger["workflows"] == ["CI Heavy"]
    assert trigger["types"] == ["completed"]
    assert trigger["branches"] == ["main"]
    condition = deploy["jobs"]["deploy"]["if"]
    assert "github.event.workflow_run.conclusion == 'success'" in condition
    assert "github.event.workflow_run.event == 'push'" in condition
    text = (WORKFLOW_DIR / "deploy.yml").read_text()
    assert "github.event.workflow_run.head_sha" in text
    assert '[[ "$sha" =~ ^[0-9a-f]{40}$ ]]' in text
    checkout = next(
        step
        for step in deploy["jobs"]["deploy"]["steps"]
        if str(step.get("uses", "")).startswith("actions/checkout@")
    )
    assert checkout["with"]["ref"] == "${{ steps.revision.outputs.sha }}"
    assert checkout["with"]["persist-credentials"] == "false"
    # The head SHA is copied into env before the shell runs. It is not expanded inside `run:`.
    for step in deploy["jobs"]["deploy"]["steps"]:
        run = step.get("run", "")
        assert "github.event" not in run


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
    assert ci["jobs"]["deploy-checks"]["if"] == "needs.changes.outputs.deploy == 'true'"
    filters = ci["jobs"]["changes"]["steps"][1]["with"]["filters"]
    assert "packages/**" in filters
    assert "apps/studio/**" in filters
    assert "deploy/**" in filters
    assert "tests/deploy/**" in filters


# Peeled commit of pnpm/action-setup v4.1.0. The 40-hex value currently in ci.yml is the
# annotated tag object, which has no action.yml.
PNPM_ACTION_COMMIT = "a7487c7e89a18df4991f7f222e4898a00d66ddda"
PNPM_ACTION_TAG_OBJECT = "7088e561eb65bb68695d245aa206f005ef30921d"


def test_pnpm_action_setup_is_pinned_to_the_v4_1_0_commit():
    text = (WORKFLOW_DIR / "ci.yml").read_text()
    pinned_to_commit = f"pnpm/action-setup@{PNPM_ACTION_COMMIT}" in text
    pinned_to_tag_object = PNPM_ACTION_TAG_OBJECT in text
    if pinned_to_tag_object or not pinned_to_commit:
        raise AssertionError(
            "pnpm/action-setup must be pinned to commit "
            f"{PNPM_ACTION_COMMIT} (v4.1.0); the annotated tag object is not a commit"
        )
