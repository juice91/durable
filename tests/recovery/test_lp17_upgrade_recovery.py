"""LP.17: upgrade recovery with the ACTUAL pre-milestone binary.

A `git archive` of the pre-milestone ref is built in a temp directory with its own virtualenv
(`uv sync --all-packages --frozen`, about 2 s from the uv cache); the pre-milestone API and
worker are then launched from that build. A loop-free workflow (parallel step + approval wait +
agent join) is published and run on the old build. At `step:approve:waiting`, the old worker is
stopped; this build's worker takes over on the same DBOS schema and database (both builds are at
alembic head 0017_api_keys, so `upgrade_to_this_build()` is a no-op migration-wise but still
switches the running build). The approval is decided through this build's API, and the run
completes.

Assertions:
- the recovered run's events start with exactly what the previous build wrote, byte-equivalent;
- its DBOS step list is the same shape as a reference run of the old build (ignoring approval
  wait rounds, as in test_ac_compat.C6);
- no `loop:`, `:tick`, `loop_iteration`, `evaluate` or `judge` key appears in the recovered run;
- the run's final status is `completed`, and SSE replay matches the event log.

Reuses the `BuildProcess`, `World` and helpers from `tests/recovery/test_ac_compat.py`, with one
LP.17 subclass that pins PREVIOUS_COMMIT to `git merge-base HEAD main` (the pre-milestone ref).
The launcher fix that matches the whole READY line (`EAP_API_READY port=\\d+`, test_ac_compat's
flake fix) is inherited.
"""

from __future__ import annotations

import subprocess
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from conftest import REPO_ROOT, wait_for
from test_ac_compat import (  # noqa: F401 - helper re-use
    BuildProcess,
    World,
    assert_gap_free,
    read_sse,
    spec,
    story,
    without_wait_rounds,
)
import test_ac_compat as ac

LP17_PREVIOUS_COMMIT = subprocess.run(
    ["git", "merge-base", "HEAD", "main"], cwd=REPO_ROOT, check=True, capture_output=True, text=True
).stdout.strip()
LP17_PREVIOUS_HEAD = "0017_api_keys"  # both builds are at this revision
FORBIDDEN_LOOP_KEY_FRAGMENTS = ("loop:", ":tick", "loop_iteration", "evaluate", "judge")


@pytest.fixture(scope="session")
def lp17_previous_build(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """`git archive` of the pre-milestone commit in a temp dir with its own uv venv (~2 s from cache)."""
    import os

    root = tmp_path_factory.mktemp(f"eap-build-lp17-{LP17_PREVIOUS_COMMIT[:12]}")
    archive = subprocess.run(["git", "archive", LP17_PREVIOUS_COMMIT], cwd=REPO_ROOT, check=True, capture_output=True)
    subprocess.run(["tar", "-x", "-C", str(root)], input=archive.stdout, check=True)
    env = {k: v for k, v in os.environ.items() if k not in {"VIRTUAL_ENV", "PYTHONPATH"}}
    env["UV_PROJECT_ENVIRONMENT"] = str(root / ".venv")
    sync = subprocess.run(["uv", "sync", "--all-packages", "--frozen", "-q"], cwd=root, env=env,
                          capture_output=True, text=True, timeout=600)
    assert sync.returncode == 0, sync.stderr[-4000:]
    probe = subprocess.run([str(root / ".venv/bin/python"), "-I", "-c",
                            "import eap_worker; print(eap_worker.__file__)"],
                           capture_output=True, text=True, check=True)
    assert probe.stdout.strip().startswith(str(root)), probe.stdout
    return root


class LoopsWorld(World):
    """World pinned to the LP.17 pre-milestone build (both builds at alembic 0017_api_keys)."""

    def __init__(self, previous: Path, tmp_path: Path) -> None:
        # The parent constructor asserts `self.revision() == PREVIOUS_HEAD`. Both builds are at
        # 0017_api_keys, so swap the module-level constant for the duration of __init__.
        original_commit, original_head = ac.PREVIOUS_COMMIT, ac.PREVIOUS_HEAD
        ac.PREVIOUS_COMMIT = LP17_PREVIOUS_COMMIT
        ac.PREVIOUS_HEAD = LP17_PREVIOUS_HEAD
        try:
            super().__init__(previous, tmp_path)
        finally:
            ac.PREVIOUS_COMMIT, ac.PREVIOUS_HEAD = original_commit, original_head


@pytest.fixture
def lp17_world(lp17_previous_build: Path, tmp_path: Path) -> Iterator[LoopsWorld]:
    w = LoopsWorld(lp17_previous_build, tmp_path)
    try:
        yield w
    finally:
        w.close()


def _fan_approval_spec(agent_id: str) -> dict:
    """Loop-free: parallel fan into two agent branches that join into an approval, then an agent."""
    return {
        "name": "LP17 pre-milestone shape",
        "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
        "start": "fan",
        "steps": [
            {"id": "fan", "type": "parallel", "branches": ["a", "b"], "join": "approve"},
            {"id": "a", "type": "agent", "agent_id": agent_id, "input": {"prompt": "left"}, "next": "approve"},
            {"id": "b", "type": "agent", "agent_id": agent_id,
             "input": {"prompt": {"$ref": "input.q"}}, "next": "approve"},
            {"id": "approve", "type": "approval", "prompt": "Ship it?", "timeout_hours": 1, "next": "done"},
            {"id": "done", "type": "agent", "agent_id": agent_id,
             "input": {"left": {"$ref": "steps.a.output"}, "right": {"$ref": "steps.b.output"}}},
        ],
    }


def _start_workflow_run(w: World, workflow_id: str, input_value: dict) -> str:
    run_id = str(uuid.uuid4())
    w.post(f"/workflows/{workflow_id}/runs", {"run_id": run_id, "input": input_value}, 202)
    return run_id


def test_upgrade_recovery_from_pre_milestone_build_with_a_parallel_step_and_an_approval(
    lp17_world: LoopsWorld, report: Callable[[str], None]
) -> None:
    """The actual pre-milestone binary starts a run of a parallel+approval workflow and pauses at
    the approval. HEAD's build recovers it on the same DBOS schema; the approval decided through
    HEAD's API completes the run with the same step list as a clean previous-build reference run.
    Nothing loops-milestone-specific leaks into the recovered event log."""
    w = lp17_world
    report(f"pre-milestone ref: {LP17_PREVIOUS_COMMIT} (merge-base HEAD main); head {LP17_PREVIOUS_HEAD}")

    # ---- reference run on the previous build (no kill): records the step list to compare against.
    w.start_api()
    reference_worker = w.start_worker("reference")
    agent_id = w.published_agent(spec("echo"))
    workflow_id = w.post("/workflows", {"spec": _fan_approval_spec(agent_id)}, 201)["id"]
    w.post(f"/workflows/{workflow_id}/versions", expect=201)
    reference = _start_workflow_run(w, workflow_id, {"q": "printer on fire"})
    wait_for(lambda: any(e[1] == "step:approve:waiting" for e in w.events(reference)),
             timeout=60, what="reference waiting")
    w.post(f"/workflows/runs/{reference}/steps/approve/approve", {"comment": "go"}, 200)
    w.wait_workflow(reference)
    reference_events = w.events(reference)
    reference_steps = w.steps(reference)
    reference_row = w.run_row(reference)
    reference_worker.stop()

    # ---- the pre-milestone run is paused at the approval, then the worker is stopped.
    paused_worker = w.start_worker("paused")
    run_id = _start_workflow_run(w, workflow_id, {"q": "printer on fire"})
    wait_for(lambda: any(e[1] == "step:approve:waiting" for e in w.events(run_id)),
             timeout=60, what="paused waiting")
    pre_recovery = w.events(run_id)
    assert "end" not in [e[1] for e in pre_recovery]
    assert pre_recovery[-1][1] == "step:approve:waiting" or any(
        e[1] == "step:approve:waiting" for e in pre_recovery
    ), [e[1] for e in pre_recovery]
    # Previous build's worker fully stopped (no crash: a regular stop still leaves the run running).
    paused_worker.stop()
    assert w.run_row(run_id)["status"] == "running"

    # ---- upgrade to HEAD and recover.
    w.upgrade_to_this_build()
    w.start_api()
    recovering = w.start_worker("recovering")

    # Decide through HEAD's route.
    decided = w.post(f"/workflows/runs/{run_id}/steps/approve/approve", {"comment": "go"}, 200)
    assert (decided["decision"], decided["decided_by"]) == ("approved", "alice")
    w.wait_workflow(run_id)
    assert "Recovering" in recovering.log(), recovering.log()[-3000:]

    # ---- invariants on the recovered run.
    events = w.events(run_id)
    assert_gap_free(events)
    # Every event the previous build committed stays byte-equivalent.
    assert events[: len(pre_recovery)] == pre_recovery, (
        "an event the previous build stored changed on recovery")
    # The recovered run has the same set of (event_key, type) pairs as the reference clean run.
    # (Parallel branches commit in nondeterministic order across runs, so compare as a set.)
    assert set(story(events)) == set(story(reference_events)), (
        set(story(events)) ^ set(story(reference_events)))
    # DBOS step list equal to the reference run's as a multiset (ignoring approval wait rounds and
    # parallel branch ordering).
    assert sorted(without_wait_rounds(w.steps(run_id))) == sorted(without_wait_rounds(reference_steps)), (
        w.steps(run_id), reference_steps,
    )
    row = w.run_row(run_id)
    assert row["status"] == "completed", (row, w.logs())
    assert row["output"] == reference_row["output"]

    # Extra: no loops-milestone key or step name in the recovered event log.
    recovered_keys = [e[1] for e in events]
    for key in recovered_keys:
        for fragment in FORBIDDEN_LOOP_KEY_FRAGMENTS:
            assert fragment not in key, f"loops-milestone key leaked into pre-milestone run: {key}"

    # Extra: the HEAD API serves the recovered run and its SSE replay matches the event log.
    body = w.get(f"/runs/{run_id}")
    assert body["status"] == "completed"
    frames = read_sse(w.client, f"/api/v1/runs/{run_id}/stream?after_seq=-1", w.headers)
    replay = [(f.data["seq"], f.data["type"]) for f in frames if f.event == "run_event"]
    terminal_seq = next(seq for seq, _k, typ, _d in events if typ == "run_completed")
    assert replay == [(seq, typ) for seq, _k, typ, _d in events if seq <= terminal_seq]

    report(
        f"upgrade recovery ok: ref={LP17_PREVIOUS_COMMIT[:12]} head={LP17_PREVIOUS_HEAD}, "
        f"recovered keys={len(recovered_keys)}, steps={len(w.steps(run_id))} "
        f"(reference {len(reference_steps)})"
    )
