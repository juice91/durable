"""deploy/scripts/toolhive.sh: lint and argument handling, no cluster needed."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "deploy" / "scripts" / "toolhive.sh"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, cwd=REPO_ROOT)


STUB = """#!/usr/bin/env bash
printf '%s\\n' "$(basename "$0") $*" >> "$STUB_LOG"
if [[ "$(basename "$0")" == kubectl && "$*" == *"get crd -o name"* ]]; then
  printf 'customresourcedefinition.apiextensions.k8s.io/mcpservers.toolhive.stacklok.dev\\n'
  printf 'customresourcedefinition.apiextensions.k8s.io/other.example.com\\n'
fi
exit 0
"""


def _stubbed(tmp_path: Path, *args: str, tools: tuple[str, ...] = ("helm", "kubectl"), unreachable: bool = False):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for tool in tools:
        stub = bin_dir / tool
        body = STUB
        if unreachable and tool == "kubectl":
            body = STUB.replace("exit 0", 'if [[ "$*" == *cluster-info* ]]; then exit 1; fi\nexit 0')
        stub.write_text(body)
        stub.chmod(0o755)
    log = tmp_path / "calls.log"
    log.write_text("")
    # Only the stub dir plus the system essentials: a real helm/kubectl must never be found.
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "STUB_LOG": str(log), "HOME": str(tmp_path)}
    proc = subprocess.run(
        ["/bin/bash", str(SCRIPT), *args], capture_output=True, text=True, cwd=REPO_ROOT, env=env
    )
    return proc, log.read_text().splitlines()


def _assert_every_call_has_context(calls: list[str], context: str = "colima-pydanticos"):
    assert calls
    for call in calls:
        tool, rest = call.split(" ", 1)
        flag = "--kube-context" if tool == "helm" else "--context"
        assert f"{flag} {context}" in rest, call


def test_install_pins_both_charts_and_scopes_every_call_to_the_context(tmp_path):
    proc, calls = _stubbed(tmp_path, "install")
    assert proc.returncode == 0, proc.stderr
    _assert_every_call_has_context(calls)
    installs = [c for c in calls if c.startswith("helm") and "upgrade --install" in c]
    assert len(installs) == 2
    for chart in (
        "oci://ghcr.io/stacklok/toolhive/toolhive-operator-crds",
        "oci://ghcr.io/stacklok/toolhive/toolhive-operator",
    ):
        (line,) = [c for c in installs if f"{chart} " in c + " "]
        assert "--version 0.65.0" in line
        assert "--namespace toolhive-system" in line


def test_custom_context_reaches_helm_and_kubectl(tmp_path):
    proc, calls = _stubbed(tmp_path, "status", "--context", "other-ctx")
    assert proc.returncode == 0, proc.stderr
    _assert_every_call_has_context(calls, "other-ctx")


def test_status_checks_reachability_then_lists_and_never_needs_brew(tmp_path):
    proc, calls = _stubbed(tmp_path, "status")
    assert proc.returncode == 0, proc.stderr
    _assert_every_call_has_context(calls)
    assert "kubectl --context colima-pydanticos cluster-info" in calls[0]
    assert any(c.startswith("helm") and " list " in c for c in calls)


def test_status_and_uninstall_fail_fast_without_helm(tmp_path):
    for args in (("status",), ("uninstall", "--yes")):
        proc, calls = _stubbed(tmp_path, *args, tools=("kubectl",))
        assert proc.returncode == 127, args
        assert "helm" in proc.stderr
        assert calls == []


def test_unreachable_cluster_fails_every_subcommand_before_helm_runs(tmp_path):
    for args in (("install",), ("status",), ("uninstall", "--yes")):
        proc, calls = _stubbed(tmp_path, *args, unreachable=True)
        assert proc.returncode == 1, args
        assert "not reachable" in proc.stderr
        assert not any(c.startswith("helm") for c in calls), args


def test_uninstall_requires_yes_before_touching_anything(tmp_path):
    proc, calls = _stubbed(tmp_path, "uninstall")
    assert proc.returncode == 64
    assert "--yes" in proc.stderr
    assert not any(c.startswith("helm") for c in calls)
    assert not any(" delete " in c for c in calls)


def test_uninstall_with_yes_removes_releases_toolhive_crds_and_namespace(tmp_path):
    proc, calls = _stubbed(tmp_path, "uninstall", "--yes")
    assert proc.returncode == 0, proc.stderr
    _assert_every_call_has_context(calls)
    assert any("helm" in c and "uninstall toolhive-operator " in c + " " for c in calls)
    assert any("helm" in c and "uninstall toolhive-operator-crds" in c for c in calls)
    deletes = [c for c in calls if c.startswith("kubectl") and " delete " in c]
    assert any("mcpservers.toolhive.stacklok.dev" in c for c in deletes)
    assert not any("other.example.com" in c for c in deletes)
    assert any("namespace toolhive-system" in c for c in deletes)


def test_toolhive_script_passes_shellcheck():
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        pytest.skip("shellcheck is not installed")
    proc = subprocess.run([shellcheck, "-x", str(SCRIPT)], capture_output=True, text=True, cwd=REPO_ROOT)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_toolhive_script_rejects_bad_arguments_without_touching_a_cluster():
    assert _run().returncode == 64
    assert _run("explode").returncode == 64
    assert _run("install", "--bogus").returncode == 64
    missing = _run("install", "--context")
    assert missing.returncode == 64
    assert "Usage" in _run("--help").stdout + _run("--help").stderr
    bad = _run("explode")
    assert "Usage" in bad.stderr


def test_up_script_installs_the_operator_after_the_cluster_is_up():
    up = (REPO_ROOT / "deploy" / "scripts" / "up.sh").read_text()
    assert 'toolhive.sh" install' in up
    assert up.index("guard_contexts") < up.index('toolhive.sh" install')
