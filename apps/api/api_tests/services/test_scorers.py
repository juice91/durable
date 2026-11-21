"""ScorerService with fake stores."""

import uuid
from unittest.mock import MagicMock

import pytest

from eap_api.services.errors import Conflict, InvalidInput, NotFound, Unavailable
from eap_api.services.scorers import ScorerService, validate_evaluator
from eap_run_store.scorers import ScorerLimitExceeded, ScorerNameConflict

TENANT = "t1"


def make(store=None, run=None, scored=()):
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = object()
    db.execute.return_value = [(f"score:{s}",) for s in scored]
    run_store = MagicMock()
    run_store.get_run.return_value = run
    return ScorerService(store or MagicMock(), db, run_store, MagicMock())


def test_invalid_evaluators_name_the_path():
    with pytest.raises(InvalidInput):
        validate_evaluator({"kind": "nope"})
    with pytest.raises(InvalidInput) as exc:
        validate_evaluator({"kind": "regex", "pattern": "(?<=a)b"})
    assert exc.value.message.startswith("evaluator.pattern:")


def test_name_and_limit_conflicts():
    store = MagicMock()
    store.create.side_effect = ScorerNameConflict()
    with pytest.raises(Conflict):
        make(store).create(TENANT, "n", {"kind": "contains", "value": "x"}, None)
    store.attach.side_effect = ScorerLimitExceeded()
    with pytest.raises(Conflict):
        make(store).attach(TENANT, uuid.uuid4(), uuid.uuid4(), 1)


def test_score_run_states():
    ids = [uuid.uuid4(), uuid.uuid4()]
    with pytest.raises(NotFound):
        make(run=None).score_run(TENANT, uuid.uuid4(), ids)
    with pytest.raises(Conflict):
        make(run={"status": "failed"}).score_run(TENANT, uuid.uuid4(), ids)
    done = make(run={"status": "completed"}, scored=[str(i) for i in ids]).score_run(TENANT, uuid.uuid4(), ids)
    assert done.scorer_ids == [] and len(done.already_scored) == 2
    partial = make(run={"status": "completed"}, scored=[str(ids[0])]).score_run(TENANT, uuid.uuid4(), ids)
    assert partial.scorer_ids == [str(ids[1])] and partial.already_scored == [str(ids[0])]


def test_a_failed_enqueue_is_unavailable():
    service = make(run={"status": "completed"})
    service.launcher.launch_score.side_effect = RuntimeError("down")
    with pytest.raises(Unavailable):
        service.score_run(TENANT, uuid.uuid4(), [uuid.uuid4()])
