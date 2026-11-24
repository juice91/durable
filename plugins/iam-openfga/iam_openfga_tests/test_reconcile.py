"""The reconcile and backfill: the diff, dry run by default, idempotence, what is never touched, and a summary that
counts types without ever printing an id."""

import asyncio
import io

import httpx2
import pytest
from eap_iam import CatalogEntry
from eap_iam_openfga.client import TupleKey
from eap_iam_openfga.reconcile import amain, build_plan, desired_tuples

from .fakes import KEY, URL, FakeOpenFga

ENV = {"EAP_OPENFGA_URL": URL, "EAP_OPENFGA_STORE_ID": "s", "EAP_OPENFGA_PRESHARED_KEY": KEY}


class Catalog:
    def __init__(self, *entries):
        self.rows = list(entries)

    def entries(self, tenant_id=None):
        return [e for e in self.rows if tenant_id in (None, e.tenant_id)]


def E(kind, id, tenant="studio", creator=None):
    return CatalogEntry(kind=kind, id=id, tenant_id=tenant, creator=creator)


CATALOG = Catalog(
    E("agent", "a1", creator="boss@example.com"),
    E("agent", "a2", creator="operator"),
    E("workflow", "w1", creator="zitadel:u9"),
    E("secret", "crm_token", creator="studio:operator"),
    E("knowledge_base", "k1"),
    E("schedule", "s1"),
    E("mcp_server", "m1"),
    E("api_key", "pos_abc", creator="dev:alice"),
    E("agent", "z1", tenant="other"),
)


def cli(fake, *args, backfill=False, catalog=CATALOG):
    out = io.StringIO()
    http = httpx2.AsyncClient(transport=fake.transport())
    code = asyncio.run(amain(list(args), backfill=backfill, env=ENV, out=out, catalog=catalog, http=http))
    return code, out.getvalue()


def test_desired_links_cover_every_resource_and_owners_are_explicit():
    links, legacy, skipped = desired_tuples(CATALOG.rows)
    assert len(links) == 9 and all(t.relation == "tenant" for t in links) and legacy == 0 and skipped == 0
    both, legacy, _ = desired_tuples(CATALOG.rows, ["zitadel:u1", "owner@example.com"], creators=True)
    owners = {t for t in both if t.relation == "owner"}
    # two explicit owners on 9 resources, plus the two creators who are real principals
    assert len([t for t in owners if t.user in ("user:zitadel:u1", "user:owner@example.com")]) == 18
    assert TupleKey("user:boss@example.com", "owner", "agent:studio/a1") in owners
    assert TupleKey("user:zitadel:u9", "owner", "workflow:studio/w1") in owners
    assert not any(t.user in ("user:operator", "user:studio:operator", "user:dev:alice") for t in owners)
    assert legacy == 3


def test_ids_openfga_cannot_hold_are_skipped_and_counted():
    links, _, skipped = desired_tuples([E("agent", "has space"), E("agent", "ok"), E("run", "r1"), E("agent", "x", tenant="a/b")])
    assert len(links) == 1 and skipped == 3


def test_a_dry_run_writes_nothing_and_says_what_it_would_do():
    fake = FakeOpenFga()
    code, text = cli(fake)
    assert code == 0 and fake.calls["write"] == 0 and not fake.tuples
    assert "dry run" in text and "missing in OpenFGA (to add): 9" in text and "link:agent: 3" in text


def test_apply_writes_the_links_and_a_second_run_is_a_no_op():
    fake = FakeOpenFga()
    code, text = cli(fake, "--apply")
    assert code == 0 and len(fake.tuples) == 9 and "applied" in text
    fake.calls.clear()
    code, text = cli(fake, "--apply")
    assert code == 0 and fake.calls["write"] == 0 and "(to add): 0" in text and len(fake.tuples) == 9


def test_backfill_adds_owners_for_the_account_and_the_real_creators_only():
    fake = FakeOpenFga()
    code, text = cli(fake, "--apply", "--owner", "zitadel:u1", "--owner", "owner@example.com", backfill=True)
    assert code == 0
    owners = {t for t in fake.tuples if t[1] == "owner"}
    assert ("user:zitadel:u1", "owner", "secret:studio/crm_token") in owners
    assert ("user:owner@example.com", "owner", "api_key:studio/pos_abc") in owners
    assert ("user:boss@example.com", "owner", "agent:studio/a1") in owners
    assert not any(u in ("user:operator", "user:dev:alice") for u, _, _ in owners)
    assert "legacy creator" in text
    fake.calls.clear()
    cli(fake, "--apply", "--owner", "zitadel:u1", "--owner", "owner@example.com", backfill=True)
    assert fake.calls["write"] == 0  # idempotent


def test_the_link_reconcile_never_removes_a_grant_or_a_revoked_owner_and_never_adds_owners():
    fake = FakeOpenFga()
    fake.tuples |= {
        ("user:zitadel:r", "runner", "workflow:studio/w1"),
        ("tenant:studio#editor", "usable_by", "secret:studio/crm_token"),
        ("user:zitadel:u1", "owner", "agent:studio/a1"),
    }
    cli(fake, "--apply", "--prune")
    assert ("user:zitadel:r", "runner", "workflow:studio/w1") in fake.tuples
    assert ("tenant:studio#editor", "usable_by", "secret:studio/crm_token") in fake.tuples
    assert ("user:zitadel:u1", "owner", "agent:studio/a1") in fake.tuples
    assert len([t for t in fake.tuples if t[1] == "owner"]) == 1


def test_orphans_are_reported_and_deleted_only_with_prune_and_apply():
    fake = FakeOpenFga()
    fake.tuples |= {
        ("tenant:studio", "tenant", "agent:studio/gone"),
        ("user:zitadel:r", "runner", "agent:studio/gone"),
        ("tenant:elsewhere", "tenant", "agent:elsewhere/untouched"),  # a tenant the catalog does not list
        ("workflow:studio/w1", "parent", "run:studio/r1"),  # runs are not this tool's
    }
    _, text = cli(fake, "--apply")
    assert "of objects no longer in the catalog: 2" in text and "kept" in text
    assert ("tenant:studio", "tenant", "agent:studio/gone") in fake.tuples
    _, text = cli(fake, "--apply", "--prune")
    assert ("tenant:studio", "tenant", "agent:studio/gone") not in fake.tuples
    assert ("user:zitadel:r", "runner", "agent:studio/gone") not in fake.tuples
    assert ("tenant:elsewhere", "tenant", "agent:elsewhere/untouched") in fake.tuples
    assert ("workflow:studio/w1", "parent", "run:studio/r1") in fake.tuples


def test_a_tenant_filter_scopes_both_sides():
    fake = FakeOpenFga()
    cli(fake, "--apply", "--tenant", "other")
    assert fake.tuples == {("tenant:other", "tenant", "agent:other/z1")}


def test_the_summary_counts_types_and_prints_no_id_or_subject():
    fake = FakeOpenFga()
    _, text = cli(fake, "--owner", "owner@example.com", backfill=True)
    for secret in ("crm_token", "pos_abc", "owner@example.com", "boss@example.com", "zitadel:u9", KEY):
        assert secret not in text
    assert "owner:secret: 1" in text


def test_prune_without_apply_is_refused(capsys):
    fake = FakeOpenFga()
    assert cli(fake, "--prune")[0] == 2 and sum(fake.calls.values()) == 0


def test_an_engine_error_stops_the_run_and_exits_nonzero(capsys):
    fake = FakeOpenFga()
    fake.fail = lambda name: httpx2.Response(503, json={"code": "service_unavailable"})
    code, _ = cli(fake, "--apply")
    assert code == 1 and "HTTP 503" in capsys.readouterr().err and not fake.tuples


def test_a_large_catalog_is_written_in_chunks_of_the_servers_limit():
    big = Catalog(*[E("agent", f"a{i}") for i in range(230)])
    fake = FakeOpenFga()
    assert cli(fake, "--apply", catalog=big)[0] == 0
    assert len(fake.tuples) == 230 and fake.calls["write"] == 3


def test_the_plan_reads_every_page_of_the_store():
    fake = FakeOpenFga()
    fake.tuples |= {("tenant:studio", "tenant", f"agent:studio/a{i:03}") for i in range(250)}
    plan = asyncio.run(build_plan(fake.client(), Catalog(*[E("agent", f"a{i:03}") for i in range(250)])))
    assert not plan.to_add and len(plan.present) == 250 and fake.calls["read"] == 3


def test_not_configured_is_a_usage_error(capsys):
    out = io.StringIO()
    assert asyncio.run(amain([], env={}, out=out, catalog=CATALOG)) == 2


def test_the_run_store_catalog_satisfies_the_core_catalog_protocol():
    from eap_iam import ResourceCatalog
    from eap_run_store.iam_catalog import RunStoreCatalog

    assert isinstance(RunStoreCatalog(object()), ResourceCatalog)  # structural: no engine is touched by constructing it
