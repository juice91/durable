"""P1.C1 acceptance: the `{observe, index}` values of `post_turn_done` for each spec combination
(observational only, semantic recall only, both, neither), failed and skipped included; a failed
turn and a conversation deleted mid-turn get no event; the event carries no text; replay of the
step cannot change a recorded event."""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from eap_run_store import MemoryStore
from eap_schemas import AgentSpec
from eap_worker import workflow as wf
from eap_worker.post_turn import finish_post_turn, post_turn_event_key
from worker_tests.conftest import event_rows, ticket_spec
from worker_tests.test_observational_memory import (  # noqa: F401 - tenant fixture (cleans observations)
    fact,
    obs_spec,
    obs_turn,
    observations,
    tenant,
    wait_workflow,
)
from worker_tests.test_semantic_recall import recall_spec, rturn
from worker_tests.test_working_memory import conversation, memory_spec

DONE = "post_turn_done"
BAD_OBSERVER = {"provider": "test", "name": "observer-invalid"}
OBS = obs_spec(keep=0)["memory"]["observational"]
OBS_BAD = obs_spec(keep=0, observer=BAD_OBSERVER)["memory"]["observational"]
OBS_QUIET = obs_spec()["memory"]["observational"]  # nothing to observe after one short turn
LONG, SHORT = fact(1, 1000), "hello there"
DISALLOWED = {"embedding_model": "openai:text-embedding-3-small"}


def _done(worker, run_id) -> list[tuple[str, dict]]:
    return [(k, d) for _, k, t, d in event_rows(worker.engine, run_id) if t == DONE]


def _forget_before_index(monkeypatch, worker, tenant, agent):
    """The resource is forgotten between the answer and the post-turn index: the index skips."""
    real = wf.index_after_turn

    async def forget_then_index(*args, **kwargs):
        await asyncio.to_thread(MemoryStore(worker.engine).forget_resource, tenant, "user-1", agent_id=str(agent))
        return await real(*args, **kwargs)

    monkeypatch.setattr(wf, "index_after_turn", forget_then_index)


CASES = [  # (id, spec, input, forget first?, expected {observe, index})
    ("obs-skipped", lambda: obs_spec(), SHORT, False, {"observe": "skipped", "index": "off"}),
    ("obs-created", lambda: obs_spec(keep=0), LONG, False, {"observe": "created", "index": "off"}),
    ("obs-failed", lambda: obs_spec(keep=0, observer=BAD_OBSERVER), LONG, False, {"observe": "failed", "index": "off"}),
    ("recall-done", lambda: recall_spec(), SHORT, False, {"observe": "off", "index": "done"}),
    ("recall-failed", lambda: recall_spec(recall=DISALLOWED), SHORT, False, {"observe": "off", "index": "failed"}),
    ("recall-skipped", lambda: recall_spec(), SHORT, True, {"observe": "off", "index": "skipped"}),
    ("both-created-done", lambda: recall_spec(observational=OBS), LONG, False, {"observe": "created", "index": "done"}),
    ("both-skipped-done", lambda: recall_spec(observational=OBS_QUIET), SHORT, False,
     {"observe": "skipped", "index": "done"}),
    ("both-failed-failed", lambda: recall_spec(recall=DISALLOWED, observational=OBS_BAD), LONG, False,
     {"observe": "failed", "index": "failed"}),
    ("both-created-failed", lambda: recall_spec(recall=DISALLOWED, observational=OBS), LONG, False,
     {"observe": "created", "index": "failed"}),
    ("both-failed-skipped", lambda: recall_spec(observational=OBS_BAD), LONG, True,
     {"observe": "failed", "index": "skipped"}),
]


@pytest.mark.parametrize(("spec", "text_in", "forget", "expected"), [c[1:] for c in CASES], ids=[c[0] for c in CASES])
def test_each_spec_combination_records_its_values_once(worker, client, worker_settings, tenant, monkeypatch,
                                                       spec, text_in, forget, expected):
    agent = uuid.uuid4()
    conv = conversation(worker, tenant, agent)
    if forget:
        _forget_before_index(monkeypatch, worker, tenant, agent)
    run_id, row = rturn(worker, client, worker_settings, tenant, conv, text_in, spec())
    assert row["status"] == "completed", row
    assert _done(worker, run_id) == [("memory:post_turn:1", expected)]
    keys = [k for _, k, _, _ in event_rows(worker.engine, run_id)]
    assert keys[-1] == "memory:post_turn:1" and keys.index("end") < keys.index("memory:post_turn:1")
    blob = json.dumps(_done(worker, run_id))
    assert "word" not in blob and "hello" not in blob and set(_done(worker, run_id)[0][1]) == {"observe", "index"}


@pytest.mark.parametrize("memory", [{}, {"working": {}}, {"history": {"last_turns": 3}}, {"working": {}, "history": {"last_turns": 3}}],
                         ids=["bare-memory", "working", "history", "working+history"])
def test_neither_feature_records_no_event(worker, client, worker_settings, tenant, memory):
    conv = conversation(worker, tenant, uuid.uuid4())
    run_id, row = rturn(worker, client, worker_settings, tenant, conv, SHORT, memory_spec(**memory))
    assert row["status"] == "completed"
    assert _done(worker, run_id) == []
    assert not any(k.startswith("memory:post_turn") for _, k, _, _ in event_rows(worker.engine, run_id))


def test_a_spec_without_a_memory_key_records_no_event(worker, client, worker_settings, tenant):
    from worker_tests.test_working_memory import turn

    conv = conversation(worker, tenant, uuid.uuid4())
    run_id, row = turn(worker, client, tenant, conv, SHORT, ticket_spec(tools=(), model="echo"))
    assert row["status"] == "completed"
    wait_workflow(worker_settings.dbos_system_schema, run_id)
    assert _done(worker, run_id) == []


def test_a_failed_turn_records_no_event(worker, client, worker_settings, tenant):
    from worker_tests.test_working_memory import turn

    conv = conversation(worker, tenant, uuid.uuid4())
    spec = recall_spec(observational=OBS)
    spec["tools"] = ["nope.missing"]  # an unknown tool fails the run before any model request
    run_id, row = turn(worker, client, tenant, conv, LONG, spec)
    assert row["status"] == "failed", row
    wait_workflow(worker_settings.dbos_system_schema, run_id)
    assert _done(worker, run_id) == []
    assert observations(worker, tenant, conv["id"]) == []


@pytest.mark.parametrize("which", ["index", "observe"])
def test_a_conversation_deleted_mid_turn_gets_no_event(worker, client, worker_settings, tenant, monkeypatch, which):
    """The conversation is deleted while the post-turn steps run: the steps and the event are no-ops."""
    agent = uuid.uuid4()
    conv = conversation(worker, tenant, agent)
    name = "index_after_turn" if which == "index" else "observe_after_turn"
    real = getattr(wf, name)

    async def delete_then_run(*args, **kwargs):
        assert await asyncio.to_thread(MemoryStore(worker.engine).delete_conversation, tenant, conv["id"])
        return await real(*args, **kwargs)

    monkeypatch.setattr(wf, name, delete_then_run)
    run_id, row = rturn(worker, client, worker_settings, tenant, conv, LONG, recall_spec(observational=OBS))
    assert row["status"] in ("completed", "failed")  # the answer was delivered before the delete
    assert _done(worker, run_id) == []
    assert not any(k.startswith("memory:post_turn") or k.startswith("memory:observe") for _, k, _, _ in
                   event_rows(worker.engine, run_id))


def test_replaying_the_step_never_changes_a_recorded_event(worker, client, worker_settings, tenant):
    agent = uuid.uuid4()
    conv = conversation(worker, tenant, agent)
    spec = recall_spec(observational=OBS)
    run_id, _ = rturn(worker, client, worker_settings, tenant, conv, LONG, spec)
    assert _done(worker, run_id) == [("memory:post_turn:1", {"observe": "created", "index": "done"})]
    [(seq, _, _, _)] = [e for e in event_rows(worker.engine, run_id) if e[2] == DONE]
    # The workflow code runs again (a recovery) with different step outcomes: the recorded event wins.
    out = asyncio.run(finish_post_turn(str(run_id), tenant, str(agent), conv["id"], 1,
                                       AgentSpec.model_validate(spec), {"status": "failed"},
                                       {"observe": "failed", "reflect": None}))
    assert out == {"seq": seq, "observe": "failed", "index": "failed"}  # the step answers the recorded seq
    assert _done(worker, run_id) == [("memory:post_turn:1", {"observe": "created", "index": "done"})]
    assert post_turn_event_key(1) == "memory:post_turn:1" and post_turn_event_key(7) == "memory:post_turn:7"
