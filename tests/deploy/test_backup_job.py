"""Run the backup job script with stubbed pg_isready, pg_dump, and pg_restore."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from kustomize_util import kustomize_docs

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "deploy" / "k8s" / "backup-job.sh"


# flock(1) is util-linux (in the job's image, not on every developer machine): the same lock on the inherited fd.
FLOCK_STUB = (
    f"#!{sys.executable}\n"
    "import fcntl, sys, time\n"
    "args = sys.argv[1:]\n"
    "fd = int(args[-1])\n"
    "deadline = time.monotonic() + (float(args[1]) if args[0] == '-w' else 0)\n"
    "while True:\n"
    "    try:\n"
    "        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
    "        sys.exit(0)\n"
    "    except BlockingIOError:\n"
    "        if time.monotonic() >= deadline:\n"
    "            sys.exit(1)\n"
    "        time.sleep(0.05)\n"
)


def _bins(tmp: Path) -> Path:
    bindir = tmp / "bin"
    bindir.mkdir()
    (bindir / "pg_isready").write_text(
        "#!/bin/sh\n"
        "if [ -n \"${STUB_PG_ISREADY_FAIL:-}\" ]; then exit 1; fi\n"
        "exit 0\n"
    )
    (bindir / "pg_dump").write_text(
        "#!/bin/sh\n"
        "file=\n"
        "prev=\n"
        "for arg in \"$@\"; do\n"
        "  case \"$arg\" in\n"
        "    --file=*) file=${arg#--file=} ;;\n"
        "    --file) prev=1 ;;\n"
        "    *) if [ \"$prev\" = 1 ]; then file=$arg; prev=; fi ;;\n"
        "  esac\ndone\n"
        ": >\"$file\"\n"
        "if [ -n \"${STUB_PG_DUMP_FAIL:-}\" ]; then exit 1; fi\n"
        "printf 'PGDMP' >\"$file\"\n"
        "exit 0\n"
    )
    (bindir / "pg_restore").write_text(
        "#!/bin/sh\n"
        "if [ \"${1:-}\" != \"--list\" ]; then exit 0; fi\n"
        "target=$2\n"
        "if [ ! -s \"$target\" ]; then exit 1; fi\n"
        "if [ -n \"${STUB_PG_RESTORE_LIST_FAIL:-}\" ]; then exit 1; fi\n"
        "exit 0\n"
    )
    (bindir / "flock").write_text(FLOCK_STUB)
    for path in bindir.iterdir():
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return bindir


def _env(tmp: Path, bindir: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
    env["BACKUP_DIR"] = str(tmp / "backups")
    env["EAP_APP_DATABASE_PASSWORD"] = "stub"
    env["EAP_DBOS_DATABASE_PASSWORD"] = "stub"
    if extra:
        env.update(extra)
    return env


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(SCRIPT)],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_publish_renames_only_after_pg_restore_list(tmp_path: Path):
    env = _env(tmp_path, _bins(tmp_path))
    proc = _run(env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    backup = tmp_path / "backups"
    dumps = sorted(backup.glob("*.dump"))
    partials = list(backup.glob("*.partial"))
    assert partials == []
    assert [p.name.split("-", 1)[0] for p in dumps] == ["eap_app", "eap_dbos"]
    assert all(p.stat().st_size > 0 for p in dumps)
    assert "stub" not in proc.stdout + proc.stderr


def test_failed_pg_dump_leaves_no_partial_or_empty_backup(tmp_path: Path):
    env = _env(tmp_path, _bins(tmp_path), {"STUB_PG_DUMP_FAIL": "1"})
    proc = _run(env)
    assert proc.returncode != 0
    backup = tmp_path / "backups"
    assert list(backup.glob("*.partial")) == []
    assert list(backup.glob("*.dump")) == []


def test_unlistable_dump_is_not_published(tmp_path: Path):
    env = _env(tmp_path, _bins(tmp_path), {"STUB_PG_RESTORE_LIST_FAIL": "1"})
    proc = _run(env)
    assert proc.returncode != 0
    assert "not publishing a backup" in proc.stderr
    backup = tmp_path / "backups"
    assert list(backup.glob("*.partial")) == []
    assert list(backup.glob("*.dump")) == []


def test_timeout_waiting_for_postgres_creates_no_dump_file(tmp_path: Path):
    env = _env(
        tmp_path,
        _bins(tmp_path),
        {"STUB_PG_ISREADY_FAIL": "1", "BACKUP_READY_TIMEOUT": "0"},
    )
    proc = _run(env)
    assert proc.returncode != 0
    assert "Timed out after 0s waiting for Postgres" in proc.stderr
    assert "NetworkPolicy" in proc.stderr
    backup = tmp_path / "backups"
    assert [p.name for p in backup.iterdir()] == [".backup.lock"]


def test_retention_never_deletes_the_newest_valid_dump(tmp_path: Path):
    backup = tmp_path / "backups"
    backup.mkdir()
    keep = backup / "eap_app-20000101T000000Z.dump"
    older = backup / "eap_app-19990101T000000Z.dump"
    empty = backup / "eap_app-20990101T000000Z.dump"
    partial = backup / "eap_app-20980101T000000Z.dump.partial"
    keep.write_bytes(b"PGDMP")
    older.write_bytes(b"PGDMP")
    empty.write_bytes(b"")
    partial.write_bytes(b"partial")
    for path in (keep, older, empty, partial):
        os.utime(path, (0, 0))
    env = _env(tmp_path, _bins(tmp_path), {"BACKUP_SKIP_MAIN": "1", "BACKUP_RETENTION_DAYS": "30"})
    proc = subprocess.run(
        ["sh", "-c", '. "$1"; retain', "sh", str(SCRIPT)],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert keep.is_file() and keep.stat().st_size > 0
    assert not older.exists()
    assert not empty.exists()
    assert not partial.exists()


def _nights(backup: Path, hours_ago: list[int]) -> list[Path]:
    files = []
    for i, h in enumerate(hours_ago):
        f = backup / f"eap_app-2026010{i + 1}T031700Z.dump"
        f.write_bytes(b"PGDMP")
        t = time.time() - h * 3600
        os.utime(f, (t, t))
        files.append(f)
    return files


def _retain(tmp_path: Path, days: str) -> subprocess.CompletedProcess[str]:
    env = _env(tmp_path, _bins(tmp_path), {"BACKUP_SKIP_MAIN": "1", "BACKUP_RETENTION_DAYS": days})
    return subprocess.run(
        ["sh", "-c", '. "$1"; retain', "sh", str(SCRIPT)], cwd=REPO_ROOT, env=env, text=True, capture_output=True, check=False
    )


def test_one_day_retention_leaves_only_the_latest_of_three_nightly_dumps(tmp_path: Path):
    backup = tmp_path / "backups"
    backup.mkdir()
    # Three nights as the next run sees them: 48 h, 24 h and a few minutes old (the new dump).
    night1, night2, night3 = _nights(backup, [48, 24, 0])
    proc = _retain(tmp_path, "1")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not night1.exists()
    assert not night2.exists(), "a 24 h old dump must go with BACKUP_RETENTION_DAYS=1 (find -mtime +1 would keep it)"
    assert night3.exists()


def test_one_day_retention_keeps_the_newest_valid_dump_even_when_old(tmp_path: Path):
    backup = tmp_path / "backups"
    backup.mkdir()
    old1, old2 = _nights(backup, [200, 100])
    proc = _retain(tmp_path, "1")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not old1.exists()
    assert old2.exists()


def test_a_second_job_leaves_the_partial_of_a_running_one(tmp_path: Path):
    """2026-11-26: a manual job and the nightly one removed each other's partial dumps, and both failed."""
    backup = tmp_path / "backups"
    backup.mkdir()
    other = backup / "eap_app-20260101T031100Z.dump.postgres-backup-manual-1.partial"
    other.write_bytes(b"still being written")
    for extra in ({}, {"STUB_PG_DUMP_FAIL": "1"}):
        bindir = tmp_path / f"run{len(extra)}"
        bindir.mkdir()
        _run(_env(tmp_path, _bins(bindir), {"BACKUP_RUN_ID": "postgres-backup-2917-1", **extra}))
        assert other.exists()
        assert list(backup.glob("*.postgres-backup-2917-1.partial")) == []


def _held_job(tmp_path: Path, bindir: Path, gate: Path) -> subprocess.Popen[str]:
    """A job whose pg_dump has written its file and now waits for `gate`."""
    (bindir / "pg_dump").write_text(
        "#!/bin/sh\n"
        'for arg in "$@"; do case "$arg" in --file=*) file=${arg#--file=} ;; esac; done\n'
        "printf PGDMP >\"$file\"\n"
        'if [ -n "${STUB_HOLD:-}" ]; then\n'
        '  : >"$STUB_HOLD.started"\n'
        '  i=0; while [ ! -e "$STUB_HOLD" ] && [ "$i" -lt 600 ]; do sleep 0.1; i=$((i + 1)); done\n'
        "fi\n"
    )
    job = subprocess.Popen(
        ["sh", str(SCRIPT)],
        env=_env(tmp_path, bindir, {"BACKUP_RUN_ID": "first", "STUB_HOLD": str(gate)}),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 60
    while not Path(f"{gate}.started").exists():
        assert job.poll() is None and time.monotonic() < deadline
        time.sleep(0.05)
    return job


def test_a_second_job_waits_for_the_first_and_both_publish(tmp_path: Path):
    bindir, gate = _bins(tmp_path), tmp_path / "gate"
    first = _held_job(tmp_path, bindir, gate)
    second = None
    try:
        second = subprocess.Popen(
            ["sh", str(SCRIPT)],
            env=_env(tmp_path, bindir, {"BACKUP_RUN_ID": "second"}),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert "Another backup job is dumping" in second.stderr.readline()
        assert list((tmp_path / "backups").glob("*.dump")) == []
        gate.touch()
        for job in (first, second):
            out, err = job.communicate(timeout=60)
            assert job.returncode == 0, out + err
    finally:
        for job in (first, second):
            if job is not None and job.poll() is None:
                job.kill()


def test_a_job_that_cannot_get_the_lock_fails_without_touching_the_running_one(tmp_path: Path):
    bindir, gate = _bins(tmp_path), tmp_path / "gate"
    first = _held_job(tmp_path, bindir, gate)
    try:
        second = _run(_env(tmp_path, bindir, {"BACKUP_RUN_ID": "second", "BACKUP_LOCK_WAIT": "0"}))
        assert second.returncode != 0
        assert "still holds the lock" in second.stderr
        assert len(list((tmp_path / "backups").glob("*.first.partial"))) == 1
        gate.touch()
        out, err = first.communicate(timeout=60)
        assert first.returncode == 0, out + err
    finally:
        if first.poll() is None:
            first.kill()


def test_a_failed_pg_dump_says_which_database_and_exit_code(tmp_path: Path):
    proc = _run(_env(tmp_path, _bins(tmp_path), {"STUB_PG_DUMP_FAIL": "1"}))
    assert "pg_dump of eap_app failed (exit 1)" in proc.stderr


def test_failed_backup_pods_are_kept():
    cron = next(d for d in kustomize_docs() if d["kind"] == "CronJob" and d["metadata"]["name"] == "postgres-backup")
    assert cron["spec"]["concurrencyPolicy"] == "Forbid"
    job = cron["spec"]["jobTemplate"]["spec"]
    assert job["template"]["spec"]["restartPolicy"] == "Never"
    assert job["backoffLimit"] == 2


def _marker(tmp_path: Path, content: str) -> Path:
    directory = tmp_path / "marker"
    directory.mkdir()
    (directory / "expires-at").write_text(content)
    return directory


def test_no_dump_while_a_deploy_is_in_progress(tmp_path: Path):
    marker = _marker(tmp_path, str(int(time.time()) + 3600))
    env = _env(tmp_path, _bins(tmp_path), {"BACKUP_DEPLOY_MARKER_DIR": str(marker), "BACKUP_DEPLOY_WAIT": "0"})
    proc = _run(env)
    assert proc.returncode != 0
    assert "A deploy is still in progress" in proc.stderr
    assert list((tmp_path / "backups").iterdir()) == []


def test_the_dump_starts_once_the_deploy_marker_goes(tmp_path: Path):
    marker = _marker(tmp_path, str(int(time.time()) + 3600))
    env = _env(tmp_path, _bins(tmp_path), {"BACKUP_DEPLOY_MARKER_DIR": str(marker), "BACKUP_DEPLOY_POLL": "0.1"})
    proc = subprocess.Popen(["sh", str(SCRIPT)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert "waiting for it to finish" in proc.stderr.readline()
        assert list((tmp_path / "backups").glob("*.dump")) == []
        (marker / "expires-at").unlink()
        out, err = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0, out + err
    assert len(list((tmp_path / "backups").glob("*.dump"))) == 2


@pytest.mark.parametrize("content", [str(int(time.time()) - 1), "", "soon"])
def test_a_stale_or_unreadable_marker_does_not_block_backups(tmp_path: Path, content: str):
    env = _env(
        tmp_path, _bins(tmp_path), {"BACKUP_DEPLOY_MARKER_DIR": str(_marker(tmp_path, content)), "BACKUP_DEPLOY_WAIT": "0"}
    )
    proc = _run(env)
    assert proc.returncode == 0, proc.stderr
