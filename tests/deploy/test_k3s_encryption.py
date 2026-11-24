"""deploy/scripts/k3s-secrets-encryption.sh against a fake `colima ssh` VM; and the flag for new clusters in up.sh."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

from test_scripts import REPO_ROOT, SCRIPTS_DIR

SCRIPT = SCRIPTS_DIR / "k3s-secrets-encryption.sh"

# A VM whose k3s answers from state files: STUB_STATUS_FILE holds the text `k3s secrets-encrypt status` prints, and a
# `rotate-keys` call moves it to the finished state. Every call is logged.
FAKE_COLIMA = r"""#!/usr/bin/env bash
printf 'colima %s\n' "$*" >>"$STUB_LOG"
case "${1:-}" in
  status) exit 0 ;;
  ssh)
    shift; [[ "$1" == -p ]] && shift 2; [[ "$1" == -- ]] && shift
    printf 'vm %s\n' "$*" >>"$STUB_LOG"
    case "$*" in
      "cat /etc/systemd/system/k3s.service")
        printf "[Service]\nExecStart=/usr/local/bin/k3s \\\\\n    server \\\\\n    '--https-listen-port' \\\\\n    '41234' \\\\\n    '--disable=traefik'\n" ;;
      "sudo k3s secrets-encrypt status"*)
        # STUB_STATUS_FLAKY_FILE: print the 08020 error this many times first (the annotation is written late).
        if [[ -n "${STUB_FLAKY_FILE:-}" && "$(cat "$STUB_FLAKY_FILE")" -gt 0 ]]; then
          echo $(($(cat "$STUB_FLAKY_FILE") - 1)) >"$STUB_FLAKY_FILE"
          echo "secret-encrypt error ID 08020: missing annotation on node colima-pydanticos"; exit 1
        fi
        cat "$STUB_STATUS_FILE" ;;
      "sudo k3s secrets-encrypt rotate-keys"*)
        printf 'Encryption Status: Enabled\nCurrent Rotation Stage: reencrypt_finished\nServer Encryption Hashes: All hashes match\n' >"$STUB_STATUS_FILE" ;;
      "sudo k3s secrets-encrypt enable"*)
        printf 'Encryption Status: Disabled\nCurrent Rotation Stage: start\nServer Encryption Hashes: All hashes match\n' >"$STUB_STATUS_FILE" ;;
      "k3s --version") printf 'k3s version %s (abcdef)\n' "${STUB_K3S_VERSION:-v1.35.3+k3s1}" ;;
      "sudo tee "*) cat >/dev/null ;;
    esac ;;
esac
exit 0
"""
FAKE_KUBECTL = '#!/usr/bin/env bash\nprintf \'kubectl %s\\n\' "$*" >>"$STUB_LOG"\nexit 0\n'

ENABLED = "Encryption Status: Enabled\nCurrent Rotation Stage: reencrypt_finished\nServer Encryption Hashes: All hashes match\n"
SERVER = " --server https://127.0.0.1:41234"
DISABLED = "Encryption Status: Disabled, no configuration file found\n"


def _run(tmp: Path, status: str, args: list[str], version: str = "v1.35.3+k3s1", flaky: int = 0):
    bindir = tmp / "bin"
    bindir.mkdir(exist_ok=True)
    for name, body in (("colima", FAKE_COLIMA), ("kubectl", FAKE_KUBECTL)):
        path = bindir / name
        path.write_text(body)
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    (tmp / "status.txt").write_text(status)
    colima_home = tmp / "colima"
    (colima_home / "pydanticos").mkdir(parents=True, exist_ok=True)
    (colima_home / "pydanticos" / "colima.yaml").write_text("kubernetes:\n  enabled: true\n  k3sArgs:\n    - --disable=traefik\n")
    env = os.environ.copy()
    if flaky:
        (tmp / "flaky").write_text(str(flaky))
        env["STUB_FLAKY_FILE"] = str(tmp / "flaky")
    env.update(
        PATH=f"{bindir}{os.pathsep}{env['PATH']}", STUB_LOG=str(tmp / "log"), STUB_STATUS_FILE=str(tmp / "status.txt"),
        STUB_K3S_VERSION=version, COLIMA_HOME=str(colima_home), PYDANTICOS_K3S_ROTATION_TIMEOUT="5", PYDANTICOS_K3S_STATUS_WAIT="10",
    )
    proc = subprocess.run(["bash", str(SCRIPT), *args], cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60)
    log = (tmp / "log").read_text() if (tmp / "log").exists() else ""
    return proc, log, (colima_home / "pydanticos" / "colima.yaml").read_text()


def test_status_is_read_only_and_reports_the_state(tmp_path: Path):
    proc, log, _ = _run(tmp_path, ENABLED, ["--status"])
    assert proc.returncode == 0 and "reencrypt_finished" in proc.stdout
    assert "restart" not in log and "rotate-keys" not in log and "tee" not in log


def test_status_fails_when_encryption_is_off(tmp_path: Path):
    proc, _, _ = _run(tmp_path, DISABLED, ["--status"])
    assert proc.returncode == 1


def test_enabling_on_an_already_encrypting_cluster_changes_nothing_but_the_persistent_flag(tmp_path: Path):
    proc, log, colima_yaml = _run(tmp_path, ENABLED, ["--yes"])
    assert proc.returncode == 0, proc.stderr
    assert "already enabled" in proc.stdout
    for action in ("rotate-keys", "systemctl restart", "secrets-encrypt enable"):
        assert action not in log
    assert "--secrets-encryption" in colima_yaml and "--disable=traefik" in colima_yaml  # the VM would keep encrypting if recreated
    # A second run is a no-op on the file too.
    proc2, _, colima_yaml2 = _run(tmp_path, ENABLED, ["--yes"])
    assert proc2.returncode == 0 and colima_yaml2.count("--secrets-encryption") == 1


def test_enabling_runs_the_documented_sequence_in_order(tmp_path: Path):
    proc, log, colima_yaml = _run(tmp_path, DISABLED, ["--yes"])
    # With the fake VM the status after `enable` is "Disabled / start", the state the real server reports.
    assert proc.returncode == 0, proc.stdout + proc.stderr
    order = [
        f"sudo k3s secrets-encrypt enable{SERVER}",
        "sudo mkdir -p /etc/rancher/k3s/config.yaml.d",
        "sudo tee /etc/rancher/k3s/config.yaml.d/20-secrets-encryption.yaml",
        "sudo systemctl restart k3s",
        f"sudo k3s secrets-encrypt rotate-keys{SERVER}",
        "sudo systemctl restart k3s",
    ]
    position = -1
    for step in order:
        position = log.find(step, position + 1)
        assert position >= 0, step
    assert "--secrets-encryption" in colima_yaml
    assert "get secrets" in log  # kubectl still works afterwards


def test_an_old_k3s_is_refused_before_anything_changes(tmp_path: Path):
    proc, log, colima_yaml = _run(tmp_path, DISABLED, ["--yes"], version="v1.35.0+k3s1")
    assert proc.returncode == 1 and "v1.35.3" in proc.stderr
    assert "restart" not in log and "enable" not in log and "tee" not in log
    assert "--secrets-encryption" not in colima_yaml


def test_without_yes_and_without_a_terminal_it_refuses(tmp_path: Path):
    proc, log, _ = _run(tmp_path, DISABLED, [])
    assert proc.returncode == 1 and "--yes" in proc.stderr
    assert "restart" not in log


def test_rollback_disables_restarts_and_rewrites_the_secrets(tmp_path: Path):
    proc, log, _ = _run(tmp_path, ENABLED, ["--rollback", "--yes"])
    assert proc.returncode == 0, proc.stderr
    assert log.index("secrets-encrypt disable") < log.index("systemctl restart k3s") < log.index("secrets-encrypt reencrypt --force --skip")


def test_usage_error_exits_64():
    proc = subprocess.run(["bash", str(SCRIPT), "--nope"], capture_output=True, text=True)
    assert proc.returncode == 64


def test_up_sh_starts_new_clusters_with_secrets_encryption_and_keeps_the_traefik_default():
    up = (SCRIPTS_DIR / "up.sh").read_text()
    new_cluster = [ln for ln in up.splitlines() if "--kubernetes --runtime docker" in ln]
    assert len(new_cluster) == 1
    tail = "\n".join(up.split(new_cluster[0], 1)[1].splitlines()[:2])  # the command continues on the next line
    assert "--k3s-arg=--disable=traefik" in tail and "--k3s-arg=--secrets-encryption" in tail


def test_every_secrets_encrypt_call_targets_the_port_read_from_the_vm(tmp_path: Path):
    proc, log, _ = _run(tmp_path, DISABLED, ["--yes"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    calls = [ln for ln in log.splitlines() if ln.startswith("vm sudo k3s secrets-encrypt")]
    assert len(calls) >= 4 and all(ln.endswith(SERVER) for ln in calls), calls
    (tmp_path / "rb").mkdir()
    proc, log, _ = _run(tmp_path / "rb", ENABLED, ["--rollback", "--yes"])
    calls = [ln for ln in log.splitlines() if ln.startswith("vm sudo k3s secrets-encrypt")]
    assert {ln.split()[4] for ln in calls} >= {"status", "disable", "reencrypt"} and all(ln.endswith(SERVER) for ln in calls)


def test_listen_port_parsing(tmp_path: Path):
    def parse(unit: str) -> str:
        fn = SCRIPT.read_text().split("parse_listen_port() {", 1)[1].split("\n}\n", 1)[0]
        out = subprocess.run(["bash", "-c", "parse_listen_port() {" + fn + "\n}\nparse_listen_port"], input=unit, capture_output=True, text=True)
        return out.stdout.strip()
    assert parse("ExecStart=/usr/local/bin/k3s \\\n  server \\\n  '--https-listen-port' \\\n  '41234' \\\n") == "41234"
    assert parse("ExecStart=/usr/local/bin/k3s server --https-listen-port=40001 --disable=traefik\n") == "40001"
    assert parse("ExecStart=/usr/local/bin/k3s server --https-listen-port 40002\n") == "40002"
    assert parse("ExecStart=/usr/local/bin/k3s server\n") == "6443"
    assert parse("") == "6443"


def test_status_retries_while_the_node_annotation_is_missing_after_a_restart(tmp_path: Path):
    proc, log, _ = _run(tmp_path, DISABLED, ["--yes"], flaky=3)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "08020" not in proc.stderr and "Unexpected status" not in proc.stderr
    assert log.count("secrets-encrypt status") >= 4


def test_status_gives_up_after_the_bounded_wait(tmp_path: Path):
    proc, _, _ = _run(tmp_path, ENABLED, ["--status"], flaky=10_000)
    assert proc.returncode == 1 and "08020" in proc.stdout
