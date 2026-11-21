"""ScorerSuggestionService with fake ports."""

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from eap_api.services.errors import NotFound
from eap_api.services.scorer_suggestions import ScorerSuggestionService, build_prompt, clear_cache
from eap_run_store.definition_errors import DefinitionError, refuse

TENANT = "t1"


class Ports:
    def __init__(self, model="test:m", answer=None, error=None):
        self.model, self.answer, self.error, self.runs = model, answer or {"suggestions": []}, error, 0

    def ensure_suggester(self, tenant_id, user_id):
        if self.error:
            raise self.error
        return "suggester-id", self.model

    def run_drafter(self, suggester_id, prompt):
        self.runs += 1
        return self.answer, [], "raw", "run-1", self.model


def make(row=SimpleNamespace(name="a", draft_spec={"instructions": "hi"})):
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = row
    run_store = MagicMock()
    run_store.list_runs.return_value = []
    return ScorerSuggestionService(db, run_store)


@pytest.fixture(autouse=True)
def _clean():
    clear_cache()
    yield
    clear_cache()


def test_unknown_target_is_not_found():
    with pytest.raises(NotFound):
        make(row=None).suggest(TENANT, "u", "agent", uuid.uuid4(), max=3, include_runs=True, ports=Ports())


def test_the_answer_is_cached_so_a_repeat_costs_no_run():
    service, ports, target = make(), Ports(), uuid.uuid4()
    first = service.suggest(TENANT, "u", "agent", target, max=3, include_runs=False, ports=ports)
    second = service.suggest(TENANT, "u", "agent", target, max=3, include_runs=False, ports=ports)
    assert first == second and ports.runs == 1


def test_choose_model_becomes_no_model():
    error = refuse(409, "choose", code="choose_model")
    with pytest.raises(DefinitionError) as exc:
        make().suggest(TENANT, "u", "agent", uuid.uuid4(), max=3, include_runs=True, ports=Ports(error=error))
    assert exc.value.code == "no_model"


def test_an_answer_without_a_list_is_suggest_invalid():
    with pytest.raises(DefinitionError) as exc:
        make().suggest(TENANT, "u", "agent", uuid.uuid4(), max=3, include_runs=True, ports=Ports(answer={"x": 1}))
    assert exc.value.code == "suggest_invalid"


def test_prompt_is_bounded():
    assert len(build_prompt("d" * 50000, [], 8)) <= 20000
