"""Run the backup job script with stubbed pg_isready, pg_dump, and pg_restore."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "deploy" / "k8s" / "backup-job.sh"


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
    assert list(backup.iterdir()) == []


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
    import time

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
