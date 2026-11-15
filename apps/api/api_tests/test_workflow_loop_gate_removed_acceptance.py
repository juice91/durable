"""LP.11 acceptance (tester): the loops preview gate is gone. Since LP.16 (the runtime gate removed)
`evaluate` publishes too, whatever the old flag says."""

from __future__ import annotations

import pathlib

import pytest

from eap_api.app import create_app

from .run_helpers import requires_pg
from .test_workflow_loops_api_acceptance import _runs  # noqa: F401
from .test_workflow_loops_gate import EVALUATE, FOREACH, LIMITS, LOOP_FREE_LIMITS, NESTED_FOREACH, VALID_LOOP, _publish
from .test_workflow_runs_api import PG_URL, api, fake  # noqa: F401 - fixtures

pytestmark = requires_pg
BASE = "/api/v1/workflows"
FLAG = "EAP_WORKFLOW_LOOPS_PREVIEW"
ROOT = pathlib.Path(__file__).resolve().parents[3]


def test_no_source_file_reads_the_flag():
    reads = []
    for sub in ("apps/api/src", "apps/worker/src", "packages"):
        for path in (ROOT / sub).rglob("*.py"):
            if "/tests/" in str(path) or path.name.startswith("test_"):
                continue
            for n, line in enumerate(path.read_text().splitlines(), 1):
                if FLAG in line and not line.lstrip().startswith(("#", '"""')):
                    reads.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()}")
    assert not reads, reads


def test_the_dev_env_example_sets_no_flag():
    active = [ln for ln in (ROOT / ".dev.env.example").read_text().splitlines()
              if ln.startswith(FLAG) or ln.startswith(f"export {FLAG}")]
    assert active == []


@pytest.mark.parametrize("value", [None, "0", "false", "garbage"])
@pytest.mark.parametrize(
    "spec", [VALID_LOOP, FOREACH, NESTED_FOREACH, LIMITS, LOOP_FREE_LIMITS],
    ids=["loop", "foreach", "nested-foreach", "loop-limits", "limits"],
)
def test_loops_and_foreach_publish_and_start_by_default(api, fake, monkeypatch, value, spec):
    client, app, tenant_of = api
    tenant, headers = tenant_of()
    if value is None:
        monkeypatch.delenv(FLAG, raising=False)
    else:
        monkeypatch.setenv(FLAG, value)
    resp = _publish(client, headers, spec)
    assert resp.status_code == 201, resp.text
    workflow_id = resp.json()["workflow_id"] if "workflow_id" in resp.json() else resp.json()["id"]
    started = client.post(f"{BASE}/{workflow_id}/runs", json={"input": {"list": [1, 2]}}, headers=headers)
    assert started.status_code == 202, started.text


@pytest.mark.parametrize("value", [None, "1", "garbage"])
def test_evaluate_publishes_whatever_the_flag_says(client, auth_header, monkeypatch, value):
    if value is None:
        monkeypatch.delenv(FLAG, raising=False)
    else:
        monkeypatch.setenv(FLAG, value)
    resp = _publish(client, auth_header, EVALUATE)
    assert resp.status_code == 201, resp.text


def test_the_app_starts_with_a_garbage_flag(monkeypatch):
    monkeypatch.setenv(FLAG, "definitely-not-a-bool")
    create_app(database_url=PG_URL).state.database_engine.dispose()
