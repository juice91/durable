"""PYDANTICOS_BACKUP_DIR: validation, the unplugged-volume check, and the Colima mounts up.sh requires."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from test_scripts import _env, _install_stubs, _log, _mount_args, _run, _secrets_env, _write_mounts

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB = REPO_ROOT / "deploy" / "scripts" / "lib.sh"


def _real(path: Path) -> str:
    return os.path.realpath(path)


def _mount_stub(tmp: Path, mounted: list[Path]) -> Path:
    bindir = tmp / "mountbin"
    bindir.mkdir(exist_ok=True)
    mount = bindir / "mount"
    lines = "".join(f"echo '/dev/disk9s1 on {m} (apfs, local, nodev)'\n" for m in mounted)
    mount.write_text("#!/bin/sh\necho '/dev/disk3s1s1 on / (apfs, sealed, local)'\n" + lines)
    mount.chmod(mount.stat().st_mode | stat.S_IEXEC)
    return bindir


def _validate(tmp: Path, backup: str | None, extra: dict[str, str] | None = None, mounted: list[Path] | None = None):
    env = os.environ.copy()
    env["PATH"] = f"{_mount_stub(tmp, mounted or [])}{os.pathsep}{env['PATH']}"
    env["PYDANTICOS_DATA_DIR"] = str(tmp / "data")
    env.pop("PYDANTICOS_BACKUP_DIR", None)
    if backup is not None:
        env["PYDANTICOS_BACKUP_DIR"] = backup
    env.update(extra or {})
    return subprocess.run(
        ["bash", "-c", '. "$1"; validate_and_prepare_data_dir; printf "%s\\n" "$PYDANTICOS_BACKUP_DIR"', "bash", str(LIB)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _backup(tmp: Path, name: str = "ssd/backups") -> Path:
    path = tmp / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.mkdir(mode=0o700, exist_ok=True)
    return Path(_real(path))


def test_default_is_data_dir_backups(tmp_path: Path):
    proc = _validate(tmp_path, None)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == f"{_real(tmp_path / 'data')}/backups"


def test_default_still_refuses_a_symlinked_backups_folder_and_names_the_fix(tmp_path: Path):
    (tmp_path / "data").mkdir(mode=0o700)
    target = _backup(tmp_path)
    (tmp_path / "data" / "backups").symlink_to(target)
    proc = _validate(tmp_path, None)
    assert proc.returncode != 0
    assert "backups is a symlink" in proc.stderr and "PYDANTICOS_BACKUP_DIR" in proc.stderr


def test_explicit_dir_outside_the_data_dir_is_used_and_the_transition_symlink_is_only_a_warning(tmp_path: Path):
    (tmp_path / "data").mkdir(mode=0o700)
    target = _backup(tmp_path)
    (tmp_path / "data" / "backups").symlink_to(target)
    proc = _validate(tmp_path, str(target))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == str(target)
    assert "backups is a symlink; it is ignored" in proc.stderr
    assert (tmp_path / "data" / "backups").is_symlink()  # left alone


def test_explicit_dir_equal_to_the_default_location_is_just_the_default(tmp_path: Path):
    data = Path(_real(tmp_path)) / "data"
    proc = _validate(tmp_path, f"{data}/backups")
    assert proc.returncode == 0, proc.stderr
    assert (data / "backups").is_dir()


def test_missing_leaf_on_an_existing_parent_is_created_mode_700(tmp_path: Path):
    parent = Path(_real(tmp_path)) / "ssd"
    parent.mkdir()
    proc = _validate(tmp_path, str(parent / "backups"))
    assert proc.returncode == 0, proc.stderr
    assert (parent / "backups").stat().st_mode & 0o777 == 0o700


def test_missing_parent_is_refused_and_nothing_is_created(tmp_path: Path):
    proc = _validate(tmp_path, str(Path(_real(tmp_path)) / "nope" / "backups"))
    assert proc.returncode != 0
    assert "its parent" in proc.stderr
    assert not (tmp_path / "nope").exists()


@pytest.mark.parametrize(
    "value, message",
    [
        ("relative/backups", "must be an absolute path"),
        ("/", "not allowed"),
        ("/Users", "not allowed"),
        ("/System/Library/x", "not allowed"),
        ("/tmp/../etc", "plain absolute path"),
    ],
)
def test_broad_or_malformed_paths_are_refused(tmp_path: Path, value: str, message: str):
    proc = _validate(tmp_path, value)
    assert proc.returncode != 0
    assert message in proc.stderr


def test_home_and_volumes_roots_are_refused(tmp_path: Path):
    assert "not allowed" in _validate(tmp_path, _real(Path.home())).stderr
    vols = Path(_real(tmp_path)) / "vols"
    vols.mkdir()
    proc = _validate(tmp_path, str(vols), {"PYDANTICOS_VOLUMES_DIR": str(vols)})
    assert proc.returncode != 0 and "not allowed" in proc.stderr


def test_the_data_dir_and_its_parents_are_not_a_backup_dir(tmp_path: Path):
    data = Path(_real(tmp_path)) / "data"
    data.mkdir(mode=0o700)
    assert "data directory or a parent" in _validate(tmp_path, str(data)).stderr
    assert "data directory or a parent" in _validate(tmp_path, str(data.parent)).stderr


def test_symlinked_backup_dir_is_refused_not_followed(tmp_path: Path):
    target = _backup(tmp_path)
    link = Path(_real(tmp_path)) / "link"
    link.symlink_to(target)
    proc = _validate(tmp_path, str(link))
    assert proc.returncode != 0
    assert "symlink" in proc.stderr and str(target) in proc.stderr


def test_symlink_in_a_parent_component_is_refused(tmp_path: Path):
    target = _backup(tmp_path)
    link = Path(_real(tmp_path)) / "link"
    link.symlink_to(target.parent)
    proc = _validate(tmp_path, str(link / "backups"))
    assert proc.returncode != 0
    assert "symlink" in proc.stderr


@pytest.mark.parametrize("mode", [0o777, 0o770, 0o757])
def test_group_or_world_writable_backup_dir_is_refused(tmp_path: Path, mode: int):
    target = _backup(tmp_path)
    target.chmod(mode)
    proc = _validate(tmp_path, str(target))
    assert proc.returncode != 0
    assert "group- or world-writable" in proc.stderr
    assert target.stat().st_mode & 0o777 == mode


def test_a_file_is_not_a_backup_dir(tmp_path: Path):
    f = Path(_real(tmp_path)) / "file"
    f.write_text("x")
    assert "not a directory" in _validate(tmp_path, str(f)).stderr


# ---- external volumes ---------------------------------------------------------------------------------------------


def _volume(tmp: Path, name: str = "EXT") -> tuple[Path, Path]:
    vols = Path(_real(tmp)) / "vols"
    vols.mkdir(exist_ok=True)
    vol = vols / name
    vol.mkdir(exist_ok=True)
    return vols, vol


def test_unplugged_volume_fails_with_a_clear_message(tmp_path: Path):
    vols = Path(_real(tmp_path)) / "vols"
    vols.mkdir()
    proc = _validate(tmp_path, str(vols / "EXT" / "backups"), {"PYDANTICOS_VOLUMES_DIR": str(vols)})
    assert proc.returncode != 0
    assert "backup volume" in proc.stderr and "is not mounted" in proc.stderr and "Plug in" in proc.stderr
    assert not (vols / "EXT").exists()


def test_stale_mount_point_folder_that_is_not_a_mounted_volume_fails(tmp_path: Path):
    vols, vol = _volume(tmp_path)
    proc = _validate(tmp_path, str(vol / "backups"), {"PYDANTICOS_VOLUMES_DIR": str(vols)}, mounted=[])
    assert proc.returncode != 0 and "is not mounted" in proc.stderr
    assert not (vol / "backups").exists()


def test_mounted_volume_passes_and_the_folder_is_created(tmp_path: Path):
    vols, vol = _volume(tmp_path)
    proc = _validate(tmp_path, str(vol / "pydanticos-backup-dir"), {"PYDANTICOS_VOLUMES_DIR": str(vols)}, mounted=[vol])
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == str(vol / "pydanticos-backup-dir")
    assert (vol / "pydanticos-backup-dir").stat().st_mode & 0o777 == 0o700


def test_volume_root_itself_is_refused(tmp_path: Path):
    vols, vol = _volume(tmp_path)
    proc = _validate(tmp_path, str(vol), {"PYDANTICOS_VOLUMES_DIR": str(vols)}, mounted=[vol])
    assert proc.returncode != 0 and "root of a volume" in proc.stderr


# ---- up.sh and the Colima mounts ----------------------------------------------------------------------------------


def _up_env(tmp: Path, backup: Path | None, extra: dict[str, str] | None = None, stopped: bool = True) -> dict[str, str]:
    env = _env(tmp, _install_stubs(tmp), extra)
    if not stopped:
        env.pop("STUB_COLIMA_STOPPED")
    if backup is not None:
        env["PYDANTICOS_BACKUP_DIR"] = str(backup)
    return env


def _up(env: dict[str, str], tmp: Path):
    return _run("up.sh", env, ["--secrets-env", str(_secrets_env(tmp))])


def test_up_sh_new_profile_passes_both_mounts_when_the_backup_dir_is_outside_the_data_dir(tmp_path: Path):
    backup = _backup(tmp_path)
    env = _up_env(tmp_path, backup, {"STUB_COLIMA_NEW": "1"})
    proc = _up(env, tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _mount_args(_log(env)) == [f"{_real(tmp_path / 'pydanticos-data')}:w", f"{backup}:w"]


def test_up_sh_new_profile_without_the_variable_still_has_one_mount(tmp_path: Path):
    env = _up_env(tmp_path, None, {"STUB_COLIMA_NEW": "1"})
    assert _up(env, tmp_path).returncode == 0
    assert _mount_args(_log(env)) == [f"{_real(tmp_path / 'pydanticos-data')}:w"]


def test_up_sh_stopped_profile_start_carries_both_mounts(tmp_path: Path):
    backup = _backup(tmp_path)
    env = _up_env(tmp_path, backup)
    assert _up(env, tmp_path).returncode == 0
    assert _mount_args(_log(env))[-1] == f"{backup}:w"


def test_up_sh_with_the_live_setup_and_the_transition_symlink_succeeds(tmp_path: Path):
    """Data dir plus the whole volume mounted, as on the live profile; the old backups symlink still exists."""
    vols, vol = _volume(tmp_path)
    backup = vol / "pydanticos-backup-dir"
    backup.mkdir(mode=0o700)
    data = Path(_real(tmp_path)) / "pydanticos-data"
    data.mkdir(mode=0o700)
    (data / "backups").symlink_to(backup)
    env = _up_env(
        tmp_path,
        backup,
        {"PYDANTICOS_VOLUMES_DIR": str(vols), "COLIMA_HOME": str(tmp_path / "colima-home")},
        stopped=False,
    )
    env["PATH"] = f"{_mount_stub(tmp_path, [vol])}{os.pathsep}{env['PATH']}"
    _write_mounts(tmp_path / "colima-home", [(str(data), "true"), (str(vol), "true")])
    proc = _up(env, tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "backups is a symlink; it is ignored" in proc.stderr
    assert "colima start" not in _log(env)


def test_up_sh_existing_profile_missing_the_backup_mount_prints_the_exact_yaml(tmp_path: Path):
    backup = _backup(tmp_path)
    data = _real(tmp_path / "pydanticos-data")
    env = _up_env(tmp_path, backup, {"COLIMA_HOME": str(tmp_path / "colima-home")}, stopped=False)
    _write_mounts(tmp_path / "colima-home", [(data, "true")])
    before = (tmp_path / "colima-home" / "pydanticos" / "colima.yaml").read_text()
    proc = _up(env, tmp_path)
    assert proc.returncode != 0
    assert f"mounts:\n  - location: {backup}\n    writable: true\n" in proc.stderr
    assert f"location: {data}" not in proc.stderr  # the data dir mount is there
    assert "colima start" not in _log(env)
    assert (tmp_path / "colima-home" / "pydanticos" / "colima.yaml").read_text() == before


def test_up_sh_existing_profile_missing_the_data_mount_prints_it(tmp_path: Path):
    backup = _backup(tmp_path)
    env = _up_env(tmp_path, backup, {"COLIMA_HOME": str(tmp_path / "colima-home")}, stopped=False)
    _write_mounts(tmp_path / "colima-home", [(str(backup), "true")])
    proc = _up(env, tmp_path)
    assert proc.returncode != 0
    assert f"  - location: {_real(tmp_path / 'pydanticos-data')}\n    writable: true" in proc.stderr


def test_up_sh_existing_profile_with_a_read_only_backup_mount_asks_for_writable(tmp_path: Path):
    backup = _backup(tmp_path)
    env = _up_env(tmp_path, backup, {"COLIMA_HOME": str(tmp_path / "colima-home")}, stopped=False)
    _write_mounts(tmp_path / "colima-home", [(_real(tmp_path / "pydanticos-data"), "true"), (str(backup), "false")])
    proc = _up(env, tmp_path)
    assert proc.returncode != 0
    assert f"location: {backup}\n    writable: true" in proc.stderr


def test_up_sh_existing_profile_with_a_covering_parent_mount_and_the_data_mount_passes(tmp_path: Path):
    backup = _backup(tmp_path)
    env = _up_env(tmp_path, backup, {"COLIMA_HOME": str(tmp_path / "colima-home")}, stopped=False)
    _write_mounts(tmp_path / "colima-home", [(_real(tmp_path / "pydanticos-data"), "true"), (str(backup.parent), "true")])
    proc = _up(env, tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "colima start" not in _log(env)


def test_up_sh_existing_profile_with_the_home_directory_mounted_is_still_refused(tmp_path: Path):
    backup = _backup(tmp_path)
    env = _up_env(tmp_path, backup, {"COLIMA_HOME": str(tmp_path / "colima-home")}, stopped=False)
    _write_mounts(
        tmp_path / "colima-home",
        [(_real(tmp_path / "pydanticos-data"), "true"), (_real(Path.home()), "true")],
    )
    proc = _up(env, tmp_path)
    assert proc.returncode != 0 and "Stop it and rerun" in proc.stderr


def test_up_sh_unplugged_backup_volume_stops_before_colima_starts(tmp_path: Path):
    vols = Path(_real(tmp_path)) / "vols"
    vols.mkdir()
    env = _up_env(tmp_path, vols / "EXT" / "backups", {"PYDANTICOS_VOLUMES_DIR": str(vols), "STUB_COLIMA_NEW": "1"})
    proc = _up(env, tmp_path)
    assert proc.returncode != 0
    assert "is not mounted" in proc.stderr
    log = _log(env)
    assert "colima start" not in log and " build " not in log
