"""ConnectionService: validation, refusals and the stale-revision conflict, on a fake store (no database)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from eap_api.services.connections import ConnectionService
from eap_api.services.errors import Conflict, InvalidInput, NotFound
from eap_run_store.kb_sources import Connection, ConnectionInUse, ConnectionNameConflict
from eap_run_store.revisions import RevisionMismatch

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
TENANT = "t1"
CID = str(uuid.uuid4())


def make(revision: int = 1, name: str = "gh") -> Connection:
    return Connection(
        id=CID, tenant_id=TENANT, name=name, kind="api_token", base_url=None, config={"provider": "github"},
        credential_refs={"token": "secret:gh"}, enabled=True, last_checked_at=None, last_status=None,
        last_error_code=None, created_at=NOW, updated_at=NOW, revision=revision,
    )


class FakeStore:
    def __init__(self) -> None:
        self.row: Connection | None = make()
        self.raise_on_create: Exception | None = None
        self.raise_on_update: Exception | None = None
        self.raise_on_delete: Exception | None = None
        self.calls: list[tuple] = []

    def create(self, tenant_id, **kw):
        self.calls.append(("create", tenant_id, kw))
        if self.raise_on_create:
            raise self.raise_on_create
        return make(name=kw["name"])

    def list(self, tenant_id):
        return [self.row] if self.row else []

    def get(self, tenant_id, cid):
        return self.row if self.row and cid == CID else None

    def update(self, tenant_id, cid, **kw):
        self.calls.append(("update", tenant_id, cid, kw))
        if self.raise_on_update:
            raise self.raise_on_update
        return make(revision=2) if self.row else None

    def delete(self, tenant_id, cid):
        if self.raise_on_delete:
            raise self.raise_on_delete
        return self.row is not None


@pytest.fixture
def svc():
    service = ConnectionService(None, lambda engine: None)
    service._store = FakeStore()
    return service


def body(**over):
    return {"name": "gh", "kind": "api_token", "config": {"provider": "github"},
            "credential_refs": {"token": "secret:gh"}, **over}


def test_create_returns_the_view_with_references_only(svc):
    made = svc.create(TENANT, body())
    assert made["credential_refs"] == {"token": "secret:gh"} and made["revision"] == 1


@pytest.mark.parametrize("over, code", [
    ({"credential_refs": {"token": "plain-value"}}, "secret_ref_required"),
    ({"base_url": "http://example.com"}, "invalid_request"),
    ({"base_url": "https://user:pw@example.com"}, "invalid_request"),
    ({"config": {}}, "invalid_request"),
    ({"kind": "nope"}, "invalid_request"),
    ({"credential_refs": {"other": "secret:x"}}, "invalid_request"),
    ({"surprise": 1}, "invalid_request"),
])
def test_create_refuses_bad_input_with_a_stable_code(svc, over, code):
    with pytest.raises(InvalidInput) as raised:
        svc.create(TENANT, body(**over))
    assert raised.value.code == code
    assert "plain-value" not in raised.value.message


def test_create_refuses_a_body_that_is_not_an_object(svc):
    with pytest.raises(InvalidInput) as raised:
        svc.create(TENANT, ["x"])
    assert raised.value.message == "body must be a JSON object"


def test_a_duplicate_name_is_a_conflict(svc):
    svc._store.raise_on_create = ConnectionNameConflict()
    with pytest.raises(Conflict) as raised:
        svc.create(TENANT, body())
    assert raised.value.code == "connection_name_conflict"


def test_get_of_a_bad_or_unknown_id_is_not_found(svc):
    for value in ("not-a-uuid", str(uuid.uuid4())):
        with pytest.raises(NotFound) as raised:
            svc.get(TENANT, value)
        assert raised.value.code == "connection_not_found"


def test_update_with_a_stale_revision_conflicts_and_carries_the_current_connection(svc):
    svc._store.row = make(revision=5)
    with pytest.raises(Conflict) as raised:
        svc.update(TENANT, CID, {"enabled": False}, expected_revision=3)
    assert raised.value.code == "revision_mismatch"
    assert raised.value.current_revision == 5 and raised.value.current["id"] == CID
    assert not [c for c in svc._store.calls if c[0] == "update"]  # nothing written


def test_update_whose_guarded_write_loses_the_race_is_the_same_conflict(svc):
    svc._store.raise_on_update = RevisionMismatch(9, 1)
    with pytest.raises(Conflict) as raised:
        svc.update(TENANT, CID, {"enabled": False}, expected_revision=1)
    assert raised.value.code == "revision_mismatch"


def test_update_without_a_precondition_writes_and_returns_the_new_revision(svc):
    changed = svc.update(TENANT, CID, {"enabled": False}, expected_revision=None)
    assert changed["revision"] == 2
    assert svc._store.calls[-1][3]["expected_revision"] is None


def test_update_of_a_missing_connection_is_not_found(svc):
    svc._store.row = None
    with pytest.raises(NotFound):
        svc.update(TENANT, CID, {"enabled": False}, expected_revision=None)


def test_delete_refuses_while_a_source_refers_to_it_and_404s_when_missing(svc):
    svc._store.raise_on_delete = ConnectionInUse()
    with pytest.raises(Conflict) as raised:
        svc.delete(TENANT, CID)
    assert raised.value.code == "connection_in_use"
    svc._store.raise_on_delete = None
    svc.delete(TENANT, CID)
    svc._store.row = None
    with pytest.raises(NotFound):
        svc.delete(TENANT, CID)
