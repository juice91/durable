"""Run options API (P0.8): request context, model override and draft test runs on runs and turns;
GET /models; the request-context endpoints.

Same Postgres app and fake DBOSClient as test_runs.py: the enqueue arguments of the hand-off
contract are checked exactly (they are unchanged; options travel on the run row).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from .run_helpers import requires_pg
from .test_conversations import finish, new_conversation, turn
from .test_runs import (  # noqa: F401 - fixtures
    SPEC,
    FakeDBOSClient,
    _no_hang,
    deltas,
    fake_dbos,
    pg,
    start,
    stream_settings,
    tenant_of,
)

pytestmark = requires_pg

CONTEXT_SPEC = {
    **SPEC,
    "instructions": "Help {{context.customer}} on the {{context.plan}} plan.",
    "context_fields": [
        {"name": "customer", "type": "string", "required": True, "max_length": 20},
        {"name": "plan", "type": "enum", "values": ["free", "pro"]},
        {"name": "seats", "type": "integer"},
    ],
}


def make_agent(client: TestClient, h: dict[str, str], spec: dict[str, Any] = CONTEXT_SPEC, publish: bool = True) -> str:
    r = client.post("/api/v1/agents", json={"spec": spec}, headers=h)
    assert r.status_code == 201, r.text
    agent_id = r.json()["id"]
    if publish:
        assert client.post(f"/api/v1/agents/{agent_id}/versions", headers=h).status_code == 201
    return agent_id


def set_draft(client: TestClient, h: dict[str, str], agent_id: str, spec: dict[str, Any]) -> None:
    r = client.patch(f"/api/v1/agents/{agent_id}", json={"spec": spec}, headers=h)
    assert r.status_code == 200, r.text


def row(app, run_id: str) -> dict[str, Any]:
    with app.state.database_engine.connect() as conn:
        return dict(conn.execute(text(
            "SELECT agent_version, request_context, draft_spec, model_id, model_overridden FROM runs WHERE id = :r"
        ), {"r": run_id}).mappings().one())


OK_CONTEXT = {"customer": "Ada", "plan": "pro"}


class TestRequestContext:
    def test_context_is_validated_stored_and_returned(self, pg, tenant_of, fake_dbos: FakeDBOSClient):
        client, app = pg
        _, h = tenant_of()
        agent_id = make_agent(client, h)
        r = start(client, h, agent_id, request_context={**OK_CONTEXT, "seats": None})
        assert r.status_code == 201, r.text
        run_id = r.json()["run_id"]
        stored = row(app, run_id)
        assert stored["request_context"] == OK_CONTEXT  # a null optional field is dropped
        assert client.get(f"/api/v1/runs/{run_id}", headers=h).json()["request_context"] == OK_CONTEXT
        # The hand-off arguments are unchanged: (run_id, tenant, agent, version, spec_json, input).
        _, args = fake_dbos.calls[-1]
        assert len(args) == 6 and args[3] == 1

    @pytest.mark.parametrize(
        ("context", "loc", "type_"),
        [
            ({"plan": "pro"}, ["body", "request_context", "customer"], "missing"),
            ({**OK_CONTEXT, "colour": "red"}, ["body", "request_context", "colour"], "extra_forbidden"),
            ({**OK_CONTEXT, "seats": True}, ["body", "request_context", "seats"], "int_type"),
            ({**OK_CONTEXT, "plan": "gold"}, ["body", "request_context", "plan"], "enum"),
            ({**OK_CONTEXT, "customer": "x" * 21}, ["body", "request_context", "customer"], "string_too_long"),
            ({**OK_CONTEXT, "tenant_id": "other"}, ["body", "request_context", "tenant_id"], "reserved_name"),
            ({**OK_CONTEXT, "user_id": "root"}, ["body", "request_context", "user_id"], "reserved_name"),
            ({**OK_CONTEXT, "seats": 1, "pad": "y" * 9000}, ["body", "request_context"], "too_large"),
        ],
    )
    def test_invalid_context_is_422_with_field_errors(self, pg, tenant_of, context, loc, type_):
        client, app = pg
        _, h = tenant_of()
        agent_id = make_agent(client, h)
        run_id = str(uuid.uuid4())
        r = start(client, h, agent_id, run_id=run_id, request_context=context)
        assert r.status_code == 422, r.text
        assert {"loc": loc, "type": type_} in [{"loc": e["loc"], "type": e["type"]} for e in r.json()["detail"]]
        assert all("Ada" not in e["msg"] for e in r.json()["detail"])  # values are never echoed
        with app.state.database_engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM runs WHERE id = :r"), {"r": run_id}).scalar() == 0

    def test_retry_with_other_context_is_409_same_context_is_200(self, pg, tenant_of):
        client, _ = pg
        _, h = tenant_of()
        agent_id = make_agent(client, h)
        run_id = str(uuid.uuid4())
        assert start(client, h, agent_id, run_id=run_id, request_context=OK_CONTEXT).status_code == 201
        assert start(client, h, agent_id, run_id=run_id, request_context=dict(reversed(OK_CONTEXT.items()))).status_code == 200
        r = start(client, h, agent_id, run_id=run_id, request_context={**OK_CONTEXT, "plan": "free"})
        assert r.status_code == 409
        assert start(client, h, agent_id, run_id=run_id).status_code == 409  # context now missing

    def test_retry_with_large_number_is_200(self, pg, tenant_of):
        """JSONB prints 1e300 back as an exponent-free integer; a retry must still match."""
        client, app = pg
        _, h = tenant_of()
        spec = {**CONTEXT_SPEC, "context_fields": [*CONTEXT_SPEC["context_fields"], {"name": "score", "type": "number"}]}
        agent_id = make_agent(client, h, spec)
        run_id = str(uuid.uuid4())
        context = {**OK_CONTEXT, "score": 1e300, "seats": 2**62}
        assert start(client, h, agent_id, run_id=run_id, request_context=context).status_code == 201
        assert start(client, h, agent_id, run_id=run_id, request_context=context).status_code == 200
        assert start(client, h, agent_id, run_id=run_id, request_context={**context, "score": 2e300}).status_code == 409
        assert start(client, h, agent_id, run_id=run_id, request_context={**context, "seats": 2**62 + 1}).status_code == 409

    def test_turn_context_per_turn(self, pg, tenant_of):
        client, app = pg
        _, h = tenant_of()
        agent_id = make_agent(client, h)
        conv = new_conversation(client, h, agent_id)
        first = turn(client, h, conv, request_context={"customer": "Ada"})
        assert first.status_code == 201, first.text
        finish(app, first.json()["run_id"])
        second = turn(client, h, conv, request_context={"customer": "Bob", "plan": "free"})
        assert second.status_code == 201, second.text
        assert row(app, first.json()["run_id"])["request_context"] == {"customer": "Ada"}
        assert row(app, second.json()["run_id"])["request_context"] == {"customer": "Bob", "plan": "free"}
        bad = turn(client, h, conv, request_context={"customer": 7})
        assert bad.status_code in (409, 422)  # second turn still running: 422 comes first only once it ends
        finish(app, second.json()["run_id"])
        bad = turn(client, h, conv, request_context={"customer": 7})
        assert bad.status_code == 422 and bad.json()["detail"][0]["loc"] == ["body", "request_context", "customer"]

    def test_agent_without_fields_rejects_any_key_and_stores_null(self, pg, tenant_of):
        client, app = pg
        _, h = tenant_of()
        agent_id = make_agent(client, h, SPEC)
        r = start(client, h, agent_id, request_context={})
        assert r.status_code == 201 and row(app, r.json()["run_id"])["request_context"] is None
        assert start(client, h, agent_id, request_context={"run_id": "x"}).status_code == 422


class TestModelOverride:
    def test_effective_model_is_recorded(self, pg, tenant_of):
        client, app = pg
        _, h = tenant_of()
        agent_id = make_agent(client, h)
        plain = start(client, h, agent_id, request_context=OK_CONTEXT)
        assert plain.status_code == 201
        stored = row(app, plain.json()["run_id"])
        assert (stored["model_id"], stored["model_overridden"]) == ("test:ticket", False)
        override = start(client, h, agent_id, request_context=OK_CONTEXT, model={"provider": "test", "name": "history"})
        assert override.status_code == 201, override.text
        body = override.json()
        assert body["model_id"] == "test:history" and body["model_overridden"] is True
        same = start(client, h, agent_id, request_context=OK_CONTEXT, model={"provider": "test", "name": "ticket"})
        assert row(app, same.json()["run_id"])["model_overridden"] is False
        run = client.get(f"/api/v1/runs/{body['run_id']}", headers=h).json()
        assert run["model_id"] == "test:history" and run["model_overridden"] is True

    def test_disallowed_model_is_422(self, pg, tenant_of):
        client, _ = pg
        _, h = tenant_of()
        agent_id = make_agent(client, h)
        r = start(client, h, agent_id, request_context=OK_CONTEXT, model={"provider": "openai", "name": "gpt-5"})
        assert r.status_code == 422 and "not allowed" in r.json()["detail"]

    def test_anthropic_override_with_temperature_is_422(self, pg, tenant_of, monkeypatch):
        monkeypatch.setenv("EAP_ALLOWED_MODELS", "test:*,anthropic:*")
        client, _ = pg
        _, h = tenant_of()
        agent_id = make_agent(client, h, {**SPEC, "model_settings": {"temperature": 0.2}})
        r = start(client, h, agent_id, model={"provider": "anthropic", "name": "claude-opus-5-5"})
        assert r.status_code == 422 and "temperature" in r.json()["detail"]

    def test_retry_with_other_model_is_409(self, pg, tenant_of):
        client, _ = pg
        _, h = tenant_of()
        agent_id = make_agent(client, h)
        run_id = str(uuid.uuid4())
        model = {"provider": "test", "name": "history"}
        assert start(client, h, agent_id, run_id=run_id, request_context=OK_CONTEXT, model=model).status_code == 201
        assert start(client, h, agent_id, run_id=run_id, request_context=OK_CONTEXT, model=model).status_code == 200
        assert start(client, h, agent_id, run_id=run_id, request_context=OK_CONTEXT).status_code == 409


class TestDraftRuns:
    def test_draft_run_starts_without_publishing(self, pg, tenant_of, fake_dbos: FakeDBOSClient):
        client, app = pg
        _, h = tenant_of()
        agent_id = make_agent(client, h, SPEC, publish=False)
        r = start(client, h, agent_id, version="draft")
        assert r.status_code == 201, r.text
        assert r.json()["version"] is None and r.json()["draft"] is True
        stored = row(app, r.json()["run_id"])
        assert stored["agent_version"] is None and stored["draft_spec"]["name"] == SPEC["name"]
        _, args = fake_dbos.calls[-1]
        assert args[3] == 0 and '"Support triage"' in args[4]
        versions = client.get(f"/api/v1/agents/{agent_id}/versions", headers=h).json()
        assert (versions.get("items") if isinstance(versions, dict) else versions) == []
        assert start(client, h, agent_id).status_code == 409  # still no published version

    def test_retry_relaunches_the_snapshot_not_the_edited_draft(self, pg, tenant_of, fake_dbos: FakeDBOSClient):
        client, _ = pg
        _, h = tenant_of()
        agent_id = make_agent(client, h, SPEC, publish=False)
        run_id = str(uuid.uuid4())
        assert start(client, h, agent_id, run_id=run_id, version="draft").status_code == 201
        set_draft(client, h, agent_id, {**SPEC, "instructions": "Edited after the run started."})
        retry = start(client, h, agent_id, run_id=run_id, version="draft")
        assert retry.status_code == 200, retry.text
        _, args = fake_dbos.calls[-1]
        assert "Open a ticket." in args[4] and "Edited" not in args[4]
        assert start(client, h, agent_id, run_id=run_id).status_code == 409  # not the latest version

    def test_invalid_draft_is_422(self, pg, tenant_of):
        client, _ = pg
        _, h = tenant_of()
        agent_id = make_agent(client, h, SPEC, publish=False)
        set_draft(client, h, agent_id, {**SPEC, "model": {"provider": "openai", "name": "gpt-5"}})
        r = start(client, h, agent_id, version="draft")
        assert r.status_code == 422 and "not allowed" in r.json()["detail"]
        set_draft(client, h, agent_id, {**SPEC, "instructions": "Hi {{context.nobody}}"})
        r = start(client, h, agent_id, version="draft")
        assert r.status_code == 422 and "instructions" in r.json()["detail"]

    def test_draft_with_invalid_re2_pattern_is_422(self, pg, tenant_of):
        client, _ = pg
        _, h = tenant_of()
        agent_id = make_agent(client, h, SPEC, publish=False)
        backreference = {"type": "regex_blocklist", "config": {"patterns": ["(a)\\1"]}}
        set_draft(client, h, agent_id, {**SPEC, "input_processors": [backreference]})
        r = start(client, h, agent_id, version="draft")
        assert r.status_code == 422, r.text

    def test_draft_naming_unregistered_mcp_server_is_422(self, pg, tenant_of):
        client, _ = pg
        _, h = tenant_of()
        agent_id = make_agent(client, h, SPEC, publish=False)
        set_draft(client, h, agent_id, {**SPEC, "mcp_tools": [{"server": "gone", "tool": "lookup"}]})
        r = start(client, h, agent_id, version="draft")
        assert r.status_code == 422 and "MCP server 'gone'" in r.json()["detail"]

    def test_draft_turn(self, pg, tenant_of):
        client, app = pg
        _, h = tenant_of()
        agent_id = make_agent(client, h)
        set_draft(client, h, agent_id, {**CONTEXT_SPEC, "instructions": "Draft for {{context.customer}}."})
        conv = new_conversation(client, h, agent_id)
        r = turn(client, h, conv, version="draft", request_context={"customer": "Ada"})
        assert r.status_code == 201, r.text
        assert r.json()["turn"] == 1 and r.json()["draft"] is True
        assert row(app, r.json()["run_id"])["draft_spec"]["instructions"] == "Draft for {{context.customer}}."


class TestModelsAndContextEndpoints:
    def test_models_lists_only_allowed(self, pg, tenant_of, monkeypatch):
        monkeypatch.setenv("EAP_ALLOWED_MODELS", "test:history,litellm:*")
        client, _ = pg
        _, h = tenant_of()
        items = client.get("/api/v1/models", headers=h).json()["items"]
        assert [i["id"] for i in items] == ["test:history", "litellm:*"]
        assert items[1]["wildcard"] is True
        monkeypatch.setenv("EAP_ALLOWED_MODELS", "test:*")
        ids = [i["id"] for i in client.get("/api/v1/models", headers=h).json()["items"]]
        assert ids[0] == "test:*" and "test:ticket" in ids and all(i.startswith("test:") for i in ids)
        assert client.get("/api/v1/models").status_code == 401

    def test_request_context_schema_and_validate(self, pg, tenant_of):
        client, _ = pg
        _, h = tenant_of()
        agent_id = make_agent(client, h)
        got = client.get(f"/api/v1/agents/{agent_id}/request-context", headers=h)
        assert got.status_code == 200, got.text
        assert got.json()["version"] == 1 and got.json()["schema"]["required"] == ["customer"]
        ok = client.post(f"/api/v1/agents/{agent_id}/request-context/validate",
                         json={"request_context": OK_CONTEXT}, headers=h)
        assert ok.status_code == 200 and ok.json() == {"valid": True, "request_context": OK_CONTEXT}
        bad = client.post(f"/api/v1/agents/{agent_id}/request-context/validate",
                          json={"request_context": {"customer": 1}}, headers=h)
        run = start(client, h, agent_id, request_context={"customer": 1})
        assert bad.status_code == run.status_code == 422 and bad.json() == run.json()
        draft = client.get(f"/api/v1/agents/{agent_id}/request-context?version=draft", headers=h)
        assert draft.status_code == 200 and draft.json()["draft"] is True
        assert client.get(f"/api/v1/agents/{agent_id}/request-context?version=x", headers=h).status_code == 422

    def test_cross_tenant_is_404(self, pg, tenant_of):
        client, _ = pg
        _, h = tenant_of()
        _, other = tenant_of()
        agent_id = make_agent(client, h)
        assert start(client, other, agent_id, version="draft").status_code == 404
        assert client.get(f"/api/v1/agents/{agent_id}/request-context", headers=other).status_code == 404
        assert client.post(f"/api/v1/agents/{agent_id}/request-context/validate",
                           json={"request_context": {}}, headers=other).status_code == 404
