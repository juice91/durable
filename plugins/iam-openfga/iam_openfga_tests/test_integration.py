"""The engine, the bootstrap and the reconcile against a REAL OpenFGA (the version pinned in deploy/k8s/iam/openfga.yaml).

Skipped unless a server is reachable: set `EAP_TEST_OPENFGA_URL` (and `EAP_TEST_OPENFGA_KEY`) to use one, or
`EAP_TEST_OPENFGA_DOCKER=1` to start the pinned image in a throw-away container (memory datastore; removed afterwards;
`EAP_TEST_DOCKER_CONTEXT` names the docker context, `EAP_TEST_OPENFGA_IMAGE` overrides the image). A skipped test is not
a pass; CI Heavy runs these on the self-hosted runner.

What the fake cannot show: that the model means what the matrix says when the roles arrive as contextual tuples, that
the server accepts the JSON, `on_duplicate`/`on_missing`, ListObjects, BatchCheck and the page walk of Read."""

import asyncio
import os
import secrets
import shutil
import socket
import subprocess
import time
import uuid

import httpx2
import pytest
from eap_iam import All, Grant, Only, PolicyUnavailable, Principal, ResourceRef
from eap_iam_openfga.bootstrap import run as bootstrap_run
from eap_iam_openfga.client import OpenFgaClient, TupleKey
from eap_iam_openfga.engine import OpenFgaPolicy
from eap_iam_openfga.model import model_text
from eap_iam_openfga.reconcile import build_plan, apply_plan
from eap_iam_openfga.settings import OpenFgaSettings

IMAGE = os.environ.get("EAP_TEST_OPENFGA_IMAGE", "openfga/openfga:v1.23.0")
T = "acme"


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    # The docker context travels in the child's environment (DOCKER_CONTEXT), never as an argument, so the command
    # line is the fixed binary plus the arguments the tests build themselves.
    env = dict(os.environ)
    ctx = os.environ.get("EAP_TEST_DOCKER_CONTEXT")
    if ctx:
        env["DOCKER_CONTEXT"] = ctx
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120, check=check, env=env)


@pytest.fixture(scope="module")
def server():
    url = os.environ.get("EAP_TEST_OPENFGA_URL")
    if url:
        yield url.rstrip("/"), os.environ.get("EAP_TEST_OPENFGA_KEY") or None
        return
    if os.environ.get("EAP_TEST_OPENFGA_DOCKER") != "1":
        pytest.skip("no OpenFGA: set EAP_TEST_OPENFGA_URL, or EAP_TEST_OPENFGA_DOCKER=1 to start the pinned image")
    if shutil.which("docker") is None:
        pytest.skip("docker is not installed")
    key = secrets.token_urlsafe(24)
    name = f"eap-openfga-test-{uuid.uuid4().hex[:8]}"
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    started = _docker(
        "run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1:{port}:8080",
        "-e", "OPENFGA_DATASTORE_ENGINE=memory", "-e", "OPENFGA_AUTHN_METHOD=preshared",
        "-e", f"OPENFGA_AUTHN_PRESHARED_KEYS={key}", "-e", "OPENFGA_PLAYGROUND_ENABLED=false", IMAGE, "run", check=False,
    )
    if started.returncode != 0:
        pytest.skip("the OpenFGA container did not start (is the image available?)")
    try:
        url = f"http://127.0.0.1:{port}"
        for _ in range(60):
            try:
                if httpx2.get(url + "/healthz", timeout=2).status_code == 200:
                    break
            except httpx2.HTTPError:
                pass
            time.sleep(0.5)
        else:
            pytest.skip("the OpenFGA container never became healthy")
        yield url, key
    finally:
        _docker("rm", "-f", name, check=False)


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


def test_usable_by_lets_editors_attach_one_secret_only(fga):
    policy, client, _ = fga
    seed(policy, kinds=("secret",), ids=("crm", "other"))
    editor, admin = P("u-editor", "editor"), P("u-admin", "admin")
    _run(client.write([TupleKey("tenant:acme#editor", "usable_by", "secret:acme/crm")]))
    check = lambda p, action, ref: _run(policy.check(p, action, ref)).allowed  # noqa: E731
    assert check(editor, "grant_use", R("secret", "crm")) and not check(editor, "grant_use", R("secret", "other"))
    assert check(editor, "read", R("secret", "other")) and not check(editor, "edit", R("secret", "crm"))
    assert check(admin, "grant_use", R("secret", "other"))
    assert not check(P("u-viewer", "viewer"), "grant_use", R("secret", "crm"))


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


def test_reconcile_repairs_a_missing_link_and_a_second_run_is_a_no_op(fga):
    policy, client, _ = fga

    class Catalog:
        def entries(self, tenant_id=None):
            from eap_iam import CatalogEntry
            return [CatalogEntry("agent", f"a{i}", T) for i in range(130)] + [CatalogEntry("secret", "tok", T, "boss@example.com")]

    plan = _run(build_plan(client, Catalog(), owners=["u-owner"], creators=True))
    assert len(plan.to_add) == 131 + 131 + 1
    _run(apply_plan(client, plan, prune=False))
    again = _run(build_plan(client, Catalog(), owners=["u-owner"], creators=True))
    assert not again.to_add
    assert _run(policy.check(P("u-owner"), "manage_access", R("agent", "a129"))).allowed
    assert _run(policy.check(P("boss@example.com"), "archive", R("secret", "tok"))).allowed


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
