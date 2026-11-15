"""AC.18 crash matrix: the agent capabilities milestone's crash points under kill -9, whole-system.

Every run starts over the real API (agents are created and published over HTTP, approvals decided
over HTTP, eval suites built and run over HTTP; runs read back through `GET /runs/{id}` and SSE) and
a real `python -m eap_worker` dies at one of the AC crash points (eap_worker.crash). A second worker
with the same executor id recovers the workflow. The worker-level crash tests
(apps/worker/worker_tests/test_structured_output.py, test_limits_fallback_crash.py,
test_limits_fallback_token_crash.py, test_tool_approval_crash.py, test_eval_run_crash*.py) enqueue
straight through DBOSClient and decide in the store; this file adds the API hand-off. Each scenario
asserts one DBOS workflow in SUCCESS recovered by the second worker, contiguous `seq`, no duplicate
`event_key`, the expected story, every event stored before the crash unchanged, one usage row per
request key (rows written before the crash unchanged), run totals equal to the rows, and the SSE
replay equal to the database.

| # | Kill | Proves |
|---|------|--------|
| SO1 | `output_candidate` on `model:1:final` after a bad first candidate (`output_retry`), tool and native mode | one `output:0:retry`, one `model:1:final`; `GET /runs/{id}` has the same `output_json` |
| LF1 | `model_request` inside the fallback attempt (primary `test:fail-503`) | the fallback answers; one priced usage row for the answering model; `fallback_index` 1 |
| LF2 | `after_check_budget` on `budget:1` under a cost cap reached by request 2 | the replayed decision refuses request 2 identically: failed `run_budget_exceeded`, two usage rows |
| TA1 | `tool_approval_waiting`; approved over HTTP while no worker runs | the decision is kept (200, notified); recovery runs the tool once (1 ticket); a second decision is 409 |
| TA2 | `tool_approval_waiting`; denied over HTTP with a comment after the restart | the tool never runs (0 tickets, no side effect); the model reads the comment |
| TA3 | SIGKILL while the wait sleeps in DBOS recv; approved while down | the recovered wait finds the decision event: 1 ticket |
| EV1 | `eval_child_done` mid-batch (case 0's child done, case 1's still running) | one result per case, one judge usage row per key, three `eval_case_result` events |
| EV2 | `eval_case_scored` on case 1 (scoring and judge steps checkpointed, record step not) | the same |
"""

from __future__ import annotations

import json
import signal
import uuid
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest

from conftest import REPO_ROOT, EventRow, Harness, WorkerProcess, wait_for

WORKER_ENV = {
    "EAP_MODEL_PRICES_FILE": str(REPO_ROOT / "config" / "model-prices.test.json"),
    "EAP_ALLOWED_MODELS": "test:*",
    "EAP_APPROVAL_POLL_SECONDS": "1",
    "EAP_WORKFLOW_HOUR_SECONDS": "3600",
}
TERMINAL_TYPES = {"run_completed", "run_failed"}
TICKET_ARGS = {"title": "Laptop broken", "body": "It will not boot."}
SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}, "score": {"type": "integer"}},
    "required": ["answer", "score"],
    "additionalProperties": False,
}


@pytest.fixture
def ac(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> Harness:
    """The harness, with the in-process API seeing the workers' model allowlist and price file."""
    for key, value in WORKER_ENV.items():
        monkeypatch.setenv(key, value)
    return harness


def spec(model: str, *, tools: tuple[str, ...] = (), **extra: Any) -> dict[str, Any]:
    return {"name": f"AC {model}", "instructions": "Answer the user.",
            "model": {"provider": "test", "name": model}, "tools": list(tools), **extra}


def start_worker(h: Harness, name: str, **crash: str) -> WorkerProcess:
    return h.start_worker(name, extra_env=WORKER_ENV, **crash)


def crash(worker: WorkerProcess, crash_at: str) -> None:
    assert worker.wait_exit() == 137, worker.log()[-4000:]
    assert f"EAP_CRASH_AT={crash_at} reached" in worker.log()


def recover(h: Harness, run_id: str) -> WorkerProcess:
    second = start_worker(h, "worker2-recovering")
    try:
        wait_for(lambda: h.workflows(run_id) and h.workflows(run_id)[0][0] == "SUCCESS", timeout=120,
                 what=f"workflow {run_id} SUCCESS")
    except AssertionError as exc:
        raise AssertionError(f"{exc}\n{h.worker_logs()}") from None
    assert "Recovering" in second.log(), second.log()[-4000:]
    return second


def start_run(h: Harness, agent_id: str, input_text: str) -> str:
    run_id = str(uuid.uuid4())
    r = h.post_run(agent_id, run_id, input_text)
    assert r.status_code == 201, r.text
    return run_id


def usage_rows(h: Harness, run_id: str) -> dict[str, dict[str, Any]]:
    rows = h.sql("SELECT request_key, model_id, operation, input_tokens, output_tokens, cost_usd, created_at"
                 " FROM run_model_requests WHERE run_id = :r", r=run_id)
    keys = [r.request_key for r in rows]
    assert len(keys) == len(set(keys)), keys
    return {r.request_key: dict(r._mapping) for r in rows}


def assert_recovered_once(
    h: Harness, run_id: str, *, before: list[EventRow], usage_before: dict[str, dict[str, Any]],
    expected_keys: list[str] | None = None, children_in_totals: bool = False,
) -> list[EventRow]:
    """The invariants above. `children_in_totals`: an eval run's totals also count its child runs'."""
    events = h.events(run_id)
    keys = [e.event_key for e in events]
    assert [e.seq for e in events] == list(range(len(events))), [e.seq for e in events]
    assert len(keys) == len(set(keys)), keys
    if expected_keys is not None:
        assert keys == expected_keys, keys
    assert [(e.seq, e.event_key, e.type, e.data) for e in events[: len(before)]] == [
        (e.seq, e.event_key, e.type, e.data) for e in before
    ], "an event written before the crash changed on recovery"
    assert sum(1 for e in events if e.type in TERMINAL_TYPES) == 1
    workflows = h.workflows(run_id)
    assert len(workflows) == 1 and workflows[0][0] == "SUCCESS", workflows

    usage = usage_rows(h, run_id)
    for key, row in usage_before.items():
        assert usage[key] == row, f"usage row {key} changed on recovery"
    (totals,) = h.sql("SELECT input_tokens, output_tokens, cost_usd FROM runs WHERE id = :r", r=run_id)
    rows = list(usage.values())
    if children_in_totals:
        rows += [dict(r._mapping) for r in h.sql(
            "SELECT input_tokens, output_tokens, cost_usd FROM run_model_requests"
            " WHERE run_id IN (SELECT id FROM runs WHERE parent_run_id = :r)", r=run_id)]
    sum_cost = sum((r["cost_usd"] or Decimal(0) for r in rows), Decimal(0))
    assert (totals.input_tokens, totals.output_tokens) == (
        sum(r["input_tokens"] for r in rows), sum(r["output_tokens"] for r in rows))
    assert (totals.cost_usd or Decimal(0)) == sum_cost
    body = h.client.get(f"/api/v1/runs/{run_id}", headers=h.headers)
    assert body.status_code == 200, body.text
    assert Decimal(body.json()["cost_usd"] or 0) == sum_cost

    terminal_seq = next(e.seq for e in events if e.type in TERMINAL_TYPES)
    db = [(e.seq, e.type, e.data) for e in events]
    for point in sorted({-1, 0, len(before) - 1, terminal_seq // 2, terminal_seq - 1, terminal_seq}):
        frames = h.sse(run_id, after_seq=point)
        got = [(f.data["seq"], f.data["type"], f.data["data"]) for f in frames if f.event == "run_event"]
        assert got == [row for row in db if point < row[0] <= max(terminal_seq, point)], f"replay after_seq={point}"
    return events


def run_json(h: Harness, run_id: str) -> dict[str, Any]:
    r = h.client.get(f"/api/v1/runs/{run_id}", headers=h.headers)
    assert r.status_code == 200, r.text
    return r.json()


# --------------------------------------------------------------------------- SO1: structured output (AC.4)


@pytest.mark.parametrize("mode", ["tool", "native"])
def test_so1_crash_before_the_second_candidate_event(ac: Harness, report: Callable[[str], None], mode: str) -> None:
    h = ac
    first = start_worker(h, "worker1-crashing", crash_at="output_candidate", event_key="model:1:final")
    agent_id = h.create_published_agent(spec("structured", output={"json_schema": SCHEMA, "mode": mode}))
    run_id = start_run(h, agent_id, json.dumps({"bad_first": True}))
    crash(first, "output_candidate")
    before = h.events(run_id)
    assert [e.event_key for e in before] == ["start", "model:0:final", "output:0:retry"]
    usage_before = usage_rows(h, run_id)

    recover(h, run_id)
    events = assert_recovered_once(
        h, run_id, before=before, usage_before=usage_before,
        expected_keys=["start", "model:0:final", "output:0:retry", "model:1:final", "end"],
    )
    assert [e.type for e in events].count("output_retry") == 1
    assert sorted(usage_rows(h, run_id)) == ["model:0", "model:1"]
    body = run_json(h, run_id)
    assert body["status"] == "completed" and body["output_json"] == {"answer": "x", "score": 1}, body
    assert json.loads(body["output"]) == body["output_json"]
    report(f"AC.18 SO1 {mode}: one retry, one model:1:final, output_json {body['output_json']}")


# --------------------------------------------------------------------------- LF1 / LF2: fallback and limits (AC.5)


def test_lf1_crash_inside_the_fallback_attempt(ac: Harness, report: Callable[[str], None]) -> None:
    h = ac
    first = start_worker(h, "worker1-crashing", crash_at="model_request")
    agent_id = h.create_published_agent(spec("fail-503", fallback_models=[{"provider": "test", "name": "priced"}]))
    run_id = start_run(h, agent_id, "hello")
    crash(first, "model_request")
    before = h.events(run_id)
    usage_before = usage_rows(h, run_id)

    recover(h, run_id)
    events = assert_recovered_once(h, run_id, before=before, usage_before=usage_before,
                                   expected_keys=["start", "model:0:final", "end"])
    message = next(e.data for e in events if e.event_key == "model:0:final")
    assert (message["model_id"], message["fallback_index"]) == ("test:priced", 1), message
    usage = usage_rows(h, run_id)
    assert [(k, r["model_id"], r["cost_usd"]) for k, r in usage.items()] == [("model:0", "test:priced", Decimal("0.02"))]
    assert h.run_row(run_id)["output"] == "Echo: hello"
    report("AC.18 LF1: the fallback answered once after the kill; one priced row for test:priced")


def test_lf2_crash_after_check_budget_refuses_the_same_way(ac: Harness, report: Callable[[str], None]) -> None:
    h = ac
    first = start_worker(h, "worker1-crashing", crash_at="after_check_budget", event_key="budget:1")
    agent_id = h.create_published_agent(spec("priced", tools=("clock.now",), limits={"max_cost_usd": "0.04"}))
    run_id = start_run(h, agent_id, json.dumps({"rounds": 2}))
    crash(first, "after_check_budget")
    before = h.events(run_id)
    usage_before = usage_rows(h, run_id)

    recover(h, run_id)
    events = assert_recovered_once(h, run_id, before=before, usage_before=usage_before)
    row = h.run_row(run_id)
    assert row["status"] == "failed" and row["error"] == "RunBudgetExceeded: the run reached its cost limit", row
    end = next(e.data for e in events if e.event_key == "end")
    assert (end["error_code"], Decimal(end["spent_usd"])) == ("run_budget_exceeded", Decimal("0.04"))
    assert sorted(usage_rows(h, run_id)) == ["model:0", "model:1"]
    report("AC.18 LF2: the replayed budget decision refused request 2; two usage rows")


# --------------------------------------------------------------------------- TA1-TA3: tool approval (AC.6)

GATED = spec("approval", tools=("tickets.create",), approvals={"tools": ["tickets.create"]})
TICKET_PROMPT = json.dumps({"tool": "tickets.create", "args": TICKET_ARGS})


def waiting_call(h: Harness, run_id: str) -> str:
    def found() -> str | None:
        return next((e.data["tool_call_id"] for e in h.events(run_id) if e.type == "tool_approval_waiting"), None)

    return wait_for(found, timeout=60, what="the tool_approval_waiting event")


def decide_tool(h: Harness, run_id: str, call_id: str, verb: str, comment: str | None = None) -> Any:
    return h.client.post(f"/api/v1/runs/{run_id}/tool-approvals/{call_id}/{verb}",
                         json={"comment": comment} if comment is not None else {}, headers=h.headers)


def inbox(h: Harness, status: str) -> list[dict[str, Any]]:
    r = h.client.get(f"/api/v1/approvals?status={status}&kind=tool", headers=h.headers)
    assert r.status_code == 200, r.text
    return r.json()["items"]


def assert_tool_story(events: list[EventRow], call_id: str, decision: str) -> None:
    keys = [e.event_key for e in events]
    for suffix in ("call", "approval:waiting", "approval:decision", "result"):
        assert keys.count(f"tool:{call_id}:{suffix}") == 1, (suffix, keys)
    assert keys.index(f"tool:{call_id}:approval:waiting") < keys.index(f"tool:{call_id}:approval:decision") < keys.index(
        f"tool:{call_id}:result")
    assert next(e.type for e in events if e.event_key == f"tool:{call_id}:approval:decision") == decision


@pytest.mark.parametrize("how", ["crash-point-approve-while-down", "sigkill-in-recv-approve-while-down"])
def test_ta1_ta3_kill_while_waiting_then_approve_over_http(ac: Harness, report: Callable[[str], None], how: str) -> None:
    h = ac
    sigkill = how.startswith("sigkill")
    first = start_worker(h, "worker1", **({} if sigkill else {"crash_at": "tool_approval_waiting"}))
    agent_id = h.create_published_agent(GATED)
    run_id = start_run(h, agent_id, TICKET_PROMPT)
    call_id = waiting_call(h, run_id)
    if sigkill:
        wait_for(lambda: any(name == "DBOS.sleep" for _f, name in h.dbos_steps(run_id)), timeout=60, what="recv")
        first.proc.send_signal(signal.SIGKILL)
        first.proc.wait(timeout=10)
    else:
        crash(first, "tool_approval_waiting")
    assert run_json(h, run_id)["display_status"] == "waiting_approval"
    assert [i["tool_call_id"] for i in inbox(h, "pending")] == [call_id]
    before = h.events(run_id)
    usage_before = usage_rows(h, run_id)

    approved = decide_tool(h, run_id, call_id, "approve", comment="go")  # no worker is running
    assert approved.status_code == 200, approved.text
    body = approved.json()
    assert (body["decision"], body["decided_by"], body["comment"], body["notified"]) == ("approved", "alice", "go", True)
    assert h.ticket_calls(run_id) == 0  # nothing runs it while the worker is down

    recover(h, run_id)
    events = assert_recovered_once(h, run_id, before=before, usage_before=usage_before)
    assert_tool_story(events, call_id, "tool_approved")
    row = h.run_row(run_id)
    assert row["status"] == "completed" and "outcome=success" in row["output"], row
    assert (len(h.tickets(run_id)), h.ticket_calls(run_id)) == (1, 1)
    assert h.side_effect_states() == ["completed"]
    assert inbox(h, "pending") == []
    assert [(i["tool_call_id"], i["decision"]) for i in inbox(h, "decided")] == [(call_id, "approved")]
    for verb in ("approve", "deny"):
        again = decide_tool(h, run_id, call_id, verb)
        assert again.status_code == 409, again.text
    report(f"AC.18 TA {how}: decided over HTTP while down; recovered; one ticket; repeat 409")


def test_ta2_deny_with_a_comment_after_the_restart(ac: Harness, report: Callable[[str], None]) -> None:
    h = ac
    first = start_worker(h, "worker1-crashing", crash_at="tool_approval_waiting")
    agent_id = h.create_published_agent(GATED)
    run_id = start_run(h, agent_id, TICKET_PROMPT)
    call_id = waiting_call(h, run_id)
    crash(first, "tool_approval_waiting")
    before = h.events(run_id)
    second = start_worker(h, "worker2-recovering")
    wait_for(lambda: "Recovering" in second.log(), timeout=30, what="recovery")
    wait_for(lambda: any(name == "DBOS.sleep" for _f, name in h.dbos_steps(run_id)), timeout=60, what="the wait again")
    assert h.run_row(run_id)["status"] == "running"

    denied = decide_tool(h, run_id, call_id, "deny", comment="use the existing ticket")
    assert denied.status_code == 200, denied.text
    wait_for(lambda: h.workflows(run_id) and h.workflows(run_id)[0][0] == "SUCCESS", timeout=60, what="SUCCESS")
    events = assert_recovered_once(h, run_id, before=before, usage_before={})
    assert_tool_story(events, call_id, "tool_denied")
    row = h.run_row(run_id)
    assert row["status"] == "completed" and "outcome=denied" in row["output"], row
    assert "use the existing ticket" in row["output"]
    result = next(e.data for e in events if e.event_key == f"tool:{call_id}:result")
    assert result["status"] == "denied"
    assert (len(h.tickets(run_id)), h.ticket_calls(run_id), h.side_effect_states()) == (0, 0, [])
    report("AC.18 TA2: denied after the restart; the model read the comment; no ticket")


# --------------------------------------------------------------------------- EV1 / EV2: eval runs (AC.11)

SLOW_ROUNDS = 30  # case c1 makes 30 tool-calling requests, so it is still running when c0 is done
JUDGE = {"provider": "test", "name": "judge"}


def eval_suite(h: Harness, agent_id: str) -> str:
    r = h.client.post(f"/api/v1/agents/{agent_id}/eval-suites", headers=h.headers, json={
        "name": "Triage",
        "evaluators": [{"kind": "contains", "value": "Echo:"},
                       {"kind": "llm_judge", "rubric": "MUST_CONTAIN:word", "model": JUDGE}],
    })
    assert r.status_code == 201, r.text
    suite_id = r.json()["id"]
    for name, text in (("c0", "word0"), ("c1", json.dumps({"rounds": SLOW_ROUNDS, "text": "word1"})), ("c2", "word2")):
        c = h.client.post(f"/api/v1/eval-suites/{suite_id}/cases", json={"name": name, "input": text}, headers=h.headers)
        assert c.status_code == 201, c.text
    return suite_id


@pytest.mark.parametrize(("crash_at", "event_key"), [("eval_child_done", "position:0"), ("eval_case_scored", "position:1")])
def test_ev_kill_during_an_eval_batch(ac: Harness, report: Callable[[str], None], crash_at: str, event_key: str) -> None:
    h = ac
    first = start_worker(h, "worker1-crashing", crash_at=crash_at, event_key=event_key)
    agent_id = h.create_published_agent(spec("priced", tools=("clock.now",)))
    suite_id = eval_suite(h, agent_id)
    run_id = str(uuid.uuid4())
    h.run_ids.append(run_id)
    r = h.client.post(f"/api/v1/agents/{agent_id}/evals/runs", headers=h.headers,
                      json={"run_id": run_id, "suite_id": suite_id, "max_concurrency": 2})
    assert r.status_code == 201, r.text
    crash(first, crash_at)
    assert h.run_row(run_id)["status"] == "running"
    recorded = h.sql("SELECT count(*) FROM eval_case_results WHERE run_id = :r", r=run_id)[0][0]
    assert recorded <= 1
    if crash_at == "eval_child_done":
        statuses = {name: status for _id, name, status, _v in h.child_runs(run_id)}
        assert list(statuses.values()).count("completed") == 1 and recorded == 0, statuses
        assert "running" in statuses.values(), statuses  # killed mid-batch
    before = h.events(run_id)
    usage_before = usage_rows(h, run_id)

    recover(h, run_id)
    events = assert_recovered_once(h, run_id, before=before, usage_before=usage_before, children_in_totals=True)
    assert h.run_row(run_id)["status"] == "completed"
    assert [e.type for e in events].count("eval_case_result") == 3
    page = h.client.get(f"/api/v1/agents/{agent_id}/evals/runs/{run_id}/results", headers=h.headers)
    assert page.status_code == 200, page.text
    items = page.json()["items"]
    assert [(i["case_name"], i["status"]) for i in items] == [("c0", "passed"), ("c1", "passed"), ("c2", "passed")]
    assert sorted(usage_rows(h, run_id)) == sorted(f"judge:{i['case_id']}:1" for i in items)
    assert h.sql("SELECT count(*) FROM eval_case_results WHERE run_id = :r", r=run_id)[0][0] == 3
    report(f"AC.18 EV {crash_at}: one result per case, one judge usage row per key, {len(events)} events")
