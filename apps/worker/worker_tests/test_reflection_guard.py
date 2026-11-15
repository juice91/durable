"""P1.C6: the reflection growth guard. The reflector's input is capped at
EAP_MEMORY_MAX_ACTIVE_OBSERVATION_TOKENS (oldest active rows left out, one `memory_observe_trimmed`
event with counts only), and no observer checkpoint holds turn or observation text (plans carry ids
and counts; the prompt is rendered inside the model-request step)."""

from __future__ import annotations

import base64
import pickle
import uuid
from types import SimpleNamespace

import psycopg
import pytest

from eap_run_store import MemoryStore
from eap_schemas.memory import ObservationalMemoryConfig
from eap_worker import observational as ob
from worker_tests.conftest import DBOS_DB_URL, event_rows
from worker_tests.test_observational_memory import (  # noqa: F401 - tenant fixture (cleans observations)
    by_key,
    completed_turn,
    obs_spec,
    obs_turn,
    observations,
    tenant,
)
from worker_tests.test_working_memory import conversation

NOTE_WORD = "zebranote"


def _obs(i: int, tokens: int):
    return SimpleNamespace(id=f"o{i}", covers_from_turn=i, covers_to_turn=i, approx_tokens=tokens, kind="observation",
                           content=f"- [medium] note {i}")


def _config(reflect_after: int = 1000) -> ObservationalMemoryConfig:
    return ObservationalMemoryConfig.model_validate(
        {"model": {"provider": "test", "name": "observer"}, "reflect_after_tokens": reflect_after})


def _checkpoints(schema: str, workflow_id: str) -> list[tuple[str, object]]:
    with psycopg.connect(DBOS_DB_URL, connect_timeout=5) as conn:
        rows = conn.execute(
            f'SELECT function_name, output FROM "{schema}".operation_outputs WHERE workflow_uuid = %s ORDER BY function_id',
            (workflow_id,),
        ).fetchall()
    out = []
    for name, raw in rows:
        value = pickle.loads(base64.b64decode(raw)) if raw else None
        out.append((name, value))
    return out


# --------------------------------------------------------------------------- unit


def test_reflect_input_keeps_the_newest_within_the_cap():
    rows = [_obs(i, 400) for i in range(1, 6)]
    kept, dropped = ob.reflect_input(rows, 1000)
    assert [o.id for o in kept] == ["o4", "o5"] and [o.id for o in dropped] == ["o1", "o2", "o3"]
    kept, dropped = ob.reflect_input([_obs(1, 10), _obs(2, 5000)], 1000)
    assert [o.id for o in kept] == ["o2"] and [o.id for o in dropped] == ["o1"]  # always the newest
    kept, dropped = ob.reflect_input(rows, 10_000)
    assert len(kept) == 5 and dropped == []


def test_the_reflect_plan_holds_ids_and_counts_only(monkeypatch):
    monkeypatch.setenv(ob.MAX_ACTIVE_OBSERVATION_TOKENS_ENV, "1000")
    rows = [_obs(i, 600) for i in range(1, 6)]  # 3000 > the floor 2 x 1000: a backlog
    plan = ob._reflect_plan(rows, _config())
    assert plan["supersede_ids"] == ["o1", "o2", "o3", "o4", "o5"]  # the reflection replaces every active row
    assert plan["input_ids"] == ["o3", "o4", "o5"]
    assert (plan["dropped"], plan["kept_tokens"], plan["dropped_tokens"], plan["max_tokens"]) == (2, 1800, 1200, 2000)
    assert "prompt" not in plan and "note" not in repr(plan)


def test_the_cap_floor_leaves_room_for_a_normal_reflection(monkeypatch):
    monkeypatch.setenv(ob.MAX_ACTIVE_OBSERVATION_TOKENS_ENV, "1000")
    # 2000 > 1900: a reflection runs, and the floor (2 x 1900) keeps every row.
    plan = ob._reflect_plan([_obs(i, 400) for i in range(1, 6)], _config(reflect_after=1900))
    assert plan["max_tokens"] == 3800 and plan["dropped"] == 0
    # The default cap with reflect_after_tokens at it: one new observation over the threshold.
    monkeypatch.delenv(ob.MAX_ACTIVE_OBSERVATION_TOKENS_ENV)
    rows = [_obs(1, 16_000), _obs(2, 16_000), _obs(3, 2_000)]
    plan = ob._reflect_plan(rows, _config(reflect_after=32_000))
    assert plan["max_tokens"] == 64_000 and plan["dropped"] == 0
    # The floor also covers one huge newest observation.
    monkeypatch.setenv(ob.MAX_ACTIVE_OBSERVATION_TOKENS_ENV, "1000")
    plan = ob._reflect_plan([_obs(1, 900), _obs(2, 5000)], _config(reflect_after=1000))
    assert plan["max_tokens"] == 6000 and plan["dropped"] == 0


@pytest.mark.parametrize("value", ["999", "abc", "-5"])
def test_a_bad_cap_is_rejected(monkeypatch, value):
    monkeypatch.setenv(ob.MAX_ACTIVE_OBSERVATION_TOKENS_ENV, value)
    with pytest.raises(ValueError):
        ob.max_active_observation_tokens()


def test_the_default_cap(monkeypatch):
    monkeypatch.delenv(ob.MAX_ACTIVE_OBSERVATION_TOKENS_ENV, raising=False)
    assert ob.max_active_observation_tokens() == 32_000


def test_an_old_plan_with_its_prompt_is_used_as_is():
    old = {"action": "observe", "covers_from_turn": 1, "covers_to_turn": 2, "prompt": "TASK: observe (observer-v1)\n..."}
    assert ob.prompt_ref("observe", old, tenant_id="t", agent_id="a", conversation_id="c", redact_json=None) == old["prompt"]
    new = {"action": "observe", "covers_from_turn": 1, "covers_to_turn": 2, "turn_numbers": [1, 2]}
    ref = ob.prompt_ref("observe", new, tenant_id="t", agent_id="a", conversation_id="c", redact_json=None)
    assert ref.startswith(ob.PROMPT_REF_PREFIX) and '"turns":[1,2]' in ref


# --------------------------------------------------------------------------- end to end


def test_a_capped_reflection_drops_the_oldest_with_one_trimmed_event_and_no_text_in_checkpoints(
    worker, client, worker_settings, tenant, monkeypatch
):
    monkeypatch.setenv(ob.MAX_ACTIVE_OBSERVATION_TOKENS_ENV, "1000")
    agent = uuid.uuid4()
    conv = conversation(worker, tenant, agent)
    store = MemoryStore(worker.engine)
    for i in (1, 2, 3):  # three ~880-token observations (~2640) over the 2000-token floor: two fit
        prior = completed_turn(worker, tenant, conv["id"], f"turn {i}")
        store.insert_observation(tenant, str(agent), conv["id"], covers_from_turn=i, covers_to_turn=i,
                                 content=f"- 2026-11-0{i} [medium] " + f"{NOTE_WORD} " * 350, run_id=str(prior))
    spec = obs_spec(keep=5, observe_after=200_000, reflect_after=1000)
    run_id, row = obs_turn(worker, client, worker_settings, tenant, conv, "next turn", spec)
    assert row["status"] == "completed"

    [reflection] = observations(worker, tenant, conv["id"])
    assert (reflection.kind, reflection.covers_from_turn, reflection.covers_to_turn) == ("reflection", 1, 3)
    events = event_rows(worker.engine, run_id)
    [(etype, data)] = by_key(events, "memory:reflect:4:trimmed")
    assert etype == "memory_observe_trimmed"
    assert data["dropped"] == 1 and data["kept_tokens"] > 0 and data["max_tokens"] == 2000
    assert NOTE_WORD not in repr(data)
    keys = [k for _, k, _, _ in events]
    assert keys.index("memory:reflect:4:trimmed") < keys.index("memory:reflect:4")

    checkpoints = _checkpoints(worker_settings.dbos_system_schema, ob.observer_workflow_id(str(run_id)))
    names = [n for n, _ in checkpoints]
    assert names[0] == ob.OBSERVE_STEP and ob.REFLECT_STEP in names
    plan = checkpoints[0][1]
    assert plan["action"] == "reflect" and "prompt" not in plan and len(plan["input_ids"]) == 2
    for name, value in checkpoints:
        if name.startswith(ob.OBSERVER_NAME):
            continue  # the model request's checkpoint is the reflector's answer (the new notes)
        assert NOTE_WORD not in repr(value), name


def test_a_render_failure_answers_a_placeholder_and_is_never_retried(monkeypatch):
    """MINOR-5 (review): a deterministic render failure (rows gone) is answered, not raised, so the
    model step's retry policy does not repeat it; _request records `observer_input_changed`."""
    import asyncio

    calls: list[int] = []

    def gone(ref_text):
        calls.append(1)
        raise ob.ObserverInputChanged("reflect", 1)

    monkeypatch.setattr(ob, "render_prompt_ref", gone)
    ref = ob.prompt_ref("reflect", {"covers_to_turn": 2, "input_ids": ["o1"]}, tenant_id="t", agent_id="a",
                        conversation_id="c", redact_json=None)
    notes, code, usage = asyncio.run(ob._request(ref, "test:observer"))
    assert (notes, code, usage) == (None, ob.INPUT_CHANGED, None)
    assert calls == [1]  # rendered once, no retry
