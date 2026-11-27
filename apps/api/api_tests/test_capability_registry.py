"""The capability registry (system-aware Architect, phase A): the inventory covers the code, gaps are generated,
probes only change status, the read API answers ETag/304 and is tenant-scoped, and MCP clients read the same truth."""

from __future__ import annotations

import asyncio
import pathlib
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from eap_api import control_catalog
from eap_api.app import create_app
from eap_api.mcp_control.server import build_server
from eap_api.routes.mcp_servers import get_outbound_policy
from eap_api.service_deps import capabilities as cap_deps
from eap_api.services.capabilities import ARCHITECT_RUN, CapabilityRegistryService, Viewer
from eap_api.services.capability_probes import (
    ProbeContext,
    ProbeOutcome,
    ProbeRegistry,
    ProbeResult,
    TtlCache,
    mcp_probe,
    merge,
    provider_probe,
    tool_allowlist_probe,
)
from eap_api.services.errors import InvalidInput, NotFound
from eap_schemas.capabilities import (
    CapabilityEntry,
    ControlToolDecl,
    approved_tool_ids,
    architect_step_kinds,
    collect_declared,
    platform_step_kinds,
)
from eap_schemas.capability_gaps import GAPS, designed_in_problem, known_gaps
from eap_schemas.tools import HIDDEN_TOOL_DECLARATIONS, TOOL_DECLARATIONS
from eap_secrets import KEY_ENV, OLD_KEYS_ENV, generate_key
from eap_secrets.outbound import DEV_ALLOW_HOSTS_ENV, OutboundPolicy

from .conftest import get_auth_header
from .mcp_control_helpers import secrets_key as _secrets_key  # noqa: F401 - fixture
from .mcp_control_helpers import world  # noqa: F401 - fixture
from .run_helpers import PG_URL, requires_pg

REPO = pathlib.Path(__file__).resolve().parents[3]
BASE = "/api/v1/capabilities"
VIEWER = Viewer("t1", "user", "u1")


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def service(
    *probes: tuple[str, Any], controls: tuple[ControlToolDecl, ...] = (), cache: TtlCache | None = None
) -> CapabilityRegistryService:
    registry = ProbeRegistry(cache or TtlCache())
    for name, probe in probes:
        registry.register(name, probe)
    return CapabilityRegistryService(registry, lambda: controls)


# ------------------------------------------------------------------------------------------- inventory


def test_every_tool_declaration_and_step_kind_has_an_entry():
    ids = {e.id for e in collect_declared()}
    for tool_id in (*TOOL_DECLARATIONS, *HIDDEN_TOOL_DECLARATIONS):
        assert f"tool.{tool_id}" in ids, tool_id
    for kind in platform_step_kinds():
        assert f"step.{kind}" in ids, kind
    assert set(architect_step_kinds()) <= set(platform_step_kinds())


def test_every_mcp_control_tool_has_an_entry():
    build_server()  # registers every @tool; control_catalog records them
    decls = control_catalog.declared()
    assert len(decls) >= 40
    ids = {e.id for e in collect_declared(decls)}
    for decl in decls:
        assert f"control.{decl.name}" in ids, decl.name
    assert {"capability.list", "capability.get"} <= {d.name for d in decls}
    assert {d.name for d in decls if d.human_only} >= {"approvals.decide", "agent.chat"}


def test_the_declared_entries_have_unique_ids_and_existing_docs():
    entries = collect_declared()
    assert len({e.id for e in entries}) == len(entries)
    for entry in entries:
        if entry.docs_ref:
            assert (REPO / entry.docs_ref).is_file(), (entry.id, entry.docs_ref)
        assert len(entry.description) <= 300


def test_the_named_entries_and_their_status():
    by_id = {e.id: e for e in collect_declared()}
    assert by_id["trigger.schedule"].status == "available"
    foreach = by_id["step.foreach"]
    assert foreach.status == "available" and foreach.writable_by_architect is False
    assert by_id["step.branch"].writable_by_architect is True
    assert by_id["tool.workflow.history"].status == "available" and by_id["tool.workflow.history"].scope == "workflow_only"
    assert by_id["tool_option.text.check.verbatim_exempt"].status == "available"
    assert by_id["feature.agent_tools"].status == "available"
    assert by_id["step.foreach"].limits["max_steps"] == 50
    assert by_id["tool.http.request"].requires_approval is True
    assert by_id["tool.clock.now"].requires_approval is False


def test_architect_format_matches_the_authoring_format():
    assert set(architect_step_kinds()) == {"agent", "tool", "parallel", "branch"}


# ----------------------------------------------------------------------------------------------- gaps


def test_gaps_are_generated_from_the_registry():
    entries = {e.id: e for e in collect_declared()}
    gaps = known_gaps()
    assert [g.id for g in gaps] == sorted(g.id for g in GAPS)
    not_available = {e.id for e in entries.values() if e.status != "available"}
    not_writable = {e.id for e in entries.values() if e.kind == "step" and e.status == "available" and not e.writable_by_architect}
    in_gaps = {i for g in gaps for i in g.entry_ids}
    assert not_available <= in_gaps and not_writable <= in_gaps  # every non-available entry is explained by a gap
    assert in_gaps <= set(entries)  # and every gap points at a real entry
    # Inbound webhooks shipped: the gap is gone and the entry is a real, available declaration (wait_event is still a gap).
    assert "gap.inbound_webhook" not in {g.id for g in gaps}
    assert entries["trigger.webhook"].status == "available" and entries["trigger.webhook"].designed_in is None
    wait = next(g for g in gaps if g.id == "gap.wait_event_step")
    assert wait.status == "designed_not_built" and entries["step.wait_event"].status == "designed_not_built"


def test_every_designed_in_is_a_file_on_main_or_a_pr_reference():
    for gap in GAPS:
        if gap.status == "designed_not_built":
            assert designed_in_problem(gap.designed_in, lambda p: (REPO / p).is_file()) is None, gap.id
    assert designed_in_problem("docs/nope.md", lambda p: False) == "designed_in 'docs/nope.md' is not a file on main"
    assert designed_in_problem("pr:111", lambda p: False) is None
    assert designed_in_problem("../etc/passwd", lambda p: True) is not None
    assert designed_in_problem(None, lambda p: True) == "designed_in is missing"
    # The designs that are only pull requests today are referenced as such (wait_event: 111, A2A: 100).
    assert {g.id: g.designed_in for g in GAPS}["gap.wait_event_step"] == "pr:111"
    assert {g.id: g.designed_in for g in GAPS}["gap.a2a_agent"] == "pr:100"


# ---------------------------------------------------------------------------------------------- probes


def test_a_probe_cannot_add_a_builtin_or_switch_on_a_design():
    declared = collect_declared()
    fake = CapabilityEntry(id="tool.made.up", kind="tool", title="x", description="x")
    outcome = ProbeOutcome(
        overrides={"tool.clock.now": ("disabled", "off"), "step.wait_event": ("available", None), "tool.nope": ("disabled", "x")},
        extra=(fake, CapabilityEntry(id="model.x", kind="model", title="x", description="x"),
               CapabilityEntry(id="tool.clock.now", kind="model", title="shadow", description="x")),
        degraded=(),
    )
    merged = {e.id: e for e in merge(declared, outcome)}
    assert "tool.made.up" not in merged and "tool.nope" not in merged
    assert merged["tool.clock.now"].status == "disabled" and merged["tool.clock.now"].kind == "tool"
    assert merged["step.wait_event"].status == "designed_not_built"
    assert "model.x" in merged


def test_a_failing_probe_is_degraded_not_fatal():
    async def boom(ctx: ProbeContext) -> ProbeResult:
        raise RuntimeError("down")

    listing = run(service(("boom", boom)).list(VIEWER))
    assert listing["degraded"] == ["boom"] and listing["total"] > 50


def test_the_tool_allowlist_probe_disables_a_tool_outside_the_deployment_allowlist():
    svc = service(("tools", tool_allowlist_probe(lambda: frozenset({"clock.now"}))))
    got = run(svc.get(VIEWER, "tool.http.request"))["entry"]
    assert got["status"] == "disabled" and "EAP_ALLOWED_TOOLS" in got["status_reason"]
    assert run(svc.get(VIEWER, "tool.clock.now"))["entry"]["status"] == "available"
    assert run(svc.get(VIEWER, "tool.architect.save_agent"))["entry"]["status"] == "available"  # the built-in Architect's own


def provider(id_: str, *, configured: bool = True, status: str = "ok", error: str | None = None) -> Any:
    return SimpleNamespace(id=id_, configured=configured, status=status, error=error)


def test_a_provider_without_credentials_is_disabled_with_a_reason():
    items = [
        {"id": "anthropic:claude-x", "provider": "anthropic", "name": "claude-x", "wildcard": False},
        {"id": "test:echo", "provider": "test", "name": "echo", "wildcard": False},
    ]
    probe = provider_probe(
        lambda: [provider("anthropic", configured=False, status="not_configured"), provider("litellm", status="unreachable", error="timeout")],
        lambda: items, lambda: frozenset({"anthropic", "litellm", "test"}))
    svc = service(("providers", probe))
    anthropic = run(svc.get(VIEWER, "model.anthropic"))["entry"]
    assert anthropic["status"] == "disabled" and "credentials" in anthropic["status_reason"]
    assert run(svc.get(VIEWER, "model.anthropic:claude-x"))["entry"]["status"] == "disabled"
    assert "timeout" in run(svc.get(VIEWER, "model.litellm"))["entry"]["status_reason"]
    assert run(svc.get(VIEWER, "model.test:echo"))["entry"]["status"] == "available"


def mcp_rows(*names: str, **extra: Any) -> list[dict[str, Any]]:
    return [{"id": f"id-{n}", "name": n, "enabled": True, "runtime": "external", "revision": 1, **extra} for n in names]


def test_an_mcp_server_that_does_not_answer_yields_no_tool_entries():
    async def lister(tenant: str, server_id: str) -> dict[str, Any]:
        if server_id == "id-good":
            return {"status": "ok", "tools": [{"name": "search"}, {"name": "fetch"}]}
        return {"status": "error", "error_code": "timeout", "tools": []}

    svc = service(("mcp", mcp_probe(lambda t: mcp_rows("good", "silent"), lister, TtlCache())))
    listing = run(svc.list(VIEWER, kind="mcp_tool"))
    assert [e["id"] for e in listing["entries"]] == ["mcp_tool.good/fetch", "mcp_tool.good/search"]
    assert all(e["requires_approval"] for e in listing["entries"])
    silent = run(svc.get(VIEWER, "mcp_server.silent"))["entry"]
    assert silent["status"] == "disabled" and "timeout" in silent["status_reason"]
    assert run(svc.get(VIEWER, "mcp_server.good"))["entry"]["limits"] == {"tools": 2}


def test_an_idle_managed_runtime_is_not_started_by_a_read():
    asked: list[str] = []

    async def lister(tenant: str, server_id: str) -> dict[str, Any]:
        asked.append(server_id)
        return {"status": "ok", "tools": []}

    rows = mcp_rows("idle", runtime="managed", runtime_state="idle") + mcp_rows("off", enabled=False)
    svc = service(("mcp", mcp_probe(lambda t: rows, lister, TtlCache())))
    idle = run(svc.get(VIEWER, "mcp_server.idle"))["entry"]
    assert idle["status"] == "available" and "starts on first use" in idle["status_reason"]
    assert run(svc.get(VIEWER, "mcp_server.off"))["entry"]["status"] == "disabled"
    assert asked == []


def test_tools_list_results_are_cached_per_server_revision():
    calls: list[str] = []

    async def lister(tenant: str, server_id: str) -> dict[str, Any]:
        calls.append(server_id)
        return {"status": "ok", "tools": [{"name": "t"}]}

    cache = TtlCache()
    rows = mcp_rows("a")
    svc = service(("mcp", mcp_probe(lambda t: rows, lister, cache)), cache=cache)
    run(svc.list(VIEWER))
    run(svc.list(VIEWER))
    assert calls == ["id-a"]
    rows[0]["revision"] = 2  # the server changed: asked again
    run(svc.list(VIEWER))
    assert calls == ["id-a", "id-a"]
    svc.invalidate("t1")
    run(svc.list(VIEWER))
    assert len(calls) == 3


# --------------------------------------------------------------------------------- the service's reads


def test_list_filters_and_refuses_unknown_values():
    svc = service()
    steps = run(svc.list(VIEWER, kind="step"))
    assert {e["id"] for e in steps["entries"]} >= {"step.agent", "step.foreach", "step.wait_event"}
    assert all("input_schema" not in e for e in steps["entries"])
    designed = run(svc.list(VIEWER, status="designed_not_built"))
    assert {e["id"] for e in designed["entries"]} == {"step.wait_event", "step.a2a_agent", "feature.iam_per_user"}
    assert [g["id"] for g in designed["known_gaps"]] == [g.id for g in known_gaps()]
    hit = run(svc.list(VIEWER, query="text check verbatim"))
    assert "tool_option.text.check.verbatim_exempt" in {e["id"] for e in hit["entries"]}
    with_schema = run(svc.list(VIEWER, kind="tool", include_schemas=True, query="http.request"))
    assert any(e.get("input_schema") for e in with_schema["entries"])
    with pytest.raises(InvalidInput) as bad:
        run(svc.list(VIEWER, kind="nope"))
    assert bad.value.code == "invalid_kind"
    with pytest.raises(InvalidInput):
        run(svc.list(VIEWER, status="maybe"))


def test_get_returns_schemas_and_the_nearest_ids_for_a_typo():
    svc = service()
    got = run(svc.get(VIEWER, "tool.http.request"))
    assert "url" in got["entry"]["input_schema"]["properties"] and got["entry"]["requires_approval"] is True
    foreach = run(svc.get(VIEWER, "step.foreach"))
    assert foreach["gaps"][0]["id"] == "gap.architect_foreach_loop"
    with pytest.raises(NotFound) as missing:
        run(svc.get(VIEWER, "tool.http.reqest"))
    assert missing.value.code == "unknown_capability" and "tool.http.request" in missing.value.message


def test_the_policy_is_pluggable_and_the_default_hides_human_only_control_tools_from_the_architect():
    controls = (ControlToolDecl(name="agent.list"), ControlToolDecl(name="approvals.decide", manage=True, human_only=True))
    svc = service(controls=controls)
    person = run(svc.list(VIEWER, kind="control_tool"))
    assert {e["id"] for e in person["entries"]} == {"control.agent.list", "control.approvals.decide"}
    architect = run(svc.list(Viewer("t1", ARCHITECT_RUN, "architect-run:x"), kind="control_tool"))
    assert {e["id"] for e in architect["entries"]} == {"control.agent.list"}
    assert architect["snapshot_hash"] != person["snapshot_hash"]  # the hash is of the filtered view

    class NoMcp:
        def visible(self, viewer: Viewer, entry: CapabilityEntry) -> bool:
            return entry.kind != "control_tool"

    custom = CapabilityRegistryService(ProbeRegistry(TtlCache()), lambda: controls, NoMcp())
    assert run(custom.list(VIEWER, kind="control_tool"))["entries"] == []


def test_approved_tool_ids_are_the_available_grantable_tools():
    granted = approved_tool_ids()
    assert {"http.request", "text.check", "workflow.history", "workflow.return", "clock.now"} <= granted
    assert not any(t.startswith(("architect.", "platform.", "kb.", "settings.")) for t in granted)
    disabled = [e.model_copy(update={"status": "disabled"}) if e.id == "tool.http.request" else e for e in collect_declared()]
    assert "http.request" not in approved_tool_ids(disabled)


# ---------------------------------------------------------------------------------------------- the API


@pytest.fixture
def fresh_probe_cache():
    from eap_api.services.capability_probes import SHARED_CACHE

    SHARED_CACHE.invalidate()
    yield
    SHARED_CACHE.invalidate()


@requires_pg
def test_listing_etag_and_304(client, fresh_probe_cache):
    h = get_auth_header("t-etag", "u")
    first = client.get(BASE, headers=h)
    assert first.status_code == 200
    body = first.json()
    assert body["matched"] == body["total"] == len(body["entries"])
    assert {"tool.http.request", "control.capability.list", "model.test"} <= {e["id"] for e in body["entries"]}
    assert body["degraded"] == [] and len(body["registry_version"]) == 64
    etag = first.headers["ETag"]
    assert etag.startswith('"') and etag.endswith('"')
    same = client.get(BASE, headers={**h, "If-None-Match": etag})
    assert same.status_code == 304 and same.content == b"" and same.headers["ETag"] == etag
    assert client.get(BASE, headers={**h, "If-None-Match": f"W/{etag}"}).status_code == 304
    assert client.get(BASE, headers={**h, "If-None-Match": "*"}).status_code == 304
    assert client.get(BASE, headers={**h, "If-None-Match": '"stale"'}).status_code == 200
    filtered = client.get(BASE, params={"kind": "trigger", "status": "available"}, headers=h).json()
    assert {e["id"] for e in filtered["entries"]} == {"trigger.api_run", "trigger.schedule", "trigger.webhook"}
    one = client.get(f"{BASE}/step.foreach", headers=h)
    assert one.status_code == 200 and one.json()["entry"]["writable_by_architect"] is False
    missing = client.get(f"{BASE}/step.foreeach", headers=h)
    assert missing.status_code == 404 and missing.json()["code"] == "unknown_capability"
    assert client.get(BASE, params={"kind": "nope"}, headers=h).status_code in (400, 422)
    assert client.get(BASE).status_code in (401, 403)


@requires_pg
def test_openapi_has_only_the_two_capability_routes(client):
    paths = [p for p in client.get("/openapi.json").json()["paths"] if p.startswith("/api/v1/capabilities")]
    assert sorted(paths) == ["/api/v1/capabilities", "/api/v1/capabilities/{capability_id}"]


class FakeConnection:
    """Stands in for the MCP connection test: no network."""

    def __init__(self, tools: dict[str, list[str]]) -> None:
        self.tools = tools

    async def test(self, tenant_id: str, server_id: uuid.UUID) -> dict[str, Any]:
        return {"status": "ok", "tools": [{"name": n} for n in self.tools.get(str(server_id), ["ping"])]}


@pytest.fixture
def mcp_client(monkeypatch, fresh_probe_cache):
    monkeypatch.setenv(KEY_ENV, generate_key())
    monkeypatch.delenv(OLD_KEYS_ENV, raising=False)
    monkeypatch.delenv(DEV_ALLOW_HOSTS_ENV, raising=False)

    async def resolve(host: str, port: int) -> list[str]:
        return ["93.184.215.14"]

    monkeypatch.setattr(cap_deps, "build_mcp_connection_service", lambda *a, **k: FakeConnection({}))
    app = create_app(database_url=PG_URL)
    app.dependency_overrides[get_outbound_policy] = lambda: OutboundPolicy(resolver=resolve)
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.state.database_engine.dispose()


@requires_pg
def test_adding_an_mcp_server_changes_the_etag_and_stays_in_its_tenant(mcp_client):
    a, b = get_auth_header(f"ta{uuid.uuid4().hex[:8]}", "u"), get_auth_header(f"tb{uuid.uuid4().hex[:8]}", "u")
    before_a, before_b = mcp_client.get(BASE, headers=a), mcp_client.get(BASE, headers=b)
    created = mcp_client.post("/api/v1/mcp-servers", json={"name": "docs-server", "url": "https://mcp.example.com/mcp"}, headers=a)
    assert created.status_code == 201, created.text
    after_a = mcp_client.get(BASE, headers={**a, "If-None-Match": before_a.headers["ETag"]})
    assert after_a.status_code == 200 and after_a.headers["ETag"] != before_a.headers["ETag"]
    ids = {e["id"] for e in after_a.json()["entries"]}
    assert {"mcp_server.docs-server", "mcp_tool.docs-server/ping"} <= ids
    # Tenant isolation: the other tenant sees neither the server nor its tool, and its ETag did not move.
    after_b = mcp_client.get(BASE, headers={**b, "If-None-Match": before_b.headers["ETag"]})
    assert after_b.status_code == 304
    assert not any("docs-server" in e["id"] for e in mcp_client.get(BASE, headers=b).json()["entries"])
    assert mcp_client.get(f"{BASE}/mcp_server.docs-server", headers=b).status_code == 404
    # Removing it moves the ETag again.
    server_id = created.json()["id"]
    assert mcp_client.delete(f"/api/v1/mcp-servers/{server_id}", headers=a).status_code == 204
    gone = mcp_client.get(BASE, headers=a)
    assert gone.headers["ETag"] == before_a.headers["ETag"]


# ----------------------------------------------------------------------------------------------- MCP


@requires_pg
def test_mcp_clients_read_the_same_registry(world):  # noqa: F811
    tenant, h = world.tenant_of()
    key = world.make_key(h)
    listing = world.call(key, "capability.list", {"kind": "step"})
    assert not listing.is_error, listing.data
    assert {e["id"] for e in listing.data["entries"]} >= {"step.agent", "step.foreach", "step.wait_event"}
    assert listing.data["known_gaps"]
    got = world.call(key, "capability.get", {"capability_id": "step.wait_event"})
    assert got.data["entry"]["status"] == "designed_not_built" and got.data["gaps"][0]["id"] == "gap.wait_event_step"
    hook = world.call(key, "capability.get", {"capability_id": "trigger.webhook"})
    assert hook.data["entry"]["status"] == "available" and not hook.data["gaps"]
    missing = world.call(key, "capability.get", {"capability_id": "trigger.webhok"})
    assert missing.error["code"] == "unknown_capability" and "trigger.webhook" in missing.error["message"]
    assert world.call(key, "capability.list", {"status": "bogus"}).error["code"] == "invalid_status"
    # The same hash as REST for the same tenant (a user principal; the dev bearer is not an architect run).
    with world.rest() as c:
        rest = c.get("/capabilities", headers=h).json()
    assert rest["registry_version"] == listing.data["registry_version"]
