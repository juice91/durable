"""shellcheck on deploy/scripts. Skip only if the binary is missing."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_ROOTS = (
    REPO_ROOT / "deploy" / "scripts",
    REPO_ROOT / "deploy" / "runner",
    REPO_ROOT / "deploy" / "k8s",
    REPO_ROOT / "deploy" / "docker",
)


def test_deploy_scripts_pass_shellcheck():
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        pytest.skip("shellcheck is not installed")
    scripts = sorted(path for root in SCRIPT_ROOTS for path in root.glob("*.sh") if path.is_file())
    assert scripts, "no deploy scripts to lint"
    proc = subprocess.run(
        [shellcheck, "-x", *[str(p) for p in scripts]],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
