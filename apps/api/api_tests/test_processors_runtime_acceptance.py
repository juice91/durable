"""P0.17 acceptance through the real API and a real `python -m eap_worker` subprocess: an email in the
prompt reaches `test:echo` as a redaction marker; FLAG_ME blocks the turn with a clear message; the
events, /processor-results, trace and metrics endpoints show the outcome and never the matched text; a
blocked turn is left out of the next turn's history; no delta frame for an agent with output processors."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from eap_api.app import create_app
from eap_api.sse import StreamSettings, get_stream_settings

from .conftest import get_auth_header
from .run_helpers import PG_URL, cleanup_tenant, read_all_frames, requires_pg
from .test_run_e2e import REPO_ROOT, _drop_schema, DBOS_URL

pytestmark = requires_pg

EMAIL = "ada.lovelace@example.com"
PII = {"type": "pii_redact", "config": {"kinds": ["email"], "action": "redact"}}
BLOCK_SECRET = {"type": "regex_blocklist", "config": {"patterns": ["ZQSECRET"], "action": "block"}}
MODERATION = {"type": "moderation", "config": {"model": {"provider": "test", "name": "moderation"}}}


class Stack:
    def __init__(self, client, headers, engine, tenant):
        self.client, self.h, self.engine, self.tenant = client, headers, engine, tenant

    def agent(self, model="echo", input_processors=(), output_processors=()) -> str:
        spec = {"name": "Guarded", "instructions": "Answer.", "model": {"provider": "test", "name": model},
                "tools": [], "input_processors": list(input_processors),
                "output_processors": list(output_processors)}
        r = self.client.post("/api/v1/agents", json={"spec": spec}, headers=self.h)
        assert r.status_code == 201, r.text
        agent_id = r.json()["id"]
        assert self.client.post(f"/api/v1/agents/{agent_id}/versions", headers=self.h).status_code == 201
        return agent_id

    def wait(self, run_id: str) -> dict:
        deadline = time.monotonic() + 90
        while True:
            run = self.client.get(f"/api/v1/runs/{run_id}", headers=self.h).json()
            if run["status"] in ("completed", "failed", "needs_attention"):
                return run
            assert time.monotonic() < deadline, run
            time.sleep(0.2)

    def run(self, agent_id: str, text: str) -> tuple[str, dict]:
        run_id = str(uuid.uuid4())
        body = {"run_id": run_id, "input": text, "version": None}
        assert self.client.post(f"/api/v1/agents/{agent_id}/runs", json=body, headers=self.h).status_code == 201
        return run_id, self.wait(run_id)

    def events(self, run_id: str) -> list[dict]:
        r = self.client.get(f"/api/v1/runs/{run_id}/events", headers=self.h)
        assert r.status_code == 200, r.text
        return r.json()

    def results(self, **params) -> list[dict]:
        r = self.client.get("/api/v1/processor-results", params=params, headers=self.h)
        assert r.status_code == 200, r.text
        return r.json()["items"]


@pytest.fixture(scope="module")
def stack() -> Iterator[Stack]:
    suffix = uuid.uuid4().hex[:10]
    env = {"EAP_DATABASE_URL": PG_URL or "", "EAP_DBOS_URL": DBOS_URL,
           "EAP_DBOS_SYSTEM_SCHEMA": f"eap_test_api_{suffix}", "EAP_APP_VERSION": f"p017-{suffix}",
           "EAP_EXECUTOR_ID": f"p017-{suffix}", "EAP_QUEUE_POLL_SECONDS": "0.2"}
    log = tempfile.NamedTemporaryFile("w+", suffix=".log", delete=False)  # noqa: SIM115
    proc = subprocess.Popen([sys.executable, "-m", "eap_worker"], cwd=REPO_ROOT, env={**os.environ, **env},
                            stdout=log, stderr=subprocess.STDOUT, text=True)
    patch = pytest.MonkeyPatch()
    engine, tenant = None, f"t{uuid.uuid4().hex[:10]}"
    try:
        deadline = time.monotonic() + 60
        while "EAP_WORKER_READY" not in open(log.name).read():
            assert proc.poll() is None and time.monotonic() < deadline, open(log.name).read()[-3000:]
            time.sleep(0.3)
        for k, v in env.items():
            patch.setenv(k, v)
        app = create_app(database_url=PG_URL)
        app.dependency_overrides[get_stream_settings] = lambda: StreamSettings(poll_interval=0.1, heartbeat_interval=10.0)
        engine = app.state.database_engine
        with TestClient(app) as client:
            yield Stack(client, get_auth_header(tenant, "alice"), engine, tenant)
    finally:
        patch.undo()
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        if engine is not None:
            cleanup_tenant(engine, tenant)
            engine.dispose()
        _drop_schema(env["EAP_DBOS_SYSTEM_SCHEMA"])
        log.close()
        os.unlink(log.name)


@pytest.fixture(scope="module")
def guarded(stack):
    """One agent with input PII + regex block + moderation, and three runs: ok, FLAG_ME, ZQSECRET."""
    agent_id = stack.agent(input_processors=[PII, BLOCK_SECRET, MODERATION])
    runs = {}
    for name, text in (("ok", f"Please mail {EMAIL} today"), ("flag", f"FLAG_ME {EMAIL}"), ("secret", "the ZQSECRET plan")):
        run_id, run = stack.run(agent_id, text)
        runs[name] = (run_id, run)
    return agent_id, runs


def test_email_reaches_the_model_as_a_redaction_marker(guarded):
    _, runs = guarded
    run_id, run = runs["ok"]
    assert run["status"] == "completed", run
    assert run["output"] == "Echo: Please mail [REDACTED:email] today"
    assert EMAIL not in json.dumps({k: v for k, v in run.items() if k != "input"})  # `input` is the submitted text


def test_flag_me_blocks_the_turn_with_a_clear_message(guarded):
    _, runs = guarded
    run_id, run = runs["flag"]
    assert run["status"] == "failed" and run["output"] is None
    assert run["error"] == "InputBlocked: the message was blocked by this agent's input processors"
    shown = json.dumps({k: v for k, v in run.items() if k != "input"})  # `input` is the submitted text
    assert "FLAG_ME" not in shown and EMAIL not in shown


def test_events_endpoint_shows_the_outcome_and_no_matched_text(stack, guarded):
    _, runs = guarded
    ok = stack.events(runs["ok"][0])
    assert [e["type"] for e in ok] == ["run_started", "processor_result", "processor_result", "processor_result",
                                       "model_message", "run_completed"]
    assert [e["data"]["index"] for e in ok[1:4]] == [0, 1, 2]
    assert ok[0]["data"]["input"] == "Please mail [REDACTED:email] today" and ok[0]["data"]["input_processed"] is True
    assert flag[0]["data"]["input"].startswith("[withheld") if (flag := stack.events(runs["flag"][0])) else False
    assert ok[1]["data"]["match_counts"] == {"email": 1} and ok[3]["data"]["action"] == "allow"
    flag = stack.events(runs["flag"][0])
    assert flag[-1]["type"] == "run_failed" and flag[-1]["data"]["error_code"] == "input_blocked"
    assert flag[-2]["data"]["action"] == "block" and flag[-2]["data"]["categories"] == ["illicit"]
    for events in (ok, flag, stack.events(runs["secret"][0])):
        blob = json.dumps(events)
        assert EMAIL not in blob and "FLAG_ME" not in blob and "ZQSECRET" not in blob


def test_processor_results_endpoint_shows_outcomes_without_text(stack, guarded):
    agent_id, runs = guarded
    items = stack.results(agent_id=agent_id)
    assert len(items) == 8  # ok 3 outcomes; flag 3; secret 2 (pii, regex)
    by_run = {rid: sorted((i["index"], i["action"]) for i in items if i["run_id"] == rid) for rid, _ in runs.values()}
    assert by_run[runs["ok"][0]] == [(0, "redact"), (1, "allow"), (2, "allow")]
    assert by_run[runs["flag"][0]] == [(0, "redact"), (1, "allow"), (2, "block")]
    assert by_run[runs["secret"][0]] == [(0, "allow"), (1, "block")]
    blocked = stack.results(agent_id=agent_id, action="block")
    assert {i["run_id"] for i in blocked} == {runs["flag"][0], runs["secret"][0]}
    blob = json.dumps(items)
    assert EMAIL not in blob and "FLAG_ME" not in blob and "ZQSECRET" not in blob
    for item in items:
        assert set(item) <= {"run_id", "seq", "ts", "agent_id", "agent_version", "parent_run_id", "phase", "index",
                             "processor_type", "action", "match_counts", "reason", "categories"}
    assert [i["categories"] for i in blocked if i["processor_type"] == "moderation"] == [["illicit"]]


def test_metrics_blocked_counts_the_blocked_runs(stack, guarded):
    agent_id, _ = guarded
    r = stack.client.get("/api/v1/metrics/summary", params={"agent_id": agent_id}, headers=stack.h)
    assert r.status_code == 200, r.text
    body = json.dumps(r.json())
    summary = r.json().get("current", r.json())
    assert summary["runs"] == 3 and summary["completed"] == 1 and summary["failed"] == 2, body
    assert summary["blocked"] == 2, body


def test_trace_endpoint_shows_processor_spans_without_matched_text(stack, guarded):
    _, runs = guarded
    r = stack.client.get(f"/api/v1/runs/{runs['flag'][0]}/trace", headers=stack.h)
    assert r.status_code == 200, r.text
    spans = [s for s in r.json()["spans"] if s["kind"] == "processor"]
    assert [s["attributes"]["processor_type"] for s in spans] == ["pii_redact", "regex_blocklist", "moderation"]
    assert [s["status"] for s in spans] == ["ok", "ok", "error"]
    assert spans[2]["attributes"]["action"] == "block"
    for needle in (EMAIL, "FLAG_ME"):
        assert needle not in json.dumps([s for s in r.json()["spans"] if s["kind"] != "run"])
    # Names: the processor type should name the span (it currently falls back to the event key).
    assert [s["name"] for s in spans] == ["pii_redact", "regex_blocklist", "moderation"]


def _turn(stack, conversation_id: str, text: str) -> dict:
    run_id = str(uuid.uuid4())
    body = {"run_id": run_id, "input": text}
    r = stack.client.post(f"/api/v1/conversations/{conversation_id}/runs", json=body, headers=stack.h)
    assert r.status_code == 201, r.text
    return stack.wait(run_id)


def test_blocked_turn_is_excluded_from_the_next_turns_history(stack):
    agent_id = stack.agent(model="history", input_processors=[BLOCK_SECRET, MODERATION],
                           output_processors=[{"type": "regex_blocklist", "config": {"patterns": ["ZQBOOM"]}}])
    r = stack.client.post(f"/api/v1/agents/{agent_id}/conversations", json={}, headers=stack.h)
    assert r.status_code == 201, r.text
    conv = r.json()["id"]
    turns = [_turn(stack, conv, t) for t in ("hello", "FLAG_ME one", "ZQSECRET two", "ZQBOOM three", "again")]
    assert [t["status"] for t in turns] == ["completed", "failed", "failed", "failed", "completed"]
    assert [t["error"].split(":")[0] for t in turns[1:4]] == ["InputBlocked", "InputBlocked", "OutputBlocked"]
    assert turns[4]["output"] == "Turn 2. You said: hello | again. I answered 1 times before."
    detail = stack.client.get(f"/api/v1/conversations/{conv}", headers=stack.h).json()
    assert [r["status"] for r in detail["runs"]] == [t["status"] for t in turns]


def test_output_processors_redact_the_answer_and_stream_no_deltas(stack):
    agent_id = stack.agent(output_processors=[PII, MODERATION])
    run_id, run = stack.run(agent_id, f"write to {EMAIL}")
    assert run["status"] == "completed" and run["output"] == "Echo: write to [REDACTED:email]"
    frames = read_all_frames(stack.client, f"/api/v1/runs/{run_id}/stream", stack.h, deadline_s=30)
    assert [f.event for f in frames if f.event == "delta"] == []
    types = [f.data["type"] for f in frames if f.event == "run_event"]
    assert types == ["run_started", "processor_result", "processor_result", "model_message", "run_completed"]
    model_message = next(f.data for f in frames if f.event == "run_event" and f.data["type"] == "model_message")
    assert EMAIL not in json.dumps(model_message)
    assert [i["phase"] for i in stack.results(run_id=run_id)] == ["output", "output"]


def test_output_block_through_the_api_shows_no_answer_and_counts_as_blocked(stack):
    agent_id = stack.agent(output_processors=[MODERATION])
    run_id, run = stack.run(agent_id, "FLAG_ME")
    assert run["status"] == "failed" and run["output"] is None
    assert run["error"] == "OutputBlocked: the answer was blocked by this agent's output processors"
    events = stack.events(run_id)
    assert events[-1]["data"]["error_code"] == "output_blocked"
    final = next(e for e in events if e["type"] == "model_message")
    assert final["data"]["text"] == "" and final["data"]["blocked"] is True
    frames = read_all_frames(stack.client, f"/api/v1/runs/{run_id}/stream", stack.h, deadline_s=30)
    assert [f for f in frames if f.event == "delta"] == []
    assert "Echo" not in json.dumps([f.data for f in frames])
    summary = stack.client.get("/api/v1/metrics/summary", params={"agent_id": agent_id}, headers=stack.h).json()
    assert (summary.get("current", summary))["blocked"] == 1
