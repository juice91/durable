"""Dry-run deploy scripts against stubbed colima/docker/kubectl/ssh."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "deploy" / "scripts"
EXPECTED_CONTEXT = "colima-pydanticos"
PROFILE = "pydanticos"

FAKE_BIN = Path(__file__).with_name("fake_bin.sh")
SECRET = "SECRETVALUE_9f3c1a7e_MUST_NOT_APPEAR"
SECRET_KEYS = (
    "POSTGRES_PASSWORD",
    "EAP_APP_DATABASE_PASSWORD",
    "EAP_DBOS_DATABASE_PASSWORD",
    "EAP_DATABASE_URL",
    "EAP_DBOS_URL",
    "EAP_SECRETS_KEY",
    "EAP_LITELLM_API_KEY",
    "EAP_LITELLM_EMBEDDING_API_KEY",
    "EAP_AUDIT_HASH_SALT",
    "EAP_BLOB_S3_ACCESS_KEY",
    "EAP_BLOB_S3_SECRET_KEY",
)
CLASH_PORTS = {"3000", "8000", "3002"}


def _install_stubs(tmp: Path) -> Path:
    bindir = tmp / "bin"
    bindir.mkdir(exist_ok=True)
    target = bindir / "stub"
    shutil.copy(FAKE_BIN, target)
    target.chmod(target.stat().st_mode | stat.S_IEXEC)
    for name in ("colima", "docker", "kubectl", "ssh"):
        link = bindir / name
        if not link.exists():
            link.symlink_to(target)
    (tmp / "docker_context").write_text(EXPECTED_CONTEXT + "\n")
    (tmp / "kube_context").write_text(EXPECTED_CONTEXT + "\n")
    return bindir


def _env(tmp: Path, bindir: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
    env["STUB_LOG"] = str(tmp / "stub.log")
    env["STUB_STATE"] = str(tmp)
    env["REAL_KUBECTL"] = shutil.which("kubectl") or "/opt/homebrew/bin/kubectl"
    env.pop("DOCKER_HOST", None)
    # Stopped profile: do not read the real ~/.colima mount list.
    env["STUB_COLIMA_STOPPED"] = "1"
    env["PYDANTICOS_DATA_DIR"] = str(tmp / "pydanticos-data")
    if extra:
        env.update(extra)
    return env


def _mount_args(log: str) -> list[str]:
    return re.findall(r"--mount\s+(\S+)", log)


def _at(log: str, needle: str, start: int = 0) -> int:
    found = log.find(needle, start)
    assert found >= 0, needle
    return found


def _secrets_env(tmp: Path) -> Path:
    path = tmp / "secrets.env"
    path.write_text("".join(f"{key}={SECRET}\n" for key in SECRET_KEYS))
    path.chmod(0o600)
    return path


def _run(
    script: str,
    env: dict[str, str],
    args: list[str] | None = None,
    stdin: str | None = None,
    timeout: float | None = None,
):
    proc = subprocess.run(
        ["bash", str(SCRIPTS_DIR / script), *(args or [])],
        cwd=REPO_ROOT,
        env=env,
        input=stdin,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )
    return proc


def _assert_no_background_kubectl(bindir: Path) -> None:
    proc = subprocess.run(
        ["ps", "-axww", "-o", "pid=,command="],
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    needle = str(bindir)
    leftovers = [line.strip() for line in proc.stdout.splitlines() if needle in line]
    assert leftovers == []


def _log(env: dict[str, str]) -> str:
    return Path(env["STUB_LOG"]).read_text() if Path(env["STUB_LOG"]).exists() else ""


def test_up_sh_refuses_wrong_docker_endpoint(tmp_path: Path):
    secrets = _secrets_env(tmp_path)
    before = secrets.read_bytes()
    env = _env(tmp_path, _install_stubs(tmp_path), {"STUB_DOCKER_HOST": "unix:///wrong/docker.sock"})
    proc = _run("up.sh", env, ["--secrets-env", str(secrets)])
    assert proc.returncode != 0
    combined = proc.stdout + proc.stderr
    assert "Refusing to continue" in combined
    assert "docker.sock" in combined
    assert secrets.read_bytes() == before
    assert secrets.is_file()


def test_up_sh_preserves_global_contexts_and_uses_explicit_contexts(tmp_path: Path):
    bindir = _install_stubs(tmp_path)
    (tmp_path / "docker_context").write_text("colima\n")
    (tmp_path / "kube_context").write_text("developer-cluster\n")
    env = _env(tmp_path, bindir)
    proc = _run("up.sh", env, ["--secrets-env", str(_secrets_env(tmp_path))])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (tmp_path / "docker_context").read_text() == "colima\n"
    assert (tmp_path / "kube_context").read_text() == "developer-cluster\n"
    log = _log(env)
    assert "docker context use" not in log
    assert "kubectl config use-context" not in log
    assert "colima start pydanticos --activate=false" in log
    data = os.path.realpath(env["PYDANTICOS_DATA_DIR"])
    assert _mount_args(log) == [f"{data}:w"]
    assert "--mount /Users/Shared:w" not in log
    assert f"--mount {Path.home()}:w" not in log
    mode = (tmp_path / "pydanticos-data").stat().st_mode & 0o777
    assert mode == 0o700
    for line in log.splitlines():
        if line.startswith("docker ") and (" build " in f" {line} " or " context inspect " in f" {line} "):
            assert f"--context {EXPECTED_CONTEXT}" in line
        if line.startswith("kubectl "):
            assert f"--context {EXPECTED_CONTEXT}" in line


def test_scripts_never_reference_colima_default_profile():
    lib = (SCRIPTS_DIR / "lib.sh").read_text()
    assert re.search(r"^PROFILE=pydanticos$", lib, re.M)
    for path in SCRIPTS_DIR.glob("*.sh"):
        assert not re.search(r"colima(?:\s+\S+)*\s+default\b", path.read_text()), path.name


def test_up_sh_builds_sha_tags_orders_rollout_and_hides_secrets(tmp_path: Path):
    bindir = _install_stubs(tmp_path)
    env = _env(tmp_path, bindir)
    proc = _run("up.sh", env, ["--secrets-env", str(_secrets_env(tmp_path))])
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, combined
    assert SECRET not in combined
    log = _log(env)
    assert SECRET not in log
    assert "colima " in log
    assert not re.search(r"^colima .* default(?:\s|$)", log, re.M)
    sha = subprocess.check_output(["git", "-C", str(REPO_ROOT), "rev-parse", "--short=12", "HEAD"], text=True).strip()
    assert f"pydanticos-python:{sha}" in log
    assert f"pydanticos-studio:{sha}" in log
    api0 = _at(log, "scale deployment/api --replicas=0")
    drain = _at(log, "exec postgres-0", api0)
    worker0 = _at(log, "scale statefulset/worker --replicas=0", drain)
    migrate = _at(log, "get job migrate", worker0)
    worker1 = _at(log, "scale statefulset/worker --replicas=1", migrate)
    api1 = _at(log, "scale deployment/api --replicas=1", worker1)
    studio1 = _at(log, "scale deployment/studio --replicas=1", api1)
    assert api0 < drain < worker0 < migrate < worker1 < api1 < studio1
    assert "wait --for=condition" not in log
    _assert_no_background_kubectl(bindir)


def test_up_sh_migrate_success_prints_logs_and_leaves_no_kubectl(tmp_path: Path):
    bindir = _install_stubs(tmp_path)
    env = _env(tmp_path, bindir)
    proc = _run("up.sh", env, ["--secrets-env", str(_secrets_env(tmp_path))], timeout=30)
    combined = proc.stdout + proc.stderr
    assert proc.returncode == 0, combined
    assert "alembic upgrade head ok" in proc.stdout
    log = _log(env)
    assert "get job migrate" in log
    assert "logs job/migrate" in log
    assert "wait --for=condition" not in log
    assert "scale statefulset/worker --replicas=1" in log
    _assert_no_background_kubectl(bindir)


def test_up_sh_migrate_failure_prints_alembic_logs_and_stops(tmp_path: Path):
    bindir = _install_stubs(tmp_path)
    env = _env(tmp_path, bindir, {"STUB_MIGRATE_STATUS": "failed"})
    proc = _run("up.sh", env, ["--secrets-env", str(_secrets_env(tmp_path))], timeout=30)
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0
    assert "Migration Job failed; Alembic output follows." in proc.stderr
    assert "ALEMBIC_ERROR_SENTINEL migration failed at revision" in proc.stderr
    assert SECRET not in combined
    log = _log(env)
    assert "scale statefulset/worker --replicas=1" not in log
    assert "scale deployment/api --replicas=1" not in log
    assert "scale deployment/studio --replicas=1" not in log
    assert "Timed out" not in proc.stderr
    _assert_no_background_kubectl(bindir)


def test_up_sh_migrate_get_exits_after_three_consecutive_errors(tmp_path: Path):
    bindir = _install_stubs(tmp_path)
    env = _env(tmp_path, bindir, {"STUB_MIGRATE_GET_ERRORS": "9"})
    proc = _run("up.sh", env, ["--secrets-env", str(_secrets_env(tmp_path))], timeout=20)
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, combined
    assert 'error: jobs.batch "migrate" not found' in proc.stderr
    assert "Timed out" not in combined
    assert "10 minutes" not in combined
    assert "did not reach Complete" not in combined
    log = _log(env)
    assert log.count("get job migrate") == 3
    assert "scale statefulset/worker --replicas=1" not in log
    _assert_no_background_kubectl(bindir)


def test_up_sh_migrate_timeout_says_it_timed_out(tmp_path: Path):
    bindir = _install_stubs(tmp_path)
    env = _env(
        tmp_path,
        bindir,
        {"STUB_MIGRATE_STATUS": "pending", "PYDANTICOS_MIGRATE_TIMEOUT": "1"},
    )
    proc = _run("up.sh", env, ["--secrets-env", str(_secrets_env(tmp_path))], timeout=20)
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, combined
    assert (
        "Timed out after 1 seconds waiting for the migrate Job to succeed or fail."
        in proc.stderr
    )
    assert "10 minutes" not in combined
    log = _log(env)
    assert log.count("get job migrate") == 1
    assert "scale statefulset/worker --replicas=1" not in log
    _assert_no_background_kubectl(bindir)


def test_destroy_sh_prompts_before_deleting(tmp_path: Path):
    env = _env(tmp_path, _install_stubs(tmp_path))
    cancelled = _run("destroy.sh", env, stdin="nope\n")
    assert cancelled.returncode != 0
    assert "colima delete" not in _log(env)
    env2 = _env(tmp_path, _install_stubs(tmp_path))
    confirmed = _run("destroy.sh", env2, stdin=f"{PROFILE}\n")
    assert confirmed.returncode == 0, confirmed.stdout + confirmed.stderr
    assert f"colima delete {PROFILE}" in _log(env2)


def test_tunnel_sh_local_ports_do_not_clash(tmp_path: Path):
    env = _env(tmp_path, _install_stubs(tmp_path), {"PYDANTICOS_TUNNEL_ONCE": "1"})
    proc = _run("tunnel.sh", env, ["deploy-host"], timeout=30)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    log = _log(env)
    assert log.startswith("ssh ")
    locals_ = re.findall(r"-L\s+127\.0\.0\.1:(\d+):127\.0\.0\.1:\1\b", log)
    assert locals_ == ["13000", "18000", "13002"], log
    assert CLASH_PORTS.isdisjoint(locals_), locals_
    remote_binds = re.findall(r"port-forward[^']*?(\d+):\d+", log)
    assert CLASH_PORTS.isdisjoint(remote_binds), remote_binds


def test_down_and_status_use_guarded_context(tmp_path: Path):
    env = _env(tmp_path, _install_stubs(tmp_path))
    down = _run("down.sh", env)
    assert down.returncode == 0, down.stdout + down.stderr
    status = _run("status.sh", env)
    assert status.returncode == 0, status.stdout + status.stderr
    log = _log(env)
    assert f"docker --context {EXPECTED_CONTEXT} context inspect" in log
    assert "docker context use" not in log and "kubectl config use-context" not in log
    assert "scale statefulset/worker" in log


def _refuse(tmp_path: Path, data: str, extra: dict[str, str] | None = None):
    env = os.environ.copy()
    env["PYDANTICOS_DATA_DIR"] = data
    if extra:
        env.update(extra)
    return _run("up.sh", env)


@pytest.mark.parametrize(
    ("path", "snippet"),
    [
        ("/", "the filesystem root"),
        ("/Users", "not allowed (/Users)"),
        ("/Users/Shared", "/Users/Shared"),
    ],
)
def test_up_sh_refuses_forbidden_roots(tmp_path: Path, path: str, snippet: str):
    proc = _refuse(tmp_path, path)
    assert proc.returncode != 0
    assert snippet in proc.stdout + proc.stderr


def test_up_sh_refuses_home_directory(tmp_path: Path):
    proc = _refuse(tmp_path, str(Path.home()))
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0
    assert "the home directory" in combined


def test_up_sh_refuses_parent_of_home(tmp_path: Path):
    home = tmp_path / "Users" / "tester"
    home.mkdir(parents=True)
    proc = _refuse(tmp_path, str(home.parent), {"HOME": str(home)})
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0, combined
    assert "a parent of the home directory" in combined


def test_up_sh_refuses_symlinked_data_dir(tmp_path: Path):
    real = tmp_path / "real-data"
    real.mkdir()
    link = tmp_path / "data-link"
    link.symlink_to(real)
    proc = _refuse(tmp_path, str(link))
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0
    assert "is a symlink" in combined


def test_up_sh_refuses_symlink_component_pointing_outside(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    safe = tmp_path / "safe"
    safe.mkdir()
    jump = safe / "jump"
    jump.symlink_to(outside)
    proc = _refuse(tmp_path, str(jump / "child"))
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0
    assert "pointing outside itself" in combined


def test_up_sh_refuses_directory_not_owned_by_the_user(tmp_path: Path):
    proc = _refuse(tmp_path, "/usr")
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0
    assert "not owned by the current user" in combined


@pytest.mark.parametrize("mode", [0o777, 0o770])
def test_up_sh_refuses_group_or_world_writable_directory(tmp_path: Path, mode: int):
    directory = tmp_path / f"mode-{mode:o}"
    directory.mkdir()
    directory.chmod(mode)
    proc = _refuse(tmp_path, str(directory))
    combined = proc.stdout + proc.stderr
    assert proc.returncode != 0
    assert "group- or world-writable" in combined
    assert directory.stat().st_mode & 0o777 == mode


def test_up_sh_start_mounts_only_the_data_dir_and_creates_it_mode_700(tmp_path: Path):
    env = _env(tmp_path, _install_stubs(tmp_path))
    proc = _run("up.sh", env, ["--secrets-env", str(_secrets_env(tmp_path))])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    data = Path(os.path.realpath(env["PYDANTICOS_DATA_DIR"]))
    assert (data.stat().st_mode & 0o777) == 0o700
    assert (data / "backups").is_dir()
    assert (data / "backups").stat().st_mode & 0o777 == 0o700
    assert _mount_args(_log(env)) == [f"{data}:w"]
    script = (SCRIPTS_DIR / "up.sh").read_text()
    assert 'XDG_CACHE_HOME="$cache_home" colima start' in script


def test_up_sh_new_profile_still_has_exactly_one_mount(tmp_path: Path):
    env = _env(tmp_path, _install_stubs(tmp_path), {"STUB_COLIMA_NEW": "1"})
    proc = _run("up.sh", env, ["--secrets-env", str(_secrets_env(tmp_path))])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    log = _log(env)
    data = os.path.realpath(env["PYDANTICOS_DATA_DIR"])
    assert _mount_args(log) == [f"{data}:w"]
    assert "--kubernetes" in log
    assert "--cpu 8" in log


def _write_mounts(colima_home: Path, mounts: list[tuple[str, str]]) -> None:
    config = colima_home / "pydanticos"
    config.mkdir(parents=True)
    lines = ["mounts:"]
    for location, writable in mounts:
        lines.append(f"  - location: {location}")
        lines.append(f"    writable: {writable}")
    (config / "colima.yaml").write_text("\n".join(lines) + "\n")


def test_up_sh_refuses_a_running_profile_with_extra_mounts(tmp_path: Path):
    env = _env(tmp_path, _install_stubs(tmp_path))
    env.pop("STUB_COLIMA_STOPPED")
    colima_home = tmp_path / "colima-home"
    env["COLIMA_HOME"] = str(colima_home)
    data = os.path.realpath(env["PYDANTICOS_DATA_DIR"])
    _write_mounts(colima_home, [(data, "true"), ("/Users/Shared", "true")])
    proc = _run("up.sh", env, ["--secrets-env", str(_secrets_env(tmp_path))])
    assert proc.returncode != 0
    assert "Stop it and rerun" in proc.stdout + proc.stderr
    assert "colima start" not in _log(env)


def test_up_sh_accepts_a_running_profile_whose_only_mount_is_the_data_dir(tmp_path: Path):
    env = _env(tmp_path, _install_stubs(tmp_path))
    env.pop("STUB_COLIMA_STOPPED")
    colima_home = tmp_path / "colima-home"
    env["COLIMA_HOME"] = str(colima_home)
    data = os.path.realpath(env["PYDANTICOS_DATA_DIR"])
    _write_mounts(colima_home, [(data, "true")])
    proc = _run("up.sh", env, ["--secrets-env", str(_secrets_env(tmp_path))])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    log = _log(env)
    assert "colima start" not in log
    assert _mount_args(log) == []


def test_secrets_file_is_unchanged_after_success_and_after_a_trapped_failure(tmp_path: Path):
    secrets = _secrets_env(tmp_path)
    before = secrets.read_bytes()
    mode = secrets.stat().st_mode
    env = _env(tmp_path, _install_stubs(tmp_path))
    ok = _run("up.sh", env, ["--secrets-env", str(secrets)])
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert secrets.read_bytes() == before
    assert secrets.stat().st_mode == mode

    failed_dir = tmp_path / "failed"
    failed_dir.mkdir()
    secrets2 = _secrets_env(failed_dir)
    before2 = secrets2.read_bytes()
    env2 = _env(failed_dir, _install_stubs(failed_dir), {"STUB_FAIL_SUBSTRING": "rollout status"})
    bad = _run("up.sh", env2, ["--secrets-env", str(secrets2)])
    assert bad.returncode != 0
    assert secrets2.read_bytes() == before2
    assert secrets2.is_file()
    assert "rollout status" in _log(env2)


def test_restore_sh_requires_confirmation_and_does_not_touch_dumps(tmp_path: Path):
    app = tmp_path / "eap_app.dump"
    dbos = tmp_path / "eap_dbos.dump"
    app.write_bytes(b"app-dump-bytes")
    dbos.write_bytes(b"dbos-dump-bytes")
    env = _env(tmp_path, _install_stubs(tmp_path))
    cancelled = _run("restore.sh", env, ["--app", str(app), "--dbos", str(dbos)], stdin="nope\n")
    assert cancelled.returncode != 0
    assert "pg_restore" not in _log(env)
    assert app.read_bytes() == b"app-dump-bytes"
    assert dbos.read_bytes() == b"dbos-dump-bytes"

    env2 = _env(tmp_path, _install_stubs(tmp_path))
    confirmed = _run("restore.sh", env2, ["--app", str(app), "--dbos", str(dbos)], stdin="restore\n")
    assert confirmed.returncode == 0, confirmed.stdout + confirmed.stderr
    log = _log(env2)
    assert log.count("pg_restore --list") == 2
    assert log.count("pg_restore --clean") == 2
    assert app.read_bytes() == b"app-dump-bytes"
    assert dbos.read_bytes() == b"dbos-dump-bytes"


def test_restore_sh_refuses_empty_and_partial_dumps_before_kubectl_restore(tmp_path: Path):
    empty = tmp_path / "empty.dump"
    partial = tmp_path / "eap_app-20261115T191625Z.dump.partial"
    good = tmp_path / "good.dump"
    empty.write_bytes(b"")
    partial.write_bytes(b"not-published")
    good.write_bytes(b"dbos-dump-bytes")
    env = _env(tmp_path, _install_stubs(tmp_path))
    refused_empty = _run("restore.sh", env, ["--app", str(empty), "--dbos", str(good)], stdin="restore\n")
    assert refused_empty.returncode != 0
    assert "Refusing an empty dump" in refused_empty.stderr
    assert "pg_restore" not in _log(env)
    assert empty.read_bytes() == b""

    env2 = _env(tmp_path, _install_stubs(tmp_path))
    refused_partial = _run("restore.sh", env2, ["--app", str(partial), "--dbos", str(good)], stdin="restore\n")
    assert refused_partial.returncode != 0
    assert "Refusing a partial dump" in refused_partial.stderr
    assert "pg_restore" not in _log(env2)
    assert partial.read_bytes() == b"not-published"


def test_restore_sh_refuses_a_dump_pg_restore_cannot_list(tmp_path: Path):
    app = tmp_path / "eap_app.dump"
    dbos = tmp_path / "eap_dbos.dump"
    app.write_bytes(b"NOT-A-DUMP")
    dbos.write_bytes(b"dbos-valid")
    env = _env(tmp_path, _install_stubs(tmp_path), {"STUB_LIST_REJECT": "NOT-A-DUMP"})
    proc = _run("restore.sh", env, ["--app", str(app), "--dbos", str(dbos)], stdin="restore\n")
    assert proc.returncode != 0
    assert "Refusing a dump pg_restore cannot list" in proc.stderr
    log = _log(env)
    assert "pg_restore --list" in log
    assert "pg_restore --clean" not in log
    assert app.read_bytes() == b"NOT-A-DUMP"
    assert dbos.read_bytes() == b"dbos-valid"


def test_restore_sh_latest_skips_empty_partial_and_unlistable_dumps(tmp_path: Path):
    backup = tmp_path / "pydanticos-data" / "backups"
    backup.mkdir(parents=True)
    (backup / "eap_app-20261115T191626Z.dump").write_bytes(b"app-valid")
    (backup / "eap_app-20261115T191700Z.dump.partial").write_bytes(b"partial-bytes")
    (backup / "eap_app-20261115T191701Z.dump").write_bytes(b"NOT-A-DUMP")
    (backup / "eap_app-20261115T191702Z.dump").write_bytes(b"")
    (backup / "eap_dbos-20261115T191626Z.dump").write_bytes(b"dbos-valid")
    (backup / "eap_dbos-20261115T191701Z.dump").write_bytes(b"NOT-A-DUMP")
    (backup / "eap_dbos-20261115T191702Z.dump").write_bytes(b"")
    env = _env(tmp_path, _install_stubs(tmp_path), {"STUB_LIST_REJECT": "NOT-A-DUMP"})
    proc = _run("restore.sh", env, ["--app", "latest", "--dbos", "latest"], stdin="restore\n")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "eap_app-20261115T191626Z.dump" in proc.stdout
    assert "eap_dbos-20261115T191626Z.dump" in proc.stdout
    assert "eap_app-20261115T191702Z.dump" not in proc.stdout
    assert "Skipping empty dump" in proc.stderr
    assert "Skipping dump pg_restore cannot list" in proc.stderr
    assert (backup / "eap_app-20261115T191700Z.dump.partial").read_bytes() == b"partial-bytes"
    assert (backup / "eap_app-20261115T191701Z.dump").read_bytes() == b"NOT-A-DUMP"
    assert (backup / "eap_app-20261115T191702Z.dump").read_bytes() == b""


def test_colima_start_is_not_tracked_by_the_actions_runner(tmp_path: Path):
    # A self-hosted runner kills every process tagged with RUNNER_TRACKING_ID when the job ends;
    # that stopped the VM after the first real deploy. up.sh must start Colima untracked.
    bindir = _install_stubs(tmp_path)
    env = _env(tmp_path, bindir, {"RUNNER_TRACKING_ID": "github_job_tracking_123"})
    _run("up.sh", env, ["--secrets-env", str(_secrets_env(tmp_path))], timeout=60)
    starts = [line for line in _log(env).splitlines() if line.startswith("COLIMA_START_TRACKING_ID=")]
    assert starts, "colima start was not invoked"
    assert all(line == "COLIMA_START_TRACKING_ID=[]" for line in starts), starts


def test_tunnel_sh_reconnects_until_stopped(tmp_path: Path):
    # Cloudflare's SSH proxy drops long sessions; the default loop must reconnect, not exit.
    env = _env(tmp_path, _install_stubs(tmp_path))
    with pytest.raises(subprocess.TimeoutExpired):
        _run("tunnel.sh", env, ["deploy-host"], timeout=8)
    assert _log(env).count("ssh ") >= 2, _log(env)
