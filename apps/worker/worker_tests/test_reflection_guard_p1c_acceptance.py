"""P1.C6 acceptance: the reflection growth guard over many turns with a failing reflector, the
`memory_observe_trimmed` counts, the environment variable's validation at start, and the cap floor
(never below 2 x reflect_after_tokens nor reflect_after_tokens + the largest active row, so a
normal reflection is never trimmed)."""

from __future__ import annotations

import json
import re
import uuid
from types import SimpleNamespace

import pytest
from pydantic_ai.messages import ModelResponse, TextPart

from eap_schemas.knowledge import approx_tokens
from eap_worker import observational as ob
from eap_worker.app import validate_startup
from worker_tests.conftest import event_rows
from worker_tests.test_crash_recovery import WorkerProcess, _env, crash_schema  # noqa: F401 - fixture
from worker_tests.test_observational_memory import (  # noqa: F401 - tenant fixture (cleans observations)
    by_key,
    fact,
    obs_spec,
    obs_turn,
    observations,
    tenant,
)
from worker_tests.test_working_memory import conversation

ENV = ob.MAX_ACTIVE_OBSERVATION_TOKENS_ENV
BULK = 6  # notes per observation, ~300 characters each


def _row(i: int, tokens: int):
    return SimpleNamespace(id=f"o{i}", covers_from_turn=i, covers_to_turn=i, approx_tokens=tokens, kind="observation",
                           content="x")


def _config(reflect_after: int):
    from eap_schemas.memory import ObservationalMemoryConfig

    return ObservationalMemoryConfig.model_validate(
        {"model": {"provider": "test", "name": "observer"}, "reflect_after_tokens": reflect_after})


# --------------------------------------------------------------------------- the cap floor


@pytest.mark.parametrize(("cap", "reflect_after", "effective"), [
    (1000, 1000, 2000), (1000, 1001, 2002), (1000, 8000, 16000), (5000, 1000, 5000), (5000, 5000, 10000),
    (32000, 8000, 32000), (32000, 32000, 64000), (1000, 200_000, 400_000),
])
def test_the_effective_cap_is_never_below_twice_reflect_after_tokens(monkeypatch, cap, reflect_after, effective):
    monkeypatch.setenv(ENV, str(cap))
    rows = [_row(i, 600) for i in range(1, 400)]  # far beyond any cap
    plan = ob._reflect_plan(rows, _config(reflect_after))
    assert plan["max_tokens"] == effective
    assert plan["kept_tokens"] <= effective and plan["kept_tokens"] + plan["dropped_tokens"] == 600 * 399
    assert plan["observations"] == 399 and plan["dropped"] == 399 - len(plan["input_ids"])
    assert plan["supersede_ids"] == [r.id for r in rows]  # a reflection replaces every active row


def test_nothing_is_trimmed_when_the_rows_fit_the_floor(monkeypatch):
    monkeypatch.setenv(ENV, "1000")
    rows = [_row(i, 500) for i in range(1, 6)]  # 2500 tokens in all
    plan = ob._reflect_plan(rows, _config(2500 - 1))
    assert plan is not None and plan["max_tokens"] == 4998 and plan["dropped"] == 0  # a normal reflection
    plan = ob._reflect_plan(rows, _config(1200))
    assert plan["max_tokens"] == 2400 and plan["dropped"] == 1  # 2500 > 2 x 1200: a backlog, trimmed
    assert ob._reflect_plan(rows, _config(2500)) is None  # the threshold is "more than": no reflection at all


@pytest.mark.parametrize("reflect_after", [8000, 30_000, 32_000, 50_000, 200_000])
def test_a_normal_reflection_is_never_trimmed(monkeypatch, reflect_after):
    """MAJOR-1 (review): rows that just crossed reflect_after_tokens with one new observation, even
    one larger than the default cap, are all sent to the reflector."""
    monkeypatch.delenv(ENV, raising=False)
    below = [_row(i, reflect_after // 4) for i in range(1, 5)]  # exactly at the threshold: no reflection
    assert ob._reflect_plan(below, _config(reflect_after)) is None
    for newest in (1, 3000, 40_000, 100_000):
        rows = [*below, _row(5, newest)]
        plan = ob._reflect_plan(rows, _config(reflect_after))
        assert plan is not None and plan["dropped"] == 0 and plan["input_ids"] == [r.id for r in rows], (newest, plan)


# --------------------------------------------------------------------------- validation at start


@pytest.mark.parametrize("value", ["999", "0", "-1", "abc", "1.5", "32000tokens", "1e4", "0x7d0"])
def test_an_invalid_value_fails_at_start(worker_settings, monkeypatch, value):
    monkeypatch.setenv(ENV, value)
    with pytest.raises(ValueError, match=ENV):
        validate_startup(worker_settings)
    with pytest.raises(ValueError, match=ENV):
        ob.max_active_observation_tokens()


@pytest.mark.parametrize(("value", "expected"), [("1000", 1000), (" 5000 ", 5000), ("32000", 32000),
                                                 ("", 32000), ("   ", 32000), ("9999999", 9_999_999)])
def test_valid_values_pass_the_start_check(worker_settings, monkeypatch, value, expected):
    monkeypatch.setenv(ENV, value)
    validate_startup(worker_settings)
    assert ob.max_active_observation_tokens() == expected


def test_an_unset_variable_means_the_default(worker_settings, monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    validate_startup(worker_settings)
    assert ob.max_active_observation_tokens() == ob.DEFAULT_MAX_ACTIVE_OBSERVATION_TOKENS == 32_000


def test_a_worker_process_with_an_invalid_value_does_not_start(crash_schema, tmp_path):  # noqa: F811
    proc = WorkerProcess(_env(crash_schema, f"cap-{uuid.uuid4().hex[:8]}", **{ENV: "12"}), tmp_path / "w.log")
    try:
        assert proc.wait_exit(timeout=60) != 0
        log = (tmp_path / "w.log").read_text()
        assert ENV in log and "EAP_WORKER_READY" not in log, log[-2000:]
    finally:
        proc.stop()


# --------------------------------------------------------------------------- many turns, failing reflector


def _scripted_observer(monkeypatch, prompts: list[tuple[str, str]]):
    """The observer's requests: observe -> BULK notes of ~300 characters (so each observation is
    ~450 tokens); reflect -> no answer at all (a failed reflection). `prompts` records (stage, the
    prompt as the model request step renders it)."""
    async def ask(ref: str, model_id: str):
        prompt = ob.render_prompt_ref(ref) if ref.startswith(ob.PROMPT_REF_PREFIX) else ref
        stage = "reflect" if prompt.startswith("TASK: reflect") else "observe"
        prompts.append((stage, prompt))
        if stage == "reflect":
            return None, None
        turn = re.search(r'<turn number="(\d+)"', prompt).group(1)
        notes = [{"date": "2026-11-01", "priority": "medium", "text": f"MARK{turn}n{n} " + "y" * 290}
                 for n in range(BULK)]
        return ModelResponse(parts=[TextPart(json.dumps({"observations": notes}))], model_name="scripted"), None

    monkeypatch.setattr(ob, "ask_observer", ask)


def _expected_kept(rows, cap: int) -> list:
    kept, total = [], 0
    for row in reversed(rows):  # newest first
        if kept and total + row.approx_tokens > cap:
            break
        kept.append(row)
        total += row.approx_tokens
    return kept[::-1]


def test_a_failing_reflector_over_many_turns_gets_a_bounded_input_and_exact_trimmed_counts(
    worker, client, worker_settings, tenant, monkeypatch
):
    monkeypatch.setenv(ENV, "1000")
    prompts: list[tuple[str, str]] = []
    _scripted_observer(monkeypatch, prompts)
    agent = uuid.uuid4()
    conv = conversation(worker, tenant, agent)
    spec = obs_spec(keep=0, observe_after=1000, reflect_after=1000)
    turns = 12
    sizes: list[list[int]] = []
    runs = []
    for i in range(1, turns + 1):
        run_id, row = obs_turn(worker, client, worker_settings, tenant, conv, fact(i, 1000), spec)
        assert row["status"] == "completed", row
        runs.append(run_id)
        sizes.append([o.approx_tokens for o in observations(worker, tenant, conv["id"])])

    active = observations(worker, tenant, conv["id"])
    assert len(active) == turns and all(o.kind == "observation" for o in active)  # no reflection ever succeeded
    assert sum(o.approx_tokens for o in active) > 5 * 1000  # growth far beyond the cap
    reflect_prompts = [p for s, p in prompts if s == "reflect"]
    attempts = [t for t, tokens in enumerate(sizes, start=1) if len(tokens) >= 2 and sum(tokens) > 1000]
    assert len(reflect_prompts) == len(attempts)  # a reflection is attempted at every turn whose rows exceed 1000
    cfg = _config(1000)
    caps = {t: ob.reflect_input_cap([_row(i, n) for i, n in enumerate(sizes[t - 1], start=1)], cfg) for t in attempts}
    assert all(c == 2000 for c in caps.values())  # 2 x reflect_after_tokens > 1000 + the largest row (~450)
    for t, prompt in zip(attempts, reflect_prompts, strict=True):  # the reflector's input stays within the cap
        assert approx_tokens(prompt) <= caps[t] + 200, approx_tokens(prompt)
        rows = [_row(i, n) for i, n in enumerate(sizes[t - 1], start=1)]
        kept = _expected_kept(rows, caps[t])
        # The newest rows only: the marks of the kept turns and no older one.
        first = t - len(kept) + 1
        assert f"MARK{t}n0" in prompt and f"MARK{first}n0" in prompt, t
        assert all(f"MARK{old}n" not in prompt for old in range(1, first)), t

    # One trimmed event per trimmed reflection attempt (the rows exceed the cap), none before, and the
    # counts equal an independent computation from the rows active at that turn.
    trimmed_turns = 0
    for t, run_id, tokens in zip(range(1, turns + 1), runs, sizes, strict=True):
        events = event_rows(worker.engine, run_id)
        trimmed = [(k, d) for _, k, ty, d in events if ty == ob.TRIMMED_EVENT]
        if t not in attempts or sum(tokens) <= caps[t]:
            assert trimmed == [], t  # a normal reflection (under 2 x the threshold) is never trimmed
            continue
        trimmed_turns += 1
        rows = [_row(i, n) for i, n in enumerate(tokens, start=1)]
        kept = _expected_kept(rows, caps[t])
        [(key, data)] = trimmed
        assert key == f"memory:reflect:{t}:trimmed"
        assert data["observations"] == t and data["dropped"] == t - len(kept)
        assert data["kept_tokens"] == sum(r.approx_tokens for r in kept)
        assert data["dropped_tokens"] == sum(tokens) - data["kept_tokens"]
        assert data["max_tokens"] == caps[t] and data["kept_tokens"] <= caps[t]
        assert (data["covers_from_turn"], data["covers_to_turn"]) == (1, t)
        assert "MARK" not in json.dumps(data) and "yyy" not in json.dumps(data)  # counts only, no text
        assert len(by_key(events, f"memory:reflect:{t}:failed")) == 1  # the reflection itself failed
        keys = [k for _, k, _, _ in events]
        assert keys.count(f"memory:post_turn:{t}") == 1
    assert trimmed_turns >= turns - 5  # the backlog past 2 x the threshold is trimmed at every later turn
