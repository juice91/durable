"""Host disk preflight (fake df), kube API port warning, build-cache prune, and the off-site backup manifests."""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import yaml

from kustomize_util import K8S_DIR, containers, kustomize_docs, pod_spec
from test_scripts import PROFILE, _env, _install_stubs, _log, _run, _secrets_env

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB = REPO_ROOT / "deploy" / "scripts" / "lib.sh"
GIB_KB = 1024 * 1024


def _fake_df(tmp: Path, total_gb: int, avail_gb: int) -> Path:
    bindir = tmp / "dfbin"
    bindir.mkdir(exist_ok=True)
    df = bindir / "df"
    df.write_text(
        "#!/bin/sh\n"
        "echo 'Filesystem 1024-blocks Used Available Capacity Mounted on'\n"
        f"echo '/dev/fake {total_gb * GIB_KB} {(total_gb - avail_gb) * GIB_KB} {avail_gb * GIB_KB} 50% /'\n"
    )
    df.chmod(df.stat().st_mode | stat.S_IEXEC)
    return bindir


def _preflight(tmp: Path, total_gb: int, avail_gb: int, extra: dict[str, str] | None = None):
    env = os.environ.copy()
    env["PATH"] = f"{_fake_df(tmp, total_gb, avail_gb)}{os.pathsep}{env['PATH']}"
    env["HOME"] = str(tmp)
    env.pop("COLIMA_HOME", None)
    env.update(extra or {})
    return subprocess.run(
        ["bash", "-c", '. "$1"; host_disk_preflight', "bash", str(LIB)], env=env, text=True, capture_output=True, check=False
    )


def test_preflight_passes_quietly_with_plenty_of_space(tmp_path: Path):
    proc = _preflight(tmp_path, 1000, 500)
    assert proc.returncode == 0 and proc.stderr == ""


def test_preflight_warns_under_15_percent_free(tmp_path: Path):
    proc = _preflight(tmp_path, 400, 50)  # 12% free, 50 GB
    assert proc.returncode == 0
    assert "Warning: only 12%" in proc.stderr


def test_preflight_fails_under_30_gb_free_with_a_clear_message(tmp_path: Path):
    proc = _preflight(tmp_path, 1000, 29)
    assert proc.returncode != 0
    assert "Refusing to deploy: only 29 GB are free" in proc.stderr
    assert "at least 30 GB" in proc.stderr
    assert "builder prune" in proc.stderr


def test_preflight_thresholds_are_configurable(tmp_path: Path):
    assert _preflight(tmp_path, 1000, 29, {"PYDANTICOS_MIN_FREE_GB": "10"}).returncode == 0
    assert _preflight(tmp_path, 1000, 120, {"PYDANTICOS_WARN_FREE_PERCENT": "20"}).stderr.startswith("Warning")
    assert _preflight(tmp_path, 1000, 120, {"PYDANTICOS_MIN_FREE_GB": "abc"}).returncode != 0


def test_up_sh_stops_on_a_full_disk_before_the_vm_or_any_build(tmp_path: Path):
    bindir = _install_stubs(tmp_path)
    env = _env(tmp_path, bindir, {"PYDANTICOS_MIN_FREE_GB": "30"})
    env["PATH"] = f"{_fake_df(tmp_path, 1000, 5)}{os.pathsep}{env['PATH']}"
    proc = _run("up.sh", env, ["--secrets-env", str(_secrets_env(tmp_path))])
    assert proc.returncode != 0
    assert "Refusing to deploy: only 5 GB are free" in proc.stderr
    log = _log(env)
    assert "colima start" not in log and " build " not in log


def test_up_sh_prunes_the_build_cache_after_the_image_builds(tmp_path: Path):
    bindir = _install_stubs(tmp_path)
    env = _env(tmp_path, bindir)
    _run("up.sh", env, ["--secrets-env", str(_secrets_env(tmp_path))])
    log = _log(env)
    prune = re.search(r"docker .*builder prune --all --force --filter until=24h", log)
    assert prune, log
    last_build = max(m.start() for m in re.finditer(r"docker .* build ", log))
    assert prune.start() > last_build


def test_colima_port_zero_warns_and_a_pinned_port_does_not(tmp_path: Path):
    def run(port_line: str):
        cfg = tmp_path / "colima.yaml"
        cfg.write_text(f"cpu: 8\nkubernetes:\n  enabled: true\n  # port: 0\n  {port_line}\nmounts: []\n")
        return subprocess.run(
            ["bash", "-c", '. "$1"; warn_unpinned_kube_port "$2"', "bash", str(LIB), str(cfg)],
            text=True,
            capture_output=True,
            check=False,
        )

    assert "kubernetes.port: 0" in run("port: 0").stderr
    assert run("port: 55382").stderr == ""
    assert run("port: 55382").returncode == 0


def test_up_sh_reads_the_live_api_port_for_the_policies_on_every_deploy():
    script = (REPO_ROOT / "deploy" / "scripts" / "up.sh").read_text()
    assert "get endpoints kubernetes -o jsonpath='{.subsets[0].ports[0].port}'" in script
    assert 's|port: 6443$|port: ${api_endpoint_port}|g' in script
    assert "warn_unpinned_kube_port" in script


# ---- off-site backup manifests -------------------------------------------------------------------------------------


def _cron() -> dict:
    return next(d for d in kustomize_docs() if d["kind"] == "CronJob" and d["metadata"]["name"] == "postgres-backup")


def test_backup_pod_runs_the_dump_in_its_own_container():
    spec = pod_spec(_cron())
    assert [c["name"] for c in spec["containers"]] == ["pg-dump"]
    assert "initContainers" not in spec
    dump = spec["containers"][0]
    assert dump["command"] == ["sh", "-eu", "/scripts/backup.sh"]
    sc = dump["securityContext"]
    assert sc["runAsNonRoot"] is True and sc["readOnlyRootFilesystem"] is True and sc["allowPrivilegeEscalation"] is False
    assert dump["resources"]["limits"]["memory"]


def test_backup_has_no_off_site_wiring():
    names = {d["metadata"]["name"] for d in kustomize_docs() if d["kind"] == "NetworkPolicy"}
    assert "backup-egress-object-storage" not in names


def test_every_backup_container_keeps_the_pinned_or_known_images():
    images = [c["image"] for c in containers(_cron())]
    assert any(i.startswith("pgvector/pgvector:") for i in images)
    assert all(":" in i for i in images)
