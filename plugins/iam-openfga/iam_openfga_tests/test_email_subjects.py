"""F1: the tool that finds and deletes the tuples whose subject is an e-mail address (the `cfaccess` era owners). Dry run
by default, idempotent, counts by tuple type only, and it will not leave a resource without an owner unnoticed."""

import asyncio
import io

import httpx2
from eap_iam_openfga.email_subjects import amain

from .fakes import KEY, URL, FakeOpenFga

ENV = {"EAP_OPENFGA_URL": URL, "EAP_OPENFGA_STORE_ID": "s", "EAP_OPENFGA_PRESHARED_KEY": KEY}
EMAIL = "user:boss@example.com"
ACCOUNT = "user:zitadel%3Au1"

EMAIL_TUPLES = {
    (EMAIL, "owner", "agent:studio/a1"),
    (EMAIL, "owner", "secret:studio/crm_token"),
    (EMAIL, "runner", "workflow:studio/w1"),
    ("user:other.person@sub.example.org", "approver", "workflow:other/w9"),
}
KEPT = {
    (ACCOUNT, "owner", "agent:studio/a1"),
    (ACCOUNT, "owner", "secret:studio/crm_token"),
    ("user:operator", "owner", "agent:studio/a2"),
    ("user:pos_abc", "owner", "agent:studio/a3"),
    ("tenant:studio", "tenant", "agent:studio/a1"),
    ("tenant:studio#editor", "legacy_usable_by", "secret:studio/crm_token"),
    ("agent:studio/a1", "granted_to", "secret:studio/crm_token"),
}


def store() -> FakeOpenFga:
    fake = FakeOpenFga()
    fake.tuples |= EMAIL_TUPLES | KEPT
    return fake


def cli(fake, *args, env=ENV):
    out = io.StringIO()
    http = httpx2.AsyncClient(transport=fake.transport())
    code = asyncio.run(amain(list(args), env=env, out=out, http=http))
    return code, out.getvalue()


def test_a_dry_run_counts_by_type_writes_nothing_and_prints_no_subject_or_id():
    fake = store()
    code, text = cli(fake)
    assert code == 0 and fake.calls["write"] == 0 and fake.tuples == EMAIL_TUPLES | KEPT
    assert "dry run" in text and "tuples with an e-mail subject: 4" in text and "distinct e-mail subjects: 2" in text
    assert "owner:agent: 1" in text and "runner:workflow: 1" in text and "approver:workflow: 1" in text
    for leaked in ("boss@", "other.person", "crm_token", "a1", KEY):
        assert leaked not in text


def test_apply_deletes_exactly_the_email_tuples_and_a_second_run_is_a_no_op():
    fake = store()
    code, text = cli(fake, "--apply")
    assert code == 0 and "deleted: 4" in text and fake.tuples == KEPT
    fake.calls.clear()
    code, text = cli(fake, "--apply")
    assert code == 0 and fake.calls["write"] == 0 and "tuples with an e-mail subject: 0" in text


def test_an_object_that_would_lose_its_only_owner_stops_the_apply():
    fake = store()
    fake.tuples.add((EMAIL, "owner", "knowledge_base:studio/k1"))
    code, text = cli(fake)
    assert code == 0 and "would be left without an owner: 1" in text and "knowledge_base: 1" in text
    code, text = cli(fake, "--apply")
    assert code == 3 and fake.calls["write"] == 0 and "--allow-ownerless" in text
    code, text = cli(fake, "--apply", "--allow-ownerless")
    assert code == 0 and fake.tuples == KEPT


def test_a_tenant_filter_limits_what_is_deleted():
    fake = store()
    code, text = cli(fake, "--apply", "--tenant", "other")
    assert code == 0 and "deleted: 1" in text
    assert fake.tuples == (EMAIL_TUPLES - {("user:other.person@sub.example.org", "approver", "workflow:other/w9")}) | KEPT


def test_it_reads_every_page_and_writes_in_chunks():
    fake = FakeOpenFga()
    fake.tuples |= {(EMAIL, "runner", f"agent:studio/a{i:03}") for i in range(250)}
    code, _ = cli(fake, "--apply")
    assert code == 0 and not fake.tuples and fake.calls["read"] == 3 and fake.calls["write"] == 3


def test_it_refuses_while_cfaccess_is_still_a_configured_identity_provider(capsys):
    fake = store()
    code, _ = cli(fake, "--apply", env={**ENV, "EAP_IAM_IDENTITY": "zitadel,cfaccess"})
    assert code == 2 and fake.calls["write"] == 0 and "cfaccess" in capsys.readouterr().err


def test_an_engine_error_exits_nonzero_and_deletes_nothing(capsys):
    fake = store()
    fake.fail = lambda name: httpx2.Response(503, json={"code": "service_unavailable"})
    code, _ = cli(fake, "--apply")
    assert code == 1 and "HTTP 503" in capsys.readouterr().err and fake.tuples == EMAIL_TUPLES | KEPT


def test_not_configured_is_a_usage_error(capsys):
    assert asyncio.run(amain([], env={}, out=io.StringIO())) == 2
