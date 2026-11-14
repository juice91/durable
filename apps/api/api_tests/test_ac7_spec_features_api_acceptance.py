"""AC.7 acceptance: tenant isolation, the Anthropic temperature rule over the whole fallback chain,
restore under today's policy, warnings that never block, `output_json` on conversation turns, the
version diff per new field, and an unchanged response shape for agents that use none of it."""

from __future__ import annotations

import json
import uuid

from sqlalchemy import text

from .test_ac7_spec_features_api import (
    FULL_SPEC,
    PERIOD,
    SCHEMA,
    _agent,
    _create,
    _finish,
    _h,
    _prices,
    _publish,
    pinned_prices,  # noqa: F401 - autouse: a fixed price file for every test here
)
from .test_runs import deltas, fake_dbos, make_agent, pg, start, stream_settings, tenant_of  # noqa: F401

PLAIN = {"name": "Plain", "model": {"provider": "test", "name": "echo"}}
ANTHROPIC = {"provider": "anthropic", "name": "claude-opus-5-5"}


def _patch(client, tenant, agent_id, spec):
    r = client.patch(f"/api/v1/agents/{agent_id}", json={"spec": spec}, headers=_h(tenant))
    assert r.status_code == 200, r.text


def _compare(client, tenant, agent_id, base="1", target="draft"):
    r = client.get(f"/api/v1/agents/{agent_id}/versions/compare?base={base}&target={target}", headers=_h(tenant))
    assert r.status_code == 200, r.text
    return {c["path"]: c for c in r.json()["changes"]}


# ------------------------------------------------------------------------------ tenant isolation


def test_other_tenant_cannot_publish_restore_or_compare(client, tenant):
    agent_id = _agent(client, tenant, FULL_SPEC)
    assert _publish(client, tenant, agent_id).status_code == 201
    other = f"{tenant}-other"
    assert _publish(client, other, agent_id).status_code == 404
    assert client.post(f"/api/v1/agents/{agent_id}/versions/1/restore", headers=_h(other)).status_code == 404
    r = client.get(f"/api/v1/agents/{agent_id}/versions/compare?base=1&target=draft", headers=_h(other))
    assert r.status_code == 404
    assert client.get(f"/api/v1/agents/{agent_id}/versions", headers=_h(other)).status_code == 404


def test_warnings_are_per_request_not_shared_between_tenants(client, tenant, tmp_path, monkeypatch):
    _prices(tmp_path, monkeypatch, {"test:structured": PERIOD})
    mine = _publish(client, tenant, _agent(client, tenant, FULL_SPEC)).json()
    assert [w["path"] for w in mine["warnings"]] == ["fallback_models[0]"]
    other = f"{tenant}-two"
    theirs = _publish(client, other, _agent(client, other, PLAIN)).json()
    assert theirs["warnings"] == []


def test_turn_output_json_is_tenant_scoped(pg, tenant_of):
    client, app = pg
    _, h = tenant_of()
    _, other = tenant_of()
    agent_id = make_agent(client, h)
    conv = client.post(f"/api/v1/agents/{agent_id}/conversations", json={}, headers=h).json()
    run_id = str(uuid.uuid4())
    r = client.post(f"/api/v1/conversations/{conv['id']}/runs", json={"run_id": run_id, "input": "hi"}, headers=h)
    assert r.status_code == 201, r.text
    _finish(app, run_id, '{"secret":"s"}', "json")
    got = client.get(f"/api/v1/conversations/{conv['id']}", headers=other)
    assert got.status_code == 404 and "secret" not in got.text


# ------------------------------------------------------------- temperature over the whole chain


def test_anthropic_anywhere_in_the_chain_with_temperature_is_422(client, tenant, monkeypatch):
    monkeypatch.setenv("EAP_ALLOWED_MODELS", "test:*,anthropic:*")
    chain = [{"provider": "test", "name": "echo"}, ANTHROPIC]
    spec = {**FULL_SPEC, "model_settings": {"temperature": 0.3}, "fallback_models": chain}
    agent_id = _agent(client, tenant, spec)
    r = _publish(client, tenant, agent_id)
    assert r.status_code == 422
    assert r.json()["detail"].startswith("fallback_models[1]: model 'anthropic:claude-opus-5-5'")
    # Draft runs take the same refusal.
    run = client.post(
        f"/api/v1/agents/{agent_id}/runs",
        json={"run_id": str(uuid.uuid4()), "input": "hi", "version": "draft"},
        headers=_h(tenant),
    )
    assert run.status_code == 422 and run.json()["detail"].startswith("fallback_models[1]:")
    # Dropping the temperature makes the same chain publishable.
    _patch(client, tenant, agent_id, {**spec, "model_settings": {}})
    assert _publish(client, tenant, agent_id).status_code == 201


def test_temperature_zero_still_counts_as_set(client, tenant, monkeypatch):
    monkeypatch.setenv("EAP_ALLOWED_MODELS", "test:*,anthropic:*")
    spec = {**FULL_SPEC, "model_settings": {"temperature": 0}, "fallback_models": [ANTHROPIC]}
    r = _publish(client, tenant, _agent(client, tenant, spec))
    assert r.status_code == 422 and r.json()["detail"].startswith("fallback_models[0]:")


# ------------------------------------------------------------ restore under today's policy


def test_restore_of_a_version_with_now_disallowed_fallbacks_is_422(client, tenant, monkeypatch):
    agent_id = _agent(client, tenant, FULL_SPEC)
    assert _publish(client, tenant, agent_id).status_code == 201
    v2 = {**FULL_SPEC, "instructions": "v2", "fallback_models": []}
    _patch(client, tenant, agent_id, v2)
    assert _publish(client, tenant, agent_id).status_code == 201
    monkeypatch.setenv("EAP_ALLOWED_MODELS", "test:structured")
    r = client.post(f"/api/v1/agents/{agent_id}/versions/1/restore", headers=_h(tenant))
    assert r.status_code == 422
    assert r.json()["detail"].startswith("fallback_models[0]: model 'test:echo' is not allowed")
    versions = client.get(f"/api/v1/agents/{agent_id}/versions", headers=_h(tenant)).json()
    assert len(versions) == 2
    draft = client.get(f"/api/v1/agents/{agent_id}", headers=_h(tenant)).json()["draft_spec"]
    assert draft["instructions"] == "v2"
    # Allowed again: the same restore works.
    monkeypatch.delenv("EAP_ALLOWED_MODELS")
    assert client.post(f"/api/v1/agents/{agent_id}/versions/1/restore", headers=_h(tenant)).status_code == 201


# ------------------------------------------------------------------ warnings never block


def test_warnings_do_not_block_publish_and_versions_persist(client, tenant, tmp_path, monkeypatch):
    _prices(tmp_path, monkeypatch, None, raw="{broken")
    redacting = {
        **FULL_SPEC,
        "output_processors": [{"type": "pii_redact", "config": {"action": "redact"}}],
    }
    agent_id = _agent(client, tenant, redacting)
    r = _publish(client, tenant, agent_id)
    assert r.status_code == 201, r.text
    codes = sorted(w["code"] for w in r.json()["warnings"])
    assert codes == ["cost_cap_unpriced_model", "cost_cap_unpriced_model", "redaction_may_break_output_schema"]
    for w in r.json()["warnings"]:
        assert set(w) == {"code", "path", "message"} and w["message"]
    listed = client.get(f"/api/v1/agents/{agent_id}/versions", headers=_h(tenant)).json()
    assert [v["version"] for v in listed] == [1]
    # Warnings are publish-time advice, not stored on the version.
    got = client.get(f"/api/v1/agents/{agent_id}/versions/1", headers=_h(tenant)).json()
    assert "warnings" not in got and "warnings" not in listed[0]


def test_published_version_with_warnings_still_starts_a_run(pg, tenant_of, tmp_path, monkeypatch):
    client, _ = pg
    _, h = tenant_of()
    _prices(tmp_path, monkeypatch, {})
    spec = {**FULL_SPEC, "name": f"w{uuid.uuid4().hex[:6]}"}
    r = client.post("/api/v1/agents", json={"spec": spec}, headers=h)
    assert r.status_code == 201, r.text
    agent_id = r.json()["id"]
    assert client.post(f"/api/v1/agents/{agent_id}/versions", headers=h).json()["warnings"]
    assert start(client, h, agent_id).status_code == 201


# --------------------------------------------------------------------- output_json on turns


def test_conversation_turns_json_and_text(pg, tenant_of):
    client, app = pg
    _, h = tenant_of()
    agent_id = make_agent(client, h)
    conv = client.post(f"/api/v1/agents/{agent_id}/conversations", json={}, headers=h).json()
    ids = []
    for _ in range(2):
        run_id = str(uuid.uuid4())
        ids.append(run_id)
        r = client.post(f"/api/v1/conversations/{conv['id']}/runs", json={"run_id": run_id, "input": "hi"}, headers=h)
        assert r.status_code == 201, r.text
        with app.state.database_engine.begin() as c:
            c.execute(text("UPDATE runs SET status='completed' WHERE id=:r"), {"r": run_id})
    _finish(app, ids[0], '{"a":1}', "json")
    _finish(app, ids[1], "words", "text")
    turns = client.get(f"/api/v1/conversations/{conv['id']}", headers=h).json()["runs"]
    by_id = {t["run_id"]: t for t in turns}
    assert by_id[ids[0]]["output_json"] == {"a": 1} and by_id[ids[0]]["output"] == '{"a":1}'
    assert "output_json" not in by_id[ids[1]] and by_id[ids[1]]["output"] == "words"


# --------------------------------------------------------------------------- version diff


def test_compare_each_new_field_through_the_api(client, tenant):
    agent_id = _agent(client, tenant, FULL_SPEC)
    assert _publish(client, tenant, agent_id).status_code == 201
    assert _compare(client, tenant, agent_id) == {}
    changed = {
        **FULL_SPEC,
        "output": {"json_schema": SCHEMA, "mode": "native", "max_retries": 1},
        "approvals": {"tools": ["tickets.create"], "timeout_hours": 5},
        "limits": {"max_requests": 20, "max_tool_calls": 5, "max_total_tokens": 100000, "max_cost_usd": "0.75"},
        "fallback_models": [{"provider": "test", "name": "echo"}, {"provider": "test", "name": "extra"}],
    }
    _patch(client, tenant, agent_id, changed)
    got = _compare(client, tenant, agent_id)
    assert got["output.mode"]["before"] == "tool" and got["output.mode"]["after"] == "native"
    assert got["output.max_retries"]["kind"] == "changed"
    assert got["approvals.timeout_hours"]["kind"] == "changed"
    assert got["limits.max_requests"]["kind"] == "changed"
    assert got["limits.max_cost_usd"]["kind"] == "changed"
    assert got["fallback_models[1]"]["kind"] == "added"
    assert got["fallback_models[1]"]["after"]["name"] == "extra"


def test_compare_equal_cost_written_differently_is_identical(client, tenant):
    agent_id = _agent(client, tenant, FULL_SPEC)
    assert _publish(client, tenant, agent_id).status_code == 201
    limits = {**FULL_SPEC["limits"], "max_cost_usd": "0.5"}
    _patch(client, tenant, agent_id, {**FULL_SPEC, "limits": limits})
    r = client.get(f"/api/v1/agents/{agent_id}/versions/compare?base=1&target=draft", headers=_h(tenant))
    assert r.json()["identical"] is True and r.json()["changes"] == []


def test_compare_removing_features_and_between_versions(client, tenant):
    agent_id = _agent(client, tenant, FULL_SPEC)
    assert _publish(client, tenant, agent_id).status_code == 201
    _patch(client, tenant, agent_id, {**PLAIN, "tools": ["tickets.create"], "instructions": "Triage."})
    assert _publish(client, tenant, agent_id).status_code == 201
    got = _compare(client, tenant, agent_id, base="1", target="2")
    for path in ("output", "approvals", "limits", "fallback_models"):
        assert got[path]["kind"] == "removed", path
    back = _compare(client, tenant, agent_id, base="2", target="1")
    assert back["output"]["kind"] == "added"


# ----------------------------------------------------------- old agents keep their shapes


def test_old_agent_responses_have_no_new_keys(pg, tenant_of):
    client, app = pg
    _, h = tenant_of()
    agent_id = make_agent(client, h)
    versions = client.get(f"/api/v1/agents/{agent_id}/versions", headers=h).json()
    agent = client.get(f"/api/v1/agents/{agent_id}", headers=h).json()
    for spec in (versions[0]["spec"], agent["draft_spec"]):
        for key in ("output", "approvals", "limits", "fallback_models"):
            assert key not in spec
    assert "warnings" not in versions[0]
    run_id = str(uuid.uuid4())
    assert start(client, h, agent_id, run_id).status_code == 201
    with app.state.database_engine.begin() as c:
        c.execute(text("UPDATE runs SET status='completed', output='hello' WHERE id=:r"), {"r": run_id})
    run = client.get(f"/api/v1/runs/{run_id}", headers=h).json()
    assert "output_json" not in run and run["output"] == "hello"
    items = client.get("/api/v1/runs", headers=h).json()["items"]
    assert all("output_json" not in i for i in items)
    r = client.get(f"/api/v1/agents/{agent_id}/versions/compare?base=1&target=draft", headers=h)
    assert r.json()["identical"] is True


def test_publish_and_restore_of_an_old_style_agent_carry_empty_warnings(client, tenant):
    agent_id = _agent(client, tenant, PLAIN)
    assert _publish(client, tenant, agent_id).json()["warnings"] == []
    _patch(client, tenant, agent_id, {**PLAIN, "instructions": "v2"})
    assert _publish(client, tenant, agent_id).status_code == 201
    r = client.post(f"/api/v1/agents/{agent_id}/versions/1/restore", headers=_h(tenant))
    assert r.status_code == 201 and r.json()["warnings"] == []
    assert json.dumps(r.json()["spec"]).count("fallback_models") == 0


def test_invalid_specs_are_422_with_a_path_at_save(client, tenant):
    r = _create(client, tenant, {**FULL_SPEC, "fallback_models": [ANTHROPIC, ANTHROPIC]})
    assert r.status_code == 422 and r.json()["detail"][0]["loc"][:2] == ["body", "spec"]
    assert "fallback_models[1]: duplicate fallback model" in r.json()["detail"][0]["msg"]
