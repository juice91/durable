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


def test_toolhive_script_has_install_status_uninstall_and_pins_0_65_0():
    text = SCRIPT.read_text()
    for command in ("install", "status", "uninstall"):
        assert f"{command})" in text, f"missing {command} subcommand"
    assert "--context" in text
    assert "0.65.0" in text
    assert "oci://ghcr.io/stacklok/toolhive/toolhive-operator-crds" in text
    assert "oci://ghcr.io/stacklok/toolhive/toolhive-operator" in text
    assert "brew install helm" in text
    assert "toolhive-system" in text
    assert "mcpservers.toolhive.stacklok.dev" in text


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


def test_up_script_installs_the_operator_after_the_cluster_is_up():
    up = (REPO_ROOT / "deploy" / "scripts" / "up.sh").read_text()
    assert 'toolhive.sh" install' in up
    assert up.index("guard_contexts") < up.index('toolhive.sh" install')
