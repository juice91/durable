"""ArchitectService with a fake run starter: the drafter run and its failures, the model check."""

import json
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from eap_api.ports import RunWaitTimeout, StartedRun
from eap_api.services import architect as architect_module
from eap_api.services.architect import (
    ArchitectService,
    DraftFailed,
    DraftTimeout,
    _compile_refusal,
    drafter_input,
    workflow_drafter_input,
)
from eap_api.services.errors import InvalidInput
from eap_run_store.definition_errors import DefinitionError
from eap_schemas.architect import Allowlist

TENANT = "t1"


class Runs:
    def __init__(self, finished=None, timeout=False):
        self.finished, self.timeout, self.started = finished, timeout, []

    def start_agent_run(self, tenant_id, agent_id, *, input_text, run_id=None, version=None, request_context=None):
        self.started.append((tenant_id, agent_id, input_text))
        return StartedRun(run={"id": str(uuid.uuid4())}, created=True)

    def wait_for_run(self, tenant_id, run_id, *, timeout, poll_interval=0.5):
        if self.timeout:
            raise RunWaitTimeout()
        return self.finished

    def read_run_json(self, tenant_id, run_id):
        raise NotImplementedError


def service(runs):
    return ArchitectService(MagicMock(), MagicMock(), runs)


def drafter_answer(output, status="completed", **extra):
    return {"status": status, "output": output, "model_id": "test:m", **extra}


def run(runs, prompt="p"):
    return service(runs).run_drafter(TENANT, str(uuid.uuid4()), prompt, "agent")


def test_run_drafter_parses_the_answer_and_strips_assumptions():
    runs = Runs(drafter_answer(json.dumps({"name": "x", "assumptions": ["a", 3, "b"]})))
    parsed, assumptions, raw, run_id, model = run(runs)
    assert parsed == {"name": "x"} and assumptions == ["a", "b"] and model == "test:m"
    assert uuid.UUID(run_id) and json.loads(raw)["name"] == "x"
    assert runs.started[0][0] == TENANT and runs.started[0][2] == "p"


def test_run_drafter_failed_run_is_502():
    with pytest.raises(DraftFailed) as caught:
        run(Runs(drafter_answer(None, status="failed", error="boom")))
    assert caught.value.status_code == 502 and caught.value.code == "draft_failed"


def test_run_drafter_timeout_is_504():
    with pytest.raises(DraftTimeout) as caught:
        run(Runs(timeout=True))
    assert caught.value.status_code == 504 and caught.value.code == "draft_timeout"


@pytest.mark.parametrize("output", ["not json", "[1]"])
def test_run_drafter_answer_that_is_not_an_object_is_422(output):
    with pytest.raises(DefinitionError) as caught:
        run(Runs(drafter_answer(output)))
    assert caught.value.status_code == 422 and caught.value.code == "draft_invalid"


def test_run_drafter_waits_with_the_module_timeout(monkeypatch):
    seen = {}
    runs = Runs(drafter_answer("{}"))
    original = runs.wait_for_run
    runs.wait_for_run = lambda *a, **k: (seen.update(k), original(*a, **k))[1]
    monkeypatch.setattr(architect_module, "DRAFT_TIMEOUT_S", 0.5)
    run(runs)
    assert seen["timeout"] == 0.5
    service(runs).run_drafter(TENANT, str(uuid.uuid4()), "p", "agent", 7.0)
    assert seen["timeout"] == 7.0


def test_set_model_refuses_a_model_the_allowlist_lacks(monkeypatch):
    monkeypatch.setenv("EAP_ALLOWED_MODELS", "test:echo")
    with pytest.raises(InvalidInput) as caught:
        service(Runs()).set_model(TENANT, "u", "test:other")
    assert caught.value.code == "model_not_allowed"


def test_compile_refusal_takes_the_code_from_the_message():
    assert _compile_refusal(ValueError("tool_not_allowed: x")).code == "tool_not_allowed"
    assert _compile_refusal(ValueError("something odd")).code == "invalid_spec"


def test_drafter_inputs_name_what_may_be_granted():
    empty = Allowlist(tools=(), mcp_servers=(), knowledge_bases=(), models=(), agents=(), default_model=None)
    assert "Tools: none" in drafter_input(" hi ", None, empty)
    assert drafter_input(" hi ", None, empty).startswith("hi")
    assert "Agents (id): none" in workflow_drafter_input("hi", None, empty)
