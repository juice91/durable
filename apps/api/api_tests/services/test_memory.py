"""MemoryService helpers and refusals, no database."""

from __future__ import annotations

import pytest
from eap_run_store.definition_errors import DefinitionError

from eap_api.services import memory as svc
from eap_api.services.memory import MemoryService, ResourceBusyRefusal


@pytest.mark.parametrize("raw,expected", [(None, None), ("3", 3), ('"3"', 3), ('W/"3"', 3), ("0", 0)])
def test_if_match_forms(raw, expected):
    assert svc.parse_if_match(raw) == expected


@pytest.mark.parametrize("raw", ['W/3', '"3', "abc", "1234567890", ""])
def test_a_malformed_if_match_is_a_422(raw):
    with pytest.raises(DefinitionError) as err:
        svc.parse_if_match(raw)
    assert err.value.status_code == 422


def test_a_malformed_id_is_a_404_like_an_unknown_one():
    with pytest.raises(DefinitionError) as err:
        svc.canonical_uuid_or_404("nope", svc.CONVERSATION_NOT_FOUND)
    assert (err.value.status_code, err.value.body) == (404, {"detail": "Conversation not found"})


def test_the_audit_hash_is_salted_when_a_salt_is_set(monkeypatch):
    monkeypatch.delenv(svc.AUDIT_HASH_SALT_ENV, raising=False)
    plain = svc.resource_audit_hash("a@b.c")
    monkeypatch.setenv(svc.AUDIT_HASH_SALT_ENV, "pepper")
    assert svc.resource_audit_hash("a@b.c") != plain and len(plain) == svc.AUDIT_HASH_HEX_CHARS


def test_a_busy_resource_is_a_conflict_that_lists_the_conversations():
    err = ResourceBusyRefusal(["c1", "c2"])
    assert err.busy_conversations == ["c1", "c2"] and "2 conversation(s)" in err.message


def test_an_unknown_conversation_is_a_404():
    class Runs:
        def get_conversation(self, tenant, conv_id):
            return None

    service = MemoryService(memory=None, run_store=Runs(), engine=None)
    with pytest.raises(DefinitionError) as err:
        service.get_conversation_note("t", "11111111-1111-1111-1111-111111111111")
    assert err.value.status_code == 404


def test_deleting_a_non_newest_observation_is_a_409():
    class Runs:
        def get_conversation(self, tenant, conv_id):
            return {"id": conv_id, "agent_id": "a"}

    class Memory:
        def delete_observation(self, *a, **kw):
            raise svc.ObservationNotLatest("c", "o")

    service = MemoryService(Memory(), Runs(), None)
    cid = "11111111-1111-1111-1111-111111111111"
    with pytest.raises(DefinitionError) as err:
        service.delete_observation("t", cid, cid)
    assert err.value.status_code == 409
