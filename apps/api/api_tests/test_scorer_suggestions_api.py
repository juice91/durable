"""Scorer suggestions: the suggester run (scripted, no worker), validation of its answer, redaction of
what it reads, the 10-minute cache and the refusals."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from eap_api.routes import scorer_suggestions
from eap_run_store import RunStore

from .test_architect_api import API, agent_row, api  # noqa: F401  (the fixture)
from .test_workflow_runs_api import publish_demo

SECRET = "sk-LIVEKEY1234567890abcdef"
AGENT = {"name": "Support", "instructions": "Answer billing questions. Never promise refunds.",
         "model": {"provider": "test", "name": "echo"}}


def flat(kind, **fields):
    base = {"kind": kind, "pattern": None, "negate": None, "field": None, "path": None, "value_json": None,
            "case_sensitive": None, "json_schema_json": None, "usd": None, "seconds": None, "rubric": None}
    return {**base, **fields}


def entry(spec, title="Rule", scope="both"):
    return {"title": title, "rationale": "Because.", "scope": scope, "evidence": "instructions line 2", "spec": spec}


GOOD = [
    entry(flat("regex", pattern="(?i)refund", negate=True), "No refund promises", "scorer"),
    entry(flat("max_cost", usd="0.05"), "Stay cheap"),
    entry(flat("llm_judge", rubric="Polite and on topic."), "Tone", "scorer"),
]


@pytest.fixture(autouse=True)
def _fresh_cache():
    scorer_suggestions.clear_cache()
    yield
    scorer_suggestions.clear_cache()


def make_agent(client, h, spec=AGENT):
    r = client.post(f"{API}/agents", json={"spec": spec}, headers=h)
    assert r.status_code == 201, r.text
    agent_id = r.json()["id"]
    assert client.post(f"{API}/agents/{agent_id}/versions", headers=h).status_code == 201
    return agent_id


def finished_run(app, tenant, agent_id, input_text, output):
    store = RunStore(app.state.database_engine)
    run_id = uuid.uuid4()
    store.create_run(tenant, uuid.UUID(agent_id), 1, input_text, run_id)
    store.finish_with_end(tenant, run_id, "completed", output=output)
    return run_id


def suggest(client, h, target, kind="agents", **body):
    return client.post(f"{API}/{kind}/{target}/scorer-suggestions", json=body or None, headers=h)


def test_suggestions_parse_and_seed_the_suggester(api):
    client, app, launcher, tenant_of = api
    tenant, h = tenant_of()
    agent_id = make_agent(client, h)
    launcher.script = lambda _in: {"suggestions": GOOD}
    r = suggest(client, h, agent_id)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dropped"] == 0 and "model" in body
    titles = [s["title"] for s in body["suggestions"]]
    assert titles == ["No refund promises", "Stay cheap", "Tone"]
    regex, cost, judge = body["suggestions"]
    assert regex["scope"] == "scorer" and regex["spec"] == {"kind": "regex", "pattern": "(?i)refund", "negate": True}
    assert cost["spec"]["usd"] == "0.05" and cost["scope"] == "both"
    assert judge["scope"] == "evaluator" and judge["spec"]["kind"] == "llm_judge" and judge["spec"]["model"]["provider"]
    assert set(regex) == {"title", "rationale", "scope", "spec", "evidence"}

    row = agent_row(app, tenant, "scorer-suggester")
    assert row["builtin"] and row["draft_spec"]["tools"] == [] and row["draft_spec"]["output"]["mode"] == "tool"
    hidden = {a["id"] for a in client.get(f"{API}/agents", headers=h).json()}
    assert str(row["id"]) not in hidden


def test_invalid_entries_are_dropped_and_counted(api):
    client, _, launcher, tenant_of = api
    _, h = tenant_of()
    agent_id = make_agent(client, h)
    launcher.script = lambda _in: {"suggestions": [
        entry(flat("regex", pattern="(?=x)y")),  # lookahead: not RE2
        entry(flat("regex")),  # no pattern
        entry(flat("max_cost", usd="free")),
        entry(flat("exec")),
        "not an object",
        entry(flat("max_cost", usd="0.5"), "Cheap"),
        entry(flat("max_cost", usd="0.5"), "Cheap again"),  # same spec
        entry(flat("llm_judge", rubric="a")), entry(flat("llm_judge", rubric="b")), entry(flat("llm_judge", rubric="c")),
    ]}
    body = suggest(client, h, agent_id).json()
    assert [s["title"] for s in body["suggestions"]][0] == "Cheap"
    assert sum(1 for s in body["suggestions"] if s["spec"]["kind"] == "llm_judge") == 2
    assert body["dropped"] == 5 + 1 + 1  # five bad entries, one repeated spec, the third judge


def test_max_limits_the_answer(api):
    client, _, launcher, tenant_of = api
    _, h = tenant_of()
    agent_id = make_agent(client, h)
    launcher.script = lambda _in: {"suggestions": [entry(flat("max_cost", usd=str(i + 1)), f"c{i}") for i in range(6)]}
    body = suggest(client, h, agent_id, max=2).json()
    assert [s["title"] for s in body["suggestions"]] == ["c0", "c1"] and body["dropped"] == 0
    assert suggest(client, h, agent_id, max=13).status_code == 422
    assert suggest(client, h, agent_id, max=0).status_code == 422
    assert suggest(client, h, agent_id, bogus=1).status_code == 422


def test_runs_and_instructions_are_redacted_and_bounded(api):
    client, app, launcher, tenant_of = api
    tenant, h = tenant_of()
    spec = {**AGENT, "instructions": f"Never promise refunds. Use the key {SECRET} when calling billing. password=hunter2hunter2"}
    agent_id = make_agent(client, h, spec)
    for i in range(7):
        finished_run(app, tenant, agent_id, f"question {i}", f"answer {i} Authorization: Bearer {SECRET} " + "x" * 5000)
    launcher.script = lambda _in: {"suggestions": []}
    assert suggest(client, h, agent_id).status_code == 200
    [prompt] = launcher.inputs
    assert SECRET not in prompt and "hunter2hunter2" not in prompt and "LIVEKEY" not in prompt
    assert "[REDACTED]" in prompt
    assert prompt.count("output:") == 5  # at most five runs
    assert len(prompt) <= scorer_suggestions.MAX_PROMPT_CHARS
    assert "Never promise refunds" in prompt

    launcher.inputs.clear()
    assert suggest(client, h, agent_id, include_runs=False).status_code == 200
    assert "output:" not in launcher.inputs[0] and "Recent runs: none" in launcher.inputs[0]


def test_cache_per_definition_and_max_for_ten_minutes(api):
    client, _, launcher, tenant_of = api
    _, h = tenant_of()
    agent_id = make_agent(client, h)
    launcher.script = lambda _in: {"suggestions": GOOD[:2]}
    first = suggest(client, h, agent_id).json()
    assert suggest(client, h, agent_id).json() == first
    assert len(launcher.inputs) == 1  # the repeat ran nothing
    suggest(client, h, agent_id, max=3)
    assert len(launcher.inputs) == 2  # another max, another run
    client.patch(f"{API}/agents/{agent_id}", json={"spec": {**AGENT, "instructions": "Changed."}}, headers=h)
    suggest(client, h, agent_id)
    assert len(launcher.inputs) == 3  # another definition, another run
    for key, (_, value) in list(scorer_suggestions._cache.items()):
        scorer_suggestions._cache[key] = (0.0, value)  # ten minutes later
    suggest(client, h, agent_id)
    assert len(launcher.inputs) == 4


def test_workflow_suggestions_are_evaluator_only(api):
    client, app, launcher, tenant_of = api
    tenant, h = tenant_of()
    workflow_id = publish_demo(client, h)
    launcher.script = lambda _in: {"suggestions": GOOD}
    r = suggest(client, h, workflow_id, kind="workflows")
    assert r.status_code == 200, r.text
    assert {s["scope"] for s in r.json()["suggestions"]} == {"evaluator"}
    assert "Target: workflow" in launcher.inputs[0] and "Steps:" in launcher.inputs[0]


def test_refusals(api, monkeypatch):
    client, _, launcher, tenant_of = api
    _, h = tenant_of()
    _, other = tenant_of("bob")
    agent_id = make_agent(client, h)
    missing = str(uuid.uuid4())
    assert suggest(client, h, missing).status_code == 404
    assert suggest(client, h, missing, kind="workflows").status_code == 404
    assert suggest(client, other, agent_id).status_code == 404

    launcher.script = lambda _in: RuntimeError("model down")
    r = suggest(client, h, agent_id)
    assert r.status_code == 502 and r.json()["code"] == "suggest_failed"
    launcher.script = lambda _in: "not json"
    r = suggest(client, h, agent_id)
    assert r.status_code == 422 and r.json()["code"] == "suggest_invalid"
    launcher.script = lambda _in: {"rules": []}
    assert suggest(client, h, agent_id).status_code == 422
    launcher.never = True
    monkeypatch.setattr("eap_api.routes.architect.DRAFT_TIMEOUT_S", 0.5)
    r = suggest(client, h, agent_id)
    assert r.status_code == 504 and r.json()["code"] == "suggest_timeout"


def test_no_model_is_409(api, monkeypatch):
    client, _, _, tenant_of = api
    _, h = tenant_of()
    agent_id = make_agent(client, h)
    monkeypatch.setenv("EAP_ALLOWED_MODELS", "litellm:*")
    r = suggest(client, h, agent_id)
    assert r.status_code == 409 and r.json()["code"] == "no_model"
