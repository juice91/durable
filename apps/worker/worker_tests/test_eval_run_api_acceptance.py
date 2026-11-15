"""AC.11/AC.12 end to end (tester): the real API (suite CRUD, run launch with the real launcher) enqueues
`eap.eval_run` on the in-process worker; results, aggregates and compare come back through the API.
Against a published version and against the draft; an invalid regex is a 422 at suite creation."""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from worker_tests.conftest import wait_terminal
from worker_tests.eval_run_helpers import cleanup_evals
from worker_tests.test_working_memory_acceptance import api, auth, make_agent  # noqa: F401 - fixtures
from worker_tests.workflow_run_helpers import agent_spec, cleanup_definitions

V1 = "/api/v1"


@pytest.fixture
def etenant(worker, tenant):
    yield tenant
    with worker.engine.begin() as conn:
        conn.execute(text("DELETE FROM knowledge_bases WHERE tenant_id = :t"), {"t": tenant})
    cleanup_evals(worker.engine, tenant)
    cleanup_definitions(worker.engine, tenant)


def build_suite(api, tenant, agent_id, evaluators, cases) -> str:
    r = api.post(f"{V1}/agents/{agent_id}/eval-suites", json={"name": "e2e", "evaluators": evaluators}, headers=auth(tenant))
    assert r.status_code == 201, r.text
    suite_id = r.json()["id"]
    for case in cases:
        assert api.post(f"{V1}/eval-suites/{suite_id}/cases", json=case, headers=auth(tenant)).status_code == 201
    return suite_id


def launch(api, worker, tenant, agent_id, suite_id, **body):
    run_id = str(uuid.uuid4())
    r = api.post(f"{V1}/agents/{agent_id}/evals/runs", json={"run_id": run_id, "suite_id": suite_id, **body}, headers=auth(tenant))
    assert r.status_code == 201, r.text
    row = wait_terminal(worker.run_store, tenant, uuid.UUID(run_id), timeout=120)
    assert row["status"] == "completed", row
    return run_id, r.json()


def test_invalid_regex_is_accepted_by_the_api_and_is_a_runtime_evaluator_failure(api, worker, etenant):
    """The plan has no publish-time regex syntax check (eap_schemas.evals' docstring says the API checks RE2
    syntax, but only length is validated): an invalid or non-RE2 pattern becomes an evaluator_failures entry."""
    agent_id = make_agent(api, etenant, agent_spec("echo"))
    evaluators = [{"kind": "regex", "pattern": "(unclosed", "name": "bad"}, {"kind": "regex", "pattern": "a(?=b)", "name": "lookahead"},
                  {"kind": "regex", "pattern": "^Echo", "name": "good"}]
    suite_id = build_suite(api, etenant, agent_id, evaluators, [{"name": "c", "input": "x"}])
    run_id, _ = launch(api, worker, etenant, agent_id, suite_id, version=1)
    (item,) = api.get(f"{V1}/agents/{agent_id}/evals/runs/{run_id}/results", headers=auth(etenant)).json()["items"]
    assert item["status"] == "failed" and item["assertions"]["good"]["value"] is True
    assert [(f["name"], f["error"]) for f in item["evaluator_failures"]] == [
        ("bad", "the evaluator raised an error"), ("lookahead", "the evaluator raised an error")]
    assert "unclosed" not in json.dumps(item["evaluator_failures"])


def test_end_to_end_version_and_draft_runs_with_results_aggregates_and_compare(api, worker, etenant):
    agent_id = make_agent(api, etenant, agent_spec("echo"))
    evaluators = [{"kind": "equals_expected"}, {"kind": "regex", "pattern": r"^Echo: "},
                  {"kind": "llm_judge", "rubric": "MUST_CONTAIN:Echo", "model": {"provider": "test", "name": "judge"}, "name": "judge"}]
    cases = [{"name": f"c{i}", "input": f"w{i}", "expected_output": f"Echo: w{i}" if i != 1 else "other"} for i in range(3)]
    suite_id = build_suite(api, etenant, agent_id, evaluators, cases)
    # Draft = same model, changed instructions; the draft target runs without a published version.
    r = api.patch(f"{V1}/agents/{agent_id}", json={"spec": {**agent_spec("echo"), "instructions": "Draft."}}, headers=auth(etenant))
    assert r.status_code == 200, r.text

    ver_id, ver = launch(api, worker, etenant, agent_id, suite_id, version=1)
    draft_id, draft = launch(api, worker, etenant, agent_id, suite_id, version="draft")
    assert ver["draft"] is False and draft["draft"] is True and draft["version"] is None
    for run_id in (ver_id, draft_id):
        got = api.get(f"{V1}/agents/{agent_id}/evals/runs/{run_id}", headers=auth(etenant)).json()
        agg = got["aggregates"]
        assert (agg["cases"], agg["passed"], agg["failed"], agg["errors"]) == (3, 2, 1, 0), got
        items = api.get(f"{V1}/agents/{agent_id}/evals/runs/{run_id}/results", headers=auth(etenant)).json()["items"]
        assert [(i["case_name"], i["status"]) for i in items] == [("c0", "passed"), ("c1", "failed"), ("c2", "passed")]
        assert items[0]["output"] == "Echo: w0" and items[0]["assertions"]["judge"]["value"] is True
        assert items[0]["child_run_id"] and items[0]["cost_usd"] is not None
        assert float(got["cost_usd"]) == pytest.approx(sum(float(i["cost_usd"]) for i in items))  # total cost
    cmp = api.get(f"{V1}/agents/{agent_id}/evals/compare?base={ver_id}&target={draft_id}", headers=auth(etenant))
    assert cmp.status_code == 200, cmp.text
    body = cmp.json()
    assert body["same_suite"] is True and body["warnings"] == []
    # A suite edit between the two launches would flag different hashes: relaunch after adding a case.
    assert api.post(f"{V1}/eval-suites/{suite_id}/cases", json={"name": "late", "input": "z", "expected_output": "Echo: z"},
                    headers=auth(etenant)).status_code == 201
    late_id, _ = launch(api, worker, etenant, agent_id, suite_id, version=1)
    diff = api.get(f"{V1}/agents/{agent_id}/evals/compare?base={ver_id}&target={late_id}", headers=auth(etenant)).json()
    assert diff["same_suite"] is False and diff["warnings"]
    assert json.loads(worker.run_store.get_run(etenant, uuid.UUID(late_id))["output"])["cases"] == 4
