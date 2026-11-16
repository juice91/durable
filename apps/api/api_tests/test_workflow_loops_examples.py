"""LP.16: the workflow loop examples (`examples/workflows/*.json`) and the guide (`docs/workflows/loops.md`).

- Every example publishes through the API (agents created and published, `${agents.<name>}`
  replaced by their ids, the workflow created and published: 201, no warnings) and runs on a real
  `python -m eap_worker` subprocess with the scripted models, priced by `config/model-prices.test.json`.
- Every JSON snippet of the guide (a fenced block under `<!-- example: <file> <json pointer> -->`)
  equals the part of the example it quotes, and the guide quotes every example.
- With `limits.max_cost_usd`, an `llm_judge` model, or an agent step's agent model, that counts as
  $0 gives the publish warning `cost_cap_unpriced_model` (the repository default price file prices
  `test:*` at $0).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from eap_api.app import create_app
from eap_schemas import AgentSpec
from eap_schemas.workflows import WorkflowSpec

from .conftest import get_auth_header
from .run_helpers import PG_URL, cleanup_tenant, requires_pg
from .test_run_e2e import DBOS_URL, REPO_ROOT, _drop_schema

EXAMPLES_DIR = REPO_ROOT / "examples" / "workflows"
GUIDE = REPO_ROOT / "docs" / "workflows" / "loops.md"
EXAMPLES = ("foreach-triage.json", "until-polling.json", "refine.json")
TEST_PRICES = REPO_ROOT / "config" / "model-prices.test.json"
DEFAULT_PRICES = REPO_ROOT / "config" / "model-prices.json"
BASE = "/api/v1/workflows"
PLACEHOLDER = re.compile(r"\$\{agents\.([a-z0-9_-]+)\}")
SNIPPET = re.compile(r"<!-- example: (?P<file>\S+) (?P<pointer>\S+) -->\n```json\n(?P<body>.*?)\n```", re.DOTALL)
TERMINAL = ("completed", "failed", "needs_attention")


def load_example(name: str) -> dict[str, Any]:
    return json.loads((EXAMPLES_DIR / name).read_text())


def with_agent_ids(spec: dict[str, Any], ids: dict[str, str]) -> dict[str, Any]:
    """The spec with every `${agents.<name>}` replaced by that agent's id."""

    def sub(match: re.Match[str]) -> str:
        return ids[match.group(1)]

    return json.loads(PLACEHOLDER.sub(sub, json.dumps(spec)))


def resolve_pointer(doc: Any, pointer: str) -> Any:
    """RFC 6901 JSON pointer."""
    assert pointer.startswith("/"), pointer
    for raw in pointer[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        doc = doc[int(token)] if isinstance(doc, list) else doc[token]
    return doc


# ------------------------------------------------------------------------------- pure checks


def test_the_examples_directory_holds_exactly_the_three_examples():
    assert sorted(p.name for p in EXAMPLES_DIR.glob("*.json")) == sorted(EXAMPLES)


@pytest.mark.parametrize("name", EXAMPLES)
def test_each_example_is_a_valid_spec_with_its_agents(name):
    example = load_example(name)
    assert set(example) == {"description", "agents", "input", "spec"}
    for agent in example["agents"].values():
        AgentSpec.model_validate(agent)
    placeholders = set(PLACEHOLDER.findall(json.dumps(example["spec"])))
    assert placeholders == set(example["agents"]), "every agent is used and every placeholder is defined"
    spec = WorkflowSpec.model_validate(with_agent_ids(example["spec"], {a: str(uuid.uuid4()) for a in placeholders}))
    assert spec.limits is None or name == "refine.json"


def test_every_guide_snippet_matches_its_example():
    guide = GUIDE.read_text()
    snippets = list(SNIPPET.finditer(guide))
    assert len(snippets) == guide.count("<!-- example:"), "every marker is followed by a ```json block"
    assert len(snippets) >= len(EXAMPLES)
    for snippet in snippets:
        quoted = json.loads(snippet["body"])
        expected = resolve_pointer(load_example(snippet["file"]), snippet["pointer"])
        assert quoted == expected, f"{snippet['file']} {snippet['pointer']} differs from the guide"
    assert {s["file"] for s in snippets} == set(EXAMPLES), "the guide quotes every example"


def test_the_guide_links_every_example():
    guide = GUIDE.read_text()
    for name in EXAMPLES:
        assert f"(../../examples/workflows/{name})" in guide


# ------------------------------------------------------------------------------- through the API


class Stack:
    def __init__(self, client: TestClient, engine: Any, tenant: str) -> None:
        self.client, self.engine, self.tenant = client, engine, tenant
        self.h = get_auth_header(tenant, "alice")

    def agent(self, spec: dict[str, Any]) -> str:
        r = self.client.post("/api/v1/agents", json={"spec": spec}, headers=self.h)
        assert r.status_code == 201, r.text
        agent_id = r.json()["id"]
        published = self.client.post(f"/api/v1/agents/{agent_id}/versions", headers=self.h)
        assert published.status_code == 201, published.text
        assert published.json()["version"] == 1
        return agent_id

    def publish(self, example: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """(workflow id, publish response) of the example, its agents created first."""
        ids = {name: self.agent(spec) for name, spec in example["agents"].items()}
        created = self.client.post(BASE, json={"spec": with_agent_ids(example["spec"], ids)}, headers=self.h)
        assert created.status_code == 201, created.text
        workflow_id = created.json()["id"]
        return workflow_id, self.client.post(f"{BASE}/{workflow_id}/versions", headers=self.h)

    def run(self, workflow_id: str, input_value: Any) -> tuple[str, dict[str, Any]]:
        r = self.client.post(f"{BASE}/{workflow_id}/runs", json={"input": input_value}, headers=self.h)
        assert r.status_code == 202, r.text
        run_id = r.json()["run_id"]
        deadline = time.monotonic() + 120
        while True:
            run = self.client.get(f"/api/v1/runs/{run_id}", headers=self.h).json()
            if run["status"] in TERMINAL:
                return run_id, run
            assert time.monotonic() < deadline, run
            time.sleep(0.25)

    def events(self, run_id: str) -> dict[str, tuple[str, dict[str, Any]]]:
        """event_key -> (type, data); the API's events list carries no key, so read the rows."""
        with self.engine.connect() as conn:
            rows = conn.execute(
                text("SELECT event_key, type, data FROM execution_events WHERE run_id = :r ORDER BY seq"),
                {"r": run_id},
            )
            return {r.event_key: (r.type, r.data) for r in rows}

    def run_example(self, name: str) -> tuple[str, dict[str, Any], dict[str, Any]]:
        example = load_example(name)
        workflow_id, published = self.publish(example)
        assert published.status_code == 201, published.text
        assert published.json()["warnings"] == []
        run_id, run = self.run(workflow_id, example["input"])
        assert run["status"] == "completed", run
        return run_id, run, json.loads(run["output"])


@pytest.fixture(scope="module")
def stack() -> Iterator[Stack]:
    """The API in-process and a real worker subprocess on a private DBOS schema, both pricing with
    the test price file (so `test:judge` and `test:refine` cost money and the cap sees them)."""
    suffix = uuid.uuid4().hex[:10]
    env = {
        "EAP_DATABASE_URL": PG_URL or "",
        "EAP_DBOS_URL": DBOS_URL,
        "EAP_DBOS_SYSTEM_SCHEMA": f"eap_test_api_{suffix}",
        "EAP_APP_VERSION": f"lp16-{suffix}",
        "EAP_EXECUTOR_ID": f"lp16-{suffix}",
        "EAP_QUEUE_POLL_SECONDS": "0.2",
        "EAP_MODEL_PRICES_FILE": str(TEST_PRICES),
        "EAP_ALLOWED_MODELS": "test:*",
    }
    log = tempfile.NamedTemporaryFile("w+", suffix=".log", delete=False)  # noqa: SIM115
    proc = subprocess.Popen([sys.executable, "-m", "eap_worker"], cwd=REPO_ROOT, env={**os.environ, **env},
                            stdout=log, stderr=subprocess.STDOUT, text=True)
    patch = pytest.MonkeyPatch()
    engine, tenant = None, f"t{uuid.uuid4().hex[:10]}"
    try:
        deadline = time.monotonic() + 60
        while "EAP_WORKER_READY" not in Path(log.name).read_text():
            assert proc.poll() is None and time.monotonic() < deadline, Path(log.name).read_text()[-3000:]
            time.sleep(0.3)
        for k, v in env.items():
            patch.setenv(k, v)
        patch.setenv("EAP_AUTH_MODE", "dev")  # the conftest's autouse fixture is function-scoped
        app = create_app(database_url=PG_URL)
        engine = app.state.database_engine
        with TestClient(app) as client:
            yield Stack(client, engine, tenant)
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
            with engine.begin() as conn:
                for table in ("workflow_versions", "workflows", "fake_external_ticket_calls", "fake_external_tickets"):
                    conn.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": tenant})
            engine.dispose()
        _drop_schema(env["EAP_DBOS_SYSTEM_SCHEMA"])
        log.close()
        os.unlink(log.name)


@pytest.fixture(autouse=True)
def _models_for_the_stack(monkeypatch):
    """The api_tests autouse allowlist (`test:*,openai:gpt-4`) is a superset; keep the worker's."""
    monkeypatch.setenv("EAP_ALLOWED_MODELS", "test:*")


@requires_pg
def test_foreach_triage_runs_every_ticket_and_escalates_only_the_p1(stack):
    run_id, _run, output = stack.run_example("foreach-triage.json")
    events = stack.events(run_id)
    assert len(output) == 3
    # ticket 0 (p1): escalated, its last step is the ticket tool; tickets 1 and 2 are filed
    assert "loop:each.0:step:escalate:completed" in events and "loop:each.0:step:file:started" not in events
    for i in (1, 2):
        assert f"loop:each.{i}:step:file:completed" in events
        assert f"loop:each.{i}:step:escalate:started" not in events
    triage = events["loop:each.0:step:triage:completed"][1]["output"]
    assert triage == {"urgency": "normal", "summary": "x"}
    # Kleene: ticket 1 has no priority, so `any(false, unknown)` is unknown and records a warning;
    # ticket 2's "p3" is plain false, with no warning
    route1 = events["loop:each.1:step:route:completed"][1]
    assert route1["output"]["case"] is None
    assert [w["code"] for w in route1.get("warnings", [])] == ["condition_unknown"]
    assert "warnings" not in events["loop:each.2:step:route:completed"][1]
    assert "warnings" not in events["loop:each.0:step:route:completed"][1]
    with stack.engine.connect() as conn:
        titles = [r[0] for r in conn.execute(
            text("SELECT title FROM fake_external_tickets WHERE tenant_id = :t AND title = 'Checkout is down'"),
            {"t": stack.tenant},
        )]
    assert titles == ["Checkout is down"]


@requires_pg
def test_until_polling_checks_three_times_then_escalates(stack):
    run_id, _run, _output = stack.run_example("until-polling.json")
    events = stack.events(run_id)
    poll = events["step:poll:completed"][1]["output"]
    assert (poll["iterations"], poll["exited"], poll["state"]) == (3, "max_iterations", {"last_status": "running"})
    assert poll["last"]["status"] == "running"
    assert "loop:poll.3:started" not in events
    # the carried state reaches the next check
    assert events["loop:poll.0:step:check:started"][1]["input"]["previous"] == "unknown"
    assert events["loop:poll.1:step:check:started"][1]["input"]["previous"] == "running"
    assert events["loop:poll.2:step:check:started"][1]["input"]["attempt"] == 2
    assert events["step:outcome:completed"][1]["output"]["next"] == "escalate"
    assert "step:escalate:completed" in events and "step:finished:started" not in events


@requires_pg
def test_refine_passes_on_the_third_attempt_with_judge_rows_metrics_and_trace(stack):
    started = datetime.now(timezone.utc) - timedelta(minutes=1)
    run_id, run, _output = stack.run_example("refine.json")
    events = stack.events(run_id)
    refine = events["step:refine:completed"][1]["output"]
    assert (refine["iterations"], refine["exited"]) == (3, "condition")
    assert refine["last"]["passed"] is True
    assert events["loop:refine.2:step:write:completed"][1]["output"]["draft"] == "attempt 3"
    reason0 = events["loop:refine.0:step:judge:completed"][1]["output"]["reason"]
    assert events["loop:refine.1:step:write:completed"][1]["output"]["after"] == reason0
    assert "loop:refine.3:started" not in events
    # judge rows: operation chat, scoped keys, priced (test:judge 200 in / 50 out at 1 / 4 per Mtok)
    with stack.engine.connect() as conn:
        rows = conn.execute(
            text("SELECT request_key, operation, cost_usd FROM run_model_requests WHERE run_id = :r ORDER BY request_key"),
            {"r": run_id},
        ).all()
    assert [(r.request_key, r.operation) for r in rows] == [
        (f"judge:refine.{i}:judge:0", "chat") for i in range(3)
    ]
    assert all(r.cost_usd == Decimal("0.0004") for r in rows)
    # metrics: the judge rows count under `judge`, the writer's child runs under `chat`
    window = {"from": started.isoformat(), "to": (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()}
    summary = stack.client.get("/api/v1/metrics/summary", params=window, headers=stack.h).json()
    judge = summary["current"]["operations"]["judge"]
    assert (judge["requests"], judge["cost_usd"]) == (3, "0.0012000000")
    # trace: a loop span, three iterations, an evaluate span per attempt
    trace = stack.client.get(f"/api/v1/runs/{run_id}/trace", headers=stack.h).json()
    kinds = [s["kind"] for s in trace["spans"]]
    assert kinds.count("loop") == 1 and kinds.count("loop_iteration") == 3 and kinds.count("evaluate") == 3
    assert run["status"] == "completed"


# ------------------------------------------------------------------------------- cost cap warning


@requires_pg
def test_unpriced_judge_under_a_cost_cap_warns_and_still_publishes(client, auth_header, monkeypatch):
    """The repository default price file prices `test:*` at $0: with `limits.max_cost_usd` the
    refine example's judge gives `cost_cap_unpriced_model` at its evaluator's path, and its writer
    agent at its step's path; without the cap there is no warning."""
    monkeypatch.setenv("EAP_MODEL_PRICES_FILE", str(DEFAULT_PRICES))
    example = load_example("refine.json")
    writer = client.post("/api/v1/agents", json={"spec": example["agents"]["writer"]}, headers=auth_header)
    writer_id = writer.json()["id"]
    assert client.post(f"/api/v1/agents/{writer_id}/versions", headers=auth_header).status_code == 201
    spec = with_agent_ids(example["spec"], {"writer": writer_id})

    def publish(spec: dict[str, Any]):
        created = client.post(BASE, json={"spec": spec}, headers=auth_header)
        assert created.status_code == 201, created.text
        return client.post(f"{BASE}/{created.json()['id']}/versions", headers=auth_header)

    capped = publish(spec)
    assert capped.status_code == 201, capped.text
    assert capped.json()["warnings"] == [{
        "code": "cost_cap_unpriced_model",
        "path": "steps[0].body.steps[1].evaluators[0]",
        "message": (
            "step 'judge': evaluators.0 (llm_judge): 'test:judge' is priced at $0, so its requests count "
            "as $0 toward limits.max_cost_usd, so the cost limit may not stop the run"
        ),
    }, {
        "code": "cost_cap_unpriced_model",
        "path": "steps[0].body.steps[0]",
        "message": (
            f"step 'write': agent '{writer_id}' version 1 model: 'test:refine' is priced at $0, so its "
            "requests count as $0 toward limits.max_cost_usd, so the cost limit may not stop the run"
        ),
    }]
    uncapped = publish({k: v for k, v in spec.items() if k != "limits"})
    assert uncapped.status_code == 201 and uncapped.json()["warnings"] == []
    monkeypatch.setenv("EAP_MODEL_PRICES_FILE", str(TEST_PRICES))  # test:judge has a price there
    again = publish({**spec, "name": "Priced"})
    assert again.status_code == 201 and again.json()["warnings"] == []


@requires_pg
def test_unpriced_judge_warning_names_a_model_with_no_price(client, auth_header, monkeypatch, tmp_path):
    prices = tmp_path / "prices.json"
    prices.write_text(json.dumps({"models": {}}))
    monkeypatch.setenv("EAP_MODEL_PRICES_FILE", str(prices))
    spec = {
        "name": "Judge only", "input_schema": {"type": "object"}, "start": "ev",
        "limits": {"max_cost_usd": "1"},
        "steps": [{"id": "ev", "type": "evaluate", "value": {"$ref": "input.text"}, "evaluators": [
            {"kind": "contains", "value": "a"},
            {"kind": "llm_judge", "rubric": "ok", "model": {"provider": "test", "name": "judge"}},
        ]}],
    }
    created = client.post(BASE, json={"spec": spec}, headers=auth_header)
    r = client.post(f"{BASE}/{created.json()['id']}/versions", headers=auth_header)
    assert r.status_code == 201, r.text
    [warning] = r.json()["warnings"]
    assert warning["path"] == "steps[0].evaluators[1]"
    assert "'test:judge' has no price in the model price override file" in warning["message"]
