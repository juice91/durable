import asyncio
from datetime import date

import pytest

from eap_kb_sources import ConnectionSnapshot, DocumentPolicy, DocumentState, EntityRef, SourceAdapter, SyncSegmentRequest
from eap_kb_sources.errors import SourceError
from eap_kb_sources.fake import FakeCatalogAdapter


class Secrets:
    def resolve(self, ref: str) -> str:
        return "value-of-" + ref.removeprefix("secret:")


class MemoryStore:
    """An in-memory SourceStoreFacade."""

    def __init__(self) -> None:
        self.records: dict[tuple[str, str], dict] = {}
        self.entities: dict[str, dict] = {}
        self.docs: dict[tuple[str, str], dict] = {}

    def upsert_record(self, external_id, record_type, payload, *, source_updated_at=None):
        old = self.records.get((external_id, record_type))
        self.records[(external_id, record_type)] = payload
        return "inserted" if old is None else ("unchanged" if old == payload else "updated")

    def upsert_entity(self, external_id, *, entity_type="product", lifecycle_status=None, attributes=None,
                      identifiers=(), parameters=()):
        self.entities[external_id] = {"identifiers": list(identifiers), "parameters": list(parameters)}
        return EntityRef(external_id, external_id, entity_type)

    def document_state(self, entity, relationship, url):
        d = self.docs.get((entity.external_id, relationship))
        return DocumentState(content_hash="h", etag=d["etag"]) if d else None

    def submit_document(self, entity, relationship, url, data, *, mime_type=None, title=None, etag=None,
                        last_modified=None, source_revision=None):
        old = self.docs.get((entity.external_id, relationship))
        self.docs[(entity.external_id, relationship)] = {"data": data, "etag": etag}
        return "unchanged" if old and old["data"] == data else "queued"


CONN = ConnectionSnapshot("t1", "c1", "fake", "fake", {}, {"client_secret": "secret:fake-secret"})


def seg(adapter, store, mode="full", cursor=None, **kw):
    request = SyncSegmentRequest(
        "s1", mode, cursor or {}, kw.pop("part_numbers", ()), kw.pop("page_budget", 10), kw.pop("call_budget", 200),
        kw.pop("config", {}), kw.pop("policy", DocumentPolicy()),
    )
    return asyncio.run(adapter.sync_segment(request, CONN, Secrets(), store))


def test_fake_is_a_source_adapter_and_validates_config():
    adapter = FakeCatalogAdapter()
    assert isinstance(adapter, SourceAdapter) and (adapter.kind, adapter.provider) == ("product_catalog", "fake")
    adapter.validate_config({"daily_limit": 5})
    with pytest.raises(ValueError):
        adapter.validate_config({"daily_limit": -1})


def test_check_connection_resolves_secrets_and_reports_auth_failure():
    adapter = FakeCatalogAdapter()
    assert asyncio.run(adapter.check_connection(CONN, Secrets())).ok
    assert adapter.secret_values == [{"client_secret": "value-of-fake-secret"}]
    bad = FakeCatalogAdapter(unauthorized=True)
    result = asyncio.run(bad.check_connection(CONN, Secrets()))
    assert (result.ok, result.error_code) == (False, "connection_auth_failed")


def test_a_full_walk_takes_segments_until_complete_and_the_cursor_resumes():
    adapter, store = FakeCatalogAdapter(size=12, page_size=5), MemoryStore()
    first = seg(adapter, store, page_budget=1)
    assert not first.complete and first.counters.discovered == 5 and first.next_cursor["cursor"] == 5
    second = seg(adapter, store, cursor=first.next_cursor, page_budget=1)
    assert [k for m, k in adapter.calls if m == "fetch_record"][5] == "FAKE0006"
    last = seg(adapter, store, cursor=second.next_cursor, page_budget=1)
    assert last.complete and last.counters.discovered == 2 and last.next_cursor["changed_since"] == 1
    assert len([k for k in store.records if k[1] == "product"]) == 12


def test_incremental_returns_only_what_changed_and_documents_skip_on_an_unchanged_etag():
    adapter, store = FakeCatalogAdapter(size=6), MemoryStore()
    done = seg(adapter, store)
    assert done.complete and done.counters.documents_queued == 6
    quiet = seg(adapter, store, "incremental", done.next_cursor)
    assert quiet.complete and quiet.counters.discovered == 0
    adapter.bump("FAKE0002")
    changed = seg(adapter, store, "incremental", done.next_cursor)
    assert changed.counters.discovered == 1 and changed.counters.updated == 2  # product and quality carry the revision
    assert store.entities["FAKE0002"]["identifiers"][0] == ("generic_part_number", "FAKE0002", True)
    assert changed.counters.discovered == 1
    assert changed.counters.documents_queued == 1 and adapter.count("fetch_document") == 7
    again = seg(adapter, store, "full", None)  # a full walk of unchanged etags downloads nothing
    assert adapter.count("fetch_document") == 7 and again.counters.documents_queued == 0


def test_targeted_takes_the_given_parts_and_counts_failures_without_raising():
    adapter, store = FakeCatalogAdapter(fail={"FAKE0002": "not_found"}), MemoryStore()
    result = seg(adapter, store, "targeted", part_numbers=("FAKE0001", "FAKE0002", "FAKE0003"))
    assert result.complete and result.counters.discovered == 2 and result.counters.failed == 1
    assert result.errors == [{"external_id": "FAKE0002", "code": "not_found", "message": "FAKE0002 failed"}]
    assert result.calls_used == 2


def test_the_call_budget_and_the_daily_quota_stop_a_segment_with_a_resumable_cursor():
    adapter, store = FakeCatalogAdapter(size=12, page_size=12), MemoryStore()
    adapter.today = date(2026, 11, 18)
    capped = seg(adapter, store, call_budget=3)
    assert not capped.complete and capped.calls_used == 3 and capped.next_cursor["daily_budget"]["used"] == {"product": 3}
    quota = seg(adapter, store, config={"daily_limit": 5}, cursor=capped.next_cursor)
    assert not quota.complete and quota.calls_used == 2 and quota.next_cursor["daily_budget"]["used"] == {"product": 5}
    spent = seg(adapter, store, config={"daily_limit": 5}, cursor=quota.next_cursor)
    assert spent.calls_used == 0 and not spent.complete


def test_unauthorized_raises_out_of_the_segment():
    with pytest.raises(SourceError) as info:
        seg(FakeCatalogAdapter(unauthorized=True), MemoryStore())
    assert info.value.code == "connection_auth_failed"


def test_calls_record_names_not_secret_values_and_the_policy_filters_documents():
    adapter = FakeCatalogAdapter(documents=("datasheet", "application_note"))
    store = MemoryStore()
    seg(adapter, store, "targeted", part_numbers=("FAKE0001",), policy=DocumentPolicy(types=("datasheet",)))
    assert list(store.docs) == [("FAKE0001", "datasheet")]
    assert "value-of" not in repr(adapter.calls) and adapter.secrets_seen == [{"client_secret"}]
