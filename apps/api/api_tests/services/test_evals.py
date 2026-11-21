"""EvalService with fake stores: the refusals, idempotent starts and cursors, no HTTP and no database."""

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from eap_api.services.errors import Conflict, InvalidInput, NotFound
from eap_api.services.evals import (
    EvalService,
    check_regex_evaluators,
    side_effect_warnings,
    workflow_input_problem,
)
from eap_run_store.evals import EvalSuiteArchived, EvalSuiteNotFound
from eap_schemas import AgentSpec
from eap_schemas.evals import EvalSuiteInvalid

TENANT = "t1"


def make(store=None, run_store=None, row=None):
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = row
    return EvalService(store or MagicMock(), db, run_store or MagicMock(), MagicMock())


def test_store_errors_become_service_refusals():
    store = MagicMock()
    store.delete_suite.side_effect = EvalSuiteNotFound(TENANT, uuid.uuid4())
    with pytest.raises(NotFound):
        make(store).delete_suite(TENANT, uuid.uuid4())
    store.update_suite.side_effect = EvalSuiteArchived("s")
    with pytest.raises(Conflict):
        make(store).update_suite(TENANT, uuid.uuid4(), {"name": "x"}, expected_revision=None)


def test_regex_that_re2_refuses_is_invalid_with_the_path():
    with pytest.raises(EvalSuiteInvalid) as exc:
        check_regex_evaluators([{"kind": "regex", "pattern": "(?<=a)b"}], "evaluators")
    assert exc.value.path == "evaluators.0.pattern"


def test_workflow_case_input_problems_carry_the_old_body():
    problem = workflow_input_problem(None, "case-1", "not json")
    assert isinstance(problem, InvalidInput)
    assert problem.detail_object["code"] == "input_schema_errors" and problem.detail_object["case"] == "case-1"
    assert workflow_input_problem(None, "c", '{"a": 1}') is None


def test_unknown_agent_is_not_found():
    with pytest.raises(NotFound):
        make(row=None).list_suites(TENANT, uuid.uuid4(), include_archived=False)


def test_a_workflow_cannot_be_evaluated_at_a_draft():
    with pytest.raises(InvalidInput):
        make().start_workflow_eval_run(
            TENANT, uuid.uuid4(), run_id=uuid.uuid4(), suite_id=uuid.uuid4(), version="draft", max_concurrency=1,
            trace_id=None, traceparent=None,
        )


def test_a_recorded_run_id_of_another_request_is_a_conflict():
    workflow_id, suite_id, run_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    run_store = MagicMock()
    run_store.get_run.return_value = {
        "id": str(run_id), "kind": "eval", "workflow_id": str(workflow_id), "eval_suite_id": str(uuid.uuid4()),
        "input": '{"max_concurrency": 1}',
    }
    service = make(run_store=run_store, row=SimpleNamespace(id=str(workflow_id), archived=False, name="w"))
    with pytest.raises(Conflict):
        service.start_workflow_eval_run(
            TENANT, workflow_id, run_id=run_id, suite_id=suite_id, version=None, max_concurrency=1,
            trace_id=None, traceparent=None,
        )


def test_result_cursor_is_checked():
    with pytest.raises(InvalidInput):
        make().list_eval_results(TENANT, uuid.uuid4(), uuid.uuid4(), workflow=False, limit=10, after="!!not-a-cursor")


def test_side_effect_warnings_skip_gated_tools():
    spec = AgentSpec.model_validate({
        "name": "a", "instructions": "i", "model": {"provider": "test", "name": "m"},
        "mcp_tools": [{"server": "s", "tool": "t"}], "approvals": {"tools": ["s/t"]},
    })
    assert side_effect_warnings(spec) == []
