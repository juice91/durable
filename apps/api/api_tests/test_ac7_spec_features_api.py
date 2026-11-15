"""AC.7 API for spec features: publish checks and warnings for structured output, approvals, usage
limits and fallback models; `output_json` in run and turn responses; `kind=eval` in the run list;
version compare over the new fields.

Most spec rules (subset schema, approval refs, distinct fallbacks, `final_result` with output) are
AgentSpec's (AC.1) and answer 422 when the draft is saved; publish adds the platform policy (the
fallback allowlist, the Anthropic temperature rule across the chain) and the warnings.
"""

from __future__ import annotations

import json
import uuid
from datetime import date

import pytest
from sqlalchemy import text

from eap_api.routes import PRICE_UNUSABLE, _fixed_string_paths, _price_status, publish_warnings
from eap_pricing import PRICE_PRICED as PRICE_SET
from eap_pricing import PRICE_UNPRICED as PRICE_NONE
from eap_pricing import PRICE_ZERO
from eap_api.routes.runs import _output_json
from eap_api.version_diff import compare_specs
from eap_schemas import AgentSpec, canonical_spec_hash

from .conftest import get_auth_header
from .test_runs import deltas, fake_dbos, make_agent, pg, start, stream_settings, tenant_of  # noqa: F401

SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": ["billing", "outage"]},
        "summary": {"type": "string", "description": "One line"},
    },
    "required": ["category", "summary"],
    "additionalProperties": False,
}

FULL_SPEC = {
    "name": "Capable agent",
    "instructions": "Triage.",
    "model": {"provider": "test", "name": "structured"},
    "tools": ["tickets.create"],
    "output": {"json_schema": SCHEMA, "mode": "tool", "max_retries": 3},
    "approvals": {"tools": ["tickets.create"], "timeout_hours": 2},
    "limits": {"max_requests": 10, "max_tool_calls": 5, "max_total_tokens": 100000, "max_cost_usd": "0.50"},
    "fallback_models": [{"provider": "test", "name": "echo"}],
}


PERIOD = [{"effective_from": "2026-01-01", "input_per_mtok": "1", "output_per_mtok": "2"}]


@pytest.fixture(autouse=True)
def pinned_prices(tmp_path, monkeypatch):
    """A non-zero price for every test and litellm model, so a developer's EAP_MODEL_PRICES_FILE (or
    the repository default, which prices them at $0) cannot change which warnings appear. Tests that
    need another file call _prices, which overrides this one."""
    path = tmp_path / "pinned-prices.json"
    path.write_text(json.dumps({"models": {"test:*": PERIOD, "litellm:*": PERIOD}}), encoding="utf-8")
    monkeypatch.setenv("EAP_MODEL_PRICES_FILE", str(path))


def _h(tenant: str) -> dict[str, str]:
    return get_auth_header(tenant, "alice")


def _create(client, tenant: str, spec: dict):
    return client.post("/api/v1/agents", json={"spec": spec}, headers=_h(tenant))


def _agent(client, tenant: str, spec: dict) -> str:
    r = _create(client, tenant, spec)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _publish(client, tenant: str, agent_id: str):
    return client.post(f"/api/v1/agents/{agent_id}/versions", headers=_h(tenant))


def _locs(response) -> list[list]:
    return [issue["loc"] for issue in response.json()["detail"]]


# ------------------------------------------------------------------------------------- publish


def test_publish_with_all_four_features_round_trips_the_hash(client, tenant):
    agent_id = _agent(client, tenant, FULL_SPEC)
    r = _publish(client, tenant, agent_id)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["warnings"] == []
    spec = AgentSpec.model_validate(FULL_SPEC)
    assert body["spec_hash"] == canonical_spec_hash(spec)
    assert body["spec"] == spec.model_dump(mode="json")
    assert body["spec"]["limits"]["max_cost_usd"] == "0.5"  # normalised
    stored = client.get(f"/api/v1/agents/{agent_id}/versions/1", headers=_h(tenant)).json()
    assert canonical_spec_hash(AgentSpec.model_validate(stored["spec"])) == body["spec_hash"]
    # Publishing the same draft again: no changes.
    assert _publish(client, tenant, agent_id).status_code == 409


def test_spec_without_new_fields_keeps_its_publish_shape(client, tenant):
    spec = {"name": "Plain", "model": {"provider": "test", "name": "echo"}}
    body = _publish(client, tenant, _agent(client, tenant, spec)).json()
    assert body["warnings"] == []
    for key in ("output", "approvals", "limits", "fallback_models"):
        assert key not in body["spec"]


@pytest.mark.parametrize(
    ("schema", "fragment"),
    [
        ({"type": "object", "properties": {"a": {"$ref": "#/x"}}}, "$ref"),
        ({"type": "object", "properties": {"a": {"type": "string", "pattern": "a+"}}}, "pattern"),
        ({"type": "object", "properties": {"a": {"anyOf": [{"type": "string"}]}}}, "anyOf"),
        ({"type": "array", "items": {"type": "string"}}, "object"),
    ],
)
def test_output_schema_outside_the_subset_is_422_with_its_path(client, tenant, schema, fragment):
    r = _create(client, tenant, {**FULL_SPEC, "output": {"json_schema": schema}})
    assert r.status_code == 422
    assert ["body", "spec", "output", "json_schema"] in _locs(r)
    assert fragment in json.dumps(r.json()["detail"])


def test_approval_for_a_tool_not_in_the_spec_is_422_naming_approvals(client, tenant):
    r = _create(client, tenant, {**FULL_SPEC, "approvals": {"tools": ["crm.delete"]}})
    assert r.status_code == 422
    assert "approvals.tools[0]: 'crm.delete' is not a tool of this agent" in json.dumps(r.json()["detail"])


def test_platform_tool_cannot_require_approval(client, tenant):
    spec = {**FULL_SPEC, "tools": ["tickets.create"], "approvals": {"tools": ["final_result"]}}
    r = _create(client, tenant, spec)
    assert r.status_code == 422
    assert "platform tool" in json.dumps(r.json()["detail"])


def test_fallback_equal_to_primary_or_duplicate_is_422(client, tenant):
    primary = {**FULL_SPEC, "fallback_models": [FULL_SPEC["model"]]}
    assert _create(client, tenant, primary).status_code == 422
    dup = {**FULL_SPEC, "fallback_models": [{"provider": "test", "name": "echo"}] * 2}
    r = _create(client, tenant, dup)
    assert r.status_code == 422 and "fallback_models[1]: duplicate fallback model" in json.dumps(r.json()["detail"])


def test_fallback_off_the_allowlist_is_422_naming_the_index(client, tenant, monkeypatch):
    spec = {**FULL_SPEC, "fallback_models": [{"provider": "test", "name": "echo"}, {"provider": "litellm", "name": "nope"}]}
    agent_id = _agent(client, tenant, spec)  # drafts may name any model; publishing is the gate
    r = _publish(client, tenant, agent_id)
    assert r.status_code == 422
    assert r.json()["detail"] == (
        "fallback_models[1]: model 'litellm:nope' is not allowed on this platform "
        "(not in the EAP_ALLOWED_MODELS allowlist)"
    )
    assert client.get(f"/api/v1/agents/{agent_id}/versions", headers=_h(tenant)).json() == []


def test_anthropic_fallback_with_temperature_is_422(client, tenant, monkeypatch):
    monkeypatch.setenv("EAP_ALLOWED_MODELS", "test:*,anthropic:*")
    spec = {
        **FULL_SPEC,
        "model_settings": {"temperature": 0.2},
        "fallback_models": [{"provider": "anthropic", "name": "claude-opus-5-5"}],
    }
    r = _publish(client, tenant, _agent(client, tenant, spec))
    assert r.status_code == 422
    assert r.json()["detail"].startswith("fallback_models[0]: model 'anthropic:claude-opus-5-5' does not accept")
    # Without temperature the same chain publishes.
    ok = {**spec, "model_settings": {}}
    assert _publish(client, tenant, _agent(client, tenant, ok)).status_code == 201


def test_final_result_tool_name_is_reserved(client, tenant):
    # With output, AgentSpec refuses it when the draft is saved.
    r = _create(client, tenant, {**FULL_SPEC, "tools": ["final_result"], "approvals": None})
    assert r.status_code == 422 and "final_result" in json.dumps(r.json()["detail"])
    # Without output the draft saves, and publish refuses it for every spec.
    plain = {"name": "Plain", "model": {"provider": "test", "name": "echo"}, "tools": ["final_result"]}
    r = _publish(client, tenant, _agent(client, tenant, plain))
    assert r.status_code == 422
    assert r.json()["detail"] == "tool name 'final_result' is reserved for a platform tool"


def test_draft_run_applies_the_fallback_allowlist(client, tenant):
    spec = {**FULL_SPEC, "fallback_models": [{"provider": "litellm", "name": "nope"}]}
    agent_id = _agent(client, tenant, spec)
    r = client.post(
        f"/api/v1/agents/{agent_id}/runs",
        json={"run_id": str(uuid.uuid4()), "input": "hi", "version": "draft"},
        headers=_h(tenant),
    )
    assert r.status_code == 422 and r.json()["detail"].startswith("fallback_models[0]:")


# ------------------------------------------------------------------------------------ warnings


def _prices(tmp_path, monkeypatch, models: dict | None, raw: str | None = None) -> None:
    path = tmp_path / "prices.json"
    path.write_text(raw if raw is not None else json.dumps({"models": models}), encoding="utf-8")
    monkeypatch.setenv("EAP_MODEL_PRICES_FILE", str(path))


def test_cost_cap_warns_for_an_unpriced_fallback(client, tenant, tmp_path, monkeypatch):
    _prices(tmp_path, monkeypatch, {"test:structured": PERIOD})
    r = _publish(client, tenant, _agent(client, tenant, FULL_SPEC))
    assert r.status_code == 201, r.text
    [warning] = r.json()["warnings"]
    assert warning["code"] == "cost_cap_unpriced_model"
    assert warning["path"] == "fallback_models[0]"
    assert "'test:echo' has no price" in warning["message"]
    assert "limits.max_cost_usd" in warning["message"]


def test_cost_cap_no_warning_when_the_wildcard_prices_the_chain(client, tenant, tmp_path, monkeypatch):
    _prices(tmp_path, monkeypatch, {"test:*": PERIOD})
    assert _publish(client, tenant, _agent(client, tenant, FULL_SPEC)).json()["warnings"] == []


def test_cost_cap_warns_for_every_model_when_the_price_file_is_unusable(client, tenant, tmp_path, monkeypatch):
    _prices(tmp_path, monkeypatch, None, raw="{not json")
    warnings = _publish(client, tenant, _agent(client, tenant, FULL_SPEC)).json()["warnings"]
    assert [w["path"] for w in warnings] == ["model", "fallback_models[0]"]
    monkeypatch.setenv("EAP_MODEL_PRICES_FILE", str(tmp_path / "missing.json"))
    spec = AgentSpec.model_validate(FULL_SPEC)
    assert [w["path"] for w in publish_warnings(spec)] == ["model", "fallback_models[0]"]


def test_no_cost_warning_without_a_cost_cap(tmp_path, monkeypatch):
    _prices(tmp_path, monkeypatch, {})
    spec = AgentSpec.model_validate({**FULL_SPEC, "limits": {"max_requests": 3}})
    assert publish_warnings(spec) == []


def test_genai_priced_model_gets_no_warning(tmp_path, monkeypatch):
    # No override entry: openai:gpt-4 is priced by genai-prices (eap_pricing.price_known, the
    # worker's own lookup), so its requests count toward the cap.
    _prices(tmp_path, monkeypatch, {})
    assert _price_status("openai:gpt-4", date(2026, 11, 15)) == PRICE_SET
    spec = AgentSpec.model_validate({**FULL_SPEC, "model": {"provider": "openai", "name": "gpt-4"}, "fallback_models": []})
    assert publish_warnings(spec) == []


def test_unknown_direct_provider_model_is_warned_about(tmp_path, monkeypatch):
    # Neither the override file nor genai-prices knows it: the worker records no cost, so the cap
    # would never trip. Before eap_pricing the API could not tell, and said nothing.
    _prices(tmp_path, monkeypatch, {})
    unknown = "no-such-model-zz9"
    assert _price_status(f"openai:{unknown}", date(2026, 11, 15)) == PRICE_NONE
    spec = AgentSpec.model_validate({
        **FULL_SPEC, "model": {"provider": "openai", "name": "gpt-4"},
        "fallback_models": [{"provider": "openai", "name": unknown}],
    })
    [warning] = publish_warnings(spec)
    assert warning["code"] == "cost_cap_unpriced_model" and warning["path"] == "fallback_models[0]"
    assert f"'openai:{unknown}' has no price in the model price override file or genai-prices" in warning["message"]


def test_unusable_price_file_status(tmp_path, monkeypatch):
    _prices(tmp_path, monkeypatch, None, raw="{not json")
    assert _price_status("openai:gpt-4", date(2026, 11, 15)) == PRICE_UNUSABLE


def test_future_price_period_does_not_count(tmp_path, monkeypatch):
    _prices(tmp_path, monkeypatch, {"test:a": [{"effective_from": "2099-01-01", "input_per_mtok": "1", "output_per_mtok": "1"}]})
    assert _price_status("test:a", date(2026, 11, 15)) == PRICE_NONE


ZERO = [{"effective_from": "2026-01-01", "input_per_mtok": "0", "output_per_mtok": "0"}]


def test_cost_cap_warns_for_zero_priced_models(client, tenant, monkeypatch):
    # The repository default file prices test:* and litellm:* at $0: a cap would never trigger.
    monkeypatch.delenv("EAP_MODEL_PRICES_FILE")
    r = _publish(client, tenant, _agent(client, tenant, FULL_SPEC))
    assert r.status_code == 201, r.text
    warnings = r.json()["warnings"]
    assert [(w["code"], w["path"]) for w in warnings] == [
        ("cost_cap_unpriced_model", "model"), ("cost_cap_unpriced_model", "fallback_models[0]")
    ]
    assert warnings[0]["message"] == (
        "model: 'test:structured' is priced at $0, so its requests count as $0 toward limits.max_cost_usd, "
        "so the cost limit may not stop the run"
    )


def test_zero_price_lookup_follows_the_worker_rules(tmp_path, monkeypatch):
    day = date(2026, 11, 15)
    later_zero = [*PERIOD, {"effective_from": "2026-06-01", "input_per_mtok": "0", "output_per_mtok": "0.0"}]
    half_zero = [{"effective_from": "2026-01-01", "input_per_mtok": "0", "output_per_mtok": "3"}]
    future_zero = [*PERIOD, {"effective_from": "2099-01-01", "input_per_mtok": "0", "output_per_mtok": "0"}]
    _prices(tmp_path, monkeypatch, {
        "test:*": ZERO, "test:exact": PERIOD, "test:later": later_zero, "test:half": half_zero,
        "test:future": future_zero, "openai:gpt-4": ZERO,
    })
    assert _price_status("test:other", day) == PRICE_ZERO  # wildcard
    assert _price_status("test:exact", day) == PRICE_SET  # the exact key wins over a $0 wildcard
    assert _price_status("test:later", day) == PRICE_ZERO  # the latest period in effect
    assert _price_status("test:half", day) == PRICE_SET  # only input free: still priced
    assert _price_status("test:future", day) == PRICE_SET  # a future $0 period is not in effect
    # A $0 override wins over genai-prices in the worker, so a direct provider is warned about too.
    spec = AgentSpec.model_validate({**FULL_SPEC, "model": {"provider": "openai", "name": "gpt-4"}, "fallback_models": []})
    [warning] = publish_warnings(spec)
    assert warning["path"] == "model" and "priced at $0" in warning["message"]


def test_redaction_over_fixed_strings_warns(client, tenant):
    spec = {**FULL_SPEC, "output_processors": [{"type": "pii_redact", "config": {"action": "redact"}}]}
    warnings = _publish(client, tenant, _agent(client, tenant, spec)).json()["warnings"]
    [warning] = [w for w in warnings if w["code"] == "redaction_may_break_output_schema"]
    assert warning["path"] == "output_processors[0]"
    assert "category" in warning["message"]


def test_blocking_pii_or_no_fixed_strings_does_not_warn():
    block = AgentSpec.model_validate(
        {**FULL_SPEC, "limits": None, "output_processors": [{"type": "pii_redact", "config": {"action": "block"}}]}
    )
    assert publish_warnings(block) == []
    free = {"type": "object", "properties": {"s": {"type": "string"}}}
    redact = AgentSpec.model_validate(
        {**FULL_SPEC, "limits": None, "output": {"json_schema": free}, "output_processors": [{"type": "pii_redact"}]}
    )
    assert publish_warnings(redact) == []


def test_fixed_string_paths_walks_nested_objects_and_items():
    schema = {
        "type": "object",
        "properties": {
            "a": {"type": "object", "properties": {"b": {"type": "string", "const": "x"}}},
            "tags": {"type": "array", "items": {"type": "string", "enum": ["p", "q"]}},
            "n": {"type": "integer", "enum": [1, 2]},
        },
    }
    assert _fixed_string_paths(schema) == ["a.b", "tags[]"]


def test_restore_carries_warnings(client, tenant, tmp_path, monkeypatch):
    agent_id = _agent(client, tenant, FULL_SPEC)
    assert _publish(client, tenant, agent_id).status_code == 201
    r = client.patch(f"/api/v1/agents/{agent_id}", json={"spec": {**FULL_SPEC, "instructions": "v2"}}, headers=_h(tenant))
    assert r.status_code == 200
    assert _publish(client, tenant, agent_id).status_code == 201
    _prices(tmp_path, monkeypatch, {})
    r = client.post(f"/api/v1/agents/{agent_id}/versions/1/restore", headers=_h(tenant))
    assert r.status_code == 201, r.text
    assert [w["path"] for w in r.json()["warnings"]] == ["model", "fallback_models[0]"]


# ------------------------------------------------------------------------------- version diff


def test_compare_shows_an_added_output_property(client, tenant):
    agent_id = _agent(client, tenant, FULL_SPEC)
    assert _publish(client, tenant, agent_id).status_code == 201
    schema = json.loads(json.dumps(SCHEMA))
    schema["properties"]["x"] = {"type": "integer"}
    r = client.patch(f"/api/v1/agents/{agent_id}", json={"spec": {**FULL_SPEC, "output": {"json_schema": schema}}}, headers=_h(tenant))
    assert r.status_code == 200
    body = client.get(f"/api/v1/agents/{agent_id}/versions/compare?base=1&target=draft", headers=_h(tenant)).json()
    changes = {c["path"]: c for c in body["changes"]}
    assert changes["output.json_schema.properties.x"] == {
        "path": "output.json_schema.properties.x", "kind": "added", "before": None, "after": {"type": "integer"}
    }
    assert changes["output.mode"]["kind"] == "removed"
    assert changes["output.max_retries"]["kind"] == "removed"
    assert body["identical"] is False


def test_compare_covers_limits_fallbacks_and_approvals():
    base = {"name": "a", "model": {"provider": "test", "name": "echo"}, "tools": ["tickets.create"]}
    target = {
        **base,
        "approvals": {"tools": ["tickets.create"]},
        "limits": {"max_cost_usd": "0.50"},
        "fallback_models": [{"provider": "test", "name": "b"}],
    }
    paths = {c["path"]: c["kind"] for c in compare_specs(base, target)["changes"]}
    assert paths == {"approvals": "added", "limits": "added", "fallback_models": "added"}
    # Equal amounts written differently are no change (the spec normalises max_cost_usd).
    same = {**target, "limits": {"max_cost_usd": "0.5"}}
    assert compare_specs(target, same)["changes"] == []
    # A second fallback is one added list item.
    more = {**target, "fallback_models": [*target["fallback_models"], {"provider": "test", "name": "c"}]}
    assert [(c["path"], c["kind"]) for c in compare_specs(target, more)["changes"]] == [("fallback_models[1]", "added")]


# --------------------------------------------------------------------------------- output_json


def _finish(app, run_id: str, output: str, output_format: str | None) -> None:
    with app.state.database_engine.begin() as conn:
        conn.execute(
            text("UPDATE runs SET status = 'completed', output = :o, output_format = :f WHERE id = :r"),
            {"o": output, "f": output_format, "r": run_id},
        )


def test_output_json_only_for_json_runs(pg, tenant_of):
    client, app = pg
    _, h = tenant_of()
    agent_id = make_agent(client, h)
    json_run, text_run, queued_run = (str(uuid.uuid4()) for _ in range(3))
    for run_id in (json_run, text_run, queued_run):
        assert start(client, h, agent_id, run_id).status_code == 201
    _finish(app, json_run, '{"category":"billing","summary":"x"}', "json")
    _finish(app, text_run, "plain text", "text")

    got = client.get(f"/api/v1/runs/{json_run}", headers=h).json()
    assert got["output"] == '{"category":"billing","summary":"x"}'
    assert got["output_json"] == {"category": "billing", "summary": "x"}
    assert "output_json" not in client.get(f"/api/v1/runs/{text_run}", headers=h).json()
    assert "output_json" not in client.get(f"/api/v1/runs/{queued_run}", headers=h).json()

    items = {i["run_id"]: i for i in client.get("/api/v1/runs", headers=h).json()["items"]}
    assert items[json_run]["output_json"] == {"category": "billing", "summary": "x"}
    assert "output_json" not in items[text_run]


def test_turn_responses_carry_output_json(pg, tenant_of):
    client, app = pg
    _, h = tenant_of()
    agent_id = make_agent(client, h)
    conv = client.post(f"/api/v1/agents/{agent_id}/conversations", json={}, headers=h).json()
    run_id = str(uuid.uuid4())
    r = client.post(f"/api/v1/conversations/{conv['id']}/runs", json={"run_id": run_id, "input": "hi"}, headers=h)
    assert r.status_code == 201, r.text
    _finish(app, run_id, '{"a":[1,2]}', "json")
    [turn] = client.get(f"/api/v1/conversations/{conv['id']}", headers=h).json()["runs"]
    assert turn["output_json"] == {"a": [1, 2]}


def test_output_json_is_tenant_scoped(pg, tenant_of):
    client, app = pg
    _, h = tenant_of()
    _, other = tenant_of()
    agent_id = make_agent(client, h)
    run_id = str(uuid.uuid4())
    assert start(client, h, agent_id, run_id).status_code == 201
    _finish(app, run_id, '{"secret":"s"}', "json")
    assert client.get(f"/api/v1/runs/{run_id}", headers=other).status_code == 404
    assert client.get("/api/v1/runs", headers=other).json()["items"] == []


def test_unparsable_json_output_is_null():
    assert _output_json({"id": "r", "output_format": "json", "output": "{nope"}) == {"output_json": None}
    deep = "[" * 100_000 + "]" * 100_000  # past the JSON parser's recursion limit
    assert _output_json({"id": "r", "output_format": "json", "output": deep}) == {"output_json": None}
    assert _output_json({"output_format": "json", "output": None}) == {}
    assert _output_json({"output_format": None, "output": "{}"}) == {}


def test_list_runs_accepts_kind_eval(pg, tenant_of):
    client, _ = pg
    _, h = tenant_of()
    make_agent(client, h)
    r = client.get("/api/v1/runs?kind=eval", headers=h)
    assert r.status_code == 200 and r.json() == {"items": [], "next_before": None}
    assert client.get("/api/v1/runs?kind=nope", headers=h).status_code == 422
