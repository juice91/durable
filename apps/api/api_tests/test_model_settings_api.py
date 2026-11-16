"""Model settings (W1.1): ranges, provider refusals at publish and on a run start, per-run
overrides stored and returned, retries compared with the recorded overrides, hash stability.

Same Postgres app and fake DBOSClient as test_run_options.py.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from eap_schemas import AgentSpec, canonical_spec_hash

from .run_helpers import requires_pg
from .test_run_options import make_agent, set_draft
from .test_runs import SPEC, FakeDBOSClient, _no_hang, deltas, fake_dbos, pg, start, stream_settings, tenant_of  # noqa: F401

pytestmark = requires_pg

ANTHROPIC = {"provider": "anthropic", "name": "claude-opus-5-5"}
OPENAI = {"provider": "openai", "name": "gpt-5"}


def stored_settings(app, run_id: str):
    with app.state.database_engine.connect() as conn:
        return conn.execute(text("SELECT model_settings FROM runs WHERE id = :r"), {"r": run_id}).scalar_one()


def with_settings(**settings):
    return {**SPEC, "model_settings": settings}


class TestSpecRanges:
    @pytest.mark.parametrize(
        "settings",
        [
            {"top_p": 1.5}, {"top_k": 0}, {"frequency_penalty": 3}, {"presence_penalty": -2.5},
            {"seed": 1.5}, {"max_steps": 0}, {"max_steps": 1000}, {"max_retries": -1}, {"max_retries": 99},
        ],
    )
    def test_out_of_range_is_422(self, pg, tenant_of, settings):
        client, _ = pg
        _, h = tenant_of()
        r = client.post("/api/v1/agents", json={"spec": with_settings(**settings)}, headers=h)
        assert r.status_code == 422, r.text

    def test_round_trip(self, pg, tenant_of):
        client, _ = pg
        _, h = tenant_of()
        settings = {"temperature": 0.3, "top_p": 0.9, "top_k": 20, "seed": 7, "max_steps": 5, "max_retries": 0,
                    "frequency_penalty": 0.5, "presence_penalty": -0.5}
        agent_id = make_agent(client, h, with_settings(**settings))
        got = client.get(f"/api/v1/agents/{agent_id}", headers=h).json()
        assert {k: v for k, v in got["draft_spec"]["model_settings"].items() if v is not None} == settings

    def test_unset_fields_are_not_stored(self, pg, tenant_of):
        client, _ = pg
        _, h = tenant_of()
        agent_id = make_agent(client, h, SPEC)
        stored = client.get(f"/api/v1/agents/{agent_id}", headers=h).json()["draft_spec"]["model_settings"]
        assert stored == {"temperature": None, "max_tokens": None}

    def test_hash_of_a_spec_without_the_new_fields_is_unchanged(self):
        spec = AgentSpec.model_validate(SPEC)
        assert spec.model_dump(mode="json")["model_settings"] == {"temperature": None, "max_tokens": None}
        # Same canonical JSON an older build produced.
        expected = AgentSpec.model_validate({**SPEC, "model_settings": {"temperature": None, "max_tokens": None}})
        assert canonical_spec_hash(spec) == canonical_spec_hash(expected)


class TestPublishRefusals:
    @pytest.mark.parametrize("name", ["top_p", "top_k", "frequency_penalty", "presence_penalty", "seed"])
    def test_anthropic_refuses(self, pg, tenant_of, monkeypatch, name):
        monkeypatch.setenv("EAP_ALLOWED_MODELS", "test:*,anthropic:*")
        client, _ = pg
        _, h = tenant_of()
        agent_id = make_agent(client, h, {**SPEC, "model": ANTHROPIC, "model_settings": {name: 1}}, publish=False)
        r = client.post(f"/api/v1/agents/{agent_id}/versions", headers=h)
        assert r.status_code == 422
        assert r.json()["detail"] == (
            f"model 'anthropic:claude-opus-5-5' does not accept a {name} setting; "
            f"remove {name} from the agent's model settings"
        )

    def test_openai_refuses_top_k_only(self, pg, tenant_of, monkeypatch):
        monkeypatch.setenv("EAP_ALLOWED_MODELS", "test:*,openai:*")
        client, _ = pg
        _, h = tenant_of()
        bad = make_agent(client, h, {**SPEC, "model": OPENAI, "model_settings": {"top_k": 5}}, publish=False)
        r = client.post(f"/api/v1/agents/{bad}/versions", headers=h)
        assert r.status_code == 422 and "does not accept a top_k setting" in r.json()["detail"]
        ok = make_agent(client, h, {**SPEC, "model": OPENAI, "model_settings": {"top_p": 0.5, "seed": 3, "max_steps": 4}})
        assert ok

    def test_fallback_model_is_checked(self, pg, tenant_of, monkeypatch):
        monkeypatch.setenv("EAP_ALLOWED_MODELS", "test:*,anthropic:*")
        client, _ = pg
        _, h = tenant_of()
        spec = {**SPEC, "fallback_models": [ANTHROPIC], "model_settings": {"seed": 1}}
        agent_id = make_agent(client, h, spec, publish=False)
        r = client.post(f"/api/v1/agents/{agent_id}/versions", headers=h)
        assert r.status_code == 422
        assert r.json()["detail"].startswith("fallback_models[0]: model 'anthropic:claude-opus-5-5' does not accept a seed")

    def test_anthropic_accepts_loop_and_retry_settings(self, pg, tenant_of, monkeypatch):
        monkeypatch.setenv("EAP_ALLOWED_MODELS", "test:*,anthropic:*")
        client, _ = pg
        _, h = tenant_of()
        assert make_agent(client, h, {**SPEC, "model": ANTHROPIC, "model_settings": {"max_tokens": 9, "max_steps": 3, "max_retries": 2}})


class TestRunOverrides:
    def test_override_is_validated_stored_and_returned(self, pg, tenant_of, fake_dbos: FakeDBOSClient):
        client, app = pg
        _, h = tenant_of()
        agent_id = make_agent(client, h, SPEC)
        r = start(client, h, agent_id, model_settings={"temperature": 0.4, "top_p": 0.8, "seed": 11, "max_steps": 3})
        assert r.status_code == 201, r.text
        run_id = r.json()["run_id"]
        assert stored_settings(app, run_id) == {"temperature": 0.4, "top_p": 0.8, "seed": 11, "max_steps": 3}
        assert client.get(f"/api/v1/runs/{run_id}", headers=h).json()["model_settings"] == {
            "temperature": 0.4, "top_p": 0.8, "seed": 11, "max_steps": 3}

    def test_no_override_stores_null_and_returns_empty(self, pg, tenant_of, fake_dbos):
        client, app = pg
        _, h = tenant_of()
        agent_id = make_agent(client, h, SPEC)
        for body in ({}, {"model_settings": {}}, {"model_settings": {"top_p": None}}):
            r = start(client, h, agent_id, **body)
            assert r.status_code == 201, r.text
            assert stored_settings(app, r.json()["run_id"]) is None
        assert client.get(f"/api/v1/runs/{r.json()['run_id']}", headers=h).json()["model_settings"] == {}

    @pytest.mark.parametrize(
        "settings",
        [{"top_p": 2}, {"top_k": 0}, {"presence_penalty": 9}, {"seed": "x"}, {"max_steps": 0}, {"max_retries": 50}, {"nope": 1}],
    )
    def test_out_of_range_is_422(self, pg, tenant_of, fake_dbos, settings):
        client, _ = pg
        _, h = tenant_of()
        agent_id = make_agent(client, h, SPEC)
        assert start(client, h, agent_id, model_settings=settings).status_code == 422

    @pytest.mark.parametrize("name", ["top_p", "top_k", "frequency_penalty", "presence_penalty", "seed", "temperature"])
    def test_anthropic_override_model_refuses(self, pg, tenant_of, monkeypatch, name):
        monkeypatch.setenv("EAP_ALLOWED_MODELS", "test:*,anthropic:*")
        client, _ = pg
        _, h = tenant_of()
        agent_id = make_agent(client, h, SPEC)
        r = start(client, h, agent_id, model=ANTHROPIC, model_settings={name: 1})
        assert r.status_code == 422
        assert f"does not accept a {name} setting" in r.json()["detail"]

    def test_override_adds_a_setting_the_spec_model_cannot_take(self, pg, tenant_of, monkeypatch):
        monkeypatch.setenv("EAP_ALLOWED_MODELS", "test:*,openai:*")
        client, _ = pg
        _, h = tenant_of()
        agent_id = make_agent(client, h, {**SPEC, "model": OPENAI})
        r = start(client, h, agent_id, model_settings={"top_k": 4})
        assert r.status_code == 422
        assert r.json()["detail"] == (
            "model 'openai:gpt-5' does not accept a top_k setting; remove top_k from the run's model settings "
            "or choose another model"
        )

    def test_loop_and_retry_overrides_pass_on_anthropic(self, pg, tenant_of, monkeypatch, fake_dbos):
        monkeypatch.setenv("EAP_ALLOWED_MODELS", "test:*,anthropic:*")
        client, _ = pg
        _, h = tenant_of()
        agent_id = make_agent(client, h, SPEC)
        r = start(client, h, agent_id, model=ANTHROPIC, model_settings={"max_steps": 2, "max_retries": 1, "max_tokens": 50})
        assert r.status_code == 201, r.text

    def test_draft_run_takes_overrides(self, pg, tenant_of, fake_dbos):
        client, app = pg
        _, h = tenant_of()
        agent_id = make_agent(client, h, SPEC)
        set_draft(client, h, agent_id, with_settings(temperature=0.1))
        r = start(client, h, agent_id, version="draft", model_settings={"top_p": 0.3})
        assert r.status_code == 201, r.text
        assert stored_settings(app, r.json()["run_id"]) == {"top_p": 0.3}

    def test_retry_must_carry_the_same_overrides(self, pg, tenant_of, fake_dbos):
        client, _ = pg
        _, h = tenant_of()
        agent_id = make_agent(client, h, SPEC)
        run_id = str(uuid.uuid4())
        assert start(client, h, agent_id, run_id=run_id, model_settings={"seed": 1}).status_code == 201
        assert start(client, h, agent_id, run_id=run_id, model_settings={"seed": 1}).status_code == 200
        assert start(client, h, agent_id, run_id=run_id, model_settings={"seed": 2}).status_code == 409
        assert start(client, h, agent_id, run_id=run_id).status_code == 409


# ------------------------------------------------------------------------------------- AG-UI path

from sqlalchemy import text as _text  # noqa: E402

from .conftest import get_auth_header  # noqa: E402
from .test_agui6_agent_endpoints import (  # noqa: E402,F401 - fixtures and helpers
    _key,
    _no_hang as _agui_no_hang,
    ag,
    agui_settings,
    body,
    post,
    tenant as agui_tenant,
    user,
    worker,
)
from .test_runs import make_agent as make_runs_agent  # noqa: E402


class TestAgUiPath:
    def test_forwarded_model_settings_reach_the_run(self, ag, agui_tenant):
        client, app = ag
        h = get_auth_header(agui_tenant, "alice")
        agent_id = make_runs_agent(client, h)
        props = {"pydanticos": {"modelSettings": {"top_p": 0.4, "max_steps": 2}}}
        status, _, _ = post(client, agent_id, body("t1", "r1", user("u1", "hi"), forwardedProps=props), h)
        assert status == 200
        with app.state.database_engine.connect() as conn:
            stored = conn.execute(
                _text("SELECT model_settings FROM runs WHERE tenant_id = :t AND model_settings IS NOT NULL"),
                {"t": agui_tenant},
            ).scalar_one()
        assert stored == {"top_p": 0.4, "max_steps": 2}

    def test_out_of_range_is_422(self, ag, agui_tenant):
        client, _ = ag
        h = get_auth_header(agui_tenant, "alice")
        agent_id = make_runs_agent(client, h)
        props = {"pydanticos": {"modelSettings": {"top_p": 7}}}
        status, detail, _ = post(client, agent_id, body("t1", "r1", user("u1", "hi"), forwardedProps=props), h)
        assert status == 422, detail

    def test_api_key_may_not_override_settings(self, ag, agui_tenant):
        client, _ = ag
        h = get_auth_header(agui_tenant, "alice")
        agent_id = make_runs_agent(client, h)
        _, key = _key(client, agui_tenant, agent_ids=[agent_id])
        props = {"pydanticos": {"modelSettings": {"seed": 3}}}
        status, detail, _ = post(
            client, agent_id, body("t1", "r1", user("u1", "hi"), forwardedProps=props), {"Authorization": f"Bearer {key}"}
        )
        assert status == 403 and detail["detail"][0]["type"] == "model_settings_override_not_allowed"
