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


CURL_STUB = """#!/usr/bin/env bash
out="" url=""
while (($#)); do
  case "$1" in -o) out=$2; shift 2 ;; -*) shift ;; *) url=$1; shift ;; esac
done
printf 'curl %s\\n' "$url" >> "$STUB_LOG"
cp "$FIXTURES/$(basename "$url")" "$out"
"""


def _helm_download(tmp_path: Path, checksum: str | None):
    """Run `install` with no helm/brew on PATH and a stubbed curl serving a fake release tarball."""
    import hashlib
    import platform
    import tarfile

    os_name = "darwin" if platform.system() == "Darwin" else "linux"
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "amd64"
    name = f"helm-v9.9.9-{os_name}-{arch}.tar.gz"
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    payload = tmp_path / "helm"
    payload.write_text("#!/usr/bin/env bash\nexit 0\n")
    payload.chmod(0o755)
    with tarfile.open(fixtures / name, "w:gz") as tar:
        tar.add(payload, arcname=f"{os_name}-{arch}/helm")
    digest = hashlib.sha256((fixtures / name).read_bytes()).hexdigest()
    (fixtures / f"{name}.sha256sum").write_text(f"{checksum or digest}  {name}\n")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool, body in (("curl", CURL_STUB), ("kubectl", STUB)):
        (bin_dir / tool).write_text(body)
        (bin_dir / tool).chmod(0o755)
    log = tmp_path / "calls.log"
    log.write_text("")
    install_dir = tmp_path / "opt" / "bin"
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "STUB_LOG": str(log),
        "FIXTURES": str(fixtures),
        "HOME": str(tmp_path),
        "HELM_VERSION": "v9.9.9",
        "HELM_INSTALL_DIR": str(install_dir),
    }
    proc = subprocess.run(["/bin/bash", str(SCRIPT), "install"], capture_output=True, text=True, cwd=REPO_ROOT, env=env)
    return proc, install_dir, log.read_text().splitlines(), name


def test_install_downloads_helm_into_install_dir_when_absent(tmp_path):
    proc, install_dir, calls, name = _helm_download(tmp_path, None)
    assert (install_dir / "helm").is_file(), proc.stderr
    assert f"curl https://get.helm.sh/{name}" in calls
    assert f"curl https://get.helm.sh/{name}.sha256sum" in calls
    assert f"Installed helm v9.9.9 to {install_dir}/helm" in proc.stderr
    # PATH was extended, so the rest of the script ran the new helm (a no-op binary) and finished.
    assert proc.returncode == 0, proc.stderr


def test_install_rejects_a_helm_tarball_with_a_bad_checksum(tmp_path):
    proc, install_dir, _calls, _name = _helm_download(tmp_path, "0" * 64)
    assert proc.returncode != 0
    assert "Checksum mismatch" in proc.stderr
    assert not (install_dir / "helm").exists()
