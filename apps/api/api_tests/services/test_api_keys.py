"""ApiKeyService: the refusals the store and the tenant's agent and workflow ids produce."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from eap_api.services.api_keys import API_KEY_NOT_FOUND_DETAIL, ApiKeyService
from eap_run_store import ApiKeyLimitExceeded
from eap_run_store.definition_errors import DefinitionConflict, DefinitionInvalid, DefinitionNotFound

KWARGS = dict(name="k", scopes=["agui:run"], agent_ids=None, workflow_ids=None, allowed_origins=None, expires_at=None)


class FakeStore:
    def __init__(self) -> None:
        self.raises: Exception | None = None
        self.revoked: object | None = object()

    def create(self, tenant_id, **kw):
        if self.raises:
            raise self.raises
        return SimpleNamespace(record="rec", key="pos_x", kw=kw, tenant_id=tenant_id)

    def revoke(self, tenant_id, key_id):
        return self.revoked

    def list(self, tenant_id):
        return ["a"]


class FakeDb:
    def __init__(self, known: list[str]) -> None:
        self.known = known

    def execute(self, _query):
        return SimpleNamespace(scalars=lambda: iter(self.known))


def make(known=(), store=None):
    return ApiKeyService(store or FakeStore(), FakeDb(list(known))), store


def test_create_passes_the_tenant_and_creator_to_the_store():
    svc = ApiKeyService(FakeStore(), FakeDb([]))
    created = svc.create("t1", "alice", **KWARGS)
    assert created.tenant_id == "t1" and created.kw["created_by"] == "alice" and created.key == "pos_x"


def test_an_unknown_agent_id_is_refused_naming_it_once():
    svc = ApiKeyService(FakeStore(), FakeDb(["a1"]))
    with pytest.raises(DefinitionInvalid) as raised:
        svc.create("t1", "alice", **{**KWARGS, "agent_ids": ["a1", "ghost", "ghost"]})
    assert raised.value.detail == "unknown agent ids: ghost"


def test_the_active_key_cap_is_a_conflict_and_a_bad_value_is_invalid():
    store = FakeStore()
    svc = ApiKeyService(store, FakeDb([]))
    store.raises = ApiKeyLimitExceeded("t", 5)
    with pytest.raises(DefinitionConflict) as conflict:
        svc.create("t", "u", **KWARGS)
    assert conflict.value.detail == str(ApiKeyLimitExceeded("t", 5))
    store.raises = ValueError("expires_at is in the past")
    with pytest.raises(DefinitionInvalid):
        svc.create("t", "u", **KWARGS)


def test_revoking_an_unknown_key_is_not_found():
    store = FakeStore()
    store.revoked = None
    with pytest.raises(DefinitionNotFound) as raised:
        ApiKeyService(store, FakeDb([])).revoke("t", "nope")
    assert raised.value.detail == API_KEY_NOT_FOUND_DETAIL
    store.revoked = object()
    ApiKeyService(store, FakeDb([])).revoke("t", "yes")
