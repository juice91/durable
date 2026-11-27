"""up.sh when a deploy fails: the running release comes back, and backups stay out of the migration (stubbed kubectl).

The incident (2026-11-26): a pg_dump held a lock, the migration failed with LockNotAvailable after the platform had been
scaled to 0, and up.sh exited with everything down on images whose code needed the missing column.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from kustomize_util import kustomize_docs
from test_scripts import SCRIPTS_DIR, _at, _env, _install_stubs, _log, _run, _secrets_env

PREVIOUS = (
    "set image statefulset/worker worker=pydanticos-python:previous000",
    "set image deployment/api api=pydanticos-python:previous000",
    "set image deployment/studio studio=pydanticos-studio:previous000",
)
TUNNEL = ""  # no extra secret fields
OPENFGA = "OPENFGA_DB_PASSWORD=openfgadbpass0123456\nOPENFGA_PRESHARED_KEY=presharedkey0123456789\n"


def _up(tmp: Path, extra: dict[str, str] | None = None, secrets_extra: str = ""):
    env = _env(tmp, _install_stubs(tmp), {"PYDANTICOS_MIGRATE_LOCK_DELAY": "0", **(extra or {})})
    secrets = _secrets_env(tmp)
    secrets.write_text(secrets.read_text() + secrets_extra)
    proc = _run("up.sh", env, ["--secrets-env", str(secrets)], timeout=60)
    return proc, _log(env)


def _after(log: str, needle: str) -> str:
    return log[_at(log, needle) :]


def test_failed_migration_puts_the_previous_release_back(tmp_path: Path):
    proc, log = _up(tmp_path, {"STUB_MIGRATE_STATUS": "failed", "STUB_API_REPLICAS": "2"}, TUNNEL)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    restore = _after(log, "logs job/migrate")
    # The Job is gone before anything old comes back, so it cannot commit afterwards.
    gone = _at(restore, "delete job migrate --ignore-not-found --cascade=foreground --wait=true")
    for line in PREVIOUS:
        assert _at(restore, line) > gone
    # A StatefulSet is stopped before its image is put back, then scaled up again.
    stopped = _at(restore, "scale statefulset/worker --replicas=0")
    image = _at(restore, PREVIOUS[0], stopped)
    _at(restore, "scale statefulset/worker --replicas=1", image)
    _at(restore, "scale deployment/api --replicas=2", _at(restore, PREVIOUS[1]))
    _at(restore, "scale deployment/studio --replicas=1", _at(restore, PREVIOUS[2]))
    assert "schema is unchanged (revision 0041_previous)" in proc.stderr
    assert "Restored deployment/api to 2 replicas with image pydanticos-python:previous000." in proc.stderr
    assert "Service was restored. The deploy itself failed." in proc.stderr
    # The identity services were not touched by this deploy yet.
    assert "-n iam scale" not in log
    # Not a lock error: no second attempt.
    assert log.count("patch job migrate") == 1


@pytest.mark.parametrize(
    "failure",
    [
        {"STUB_FAIL_ONCE": "patch job migrate"},
        {"STUB_MIGRATE_STATUS": "pending", "PYDANTICOS_MIGRATE_TIMEOUT": "0"},
        {"STUB_MIGRATE_GET_ERRORS": "9"},
        # A code-only deploy whose new API never becomes ready.
        {"STUB_FAIL_ONCE": "rollout status deployment/api"},
    ],
    ids=["unsuspend", "migrate-timeout", "job-unreadable", "api-rollout"],
)
def test_any_failure_after_the_scale_down_restores_when_the_schema_is_unchanged(tmp_path: Path, failure: dict[str, str]):
    proc, log = _up(tmp_path, failure)
    assert proc.returncode != 0, proc.stdout + proc.stderr
    for line in PREVIOUS:
        assert line in log
    assert "Service was restored" in proc.stderr


def test_a_worker_that_will_not_stop_scales_everything_back_without_touching_images(tmp_path: Path):
    env_extra = {"PYDANTICOS_WORKER_STOP_TIMEOUT": "0"}
    bindir = _install_stubs(tmp_path)
    stub = bindir / "stub"
    stub.write_text(
        stub.read_text().replace(
            'case "$tool" in\n  docker)',
            'if [[ " $* " == *" get pods -l app.kubernetes.io/name=worker "* ]]; then echo worker-0; exit 0; fi\n'
            'case "$tool" in\n  docker)',
        )
    )
    env = _env(tmp_path, bindir, env_extra)
    proc = _run("up.sh", env, ["--secrets-env", str(_secrets_env(tmp_path))], timeout=60)
    log = _log(env)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "set image" not in log
    assert "delete job migrate" not in log
    assert "scale statefulset/worker --replicas=1" in log
    assert "scale deployment/api --replicas=1" in log


@pytest.mark.parametrize(
    "failure",
    [
        # The migration committed, then a rollout failed.
        {"STUB_SCHEMA_ADVANCES": "1", "STUB_FAIL_ONCE": "rollout status statefulset/worker"},
        # Core committed, then the plug-in branches failed the Job.
        {"STUB_SCHEMA_ADVANCES": "1", "STUB_MIGRATE_STATUS": "failed"},
        # The Job succeeded and the revision cannot be read afterwards.
        {"STUB_SCHEMA_UNREADABLE_AFTER": "1", "STUB_FAIL_ONCE": "rollout status deployment/api"},
    ],
    ids=["rollout-after-migrate", "failed-after-commit", "unreadable-after-success"],
)
def test_a_migrated_database_keeps_the_new_images(tmp_path: Path, failure: dict[str, str]):
    proc, log = _up(tmp_path, failure)
    assert proc.returncode != 0
    assert "set image" not in log
    assert "the new images stay; fix forward" in proc.stderr
    restore = _after(log, "delete job migrate --ignore-not-found --cascade=foreground")
    for scale in ("statefulset/worker", "deployment/api", "deployment/studio"):
        assert f"scale {scale} --replicas=1" in restore


def test_an_unreadable_revision_before_the_migration_stops_it_and_restores(tmp_path: Path):
    proc, log = _up(tmp_path, {"STUB_SCHEMA_UNREADABLE": "1"})
    assert proc.returncode == 1
    assert "not migrating" in proc.stderr
    assert "patch job migrate" not in log
    for line in PREVIOUS:
        assert line in log


def test_a_first_install_has_nothing_to_restore(tmp_path: Path):
    proc, log = _up(
        tmp_path,
        {
            "STUB_ABSENT": "api,worker,studio,openfga,zitadel",
            "STUB_SCHEMA_ABSENT": "1",
            "STUB_MIGRATE_STATUS": "failed",
        },
    )
    assert proc.returncode == 1
    assert "set image" not in log
    assert "restoring service" not in proc.stderr
    assert "scale statefulset/worker" not in log


def test_a_failed_identity_stage_restores_the_identity_services_and_keeps_the_new_release(tmp_path: Path):
    proc, log = _up(tmp_path, {"STUB_FAIL_ONCE": "rollout status deployment/openfga"}, OPENFGA)
    assert proc.returncode != 0
    assert "set image" not in log
    restore = _after(log, "rollout status deployment/openfga")
    assert "-n iam scale deployment/openfga --replicas=1" in restore
    # No ZITADEL fields: the rule of a deploy holds, it stays off.
    assert "-n iam scale deployment/zitadel --replicas=0" in restore
    assert "scale deployment/api" not in restore


@pytest.mark.parametrize("failures, ok", [(1, True), (2, True), (3, False)])
def test_only_a_lock_failure_is_retried_and_only_a_few_times(tmp_path: Path, failures: int, ok: bool):
    proc, log = _up(tmp_path, {"STUB_MIGRATE_LOCK_FAILURES": str(failures)})
    assert (proc.returncode == 0) is ok, proc.stdout + proc.stderr
    assert log.count("patch job migrate") == min(failures + 1, 3)
    assert "LockNotAvailable" in proc.stderr
    if ok:
        assert "set image" not in log
    else:
        for line in PREVIOUS:
            assert line in log


def test_a_cancelled_deploy_restores_too(tmp_path: Path):
    env = _env(tmp_path, _install_stubs(tmp_path), {"STUB_MIGRATE_STATUS": "pending"})
    proc = subprocess.Popen(
        ["bash", str(SCRIPTS_DIR / "up.sh"), "--secrets-env", str(_secrets_env(tmp_path))],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 60
        while "get job migrate" not in _log(env):
            assert proc.poll() is None and time.monotonic() < deadline
            time.sleep(0.1)
        os.killpg(proc.pid, signal.SIGTERM)
        _, stderr = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
    assert proc.returncode == 143, stderr
    log = _log(env)
    for line in PREVIOUS:
        assert line in log
    assert "delete configmap deploy-in-progress" in _after(log, PREVIOUS[0])


# ---- backups stay out of the deploy -------------------------------------------------------------------------------

OLD_POD = "postgres-backup-manual-x Running 2026-01-01T00:00:00Z"


def test_the_marker_is_set_before_anything_stops_and_removed_at_the_end(tmp_path: Path):
    proc, log = _up(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    created = _at(log, "create configmap deploy-in-progress --from-literal=expires-at=")
    listed = _at(log, "get pods -l app.kubernetes.io/name=postgres-backup", created)
    api0 = _at(log, "scale deployment/api --replicas=0", listed)
    _at(log, "delete configmap deploy-in-progress", _at(log, "get job migrate", api0))


def test_a_running_backup_stops_the_deploy_before_anything_is_touched(tmp_path: Path):
    proc, log = _up(tmp_path, {"STUB_BACKUP_PODS": OLD_POD, "PYDANTICOS_BACKUP_WAIT": "0"})
    assert proc.returncode == 1
    assert "A postgres-backup job is still running" in proc.stderr
    assert "postgres-backup-manual-x" in proc.stderr
    assert " scale " not in log
    assert "apply -f -\n" in log  # the Secrets, before the guard
    assert "patch job migrate" not in log
    assert log.rindex("delete configmap deploy-in-progress") > _at(log, "create configmap deploy-in-progress")


def test_the_deploy_waits_for_a_running_backup_to_finish(tmp_path: Path):
    proc, log = _up(tmp_path, {"STUB_BACKUP_PODS": OLD_POD, "STUB_BACKUP_POLLS": "1"})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Waiting up to 1800 seconds" in proc.stdout
    assert log.count("name=postgres-backup") == 2


@pytest.mark.parametrize(
    "pod",
    [
        "postgres-backup-1 Succeeded 2026-01-01T00:00:00Z",
        "postgres-backup-2 Failed 2026-01-01T00:00:00Z",
        # Created after the marker: it is waiting for the deploy, so waiting for it would deadlock.
        "postgres-backup-3 Pending 2026-01-01T00:00:11Z",
    ],
)
def test_finished_and_newer_backup_pods_do_not_hold_the_deploy(tmp_path: Path, pod: str):
    proc, _ = _up(tmp_path, {"STUB_BACKUP_PODS": pod, "PYDANTICOS_BACKUP_WAIT": "0"})
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_the_backup_pod_mounts_the_marker_as_an_optional_volume():
    cron = next(d for d in kustomize_docs() if d["kind"] == "CronJob" and d["metadata"]["name"] == "postgres-backup")
    spec = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    volume = next(v for v in spec["volumes"] if v["name"] == "deploy-marker")
    assert volume["configMap"] == {"name": "deploy-in-progress", "optional": True}
    containers = spec.get("initContainers", []) + spec["containers"]
    dump = next(c for c in containers if c["name"] == "pg-dump")
    assert {"name": "deploy-marker", "mountPath": "/deploy-marker", "readOnly": True} in dump["volumeMounts"]


# ---- the migration really is one transaction ----------------------------------------------------------------------


def test_no_migration_leaves_the_single_transaction():
    """Restoring the old images after a failed migration relies on Postgres rolling all of it back."""
    root = SCRIPTS_DIR.parents[1] / "packages" / "db" / "src" / "eap_db" / "migrations"
    env = (root / "env.py").read_text()
    assert "with engine.begin() as connection:" in env
    assert "transaction_per_migration" not in env
    for path in sorted((root / "versions").glob("*.py")):
        source = path.read_text()
        for token in ("autocommit_block", "AUTOCOMMIT", 'execute("COMMIT', "execute('COMMIT", "postgresql_concurrently"):
            assert token not in source, f"{path.name}: {token}"
