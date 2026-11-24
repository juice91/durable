"""System context and provenance (system-aware Architect, phase C), API side: the layers a principal gets
(`GET /system-context`, MCP `system.context`), tenant isolation, no secrets, the IAM filter call, degraded layers,
and the provenance rows the Architect's run records (`provenance.record`, `provenance.get`, `GET .../provenance`)."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import pytest

from eap_run_store import EventLog
from eap_schemas.system_context import ContextLayers, render_block
from eap_iam import Only
from eap_api.services.capabilities import ARCHITECT_RUN, CapabilityRegistryService, Viewer
from eap_api.services.capability_probes import ProbeRegistry, TtlCache
from eap_api.services.system_context import SystemContextService, TenantReads

from .mcp_control_helpers import AGENT_SPEC, secrets_key, world  # noqa: F401 - fixtures
from .run_helpers import requires_pg
from .test_architect_control_seam import cleanup_agents, seed_architect, token  # noqa: F401 - fixture

pytestmark = requires_pg

CANARY = "CANARY-7f3a91-do-not-leak"
SECRET_VALUE = "hunter2-very-secret-value"


@pytest.fixture(autouse=True)
def clean_provenance(world):  # noqa: F811
    yield
    for t in world.tenants:
        world.sql("DELETE FROM definition_provenance WHERE tenant_id = :t", t=t)
        world.sql("DELETE FROM runs WHERE tenant_id = :t", t=t)
        world.sql("DELETE FROM agent_versions WHERE tenant_id = :t", t=t)
        world.sql("DELETE FROM agents WHERE tenant_id = :t", t=t)
        world.sql("DELETE FROM tenant_secrets WHERE tenant_id = :t", t=t)


@pytest.fixture
def events_cleanup(world, cleanup_agents):  # noqa: F811
    """Torn down before `cleanup_agents` deletes the runs: the events reference them."""
    yield
    for t in world.tenants:
        world.sql("DELETE FROM execution_events WHERE tenant_id = :t", t=t)
        world.sql("DELETE FROM agent_versions WHERE tenant_id = :t", t=t)


def make_agent(world, headers, name: str, description: str | None = None, **extra: Any) -> str:  # noqa: F811
    spec = {**AGENT_SPEC, "name": name, **({"instructions": description} if description else {}), **extra}  # free text: never in the block
    with world.rest() as c:
        r = c.post("/agents", json={"spec": spec}, headers=headers)
        assert r.status_code == 201, r.text
        return r.json()["id"]


def put_secret(world, headers, name: str, value: str) -> None:  # noqa: F811
    with world.rest() as c:
        assert c.put(f"/secrets/{name}", json={"value": value}, headers=headers).status_code == 200


def tenant_context(world, key: str) -> dict[str, Any]:  # noqa: F811
    reply = world.call(key, "system.context")
    assert not reply.is_error, reply.data
    return reply.data


# ------------------------------------------------------------------------------------------- the layers


def test_context_has_names_counts_gaps_and_is_the_same_over_rest_and_mcp(world):  # noqa: F811
    tenant, h = world.tenant_of()
    make_agent(world, h, "Support triage", CANARY)
    put_secret(world, h, "slack-token", SECRET_VALUE)
    key = world.make_key(h)
    data = tenant_context(world, key)
    assert data["degraded"] == [] and len(data["snapshot_hash"]) == 64 and len(data["registry_version"]) == 64
    assert [a["name"] for a in data["tenant"]["agents"]] == ["Support triage"]
    assert data["tenant"]["secrets"] == ["slack-token"] and data["tenant"]["counts"]["secrets"] == 1
    assert any(g["id"] == "gap.inbound_webhook" for g in data["known_gaps"])
    assert any(c["id"] == "trigger.schedule" and c["status"] == "available" for c in data["capabilities"])
    assert "architect.publish_agent" in data["policies"]["approvals"]
    with world.rest() as c:
        rest = c.get("/system-context", headers=h)
    assert rest.status_code == 200
    assert rest.json()["tenant"] == data["tenant"] and rest.json()["known_gaps"] == data["known_gaps"]


def test_no_secret_value_key_or_tenant_description_reaches_the_context(world):  # noqa: F811
    tenant, h = world.tenant_of()
    make_agent(world, h, "Triage", CANARY)
    put_secret(world, h, "slack-token", SECRET_VALUE)
    key = world.make_key(h)
    data = tenant_context(world, key)
    block = render_block("platform", "v", ContextLayers.model_validate(data)).text
    for haystack in (json.dumps(data), block):
        assert CANARY not in haystack and SECRET_VALUE not in haystack and key not in haystack
        assert "pos_" not in haystack
    assert "slack-token" in block  # names are shown


def test_tenant_isolation_no_name_id_or_count_crosses(world):  # noqa: F811
    a, ha = world.tenant_of("alice")
    b, hb = world.tenant_of("bob")
    a_agent = make_agent(world, ha, "Alpha-only-agent")
    put_secret(world, ha, "alpha-secret", "v1")
    b_agent = make_agent(world, hb, "Bravo-only-agent")
    ka, kb = world.make_key(ha), world.make_key(hb)
    for _ in range(2):  # the second round is served from the caches
        data_a, data_b = tenant_context(world, ka), tenant_context(world, kb)
        assert [x["name"] for x in data_a["tenant"]["agents"]] == ["Alpha-only-agent"]
        assert [x["name"] for x in data_b["tenant"]["agents"]] == ["Bravo-only-agent"]
        assert data_a["tenant"]["secrets"] == ["alpha-secret"] and data_b["tenant"]["secrets"] == []
        for mine, theirs in ((data_a, (b, b_agent, "Bravo")), (data_b, (a, a_agent, "Alpha", "alpha-secret"))):
            text = json.dumps(mine)
            assert not any(token_ in text for token_ in theirs)
        assert data_b["tenant"]["counts"]["agents"] == 1 and data_b["tenant"]["counts"]["secrets"] == 0


def test_a_tenant_with_500_agents_reports_the_count_and_at_most_ten_lines(world):  # noqa: F811
    tenant, h = world.tenant_of()
    world.sql(
        "INSERT INTO agents (id, tenant_id, name, draft_spec, archived, created_at, updated_at, builtin) "
        "SELECT md5(random()::text || g::text), :t, 'bulk-agent-' || g, NULL, false, now(), now(), false "
        "FROM generate_series(1, 500) g", t=tenant,
    )
    data = tenant_context(world, world.make_key(h))
    assert data["tenant"]["counts"]["agents"] == 500 and len(data["tenant"]["agents"]) == 10
    text = render_block("p", "v", ContextLayers.model_validate(data)).text
    assert "agents 500" in text and "490 more: use platform.list_*" in text


# --------------------------------------------------------------------------- degraded layers and IAM calls


def reads(**over) -> TenantReads:
    base = dict(
        agents=lambda t: {"total": 3, "items": [{"id": f"a{i}", "name": f"Agent {i}"} for i in range(3)]},
        workflows=lambda t: {"total": 1, "items": [{"id": "w1", "name": "Weekly"}]},
        knowledge_bases=lambda t: {"total": 1, "items": [{"id": "k1", "name": "Docs"}]},
        schedules=lambda t: {"items": []}, connections=lambda t: {"items": []}, secret_names=lambda t: ["tok"],
        failures=lambda t: {"items": []},
    )
    base.update(over)
    return TenantReads(**base)


def registry(raises: bool = False) -> CapabilityRegistryService:
    service = CapabilityRegistryService(ProbeRegistry(TtlCache()), lambda: ())
    if raises:
        async def boom(viewer):
            raise RuntimeError("registry down")

        service.snapshot = boom  # type: ignore[method-assign]
    return service


VIEWER = Viewer("t1", ARCHITECT_RUN, "architect-run:r1")


def test_a_failing_registry_degrades_its_layers_and_the_rest_still_builds():
    layers = asyncio.run(SystemContextService(registry(raises=True), reads()).build(VIEWER))
    assert set(layers.degraded) == {"capability", "known_gaps"}
    assert layers.capabilities is None and layers.known_gaps is None
    assert layers.tenant is not None and layers.policies is not None
    block = render_block("p", "v", layers).text
    assert "do not claim any capability is present" in block


def test_a_failing_tenant_read_degrades_only_the_tenant_layer():
    def boom(t):
        raise RuntimeError("db down")

    layers = asyncio.run(SystemContextService(registry(), reads(agents=boom)).build(VIEWER))
    assert layers.degraded == ("tenant",) and layers.capabilities is not None


class FakeAuthz:
    """Stands in for AuthzService: records the calls and lets only one agent through."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def filter_ids(self, principal, action, kind, candidate_ids=None):
        self.calls.append((principal.kind, action, kind))
        return Only(("a1",)) if kind == "agent" else __import__("eap_iam").All()


def test_the_tenant_layer_is_filtered_through_the_policy_engine_once_per_kind():
    authz = FakeAuthz()
    layers = asyncio.run(SystemContextService(registry(), reads(), authz).build(VIEWER))  # type: ignore[arg-type]
    assert [a.id for a in layers.tenant.agents] == ["a1"] and layers.tenant.counts["agents"] == 1
    kinds = [kind for _, _, kind in authz.calls]
    assert sorted(set(kinds)) == sorted(kinds), "one call per kind, never per row"
    assert {"agent", "workflow", "knowledge_base", "secret"} <= set(kinds)  # a kind with nothing to filter makes no call
    assert all(action == "read" and principal_kind == "run" for principal_kind, action, _ in authz.calls)


def test_the_snapshot_copy_is_stored_content_addressed_without_text():
    stored: dict[str, Any] = {}
    layers = asyncio.run(SystemContextService(registry(), reads(), None, lambda h, body: stored.setdefault(h, body)).build(VIEWER))
    body = stored[layers.snapshot_hash]
    assert body["trigger.schedule"]["s"] == "available" and set(body["trigger.schedule"]) == {"s", "h"}


# ----------------------------------------------------------------------------------------------- provenance


def record_events(world, tenant: str, run_id: str, snapshot_hash: str) -> None:  # noqa: F811
    log = EventLog(world.app.state.database_engine)
    log.append(tenant, uuid.UUID(run_id), "system_context", "system_context_loaded", {
        "platform_version": "0.1.0+test", "manifest_version": 2, "manifest_hash": "m" * 64, "registry_version": "r" * 64,
        "capability_snapshot_hash": snapshot_hash, "principal": {"tenant_id": tenant, "subject": f"architect-run:{run_id}", "kind": "architect_run", "roles": []},
        "degraded": [], "truncated": ["capability"],
    })
    log.append(tenant, uuid.UUID(run_id), "tool:c1:call", "tool_call",
               {"tool_call_id": "c1", "tool_name": "architect__list_capabilities", "args": {"query": SECRET_VALUE}})
    log.append(tenant, uuid.UUID(run_id), "tool:c1:result", "tool_result", {"tool_call_id": "c1", "status": "ok", "content": "x"})


def test_provenance_rows_for_a_draft_and_a_publish_card_and_the_changed_notice(world, secrets_key, events_cleanup, monkeypatch):  # noqa: F811
    tenant, h = world.tenant_of()
    run_id = seed_architect(world, tenant)
    bearer = token(secrets_key, tenant, run_id)
    built = world.call(bearer, "system.context")  # as the run's start does: stores the snapshot copy
    assert not built.is_error, built.data
    record_events(world, tenant, run_id, built.data["snapshot_hash"])
    agent_id = make_agent(world, h, "Reporter", tools=["http.request"])

    draft = world.call(bearer, "provenance.record", {"kind": "agent", "definition_id": agent_id})
    assert not draft.is_error, draft.data
    assert draft.data["capability_snapshot_hash"] == built.data["snapshot_hash"] and draft.data["manifest_hash"] == "m" * 64
    assert draft.data["platform_version"] == "0.1.0+test" and draft.data["registry_version"] == "r" * 64
    assert draft.data["tool_calls"][0]["name"] == "architect__list_capabilities" and draft.data["tool_calls"][0]["outcome"] == "ok"
    assert SECRET_VALUE not in json.dumps(draft.data) and "args" not in draft.data["tool_calls"][0]
    assert draft.data["card"] == "draft" and len(draft.data["spec_hash"]) == 64 and draft.data["run_id"] == run_id

    with world.rest() as c:
        assert c.post(f"/agents/{agent_id}/versions", headers=h).status_code == 201
    card = world.call(bearer, "provenance.record", {"kind": "agent", "definition_id": agent_id, "card": "publish", "version": 1})
    assert not card.is_error, card.data
    assert card.data["version"] == 1 and card.data["card"] == "publish" and card.data["spec_hash"] == draft.data["spec_hash"]

    with world.rest() as c:
        got = c.get(f"/definitions/agent/{agent_id}/provenance", headers=h)
        assert got.status_code == 200
        body = got.json()
        assert [i["card"] for i in body["items"]] == ["publish", "draft"] and body["capabilities_changed"] == []
        only = c.get(f"/definitions/agent/{agent_id}/provenance", params={"spec_hash": draft.data["spec_hash"]}, headers=h).json()
        assert len(only["items"]) == 2
        # The platform changes: the tool the draft uses is no longer allowed.
        monkeypatch.setenv("EAP_ALLOWED_TOOLS", "clock.now")
        from eap_api.services.capability_probes import SHARED_CACHE

        SHARED_CACHE.invalidate()
        changed = c.get(f"/definitions/agent/{agent_id}/provenance", headers=h).json()
    assert changed["capabilities_changed"] == [{"id": "tool.http.request", "was": "available", "now": "disabled"}]
    mcp = world.call(world.make_key(h), "provenance.get", {"kind": "agent", "definition_id": agent_id})
    assert not mcp.is_error and len(mcp.data["items"]) == 2


def test_only_the_architects_run_records_and_another_tenant_cannot_read(world, secrets_key, cleanup_agents):  # noqa: F811
    tenant, h = world.tenant_of("alice")
    other, ho = world.tenant_of("mallory")
    agent_id = make_agent(world, h, "Mine")
    refused = world.call(world.make_key(h), "provenance.record", {"kind": "agent", "definition_id": agent_id})
    assert refused.is_error and refused.error["code"] in ("architect_only", "scope_required")
    manage = world.call(world.make_key(h, scopes=["mcp", "manage"]), "provenance.record", {"kind": "agent", "definition_id": agent_id})
    assert manage.is_error and manage.error["code"] == "architect_only"
    with world.rest() as c:
        assert c.get(f"/definitions/agent/{agent_id}/provenance", headers=ho).status_code == 404
        assert c.get(f"/definitions/nope/{agent_id}/provenance", headers=h).status_code in (400, 404, 422)
    run_id = seed_architect(world, other)
    stranger = world.call(token(secrets_key, other, run_id), "provenance.record", {"kind": "agent", "definition_id": agent_id})
    assert stranger.is_error and stranger.error["code"] == "not_found"


def test_provenance_record_is_hidden_from_the_architects_model_but_callable(world, secrets_key, cleanup_agents):  # noqa: F811
    tenant, _ = world.tenant_of()
    run_id = seed_architect(world, tenant)
    bearer = token(secrets_key, tenant, run_id)

    async def tags():
        async with world.session(bearer) as client:
            return {t.name: set((t.meta or {}).get("fastmcp", {}).get("tags", ())) for t in await client.list_tools()}

    listed = asyncio.run(tags())
    assert "internal" in listed["provenance.record"] and "manage" in listed["provenance.record"]
    assert "internal" not in listed["system.context"]
    reply = world.call(bearer, "capability.list", {"kind": "control_tool"})
    assert "control.provenance.record" not in {e["id"] for e in reply.data["entries"]}  # hidden from the Architect's registry view


def test_openapi_has_only_the_new_routes(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/v1/system-context" in paths and "/api/v1/definitions/{kind}/{definition_id}/provenance" in paths
    assert set(paths["/api/v1/system-context"]) == {"get"}
    assert set(paths["/api/v1/definitions/{kind}/{definition_id}/provenance"]) == {"get"}
