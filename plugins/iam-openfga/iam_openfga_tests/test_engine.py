"""The engine against a fake OpenFGA: what it asks, how many calls it makes, and that it fails closed."""

import asyncio
import logging

import httpx2
import pytest
from eap_iam import All, GRANT_RELATIONS, Grant, GrantError, GrantingPolicyEngine, Only, PolicyEngine, PolicyUnavailable, Principal, ResourceRef
from eap_iam_openfga import OpenFgaPolicy, create
from eap_iam_openfga.settings import ENV_STORE_ID, ENV_URL

from .fakes import KEY, FakeOpenFga, settings

T = "acme"


def principal(subject="zitadel:u1", roles=(), kind="user", **kw) -> Principal:
    return Principal(tenant_id=T, kind=kind, subject=subject, provider="t", roles=frozenset(roles), **kw)


def ref(kind="agent", id="a1", tenant=T) -> ResourceRef:
    return ResourceRef(kind=kind, id=id, tenant_id=tenant)


def engine(fake: FakeOpenFga, **over) -> OpenFgaPolicy:
    return OpenFgaPolicy(fake.client(**over))


def run(coro):
    return asyncio.run(coro)


def test_it_satisfies_the_core_protocols():
    policy = engine(FakeOpenFga())
    assert isinstance(policy, PolicyEngine) and isinstance(policy, GrantingPolicyEngine)
    assert policy.name == "openfga" and not policy.transitional
    assert {"grants", "list_filter", "explain", "contextual_roles"} == set(policy.features)


def test_check_asks_the_relation_of_the_action_and_sends_the_roles_as_contextual_tuples():
    fake = FakeOpenFga()
    fake.allowed.add(("user:zitadel:u1", "can_publish", "agent:acme/a1"))
    d = run(engine(fake).check(principal(roles=["editor", "unknown-role"]), "publish", ref()))
    assert d.allowed and d.reason == "allow:can_publish" and d.explain == {"relation": "can_publish"}
    assert fake.calls["check"] == 1
    name, body = fake.requests[0]
    assert body["tuple_key"] == {"user": "user:zitadel:u1", "relation": "can_publish", "object": "agent:acme/a1"}
    assert body["contextual_tuples"]["tuple_keys"] == [{"user": "user:zitadel:u1", "relation": "editor", "object": "tenant:acme"}]
    assert body["authorization_model_id"] == "01FAKEMODEL"


def test_a_denied_check_says_why_without_naming_anything_hidden():
    d = run(engine(FakeOpenFga()).check(principal(), "run", ref()))
    assert not d.allowed and d.reason == "deny:no_grant" and d.explain == {"relation": "can_run"}


def test_the_action_may_carry_its_kind():
    fake = FakeOpenFga()
    fake.allowed.add(("user:zitadel:u1", "can_run", "workflow:acme/w"))
    assert run(engine(fake).check(principal(), "workflow.run", ref("workflow", "w"))).allowed


def test_a_collection_answers_create_against_the_tenant():
    fake = FakeOpenFga()
    fake.allowed.add(("user:zitadel:u1", "can_create_secret", "tenant:acme"))
    p = principal(roles=["admin"])
    assert run(engine(fake).check(p, "create", ResourceRef("secret", None, T))).allowed
    assert not run(engine(fake).check(p, "read", ResourceRef("secret", None, T))).allowed  # no such collection action
    assert fake.calls["check"] == 1


def test_the_tenant_itself_has_its_own_relations():
    fake = FakeOpenFga()
    fake.allowed.add(("user:zitadel:u1", "can_manage_members", "tenant:acme"))
    assert run(engine(fake).check(principal(), "manage_members", ResourceRef("tenant", T, T))).allowed


@pytest.mark.parametrize(
    "action,kind,reason",
    [("fly", "agent", "deny:unknown_action"), ("run", "spaceship", "deny:unknown_action"), ("read", "tenant_x", "deny:unknown_action")],
)
def test_what_the_model_cannot_express_is_denied_without_a_call(action, kind, reason):
    fake = FakeOpenFga()
    d = run(engine(fake).check(principal(), action, ref(kind)))
    assert not d.allowed and d.reason == reason and sum(fake.calls.values()) == 0


def test_another_tenants_resource_is_denied_without_a_call():
    fake = FakeOpenFga()
    d = run(engine(fake).check(principal(), "read", ref(tenant="other")))
    assert not d.allowed and d.reason == "deny:cross_tenant" and sum(fake.calls.values()) == 0


def test_an_id_openfga_would_refuse_is_denied_without_a_call():
    fake = FakeOpenFga()
    assert not run(engine(fake).check(principal(), "read", ref(id="has space"))).allowed
    assert not run(engine(fake).check(principal(), "read", ref(id="a#b"))).allowed
    assert sum(fake.calls.values()) == 0


def test_an_api_key_abstains_because_its_constraints_decide():
    fake = FakeOpenFga()
    key = principal(subject="key1", kind="api_key", scopes=frozenset({"mcp"}))
    d = run(engine(fake).check(key, "read", ref()))
    assert d.allowed and d.reason == "abstain:constraints"
    assert isinstance(run(engine(fake).filter_ids(key, "read", "agent")), All)
    assert sum(fake.calls.values()) == 0


def test_a_run_is_checked_as_the_principal_it_acts_for():
    fake = FakeOpenFga()
    fake.allowed.add(("user:zitadel:boss", "can_read", "agent:acme/a1"))
    p = principal(subject="architect-run:r1", kind="run", on_behalf_of="zitadel:boss")
    assert run(engine(fake).check(p, "read", ref())).allowed
    orphan = principal(subject="architect-run:r1", kind="run")
    assert run(engine(fake).check(orphan, "read", ref())).reason == "deny:no_subject"


# --- batching ----------------------------------------------------------------------------------------------------------


def test_check_many_is_one_batch_check_for_a_page():
    fake = FakeOpenFga()
    fake.allowed |= {("user:zitadel:u1", "can_read", f"agent:acme/a{i}") for i in range(0, 50, 2)}
    out = run(engine(fake).check_many(principal(), "read", [ref(id=f"a{i}") for i in range(50)]))
    assert fake.calls["batch_check"] == 1 and sum(fake.calls.values()) == 1
    assert [k for k, d in out.items() if d.allowed] == [f"a{i}" for i in range(0, 50, 2)]


def test_check_many_over_the_batch_limit_costs_one_list_not_one_call_per_row():
    fake = FakeOpenFga()
    fake.allowed |= {("user:zitadel:u1", "can_read", f"agent:acme/a{i}") for i in range(0, 300, 3)}
    out = run(engine(fake).check_many(principal(), "read", [ref(id=f"a{i}") for i in range(300)]))
    assert fake.calls["list_objects"] == 1 and fake.calls["batch_check"] == 0 and fake.calls["check"] == 0
    assert sum(d.allowed for d in out.values()) == 100 and out["a3"].allowed and not out["a4"].allowed


def test_check_many_falls_back_to_batches_when_the_list_is_truncated_and_logs_it(caplog):
    fake = FakeOpenFga()
    fake.allowed |= {("user:zitadel:u1", "can_read", f"agent:acme/a{i}") for i in range(0, 120, 2)}
    fake.list_cap = 10
    with caplog.at_level(logging.WARNING, logger="eap.iam.openfga"):
        out = run(engine(fake, list_limit=10).check_many(principal(), "read", [ref(id=f"a{i}") for i in range(120)]))
    assert fake.calls["list_objects"] == 1 and fake.calls["batch_check"] == 3  # 120 rows, 50 per batch
    assert sum(d.allowed for d in out.values()) == 60  # exact: the truncated list was not read as "denied"
    assert "limit" in caplog.text


def test_check_many_repeats_and_untestable_rows_do_not_cost_calls():
    fake = FakeOpenFga()
    rows = [ref(id="a1"), ref(id="a1"), ref(id="b", tenant="other"), ref(id="a2")]
    out = run(engine(fake).check_many(principal(), "read", rows))
    assert fake.calls["batch_check"] == 1 and len(fake.requests[0][1]["checks"]) == 2
    assert out["b"].reason == "deny:cross_tenant"
    assert run(engine(fake).check_many(principal(), "read", [])) == {}


# --- lists -------------------------------------------------------------------------------------------------------------


def test_filter_ids_is_one_admin_check_and_one_list():
    fake = FakeOpenFga()
    fake.allowed |= {("user:zitadel:u1", "can_read", f"agent:acme/a{i}") for i in range(5)}
    fake.allowed.add(("user:zitadel:u1", "can_read", "agent:other/stolen"))  # a stray tuple of another tenant
    f = run(engine(fake).filter_ids(principal(roles=["viewer"]), "read", "agent"))
    assert isinstance(f, Only) and sorted(f.ids) == [f"a{i}" for i in range(5)]
    assert fake.calls["check"] == 1 and fake.calls["list_objects"] == 1 and sum(fake.calls.values()) == 2


def test_a_tenant_admin_is_all_after_one_check():
    fake = FakeOpenFga()
    fake.allowed.add(("user:zitadel:u1", "admin", "tenant:acme"))
    assert isinstance(run(engine(fake).filter_ids(principal(roles=["admin"]), "read", "agent")), All)
    assert sum(fake.calls.values()) == 1


def test_filter_ids_narrows_to_the_candidates():
    fake = FakeOpenFga()
    fake.allowed |= {("user:zitadel:u1", "can_read", f"agent:acme/a{i}") for i in range(5)}
    f = run(engine(fake).filter_ids(principal(), "read", "agent", ["a1", "a9", "a3"]))
    assert f == Only(("a1", "a3"))


def test_a_truncated_list_with_candidates_is_checked_exactly_and_without_them_only_hides(caplog):
    fake = FakeOpenFga()
    fake.allowed |= {("user:zitadel:u1", "can_read", f"agent:acme/a{i:03}") for i in range(30)}
    fake.list_cap = 5
    p = principal()
    with caplog.at_level(logging.WARNING, logger="eap.iam.openfga"):
        exact = run(engine(fake, list_limit=5).filter_ids(p, "read", "agent", [f"a{i:03}" for i in range(40)]))
        partial = run(engine(fake, list_limit=5).filter_ids(p, "read", "agent"))
    assert exact == Only(tuple(f"a{i:03}" for i in range(30)))
    assert isinstance(partial, Only) and len(partial.ids) == 5  # a subset: it can hide rows, never show one
    assert "limit" in caplog.text


def test_an_unknown_action_lists_nothing():
    fake = FakeOpenFga()
    assert run(engine(fake).filter_ids(principal(), "fly", "agent")) == Only(())
    assert sum(fake.calls.values()) == 0


# --- failing closed ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "outcome",
    [
        httpx2.Response(500, json={"code": "internal_error", "message": f"boom {KEY}"}),
        httpx2.Response(401, json={"code": "unauthenticated", "message": "bad key"}),
        httpx2.Response(200, text="<html>not json</html>"),
        httpx2.Response(200, json={"unexpected": True}),
        httpx2.ConnectError("refused"),
        httpx2.ReadTimeout("slow"),
    ],
)
@pytest.mark.parametrize("call", ["check", "check_many", "filter_ids"])
def test_an_engine_error_is_unavailable_never_an_allow(outcome, call):
    fake = FakeOpenFga()
    fake.fail = lambda name: outcome
    p = principal()
    with pytest.raises(PolicyUnavailable) as err:
        if call == "check":
            run(engine(fake).check(p, "read", ref()))
        elif call == "check_many":
            run(engine(fake).check_many(p, "read", [ref()]))
        else:
            run(engine(fake).filter_ids(p, "read", "agent"))
    # An error names the call and the status, never the body (which here carries the key) or the key itself.
    assert KEY not in err.value.detail and "boom" not in err.value.detail and "refused" not in err.value.detail
    assert err.value.detail.startswith("openfga ")


def test_a_batch_item_without_a_decision_is_unavailable_not_a_deny():
    fake = FakeOpenFga()
    fake._batch_check = lambda r, b: {"result": {"0": {"error": {"code": "validation_error", "message": "x"}}}}
    with pytest.raises(PolicyUnavailable):
        run(engine(fake).check_many(principal(), "read", [ref()]))


def test_the_key_goes_in_the_header_and_nowhere_else():
    fake = FakeOpenFga()
    run(engine(fake).check(principal(), "read", ref()))
    assert fake.auth_ok
    assert KEY not in repr(settings()) and KEY not in repr(engine(fake).client.settings)


def test_a_wrong_key_is_seen_by_the_server_as_wrong():
    fake = FakeOpenFga()
    run(engine(fake, preshared_key="another-key-0123456789").check(principal(), "read", ref()))
    assert not fake.auth_ok


# --- resources and grants ----------------------------------------------------------------------------------------------


def test_register_links_the_resource_to_its_tenant_and_makes_the_creator_owner():
    fake = FakeOpenFga()
    run(engine(fake).register_resource(ref(), creator="zitadel:u1"))
    assert fake.tuples == {("tenant:acme", "tenant", "agent:acme/a1"), ("user:zitadel:u1", "owner", "agent:acme/a1")}
    run(engine(fake).register_resource(ref(), creator="zitadel:u1"))  # idempotent
    assert len(fake.tuples) == 2


def test_register_without_a_creator_links_only():
    fake = FakeOpenFga()
    run(engine(fake).register_resource(ref("secret", "token")))
    assert fake.tuples == {("tenant:acme", "tenant", "secret:acme/token")}


def test_a_run_is_registered_under_its_parent_and_has_no_owner():
    fake = FakeOpenFga()
    run(engine(fake).register_resource(ref("run", "r1"), creator="zitadel:u1", parent=ref("workflow", "w1")))
    assert fake.tuples == {("workflow:acme/w1", "parent", "run:acme/r1")}
    with pytest.raises(GrantError):
        run(engine(fake).register_resource(ref("run", "r2")))
    with pytest.raises(GrantError):
        run(engine(fake).register_resource(ref("run", "r2"), parent=ref("workflow", "w1", tenant="other")))


def test_unregister_deletes_the_links_the_grants_and_the_runs_below():
    fake = FakeOpenFga()
    e = engine(fake)
    run(e.register_resource(ref("workflow", "w1"), creator="zitadel:u1"))
    run(e.grant(Grant("zitadel:u2", "runner", ref("workflow", "w1"))))
    run(e.register_resource(ref("run", "r1"), parent=ref("workflow", "w1")))
    run(e.register_resource(ref("workflow", "w2"), creator="zitadel:u1"))
    run(e.unregister_resource(ref("workflow", "w1")))
    assert fake.tuples == {("tenant:acme", "tenant", "workflow:acme/w2"), ("user:zitadel:u1", "owner", "workflow:acme/w2")}


def test_grant_revoke_and_list():
    fake = FakeOpenFga()
    e = engine(fake)
    run(e.register_resource(ref(), creator="zitadel:u1"))
    run(e.grant(Grant("zitadel:u2", "runner", ref())))
    run(e.grant(Grant("zitadel:u3", "approver", ref())))
    run(e.grant(Grant("zitadel:u2", "runner", ref())))  # again: no error, no duplicate
    listed = run(e.list_grants(ref()))
    assert sorted((g.relation, g.subject) for g in listed) == [("approver", "zitadel:u3"), ("owner", "zitadel:u1"), ("runner", "zitadel:u2")]
    run(e.revoke(Grant("zitadel:u2", "runner", ref())))
    assert ("user:zitadel:u2", "runner", "agent:acme/a1") not in fake.tuples
    assert all(g.relation in GRANT_RELATIONS for g in run(e.list_grants(ref())))


@pytest.mark.parametrize(
    "grant",
    [
        Grant("zitadel:u2", "tenant", ref()),  # an internal relation
        Grant("zitadel:u2", "usable_by", ref("secret", "s")),
        Grant("zitadel:u2", "runner", ref("secret", "s")),  # a secret has no runner
        Grant("zitadel:u2", "approver", ref("knowledge_base", "k")),
        Grant("*", "runner", ref()),  # a wildcard subject
        Grant("a b", "runner", ref()),
        Grant("zitadel:u2", "runner", ResourceRef("agent", None, T)),
    ],
)
def test_a_malformed_grant_is_refused_and_writes_nothing(grant):
    fake = FakeOpenFga()
    with pytest.raises(GrantError):
        run(engine(fake).grant(grant))
    assert not fake.tuples and sum(fake.calls.values()) == 0


def test_writes_are_chunked_to_the_servers_limit():
    fake = FakeOpenFga()
    from eap_iam_openfga.client import TupleKey

    tuples = [TupleKey("tenant:acme", "tenant", f"agent:acme/a{i}") for i in range(250)]
    run(fake.client().write(tuples))
    assert fake.calls["write"] == 3 and len(fake.tuples) == 250  # the fake asserts <= 100 per request


# --- configuration -----------------------------------------------------------------------------------------------------


def test_the_factory_refuses_to_start_unconfigured_and_never_echoes_a_value():
    from eap_iam import IamConfigError, PluginContext

    with pytest.raises(IamConfigError, match=ENV_URL):
        create(PluginContext(env={}))
    with pytest.raises(IamConfigError, match=ENV_STORE_ID):
        create(PluginContext(env={ENV_URL: "http://openfga:8080"}))
    with pytest.raises(IamConfigError):
        create(PluginContext(env={ENV_URL: "ftp://x", ENV_STORE_ID: "s"}))
    ok = create(PluginContext(env={ENV_URL: "http://openfga:8080/", ENV_STORE_ID: "s", "OPENFGA_PRESHARED_KEY": KEY}))
    assert ok.client.settings.url == "http://openfga:8080" and ok.client.settings.preshared_key == KEY


def test_it_is_discovered_through_the_entry_point():
    from eap_iam.protocols import GROUP_POLICY
    from eap_iam.registry import installed

    assert "openfga" in installed(GROUP_POLICY, {})
