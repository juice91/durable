"""Under pytest-xdist every worker process gets its own migrated schema, so `-n N` suites do not share tables."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

from eap_db.testing import SCHEMA_ENV

requires_schema = pytest.mark.skipif(
    not os.environ.get(SCHEMA_ENV), reason="needs the eap_db pytest plugin's migrated schema (dev Postgres)"
)

PROBE = """
import os, pathlib

OUT = pathlib.Path(os.environ["XDIST_PROBE_OUT"])

def _record():
    with OUT.open("a") as fh:
        fh.write(os.getpid().__str__() + " " + os.environ.get("EAP_TEST_SCHEMA", "") + "\\n")

def test_a(): _record()
def test_b(): _record()
def test_c(): _record()
def test_d(): _record()
"""


@requires_schema
def test_each_xdist_worker_runs_in_its_own_schema(tmp_path):
    probe = tmp_path / "test_probe.py"
    probe.write_text(textwrap.dedent(PROBE))
    out = tmp_path / "out.txt"
    env = {**os.environ, "XDIST_PROBE_OUT": str(out)}
    env.pop(SCHEMA_ENV, None)  # a worker must create its own, not inherit the parent session's
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-n", "2", "--dist", "load", "-q", "-p", "no:cacheprovider", str(probe)],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    rows = [line.split() for line in out.read_text().splitlines()]
    assert len(rows) == 4
    schemas_by_pid: dict[str, set[str]] = {}
    for pid, schema in rows:
        assert schema.startswith("eap_test_")
        assert schema != os.environ[SCHEMA_ENV]
        schemas_by_pid.setdefault(pid, set()).add(schema)
    assert all(len(schemas) == 1 for schemas in schemas_by_pid.values())  # one schema per worker
    assert len({next(iter(s)) for s in schemas_by_pid.values()}) == len(schemas_by_pid) >= 2  # distinct across workers
