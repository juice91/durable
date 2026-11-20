"""AI explanation of a published workflow: generate, cache, refresh, the refusals, the seeded helper.
The explainer run is scripted (no worker): the launcher finishes it with the answer a test scripts."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from eap_api.routes.architect import reconcile_all_seeds
from eap_schemas.workflow_explanations import EXPLAINER_INSTRUCTIONS

from .test_architect_api import API, agent_row, api  # noqa: F401  (the fixture)

AGENT = {"name": "Sports writer", "instructions": "Write one short post about the game. " + "x" * 400,
         "model": {"provider": "test", "name": "echo"}}
INPUTS = {"type": "object", "properties": {"slot": {"type": "string", "description": "time slot"}}, "required": ["slot"],
          "additionalProperties": False}
ANSWER = {
    "summary": "每個時段先確認是否已經發過文章；尚未發過時，請寫手撰寫貼文並回傳結果。已發過時不做任何事。",
    "inputs": [{"name": "slot", "meaning": "這次要處理的時段"}],
    "outcomes": [{"status": "skipped", "meaning": "這個時段已經發過了"}, {"status": "completed", "meaning": "已產生貼文內容"}],
    "steps": [
        {"id": "posted_gate", "note": "檢查這個時段是否已經有貼文。"},
        {"id": "write", "note": "請寫手依比賽內容撰寫一則短貼文。"},
        {"id": "slot_done", "note": "這個時段已發過，直接結束。"},
        {"id": "finish", "note": "回傳寫好的貼文。"},
        {"id": "not_a_step", "note": "這一筆不存在於工作流，應被丟棄。"},
    ],
}


@pytest.fixture(autouse=True)
def _scripted(api):
    api[2].script = lambda _in: ANSWER


def make_workflow(client, h, *, publish=True):
    agent_id = client.post(f"{API}/agents", json={"spec": AGENT}, headers=h).json()["id"]
    assert client.post(f"{API}/agents/{agent_id}/versions", headers=h).status_code == 201
    spec = {
        "name": "Sports post", "input_schema": INPUTS, "start": "posted_gate",
        "steps": [
            {"id": "posted_gate", "type": "tool", "tool_id": "clock.now", "input": {}, "next": "route"},
            {"id": "route", "type": "branch", "default": "write",
             "cases": [{"when": {"op": "eq", "left": {"$ref": "input.slot"}, "right": "done"}, "next": "slot_done"}]},
            {"id": "slot_done", "type": "tool", "tool_id": "workflow.return",
             "input": {"value": {"ended": "already posted"}, "outcome": "skipped"}},
            {"id": "write", "type": "agent", "agent_id": agent_id, "input": {"slot": {"$ref": "input.slot"}}, "next": "finish"},
            {"id": "finish", "type": "tool", "tool_id": "workflow.return", "input": {"value": {"$ref": "steps.write.output"}}},
        ],
    }
    r = client.post(f"{API}/workflows", json={"spec": spec}, headers=h)
    assert r.status_code == 201, r.text
    wid = r.json()["id"]
    if publish:
        assert client.post(f"{API}/workflows/{wid}/versions", headers=h).status_code == 201
    return wid


def explain(client, h, wid, **body):
    return client.post(f"{API}/workflows/{wid}/explanation", json=body or None, headers=h)


def test_generate_validates_stores_and_caches(api):
    client, app, launcher, tenant_of = api
    tenant, h = tenant_of()
    wid = make_workflow(client, h)
    assert client.get(f"{API}/workflows/{wid}/explanation", headers=h).json()["code"] == "explanation_missing"

    r = explain(client, h, wid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 1 and body["locale"] == "zh-TW" and body["model"] and body["created_at"]
    assert body["summary"] == ANSWER["summary"] and body["inputs"] == ANSWER["inputs"]
    assert [s["id"] for s in body["steps"]] == ["posted_gate", "write", "slot_done", "finish"]  # the invented id is dropped
    assert body["dropped"] == 1 and len(launcher.inputs) == 1

    prompt = launcher.inputs[0]
    assert "zh-TW" in prompt and "clock.now" in prompt and "Sports writer" in prompt
    assert "Return a value" in prompt  # the tool's title, not the whole declaration
    assert "x" * 400 not in prompt and "Write one short post about the game." in prompt  # 300-grapheme excerpt

    cached = client.get(f"{API}/workflows/{wid}/explanation", headers=h)
    assert cached.status_code == 200 and cached.json()["summary"] == ANSWER["summary"]
    assert cached.json()["created_at"] == body["created_at"]
    again = explain(client, h, wid)  # the cache, no new run
    assert again.status_code == 200 and len(launcher.inputs) == 1 and again.json()["created_at"] == body["created_at"]
    # another locale is its own row
    assert client.get(f"{API}/workflows/{wid}/explanation?locale=en", headers=h).status_code == 404
    en = explain(client, h, wid, locale="en")
    assert en.status_code == 200 and en.json()["locale"] == "en" and "en (plain English)" in launcher.inputs[1]


def test_refresh_replaces_the_stored_answer(api):
    client, _, launcher, tenant_of = api
    _, h = tenant_of()
    wid = make_workflow(client, h)
    first = explain(client, h, wid).json()
    launcher.script = lambda _in: {**ANSWER, "summary": "新的說明。"}
    refreshed = explain(client, h, wid, refresh=True)
    assert refreshed.status_code == 200 and refreshed.json()["summary"] == "新的說明。"
    assert refreshed.json()["created_at"] >= first["created_at"] and len(launcher.inputs) == 2
    assert client.get(f"{API}/workflows/{wid}/explanation", headers=h).json()["summary"] == "新的說明。"


def test_long_texts_are_cut_to_their_caps(api):
    client, _, launcher, tenant_of = api
    _, h = tenant_of()
    wid = make_workflow(client, h)
    launcher.script = lambda _in: {**ANSWER, "summary": "字" * 900, "steps": [{"id": "write", "note": "步" * 300}]}
    body = explain(client, h, wid).json()
    assert len(body["summary"]) == 600 and len(body["steps"][0]["note"]) == 160


def test_versions_default_to_latest_and_unknown_is_404(api):
    client, _, launcher, tenant_of = api
    _, h = tenant_of()
    wid = make_workflow(client, h)
    draft = client.get(f"{API}/workflows/{wid}", headers=h).json()["draft_spec"]
    assert client.patch(f"{API}/workflows/{wid}", json={"spec": {**draft, "description": "v2"}}, headers=h).status_code == 200
    assert client.post(f"{API}/workflows/{wid}/versions", headers=h).status_code == 201  # version 2
    assert explain(client, h, wid).json()["version"] == 2
    assert explain(client, h, wid, version=1).json()["version"] == 1
    assert client.get(f"{API}/workflows/{wid}/explanation?version=1", headers=h).status_code == 200
    for r in (explain(client, h, wid, version=9), client.get(f"{API}/workflows/{wid}/explanation?version=9", headers=h)):
        assert r.status_code == 404 and r.json()["code"] == "version_not_found"
    assert explain(client, h, wid, version=0).status_code == 422
    assert explain(client, h, wid, locale="fr").status_code == 422
    assert explain(client, h, wid, bogus=1).status_code == 422


def test_draft_only_and_foreign_workflows_are_404(api):
    client, _, launcher, tenant_of = api
    _, h = tenant_of()
    _, other = tenant_of()
    draft = make_workflow(client, h, publish=False)
    r = explain(client, h, draft)
    assert r.status_code == 404 and r.json()["code"] == "version_not_found" and launcher.inputs == []
    published = make_workflow(client, h)
    assert explain(client, other, published).status_code == 404
    assert client.get(f"{API}/workflows/{published}/explanation", headers=other).status_code == 404
    assert explain(client, h, str(uuid.uuid4())).status_code == 404


def test_run_failures_map_to_explanation_codes(api, monkeypatch):
    client, _, launcher, tenant_of = api
    _, h = tenant_of()
    wid = make_workflow(client, h)
    launcher.script = lambda _in: RuntimeError("model down")
    r = explain(client, h, wid)
    assert r.status_code == 502 and r.json()["code"] == "explanation_failed"
    launcher.script = lambda _in: "not json"
    assert explain(client, h, wid).json()["code"] == "explanation_invalid"
    launcher.script = lambda _in: {"summary": "only a summary"}
    r = explain(client, h, wid)
    assert r.status_code == 422 and r.json()["code"] == "explanation_invalid"
    assert client.get(f"{API}/workflows/{wid}/explanation", headers=h).status_code == 404  # nothing stored
    launcher.never = True
    monkeypatch.setattr("eap_api.routes.workflow_explanations.EXPLAIN_TIMEOUT_S", 0.5)
    r = explain(client, h, wid)
    assert r.status_code == 504 and r.json()["code"] == "explanation_timeout"


def test_explainer_is_seeded_hidden_and_reconciled(api):
    client, app, _, tenant_of = api
    tenant, h = tenant_of()
    explain(client, h, make_workflow(client, h))
    row = agent_row(app, tenant, "workflow-explainer")
    assert row["builtin"] and row["name"] == "工作流說明員" and row["draft_spec"]["tools"] == []
    assert row["draft_spec"]["output"]["mode"] == "tool" and row["draft_spec"]["instructions"] == EXPLAINER_INSTRUCTIONS
    listed = client.get(f"{API}/agents", headers=h).json()
    assert str(row["id"]) not in {a["id"] for a in listed}
    shown = client.get(f"{API}/agents?include_system=true", headers=h).json()
    assert str(row["id"]) in {a["id"] for a in shown}

    # a tenant seeded before the helper existed gets it from the startup reconcile
    assert client.post(f"{API}/architect", headers=h).status_code in (200, 201)
    with app.state.database_engine.begin() as conn:
        conn.execute(text("DELETE FROM agent_versions WHERE agent_id = :i"), {"i": str(row["id"])})
        conn.execute(text("DELETE FROM agents WHERE id = :i"), {"i": str(row["id"])})
    assert agent_row(app, tenant, "workflow-explainer") == {}
    reconcile_all_seeds(app.state.database_engine)
    assert agent_row(app, tenant, "workflow-explainer")["builtin"]


def test_deleting_the_workflow_deletes_its_explanations(api):
    client, app, _, tenant_of = api
    tenant, h = tenant_of()
    wid = make_workflow(client, h)
    explain(client, h, wid)
    with app.state.database_engine.begin() as conn:
        assert conn.execute(text("SELECT count(*) FROM workflow_explanations WHERE tenant_id = :t"), {"t": tenant}).scalar_one() == 1
