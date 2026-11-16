"""W2.3: an agent's attached scorers run after a completed run and are recorded as `score` events."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text

from eap_run_store import ScorerStore
from eap_run_store.scorers import sampled
from eap_worker.scoring import score_data
from eap_worker.evaluators import CaseOutcome
from worker_tests.conftest import APP_VERSION, enqueue_run, event_rows, wait_for, wait_terminal

SPEC = {
    "spec_version": 1, "name": "Echo", "instructions": "Echo.", "model": {"provider": "test", "name": "echo"},
    "tools": [], "model_settings": {},
}


@pytest.fixture
def store(worker):
    return ScorerStore(worker.engine)


@pytest.fixture
def scorer_cleanup(worker, tenant):
    yield
    with worker.engine.begin() as conn:
        conn.execute(text("DELETE FROM scorers WHERE tenant_id = :t"), {"t": tenant})


def _scores(worker, run_id) -> dict[str, dict]:
    return {k: d for _, k, t, d in event_rows(worker.engine, run_id) if t == "score"}


def test_score_data_maps_outcomes():
    scorer = {"id": "s1", "name": "n", "kind": "contains"}
    ok = CaseOutcome(assertions={"n": {"value": True, "reason": None}})
    assert score_data(scorer, ok) == {"scorer": "n", "scorer_id": "s1", "score": 1.0, "passed": True, "reason": None}
    bad = CaseOutcome(assertions={"n": {"value": False, "reason": "no"}})
    assert score_data(scorer, bad)["passed"] is False and "banana" not in score_data(scorer, bad)["reason"]
    failed = CaseOutcome(evaluator_failures=[{"name": "n"}])
    assert score_data(scorer, failed)["score"] is None
    assert score_data(scorer, CaseOutcome()) is None


def test_sampling_is_stable_and_proportional():
    assert sampled("r", "s", 1) and not sampled("r", "s", 0.0)
    assert sampled("r", "s", 0.3) == sampled("r", "s", 0.3)
    hits = sum(sampled(str(uuid.uuid4()), "s", 0.5) for _ in range(400))
    assert 140 < hits < 260


def test_attached_scorers_write_score_events_after_the_run(worker, client, store, tenant, scorer_cleanup):
    agent = uuid.uuid4()
    echo = store.create(tenant, "echoes-input", {"kind": "contains", "value": "pineapple"})
    miss = store.create(tenant, "mentions-banana", {"kind": "contains", "value": "banana"})
    rx = store.create(tenant, "has-digits", {"kind": "regex", "pattern": "\\d+"})
    off = store.create(tenant, "never-sampled", {"kind": "contains", "value": "pine"})
    for s in (echo, miss, rx):
        store.attach(tenant, agent, s.id)
    store.attach(tenant, agent, off.id, Decimal("0.0001"))  # rate small: sampled out for this run
    run_id = enqueue_run(client, worker.run_store, tenant_id=tenant, spec=SPEC, input_text="pineapple 42",
                         app_version=APP_VERSION, agent_id=agent)
    run = wait_terminal(worker.run_store, tenant, run_id)
    assert run["status"] == "completed", run["error"]
    scores = wait_for(lambda: (lambda s: s if len(s) >= 3 else None)(_scores(worker, run_id)), what="score events")

    assert set(scores) >= {f"score:{echo.id}", f"score:{miss.id}", f"score:{rx.id}"}
    assert scores[f"score:{echo.id}"] == {
        "scorer": "echoes-input", "scorer_id": echo.id, "score": 1.0, "passed": True, "reason": None
    }
    assert scores[f"score:{miss.id}"]["passed"] is False and scores[f"score:{miss.id}"]["score"] == 0.0
    assert scores[f"score:{rx.id}"]["passed"] is True
    assert sampled(str(run_id), off.id, 0.0001) == (f"score:{off.id}" in scores)

    events = event_rows(worker.engine, run_id)
    assert events[[e[1] for e in events].index("end")][2] == "run_completed"
    assert [e[0] for e in events] == list(range(len(events)))  # gap-free seq
    # The status is unchanged and no output text is in the score events.
    assert worker.run_store.get_run(tenant, run_id)["status"] == "completed"
    assert "pineapple 42" not in str(scores)


def test_an_agent_without_scorers_has_no_score_events(worker, client, tenant):
    run_id = enqueue_run(client, worker.run_store, tenant_id=tenant, spec=SPEC, input_text="hello",
                         app_version=APP_VERSION)
    assert wait_terminal(worker.run_store, tenant, run_id)["status"] == "completed"
    assert _scores(worker, run_id) == {}
