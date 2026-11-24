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
