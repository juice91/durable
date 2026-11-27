"""The one encoding between platform ids and OpenFGA ids (docs/iam.md, "Ids in OpenFGA"): OpenFGA refuses a second ':'
in `type:id`, and platform subjects (`zitadel:<sub>`) have one."""

import asyncio

import pytest
from eap_iam import All, Grant, GrantError, Only, Principal, ResourceRef, UseGrant
from eap_iam_openfga.client import TupleKey
from eap_iam_openfga.engine import OpenFgaPolicy
from eap_iam_openfga.names import (
    decode_id,
    encode_id,
    object_ref,
    split_object,
    strip_object,
    subject_of,
    user_ref,
)

from .fakes import FakeOpenFga

SUBJECTS = [
    "zitadel:400738350308982982", "boss@example.com", "run:0b1c", "apikey:pos_abc", "cfaccess:ann@example.com",
    "studio:operator", "a%3Ab", "a%b", "100%", "%%::", "a/b:c", "使用者:é😀", "a\\b", "plain",
]


@pytest.mark.parametrize("raw", SUBJECTS)
def test_an_id_round_trips_and_has_no_colon_once_encoded(raw):
    encoded = encode_id(raw)
    assert ":" not in encoded and decode_id(encoded) == raw
    assert subject_of(user_ref(raw)) == raw
    ref = object_ref(ResourceRef("secret", raw, "acme"))
    assert ref.count(":") == 1 and strip_object(ref, "secret", "acme") == raw and split_object(ref) == ("secret", "acme", raw)


def test_the_encoding_is_what_the_docs_say_and_distinct_ids_stay_distinct():
    assert user_ref("zitadel:123") == "user:zitadel%3A123"
    assert user_ref("a%3Ab") == "user:a%253Ab"
    assert object_ref(ResourceRef("secret", "a:b%c", "acme")) == "secret:acme/a%3Ab%25c"
    assert len({encode_id(s) for s in SUBJECTS}) == len(SUBJECTS)


@pytest.mark.parametrize("stored", ["zitadel:1", "a%3ab", "a%zz", "a%", "*", ""])
def test_a_stored_id_the_encoder_could_not_have_written_decodes_to_nothing(stored):
    assert decode_id(stored) is None
    assert subject_of(f"user:{stored}") is None
    assert split_object(f"agent:acme/{stored}") is None and strip_object(f"agent:acme/{stored}", "agent", "acme") is None


@pytest.mark.parametrize("userset", ["tenant:acme#admin", "group:g#member", "agent:acme/a", "user:a#member"])
def test_only_a_user_ref_names_a_subject(userset):
    assert subject_of(userset) is None


@pytest.mark.parametrize("raw", ["", "a b", "a\tb", "a\nb", "a　b", "a#b", "*", "a*", "a\x00b", "a\x1bb", "a\x7fb", "a\x85b", "a\ud800b", None, 7])
def test_an_id_openfga_cannot_hold_is_refused_with_a_clear_error(raw):
    with pytest.raises(GrantError) as user:
        user_ref(raw)
    assert user.value.code == "invalid_subject"
    with pytest.raises(GrantError) as obj:
        object_ref(ResourceRef("agent", raw, "acme"))
    assert obj.value.code == "invalid_resource"


def test_the_length_limits_are_the_servers_and_apply_to_the_encoded_ref():
    # A user is at most 512 bytes, an object at most 256 characters, both with the type.
    assert len(user_ref("u" * 507)) == 512
    for long in ("u" * 508, ":" * 170, "é" * 254):
        with pytest.raises(GrantError, match="too long"):
            user_ref(long)
    assert len(object_ref(ResourceRef("agent", "a" * 245, "acme"))) == 256
    for long in ("a" * 246, ":" * 82):
        with pytest.raises(GrantError, match="too long"):
            object_ref(ResourceRef("agent", long, "acme"))


def test_nothing_reaches_openfga_for_an_id_it_cannot_hold():
    fake = FakeOpenFga()
    policy = OpenFgaPolicy(fake.client())
    agent, secret = ResourceRef("agent", "a" * 300, "acme"), ResourceRef("secret", "s", "acme")
    for call in (
        policy.register_resource(ResourceRef("agent", "ok", "acme"), creator="u" * 600),
        policy.register_resource(agent),
        policy.grant(Grant("u" * 600, "owner", secret)),
        policy.grant_use(UseGrant(agent, secret)),
    ):
        with pytest.raises(GrantError):
            asyncio.run(call)
    long_user = Principal(tenant_id="acme", kind="user", subject="u" * 600, provider="t", roles=frozenset({"admin"}))
    assert asyncio.run(policy.check(long_user, "read", secret)).reason == "deny:no_subject"
    assert asyncio.run(policy.filter_ids(long_user, "read", "agent")) == Only(())
    assert not fake.calls


def test_the_engine_sends_encoded_ids_and_answers_in_platform_ids():
    fake = FakeOpenFga()
    policy = OpenFgaPolicy(fake.client())
    person = Principal(tenant_id="acme", kind="user", subject="zitadel:7", provider="t", roles=frozenset({"viewer"}))
    secret = ResourceRef("secret", "ns:tok", "acme")
    run = lambda c: asyncio.run(c)  # noqa: E731

    run(policy.register_resource(secret, creator="zitadel:7"))
    run(policy.grant(Grant("zitadel:8", "owner", secret)))
    assert ("user:zitadel%3A7", "owner", "secret:acme/ns%3Atok") in fake.tuples
    assert [g.subject for g in run(policy.list_grants(secret))] == ["zitadel:7", "zitadel:8"]
    run(policy.revoke(Grant("zitadel:8", "owner", secret)))
    assert [g.subject for g in run(policy.list_grants(secret))] == ["zitadel:7"]

    fake.allowed.add(("user:zitadel%3A7", "can_read", "secret:acme/ns%3Atok"))
    assert run(policy.check(person, "read", secret)).allowed
    assert run(policy.check_many(person, "read", [secret]))["ns:tok"].allowed
    assert run(policy.filter_ids(person, "read", "secret")) == Only(("ns:tok",))
    assert fake.contextual_seen[-1] == [TupleKey("user:zitadel%3A7", "viewer", "tenant:acme").as_json()]
    assert not isinstance(run(policy.filter_ids(person, "read", "secret", ["ns:tok"])), All)
    # Over the list limit the candidates are checked one by one, with the same encoding.
    limited = OpenFgaPolicy(fake.client(list_limit=1))
    assert run(limited.filter_ids(person, "read", "secret", ["ns:tok", "other", "bad id"])) == Only(("ns:tok",))

    agent = ResourceRef("agent", "a:1", "acme")
    run(policy.grant_use(UseGrant(agent, secret)))
    assert run(policy.grantees(secret)) == [agent]
    fake.allowed.add(("agent:acme/a%3A1", "can_use_as_agent", "secret:acme/ns%3Atok"))
    assert run(policy.used_by(agent, "secret")) == ["ns:tok"]
    run(policy.unregister_resource(secret))
    assert not fake.tuples
