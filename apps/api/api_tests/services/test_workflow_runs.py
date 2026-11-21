"""WorkflowRunService helpers and refusals that need no database."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from eap_api.services.workflow_runs import WorkflowRunService, canonical_json, resume_data_errors
from eap_run_store.definition_errors import DefinitionError


def test_canonical_json_is_sorted_and_compact():
    assert canonical_json({"b": 1, "a": [1, 2]}) == '{"a":[1,2],"b":1}'


def test_resume_data_errors_name_the_path_and_keyword_never_the_value():
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
    assert resume_data_errors(schema, {"n": 1}) == []
    [error] = resume_data_errors(schema, {"n": "secret"})
    assert error["loc"] == ["body", "data", "n"] and error["type"] == "json_schema.type"
    assert "secret" not in str(error)
    assert resume_data_errors(schema, {"n": float("nan")})[0]["type"] == "finite_number"


class _Runs:
    def __init__(self, run):
        self.run = run

    def get_run(self, tenant_id, run_id):
        return self.run


def test_decide_step_refuses_an_unknown_or_non_workflow_run():
    for run in (None, {"kind": "agent"}):
        service = WorkflowRunService(SimpleNamespace(), _Runs(run), SimpleNamespace(), SimpleNamespace())
        with pytest.raises(DefinitionError) as refused:
            service.decide_step("t", "alice", uuid.uuid4(), "ask", "approved", None)
        assert refused.value.status_code == 404 and refused.value.body == {"detail": "Run not found"}


def test_cancel_refuses_a_run_that_is_not_a_workflow_run_with_its_code():
    service = WorkflowRunService(SimpleNamespace(), _Runs({"kind": "agent"}), SimpleNamespace(), SimpleNamespace())
    with pytest.raises(DefinitionError) as refused:
        service.cancel("t", "alice", uuid.uuid4())
    assert refused.value.status_code == 409
    assert refused.value.body == {"detail": "only workflow runs can be cancelled", "code": "cancel_not_supported"}
