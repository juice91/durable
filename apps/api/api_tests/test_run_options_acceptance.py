"""P0.8: run options API acceptance tests.

Reserved context fields from a client → 422; a model override outside the allowlist → 422; the override is recorded on the run.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from .conftest import get_auth_header
from .test_telemetry import traced  # noqa: F401 - fixture
from .test_runs import SPEC, FakeDBOSClient, _no_hang, deltas, fake_dbos, pg, start, stream_settings, tenant_of


CONTEXT_SPEC = {
    **SPEC,
    "instructions": "Help {{context.customer}} on the {{context.plan}} plan.",
    "context_fields": [
        {"name": "customer", "type": "string", "required": True, "max_length": 20},
        {"name": "plan", "type": "enum", "values": ["free", "pro"]},
        {"name": "seats", "type": "integer"},
    ],
}


def _spec(instructions: str = "", model: str = "gpt-4", provider: str = "openai") -> dict:
    from eap_schemas import AgentSpec, ModelRef

    return AgentSpec(
        name="Run Options Agent", instructions=instructions, model=ModelRef(provider=provider, name=model)
    ).model_dump(mode="json")


def _agent_with_context(client, h: dict, spec: dict | None = None) -> str:
    r = client.post("/api/v1/agents", json={"spec": spec or CONTEXT_SPEC}, headers=h)
    assert r.status_code == 201
    agent_id = r.json()["id"]
    assert client.post(f"/api/v1/agents/{agent_id}/versions", headers=h).status_code == 201
    return agent_id


OK_CONTEXT = {"customer": "Ada", "plan": "pro"}


class TestReservedContextFields:
    """Reserved context fields (tenant_id, user_id, run_id) are rejected with 422."""

    def test_tenant_id_reserved(self, pg, tenant_of):
        client, app = pg
        _, h = tenant_of()
        agent_id = _agent_with_context(client, h)
        run_id = str(uuid.uuid4())

        r = start(client, h, agent_id, run_id=run_id, request_context={"customer": "Ada", "tenant_id": "other"})
        assert r.status_code == 422
        assert any("tenant_id" in str(e.get("loc", [])) for e in r.json()["detail"])

    def test_user_id_reserved(self, pg, tenant_of):
        client, app = pg
        _, h = tenant_of()
        agent_id = _agent_with_context(client, h)
        run_id = str(uuid.uuid4())

        r = start(client, h, agent_id, run_id=run_id, request_context={"customer": "Ada", "user_id": "root"})
        assert r.status_code == 422
        assert any("user_id" in str(e.get("loc", [])) for e in r.json()["detail"])

    def test_run_id_reserved(self, pg, tenant_of):
        client, app = pg
        _, h = tenant_of()
        agent_id = _agent_with_context(client, h)
        run_id = str(uuid.uuid4())

        r = start(client, h, agent_id, run_id=run_id, request_context={"customer": "Ada", "run_id": "x"})
        assert r.status_code == 422
        assert any("run_id" in str(e.get("loc", [])) for e in r.json()["detail"])


class TestModelOverride:
    """Model override outside allowlist → 422; the override is recorded on the run."""

    def test_disallowed_model_is_422(self, pg, tenant_of):
        client, app = pg
        _, h = tenant_of()
        agent_id = _agent_with_context(client, h)
        run_id = str(uuid.uuid4())

        r = start(client, h, agent_id, run_id=run_id, request_context=OK_CONTEXT, model={"provider": "openai", "name": "gpt-5"})
        assert r.status_code == 422
        assert "not allowed" in r.json()["detail"]

    def test_anthropic_with_temperature_is_422(self, pg, tenant_of, monkeypatch):
        monkeypatch.setenv("EAP_ALLOWED_MODELS", "test:*,anthropic:*")
        client, app = pg
        _, h = tenant_of()
        spec = {**CONTEXT_SPEC, "model_settings": {"temperature": 0.2}}
        agent_id = _agent_with_context(client, h, spec)
        run_id = str(uuid.uuid4())

        r = start(client, h, agent_id, run_id=run_id, request_context=OK_CONTEXT, model={"provider": "anthropic", "name": "claude-opus-5-5"})
        assert r.status_code == 422
        assert "temperature" in r.json()["detail"]

    def test_effective_model_is_recorded(self, pg, tenant_of):
        client, app = pg
        _, h = tenant_of()
        agent_id = _agent_with_context(client, h)
        run_id = str(uuid.uuid4())

        r = start(client, h, agent_id, run_id=run_id, request_context=OK_CONTEXT, model={"provider": "test", "name": "history"})
        assert r.status_code == 201
        assert r.json()["model_id"] == "test:history"
        assert r.json()["model_overridden"] is True

        with app.state.database_engine.connect() as conn:
            row = conn.execute(
                text("SELECT model_id, model_overridden FROM runs WHERE id = :r"),
                {"r": run_id},
            ).mappings().one()
            assert row["model_id"] == "test:history"
            assert row["model_overridden"] is True


class TestDraftRuns:
    """Draft run runs its snapshot even if the draft is edited afterwards."""

    def test_draft_run_starts_without_publishing(self, pg, tenant_of):
        client, app = pg
        _, h = tenant_of()
        spec = _spec("Open a ticket.")
        r = client.post("/api/v1/agents", json={"spec": spec}, headers=h)
        agent_id = r.json()["id"]

        r = start(client, h, agent_id, run_id=str(uuid.uuid4()), version="draft")
        assert r.status_code == 201
        assert r.json()["version"] is None
        assert r.json()["draft"] is True

        versions = client.get(f"/api/v1/agents/{agent_id}/versions", headers=h).json()
        assert (versions.get("items") if isinstance(versions, dict) else versions) == []

    def test_retry_relaunches_snapshot_not_edited_draft(self, pg, tenant_of, fake_dbos: FakeDBOSClient):
        client, app = pg
        _, h = tenant_of()
        spec = _spec("Open a ticket.")
        r = client.post("/api/v1/agents", json={"spec": spec}, headers=h)
        agent_id = r.json()["id"]
        run_id = str(uuid.uuid4())

        r = start(client, h, agent_id, run_id=run_id, version="draft")
        assert r.status_code == 201

        client.patch(
            f"/api/v1/agents/{agent_id}",
            json={"spec": _spec("Edited after the run started.")},
            headers=h,
        )

        r = start(client, h, agent_id, run_id=run_id, version="draft")
        assert r.status_code == 200


class TestContextInLogsAndSpans:
    """Context values never in logs, spans, or the stored start event (telemetry fixture)."""

    def test_context_values_not_in_telemetry_events_or_enqueue(self, traced, caplog):
        import logging

        caplog.set_level(logging.DEBUG)
        client, app, fake, tenant, spans, logs = traced
        h = get_auth_header(tenant, "alice")
        agent_id = _agent_with_context(client, h)
        run_id = str(uuid.uuid4())

        r = start(client, h, agent_id, run_id=run_id, request_context={"customer": "secret-value", "plan": "pro"})
        assert r.status_code == 201

        finished = spans()
        assert any(s.name.startswith("POST ") for s in finished), "telemetry captured nothing"
        traced_text = repr([(s.name, dict(s.attributes or {}), [(e.name, dict(e.attributes or {})) for e in s.events])
                            for s in finished])
        logged = caplog.text + repr([(rec.body, dict(rec.attributes or {})) for rec in logs()])
        assert "secret-value" not in traced_text
        assert "secret-value" not in logged
        assert "secret-value" not in repr(fake.calls)  # the hand-off carries the spec, not the context
        with app.state.database_engine.connect() as conn:
            events = conn.execute(
                text("SELECT data FROM execution_events WHERE run_id = :r"), {"r": run_id},
            ).scalars().all()
        assert "secret-value" not in str(events)
        with app.state.database_engine.connect() as conn:
            stored = conn.execute(text("SELECT request_context FROM runs WHERE id = :r"), {"r": run_id}).scalar_one()
        assert stored == {"customer": "secret-value", "plan": "pro"}  # it is stored on the run row only


class TestIdempotentRetry:
    """Retry idempotency with the same run_id: 200 for the same request, 409 for a different one."""

    def test_same_run_id_same_request_is_200(self, pg, tenant_of):
        client, app = pg
        _, h = tenant_of()
        agent_id = _agent_with_context(client, h)

        run_id = str(uuid.uuid4())
        r1 = start(client, h, agent_id, run_id=run_id, request_context=OK_CONTEXT)
        assert r1.status_code == 201

        r2 = start(client, h, agent_id, run_id=run_id, request_context=OK_CONTEXT)
        assert r2.status_code == 200
        assert r2.json()["run_id"] == run_id

    def test_same_run_id_different_request_is_409(self, pg, tenant_of):
        client, app = pg
        _, h = tenant_of()
        agent_id = _agent_with_context(client, h)

        run_id = str(uuid.uuid4())
        r1 = start(client, h, agent_id, run_id=run_id, request_context=OK_CONTEXT)
        assert r1.status_code == 201

        r2 = start(client, h, agent_id, run_id=run_id, request_context=OK_CONTEXT, input="different")
        assert r2.status_code == 409

    def test_retry_with_other_context_is_409(self, pg, tenant_of):
        client, app = pg
        _, h = tenant_of()
        agent_id = _agent_with_context(client, h)

        run_id = str(uuid.uuid4())
        r1 = start(client, h, agent_id, run_id=run_id, request_context=OK_CONTEXT)
        assert r1.status_code == 201

        r2 = start(client, h, agent_id, run_id=run_id, request_context={"customer": "Ada", "plan": "free"})
        assert r2.status_code == 409


class TestModelsEndpoint:
    """GET /models lists only allowlisted models."""

    def test_models_lists_only_allowed(self, pg, tenant_of, monkeypatch):
        monkeypatch.setenv("EAP_ALLOWED_MODELS", "test:history,litellm:*")
        client, _ = pg
        _, h = tenant_of()

        r = client.get("/api/v1/models", headers=h)
        assert r.status_code == 200
        items = r.json()["items"]
        assert [i["id"] for i in items] == ["test:history", "litellm:*"]
        assert items[1]["wildcard"] is True

        monkeypatch.setenv("EAP_ALLOWED_MODELS", "test:*")
        r = client.get("/api/v1/models", headers=h)
        assert r.status_code == 200
        ids = [i["id"] for i in r.json()["items"]]
        assert ids[0] == "test:*"
        assert "test:ticket" in ids


class TestConversationPagingAcceptance:
    """P0.5: a full walk of the pages has no gaps or repeats; cursors do not cross tenants."""

    def test_walk_all_pages_is_stable_and_complete(self, pg, tenant_of):
        from .test_conversations import new_conversation
        from .test_runs import make_agent

        client, _ = pg
        _, h = tenant_of()
        agent_id = make_agent(client, h)
        ids = [new_conversation(client, h, agent_id) for _ in range(5)]
        seen, before = [], None
        for _ in range(10):
            url = f"/api/v1/conversations?agent_id={agent_id}&limit=2" + (f"&before={before}" if before else "")
            page = client.get(url, headers=h).json()
            seen += [c["id"] for c in page["items"]]
            before = page["next_before"]
            if before is None:
                break
        assert seen == list(reversed(ids))  # newest first, each once
        again = client.get(f"/api/v1/conversations?agent_id={agent_id}&limit=10", headers=h).json()["items"]
        assert [c["id"] for c in again] == seen  # a second read gives the same order

    def test_other_tenant_sees_nothing_and_a_foreign_cursor_leaks_nothing(self, pg, tenant_of):
        from .test_conversations import new_conversation
        from .test_runs import make_agent

        client, _ = pg
        _, h = tenant_of()
        _, other = tenant_of()
        agent_id = make_agent(client, h)
        for _ in range(3):
            new_conversation(client, h, agent_id)
        cursor = client.get(f"/api/v1/conversations?limit=1", headers=h).json()["next_before"]
        assert cursor
        assert client.get("/api/v1/conversations", headers=other).json()["items"] == []
        leaked = client.get(f"/api/v1/conversations?before={cursor}", headers=other)
        assert leaked.status_code == 200 and leaked.json()["items"] == []
