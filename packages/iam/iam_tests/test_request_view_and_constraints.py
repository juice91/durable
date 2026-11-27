import pytest

from eap_iam import Principal, RequestView, ResourceRef, has_required_scope, permits


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "::ffff:127.0.0.1", "127.8.9.10"])
def test_loopback_peers(host):
    assert RequestView(peer_host=host).is_loopback


@pytest.mark.parametrize("host", ["10.42.0.7", "192.168.1.5", "localhost", "testclient", "", None, "2001:db8::1"])
def test_non_loopback_peers(host):
    assert not RequestView(peer_host=host).is_loopback


@pytest.mark.parametrize(
    "header", ["x-forwarded-for", "cf-connecting-ip", "cf-access-jwt-assertion", "forwarded", "x-real-ip"]
)
def test_a_proxy_header_means_never_loopback_even_from_127_0_0_1(header):
    assert not RequestView(peer_host="127.0.0.1", headers={header: "127.0.0.1"}).is_loopback


def key(scopes, agents=None, workflows=None):
    return Principal(
        tenant_id="t", kind="api_key", subject="k", provider="api-key", scopes=frozenset(scopes),
        agent_ids=agents, workflow_ids=workflows,
    )


def test_scope_per_action():
    assert has_required_scope(key({"agui:run"}), "run.agui") and not has_required_scope(key({"otlp"}), "run.agui")
    assert has_required_scope(key({"manage"}), "mcp.read") and has_required_scope(key({"mcp"}), "mcp.read")
    assert not has_required_scope(key({"mcp"}), "mcp.manage") and has_required_scope(key(set()), "unmapped.action")


def test_allowlists_fail_closed():
    agent = ResourceRef("agent", "a1", "t")
    assert permits(key({"agui:run"}), "run.agui", agent)  # neither list set: tenant-wide
    assert permits(key({"agui:run"}, agents=("a1",), workflows=()), "run.agui", agent)
    assert not permits(key({"agui:run"}, agents=("a2",), workflows=()), "run.agui", agent)
    assert not permits(key({"agui:run"}, workflows=("w1",)), "run.agui", agent)  # a NULL list allows nothing
    assert permits(key({"agui:run"}, workflows=("w1",), agents=()), "run.agui", ResourceRef("workflow", "w1", "t"))


def test_non_key_principals_have_no_constraints():
    user = Principal(tenant_id="t", kind="user", subject="u", provider="p")
    assert permits(user, "run.agui", ResourceRef("agent", "a", "t"))


def test_the_webhook_kind_is_never_in_the_default_accepts_and_the_view_carries_route_params_and_body():
    from eap_iam import ACTION_INGEST_WEBHOOK, ALL_KINDS, AuthError

    assert "webhook" not in ALL_KINDS and "webhook" not in RequestView().accepts
    view = RequestView(route_params={"trigger_id": "x"}, body=b"raw", action=ACTION_INGEST_WEBHOOK)
    assert view.route_params["trigger_id"] == "x" and view.body == b"raw" and RequestView().body is None
    assert AuthError("x", reason="signature_mismatch").reason == "signature_mismatch" and AuthError("x").reason is None
    # a webhook principal passes no scope check: it never holds API-key scopes and is never an api_key
    assert permits(Principal(tenant_id="t", kind="webhook", subject="s", provider="p"), "ingest.otlp") is True


def test_run_external_needs_the_run_scope_and_the_workflow_allowlist():
    from eap_iam import ACTION_RUN_EXTERNAL, ROLE_SCOPES

    workflow = ResourceRef("workflow", "w1", "t")
    assert ACTION_RUN_EXTERNAL == "run.external"
    assert permits(key({"run"}, workflows=("w1",)), ACTION_RUN_EXTERNAL, workflow)
    assert not permits(key({"run"}, workflows=("w2",)), ACTION_RUN_EXTERNAL, workflow)
    for other in ("agui:run", "mcp", "manage", "otlp"):
        assert not permits(key({other}, workflows=("w1",)), ACTION_RUN_EXTERNAL, workflow)
    # a run key satisfies no other action's scope
    for action in ("run.agui", "ingest.otlp", "mcp.read", "mcp.manage"):
        assert not has_required_scope(key({"run"}), action)
    # no role lets a person hold a run key: an external agent's identity is a client's, never a person's
    assert not any("run" in scopes for scopes in ROLE_SCOPES.values())
