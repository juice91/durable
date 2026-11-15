"""LP.8: EAP_CANCEL_RECONCILE_* are validated when `python -m eap_worker` starts (before DBOS launches)."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from worker_tests.conftest import APP_DB_URL, DBOS_DB_URL

BAD = [
    ("EAP_CANCEL_RECONCILE_AFTER_SECONDS", "0"),
    ("EAP_CANCEL_RECONCILE_AFTER_SECONDS", "-5"),
    ("EAP_CANCEL_RECONCILE_AFTER_SECONDS", "soon"),
    ("EAP_CANCEL_RECONCILE_LIMIT", "0"),
    ("EAP_CANCEL_RECONCILE_LIMIT", "1001"),
    ("EAP_CANCEL_RECONCILE_LIMIT", "many"),
]


@pytest.mark.parametrize(("name", "value"), BAD)
def test_invalid_value_fails_the_worker_at_startup(name, value):
    env = {**os.environ, "EAP_DATABASE_URL": APP_DB_URL, "EAP_DBOS_URL": DBOS_DB_URL,
           "EAP_DBOS_SYSTEM_SCHEMA": "eap_cancel_env_never_created", "EAP_EXECUTOR_ID": "env-check", name: value}
    proc = subprocess.run([sys.executable, "-m", "eap_worker"], env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode != 0
    assert name in proc.stderr + proc.stdout
    assert "EAP_WORKER_READY" not in proc.stdout + proc.stderr
