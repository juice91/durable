"""The engine, the bootstrap and the reconcile against a REAL OpenFGA (the version pinned in deploy/k8s/iam/openfga.yaml).

Skipped unless a server is reachable (see `server.py`: `EAP_TEST_OPENFGA_URL` or `EAP_TEST_OPENFGA_DOCKER=1`). A
skipped test is not a pass; CI Heavy runs these on the self-hosted runner.

What the fake cannot show: that the model means what the matrix says when the roles arrive as contextual tuples, that
the server accepts the JSON, `on_duplicate`/`on_missing`, ListObjects, BatchCheck and the page walk of Read."""

import asyncio
import io
import uuid

import pytest
from eap_iam import All, CatalogEntry, Grant, Member, Only, PolicyUnavailable, Principal, ResourceRef
from eap_iam.protocols import GROUP_MEMBERSHIP
from eap_iam.registry import register_local
from eap_iam_openfga.bootstrap import run as bootstrap_run
from eap_iam_openfga.client import OpenFgaClient, TupleKey
from eap_iam_openfga.engine import OpenFgaPolicy
from eap_iam_openfga.model import model_text
from eap_iam_openfga.names import user_ref
from eap_iam_openfga.reconcile import amain, apply_plan, build_plan
from eap_iam_openfga.settings import OpenFgaSettings

from .server import server  # noqa: F401 - the fixture

T = "acme"


# One loop for the whole module: a client keeps pooled connections, and they belong to the loop that opened them.
_LOOP = asyncio.new_event_loop()


def _run(coro):
    return _LOOP.run_until_complete(coro)


@pytest.fixture
def fga(server):
    """A fresh store with the shipped model, a client on it and the engine."""
    url, key = server
    base = OpenFgaSettings(url=url, store_id="", preshared_key=key, timeout=10.0)
    result = _run(bootstrap_run(base, store_name=f"t-{uuid.uuid4().hex[:8]}", model_text_=model_text(), store_id=None, dry_run=False))
    settings = OpenFgaSettings(url=url, store_id=result.store_id, model_id=result.model_id, preshared_key=key, timeout=10.0)
    client = OpenFgaClient(settings)
    return OpenFgaPolicy(client), client, result


def P(subject, *roles, tenant=T, **kw) -> Principal:
    return Principal(tenant_id=tenant, kind="user", subject=subject, provider="t", roles=frozenset(roles), **kw)


def R(kind, id, tenant=T) -> ResourceRef:
    return ResourceRef(kind, id, tenant)


def seed(policy, kinds=("agent", "workflow"), ids=("r1", "r2"), creator=None):
    async def go():
        for kind in kinds:
            for i in ids:
                await policy.register_resource(R(kind, i), creator=creator)
    _run(go())


def test_bootstrap_is_idempotent_against_a_real_server(server, fga):
    _, _, first = fga
    url, key = server
    base = OpenFgaSettings(url=url, store_id="", preshared_key=key, timeout=10.0)
    again = _run(bootstrap_run(base, store_name="ignored", model_text_=model_text(), store_id=first.store_id, dry_run=False))
    assert again.model_id == first.model_id and not again.model_written


def test_the_role_matrix_with_roles_as_contextual_tuples(fga):
    policy, client, _ = fga
    seed(policy)
    check = lambda p, action, ref: _run(policy.check(p, action, ref)).allowed  # noqa: E731
    viewer, editor, admin = P("u-viewer", "viewer"), P("u-editor", "editor"), P("u-admin", "admin")
    a = R("agent", "r1")
    assert [check(viewer, x, a) for x in ("read", "run", "edit", "publish", "approve", "archive", "manage_access")] == [True, False, False, False, False, False, False]
    assert [check(editor, x, a) for x in ("read", "run", "edit", "publish", "approve", "archive", "manage_access", "grant_use")] == [True, True, True, True, False, False, False, True]
    assert all(check(admin, x, a) for x in ("read", "run", "edit", "publish", "approve", "archive", "manage_access", "grant_use"))
    # No role, no right (nothing is stored for the user: roles are per request).
    assert not check(P("u-nobody"), "read", a)
    # Another tenant's admin reaches nothing here.
    assert not check(P("u-admin", "admin", tenant="other"), "read", ResourceRef("agent", "r1", "other"))


def test_a_runner_grant_an_approver_grant_and_a_run_that_inherits_approve(fga):
    policy, _, _ = fga
    seed(policy, kinds=("workflow",))
    _run(policy.register_resource(R("run", "run1"), parent=R("workflow", "r1")))
    viewer, editor = P("u-viewer", "viewer"), P("u-editor", "editor")
    check = lambda p, action, ref: _run(policy.check(p, action, ref)).allowed  # noqa: E731
    assert not check(viewer, "run", R("workflow", "r1"))
    _run(policy.grant(Grant("u-viewer", "runner", R("workflow", "r1"))))
    assert check(viewer, "run", R("workflow", "r1")) and not check(viewer, "run", R("workflow", "r2")) and not check(viewer, "edit", R("workflow", "r1"))
    assert not check(editor, "approve", R("run", "run1"))
    _run(policy.grant(Grant("u-editor", "approver", R("workflow", "r1"))))
    assert check(editor, "approve", R("run", "run1")) and not check(editor, "approve", R("workflow", "r2"))
    assert check(viewer, "cancel", R("run", "run1")) and check(viewer, "read", R("run", "run1"))
    _run(policy.revoke(Grant("u-viewer", "runner", R("workflow", "r1"))))
    assert not check(viewer, "run", R("workflow", "r1"))


def test_only_admins_grant_use_and_the_old_usable_by_relation_is_read_by_nothing(fga):
    policy, client, _ = fga
    seed(policy, kinds=("secret",), ids=("crm", "other"))
    editor, admin = P("u-editor", "editor"), P("u-admin", "admin")
    _run(client.write([TupleKey("tenant:acme#editor", "legacy_usable_by", "secret:acme/crm")]))
    check = lambda p, action, ref: _run(policy.check(p, action, ref)).allowed  # noqa: E731
    assert not check(editor, "grant_use", R("secret", "crm")) and not check(editor, "grant_use", R("secret", "other"))
    assert check(editor, "read", R("secret", "other")) and not check(editor, "edit", R("secret", "crm"))
    assert check(admin, "grant_use", R("secret", "other"))
    assert not check(P("u-viewer", "viewer"), "grant_use", R("secret", "crm"))


def test_agent_grants_use_the_model_end_to_end(fga):
    """grant_use / used_by / grantees / revoke_use against the real server: a grant is per agent or workflow, a revoke
    removes it, and deleting the agent or the resource takes its tuples along."""
    from eap_iam import UseGrant

    policy, _, _ = fga
    seed(policy, kinds=("secret", "mcp_server", "agent", "workflow"), ids=("a1", "a2"))
    a1, w1, s, m = R("agent", "a1"), R("workflow", "w1"), R("secret", "a1"), R("mcp_server", "a1")
    _run(policy.register_resource(w1))
    for grant in (UseGrant(a1, s), UseGrant(a1, m), UseGrant(w1, s)):
        _run(policy.grant_use(grant))
        _run(policy.grant_use(grant))  # idempotent
    assert _run(policy.used_by(a1, "secret")) == ["a1"] and _run(policy.used_by(a1, "mcp_server")) == ["a1"]
    assert _run(policy.used_by(R("agent", "a2"), "secret")) == []
    assert [g.id for g in _run(policy.grantees(s))] == ["a1", "w1"]
    assert not _run(policy.check(P("u-editor", "editor"), "grant_use", s)).allowed
    _run(policy.revoke_use(UseGrant(a1, s)))
    assert _run(policy.used_by(a1, "secret")) == [] and _run(policy.used_by(w1, "secret")) == ["a1"]
    _run(policy.unregister_resource(a1))  # the agent goes: its grant on the server goes too
    assert _run(policy.grantees(m)) == []
    _run(policy.unregister_resource(s))  # the secret goes: nobody is granted it
    assert _run(policy.used_by(w1, "secret")) == []


def test_an_owner_acts_on_their_own_resource_without_a_role(fga):
    policy, _, _ = fga
    _run(policy.register_resource(R("agent", "mine"), creator="u-owner"))
    seed(policy, kinds=("agent",), ids=("theirs",))
    check = lambda p, action, ref: _run(policy.check(p, action, ref)).allowed  # noqa: E731
    owner = P("u-owner", "editor")
    assert check(owner, "archive", R("agent", "mine")) and check(owner, "manage_access", R("agent", "mine"))
    assert not check(owner, "archive", R("agent", "theirs"))


def test_collection_actions_follow_the_matrix(fga):
    policy, _, _ = fga
    ok = lambda p, kind: _run(policy.check(p, "create", ResourceRef(kind, None, T))).allowed  # noqa: E731
    assert ok(P("e", "editor"), "agent") and not ok(P("e", "editor"), "secret") and ok(P("a", "admin"), "api_key") and not ok(P("v", "viewer"), "agent")
    assert _run(policy.check(P("a", "admin"), "manage_members", ResourceRef("tenant", T, T))).allowed
    assert not _run(policy.check(P("e", "editor"), "manage_members", ResourceRef("tenant", T, T))).allowed


def test_lists_one_admin_check_one_list_and_a_constant_cost_for_a_long_page(fga):
    policy, client, _ = fga
    ids = [f"a{i:03}" for i in range(120)]
    seed(policy, kinds=("agent",), ids=ids)
    _run(policy.grant(Grant("u-viewer", "runner", R("agent", "a007"))))
    runner = P("u-viewer", "viewer")
    client.stats.calls.clear()
    f = _run(policy.filter_ids(runner, "run", "agent"))
    assert f == Only(("a007",)) and client.stats.count() == 2
    assert isinstance(_run(policy.filter_ids(P("u-admin", "admin"), "run", "agent")), All)
    client.stats.calls.clear()
    out = _run(policy.check_many(P("u-editor", "editor"), "publish", [R("agent", i) for i in ids]))
    assert all(d.allowed for d in out.values()) and len(out) == 120
    assert client.stats.count() == 1 and client.stats.count("list_objects") == 1  # 120 rows, one engine call
    client.stats.calls.clear()
    page = _run(policy.check_many(runner, "run", [R("agent", i) for i in ids[:40]]))
    assert [k for k, d in page.items() if d.allowed] == ["a007"] and client.stats.count("batch_check") == 1


def test_a_list_over_the_limit_is_exact_with_candidates_and_logs(fga, caplog):
    policy, client, _ = fga
    ids = [f"a{i:02}" for i in range(12)]
    seed(policy, kinds=("agent",), ids=ids)
    limited = OpenFgaPolicy(OpenFgaClient(OpenFgaSettings(**{**client.settings.__dict__, "list_limit": 5})))
    p = P("u-editor", "editor")
    with caplog.at_level("WARNING", logger="eap.iam.openfga"):
        exact = _run(limited.filter_ids(p, "read", "agent", ids + ["missing"]))
    assert exact == Only(tuple(ids))
    assert "limit" in caplog.text


def test_unregister_removes_everything_about_a_resource(fga):
    policy, client, _ = fga
    seed(policy, kinds=("workflow",), ids=("w",), creator="u-owner")
    _run(policy.grant(Grant("u2", "runner", R("workflow", "w"))))
    _run(policy.register_resource(R("run", "r"), parent=R("workflow", "w")))
    _run(policy.unregister_resource(R("workflow", "w")))
    assert _run(client.read()) == []
    assert not _run(policy.check(P("u-owner"), "read", R("workflow", "w"))).allowed


def test_the_list_of_grants_is_read_back(fga):
    policy, _, _ = fga
    _run(policy.register_resource(R("agent", "g"), creator="u-owner"))
    _run(policy.grant(Grant("u2", "runner", R("agent", "g"))))
    got = _run(policy.list_grants(R("agent", "g")))
    assert sorted((g.relation, g.subject) for g in got) == [("owner", "u-owner"), ("runner", "u2")]


def test_a_role_grant_reaches_the_role_through_contextual_tuples_and_is_listed_and_revoked(fga):
    policy, _, _ = fga
    seed(policy, kinds=("workflow",), ids=("role1", "role2"))
    one, two = R("workflow", "role1"), R("workflow", "role2")
    viewer, editor = P("u-viewer", "viewer"), P("u-editor", "editor")
    check = lambda p, action, ref: _run(policy.check(p, action, ref)).allowed  # noqa: E731
    grant = Grant("viewer", "runner", one, subject_type="role")
    _run(policy.grant(grant))
    assert check(viewer, "run", one) and check(editor, "run", one) and not check(viewer, "run", two)
    assert not check(P("u-nobody"), "run", one)
    assert [(g.subject_type, g.subject, g.relation) for g in _run(policy.list_grants(one))] == [("role", "viewer", "runner")]
    _run(policy.revoke(grant))
    assert not check(viewer, "run", one)


def test_reconcile_repairs_a_missing_link_and_a_second_run_is_a_no_op(fga):
    policy, client, _ = fga

    class Catalog:
        def entries(self, tenant_id=None):
            from eap_iam import CatalogEntry
            return [CatalogEntry("agent", f"a{i}", T) for i in range(130)] + [CatalogEntry("secret", "tok", T, "zitadel:boss")]

    plan = _run(build_plan(client, Catalog(), owners=["u-owner"], creators=True))
    assert len(plan.to_add) == 131 + 131 + 1
    _run(apply_plan(client, plan, prune=False))
    again = _run(build_plan(client, Catalog(), owners=["u-owner"], creators=True))
    assert not again.to_add
    assert _run(policy.check(P("u-owner"), "manage_access", R("agent", "a129"))).allowed
    assert _run(policy.check(P("zitadel:boss"), "archive", R("secret", "tok"))).allowed


def test_the_engine_down_fails_closed_and_a_wrong_key_does_too(server, fga):
    _, client, _ = fga
    url, key = server
    dead = OpenFgaPolicy(OpenFgaClient(OpenFgaSettings(url="http://127.0.0.1:9", store_id="s", preshared_key=key, timeout=2.0)))
    with pytest.raises(PolicyUnavailable):
        _run(dead.check(P("u", "admin"), "read", R("agent", "x")))
    if key:
        wrong = OpenFgaPolicy(OpenFgaClient(OpenFgaSettings(**{**client.settings.__dict__, "preshared_key": "not-the-key-0123456789"})))
        with pytest.raises(PolicyUnavailable) as err:
            _run(wrong.check(P("u", "admin"), "read", R("agent", "x")))
        assert "HTTP 401" in err.value.detail and key not in err.value.detail


# --------------------------------------------------------------------------------------------- platform ids (encoding)

PERSON = "zitadel:400738350308982982"
PLATFORM_SUBJECTS = [
    PERSON, "boss@example.com", "pos_Ab12cd34", "apikey:pos_Ab12cd34", "run:0f8e6a52-1c1e-4a3e-9d55-1b0f0e2a9a10",
    "cfaccess:ann@example.com", "studio:operator", "a%3Ab", "100%:/x",
]


@pytest.mark.parametrize("subject", PLATFORM_SUBJECTS)
def test_a_platform_subject_is_written_checked_listed_and_deleted(fga, subject):
    policy, client, _ = fga
    mine, granted = R("agent", "mine"), R("workflow", "granted")
    person = P(subject)
    _run(policy.register_resource(mine, creator=subject))
    _run(policy.register_resource(granted))
    _run(policy.grant(Grant(subject, "runner", granted)))
    assert _run(policy.check(person, "manage_access", mine)).allowed
    assert _run(policy.check(person, "run", granted)).allowed and not _run(policy.check(person, "edit", granted)).allowed
    assert _run(policy.check_many(person, "read", [mine, R("agent", "no")]))["mine"].allowed
    assert _run(policy.filter_ids(person, "read", "agent")) == Only(("mine",))
    assert _run(policy.filter_ids(person, "run", "workflow", ["granted", "no"])) == Only(("granted",))
    assert [(g.subject, g.relation) for g in _run(policy.list_grants(mine))] == [(subject, "owner")]
    assert [(g.subject, g.relation) for g in _run(policy.list_grants(granted))] == [(subject, "runner")]
    # A role travels as a contextual tuple that names the same encoded user.
    assert _run(policy.check(P(subject, "editor"), "edit", granted)).allowed
    assert isinstance(_run(policy.filter_ids(P(subject, "admin"), "read", "agent")), All)
    _run(policy.revoke(Grant(subject, "runner", granted)))
    assert not _run(policy.check(person, "run", granted)).allowed
    _run(policy.unregister_resource(mine))
    _run(policy.unregister_resource(granted))
    assert _run(client.read()) == []


def test_an_architect_run_is_decided_for_its_person_with_their_roles(fga):
    policy, _, _ = fga
    _run(policy.register_resource(R("agent", "a"), creator=PERSON))
    _run(policy.register_resource(R("agent", "b")))

    def run_for(*roles, person=PERSON):
        return Principal(tenant_id=T, kind="run", subject="run:0f8e6a52", provider="run-token", on_behalf_of=person, roles=frozenset(roles))

    assert _run(policy.check(run_for("editor"), "publish", R("agent", "b"))).allowed
    assert not _run(policy.check(run_for("viewer"), "publish", R("agent", "b"))).allowed
    assert _run(policy.check(run_for(), "archive", R("agent", "a"))).allowed
    assert _run(policy.filter_ids(run_for(), "read", "agent")) == Only(("a",))
    assert sorted(_run(policy.filter_ids(run_for("viewer"), "read", "agent")).ids) == ["a", "b"]
    assert _run(policy.check(run_for(person=None), "read", R("agent", "a"))).reason == "deny:no_subject"


def test_an_api_key_abstains_and_its_subject_can_still_own_a_resource(fga):
    policy, client, _ = fga
    key = Principal(tenant_id=T, kind="api_key", subject="apikey:pos_Ab12cd34", provider="api-key", on_behalf_of=PERSON)
    _run(policy.register_resource(R("agent", "k"), creator=key.subject))
    client.stats.calls.clear()
    assert _run(policy.check(key, "read", R("agent", "k"))).reason == "abstain:constraints" and not client.stats.count()
    assert [g.subject for g in _run(policy.list_grants(R("agent", "k")))] == [key.subject]


def test_a_group_member_and_a_role_grant_work_with_a_zitadel_subject(fga):
    policy, client, _ = fga
    seed(policy, kinds=("workflow",), ids=("g", "r"))
    _run(client.write([TupleKey(user_ref(PERSON), "member", "group:ops"), TupleKey("group:ops#member", "runner", "workflow:acme/g")]))
    assert _run(policy.check(P(PERSON), "run", R("workflow", "g"))).allowed
    assert _run(policy.filter_ids(P(PERSON), "run", "workflow")) == Only(("g",))
    assert _run(policy.list_grants(R("workflow", "g"))) == []  # a group is not a platform grant
    _run(policy.grant(Grant("viewer", "runner", R("workflow", "r"), subject_type="role")))
    assert _run(policy.check(P(PERSON, "viewer"), "run", R("workflow", "r"))).allowed
    assert [(g.subject_type, g.subject) for g in _run(policy.list_grants(R("workflow", "r")))] == [("role", "viewer")]


def test_resource_ids_with_colons_come_back_as_platform_ids(fga):
    from eap_iam import UseGrant

    policy, client, _ = fga
    agent, secret, run = R("agent", "ns:a%1"), R("secret", "vault:crm/token"), R("run", "run:1")
    _run(policy.register_resource(agent, creator=PERSON))
    _run(policy.register_resource(secret, creator=PERSON))
    _run(policy.register_resource(run, parent=agent))
    _run(policy.grant_use(UseGrant(agent, secret)))
    assert _run(policy.used_by(agent, "secret")) == [secret.id]
    assert [(g.kind, g.id) for g in _run(policy.grantees(secret))] == [("agent", agent.id)]
    assert _run(policy.filter_ids(P(PERSON), "read", "secret")) == Only((secret.id,))
    assert _run(policy.check(P(PERSON), "read", run)).allowed
    out = _run(policy.check_many(P(PERSON), "read", [secret, R("secret", "other:x")]))
    assert out[secret.id].allowed and not out["other:x"].allowed
    _run(policy.unregister_resource(agent))
    _run(policy.unregister_resource(secret))
    assert _run(client.read()) == []


def test_the_server_accepts_every_character_and_length_the_guard_lets_through(fga):
    policy, _, _ = fga
    printable = "".join(chr(i) for i in range(33, 127) if chr(i) not in "#*") + "é中😀\u200b"
    for ch in printable:
        subject = f"s{ch}{ch}t"
        resource = R("secret", f"n{ch}{ch}m")
        _run(policy.register_resource(resource, creator=subject))
        assert _run(policy.check(P(subject), "read", resource)).allowed, repr(ch)
        assert _run(policy.filter_ids(P(subject), "read", "secret")) == Only((resource.id,)), repr(ch)
        assert [g.subject for g in _run(policy.list_grants(resource))] == [subject]
        _run(policy.unregister_resource(resource))
    longest_user, longest_id = "u" * 507, "é" * 245  # `user:` + 507 bytes; `agent:acme/` + 245 characters
    agent = R("agent", longest_id)
    _run(policy.register_resource(agent, creator=longest_user))
    assert _run(policy.check(P(longest_user), "read", agent)).allowed
    assert _run(policy.filter_ids(P(longest_user), "read", "agent")) == Only((longest_id,))
    _run(policy.unregister_resource(agent))


def test_the_server_refuses_a_raw_zitadel_subject(fga):
    """Why the encoding exists: the id as the platform knows it is a malformed user for OpenFGA."""
    policy, client, _ = fga
    seed(policy, kinds=("agent",), ids=("x",))
    with pytest.raises(PolicyUnavailable) as err:
        _run(client.check(TupleKey(f"user:{PERSON}", "owner", "agent:acme/x")))
    assert "HTTP 400" in err.value.detail


def test_backfill_apply_with_an_owner_email_makes_the_zitadel_account_owner(fga):
    policy, client, _ = fga

    class Members:
        name = "zitadel"

        async def list_members(self, tenant_id):
            return [Member(id="400738350308982982", email="Boss@example.com", roles=("admin",))]

    class Catalog:
        def entries(self, tenant_id=None):
            return [
                CatalogEntry("agent", "a1", T, PERSON), CatalogEntry("secret", "ns:tok", T, "studio:operator"),
                CatalogEntry("workflow", "w1", T, "zitadel:7"),
            ]

    register_local(GROUP_MEMBERSHIP, "openfga-test-members", lambda ctx: Members())
    s = client.settings
    env = {
        "EAP_OPENFGA_URL": s.url, "EAP_OPENFGA_STORE_ID": s.store_id, "EAP_OPENFGA_MODEL_ID": s.model_id,
        "EAP_OPENFGA_PRESHARED_KEY": s.preshared_key or "", "EAP_IAM_MEMBERSHIP": "openfga-test-members",
    }
    argv = ["--tenant", T, "--owner-email", "boss@example.com", "--apply"]
    first, again = io.StringIO(), io.StringIO()
    assert _run(amain(argv, backfill=True, env=env, out=first, catalog=Catalog())) == 0
    assert "rows skipped" not in first.getvalue() and "legacy creator (no owner tuple): 1" in first.getvalue()
    assert _run(amain([*argv, "--prune"], backfill=True, env=env, out=again, catalog=Catalog())) == 0
    assert "(to add): 0" in again.getvalue() and "no longer in the catalog: 0" in again.getvalue()
    owners = sorted(g.subject for g in _run(policy.list_grants(R("secret", "ns:tok"))))
    assert owners == [PERSON]  # without cfaccess the address itself is never a principal (F1)
    assert sorted(_run(policy.filter_ids(P(PERSON), "manage_access", "agent")).ids) == ["a1"]
    assert _run(policy.filter_ids(P(PERSON), "read", "secret")) == Only(("ns:tok",))
    assert _run(policy.check(P("zitadel:7"), "archive", R("workflow", "w1"))).allowed


def test_the_email_subject_tool_deletes_exactly_those_tuples_on_a_real_server(fga):
    from eap_iam_openfga.email_subjects import amain as purge

    _, client, _ = fga
    s = client.settings
    _run(client.write([
        TupleKey("user:boss@example.com", "owner", "agent:acme/mail"),
        TupleKey("user:zitadel%3A7", "owner", "agent:acme/mail"),
        TupleKey("tenant:acme", "tenant", "agent:acme/mail"),
    ]))
    env = {
        "EAP_OPENFGA_URL": s.url, "EAP_OPENFGA_STORE_ID": s.store_id, "EAP_OPENFGA_MODEL_ID": s.model_id,
        "EAP_OPENFGA_PRESHARED_KEY": s.preshared_key or "",
    }
    first, again = io.StringIO(), io.StringIO()
    assert _run(purge(["--apply"], env=env, out=first)) == 0 and "deleted: 1" in first.getvalue()
    assert _run(purge(["--apply"], env=env, out=again)) == 0 and "deleted: 0" in again.getvalue()
    left = {t.user for t in _run(client.read()) if t.object == "agent:acme/mail"}
    assert left == {"user:zitadel%3A7", "tenant:acme"}
