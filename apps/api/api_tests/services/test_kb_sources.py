"""KbSourceService, with fake stores: not-found codes, the stale-revision conflict and launch refusals."""

from __future__ import annotations

import types

import pytest

from eap_api.services import kb_sources as svc
from eap_api.services.errors import Conflict, InvalidInput, NotFound
from eap_run_store.kb_source_launch import SourceLaunchError

TENANT = "t1"
KB_ID = "11111111-1111-1111-1111-111111111111"
SRC_ID = "22222222-2222-2222-2222-222222222222"


def source(revision=3):
    return types.SimpleNamespace(
        id=SRC_ID, knowledge_base_id=KB_ID, kind="catalog", provider="p", revision=revision, enabled=True
    )


class FakeSources:
    def __init__(self, found=None):
        self.found = found

    def get(self, tenant, sid):
        return self.found


def make(found=None, kb=True):
    kb_store = types.SimpleNamespace(get_knowledge_base=lambda t, k: {"id": k} if kb else None)
    return svc.KbSourceService(
        kb_store, FakeSources(found), connections=None, syncs=None, run_store=None, engine=None,
        launcher=None, trace_context=lambda: (None, None),
    )


def test_the_refusals_carry_the_codes_the_api_always_answered():
    with pytest.raises(NotFound) as err:
        make(kb=False).list_sources(TENANT, KB_ID)
    assert err.value.code == "knowledge_base_not_found"
    with pytest.raises(NotFound) as err:
        make(found=None).get_source(TENANT, KB_ID, SRC_ID)
    assert err.value.code == "source_not_found"
    with pytest.raises(NotFound) as err:
        make(found=source()).get_sync(TENANT, KB_ID, SRC_ID, "not-a-uuid")
    assert err.value.code == "sync_not_found"


def test_a_source_of_another_knowledge_base_is_not_found():
    other = types.SimpleNamespace(**{**source().__dict__, "knowledge_base_id": "33333333-3333-3333-3333-333333333333"})
    with pytest.raises(NotFound):
        make(found=other).get_source(TENANT, KB_ID, SRC_ID)


def test_a_stale_if_match_is_a_revision_mismatch_carrying_the_current_source(monkeypatch):
    monkeypatch.setattr(svc, "source_view", lambda s: {"id": s.id, "revision": s.revision})
    with pytest.raises(Conflict) as err:
        make(found=source(revision=3)).update_source(TENANT, KB_ID, SRC_ID, svc.SourcePatch(name="n"), expected_revision=2)
    assert err.value.code == "revision_mismatch"
    assert err.value.current_revision == 3 and err.value.current == {"id": SRC_ID, "revision": 3}


def test_payloads_are_validated_with_locations_only():
    with pytest.raises(InvalidInput) as err:
        svc.parse_payload([], svc.SourceCreate)
    assert err.value.code == "invalid_request"
    with pytest.raises(InvalidInput) as err:
        svc.parse_payload({"name": "", "extra": 1}, svc.SourceCreate)
    assert "extra" in err.value.message


def test_launch_refusals_keep_their_code_and_status(monkeypatch):
    def refuse(*a, **kw):
        raise SourceLaunchError("source_disabled", 409, "the source is disabled")

    monkeypatch.setattr(svc, "launch_sync", refuse)
    body = svc.SyncCreate(mode="full")
    with pytest.raises(Conflict) as err:
        make(found=source()).start_sync(TENANT, KB_ID, SRC_ID, body, None)
    assert err.value.code == "source_disabled"
