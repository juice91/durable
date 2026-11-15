"""RAG P1 quality fix 4, acceptance: kb_import validates --report-json up front and guards the write."""

from __future__ import annotations

import json
from pathlib import Path

import importlib.util
import sys

_spec = importlib.util.spec_from_file_location("kb_import_p1q_helpers", Path(__file__).with_name("test_kb_import.py"))
assert _spec is not None and _spec.loader is not None
helpers = importlib.util.module_from_spec(_spec)
sys.modules["kb_import_p1q_helpers"] = helpers
_spec.loader.exec_module(helpers)
KB, FakeApi, run_cli, tree = helpers.KB, helpers.FakeApi, helpers.run_cli, helpers.tree  # noqa: F401 - tree is a fixture

UPLOADS = ("PUT", "DELETE")


def uploaded(api: FakeApi) -> bool:
    return bool(api.docs) or any(m in UPLOADS for m, *_ in api.requests)


def test_missing_directory_exits_2_before_any_request_and_is_not_created(tree: Path, tmp_path: Path) -> None:
    api = FakeApi()
    target = tmp_path / "a" / "b" / "r.json"
    code, out, err = run_cli([str(tree), "--kb", KB, "--report-json", str(target)], api)
    assert code == 2 and "does not exist" in err and "Traceback" not in err
    assert not uploaded(api) and not target.parent.exists() and out == ""


def test_parent_that_is_a_file_exits_2_with_no_upload(tree: Path, tmp_path: Path) -> None:
    api = FakeApi()
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    code, _, err = run_cli([str(tree), "--kb", KB, "--report-json", str(blocker / "r.json")], api)
    assert code == 2 and "--report-json" in err and "Traceback" not in err
    assert "is not a directory" in err
    assert not uploaded(api)


def test_path_that_is_a_directory_exits_2_with_no_upload(tree: Path, tmp_path: Path) -> None:
    api = FakeApi()
    code, _, err = run_cli([str(tree), "--kb", KB, "--report-json", str(tmp_path)], api)
    assert code == 2 and "is a directory" in err and not uploaded(api)


def test_unwritable_directory_exits_2_with_no_upload(tree: Path, tmp_path: Path) -> None:
    """A read-only report directory is refused up front (review MINOR 5), not found out after the
    whole import uploaded."""
    out_dir = tmp_path / "ro"
    out_dir.mkdir()
    api = FakeApi()
    out_dir.chmod(0o500)
    try:
        code, out, err = run_cli([str(tree), "--kb", KB, "--report-json", str(out_dir / "r.json")], api)
    finally:
        out_dir.chmod(0o700)
    assert code == 2 and not uploaded(api)
    assert "is not writable" in err and "Traceback" not in err and not (out_dir / "r.json").exists()


def test_valid_path_writes_a_json_report_and_exits_0(tree: Path, tmp_path: Path) -> None:
    api = FakeApi()
    target = tmp_path / "report.json"
    code, out, err = run_cli([str(tree), "--kb", KB, "--report-json", str(target)], api)
    assert code == 0, err
    data = json.loads(target.read_text())
    assert isinstance(data, dict) and data and uploaded(api)


def test_relative_filename_in_the_current_directory_is_valid(tree: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    code, _, err = run_cli([str(tree), "--kb", KB, "--report-json", "r.json"], FakeApi())
    assert code == 0, err
    assert (tmp_path / "r.json").is_file()


def test_dry_run_also_checks_the_report_path_first(tree: Path, tmp_path: Path) -> None:
    api = FakeApi()
    code, _, err = run_cli([str(tree), "--kb", KB, "--dry-run", "--report-json", str(tmp_path / "no" / "r.json")], api)
    assert code == 2 and "does not exist" in err
