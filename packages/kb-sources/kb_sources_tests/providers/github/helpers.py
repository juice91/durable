"""Shared harness of the GitHub adapter tests: an in-memory store facade, a secret resolver, builders."""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

import httpx2

from eap_kb_sources import ConnectionSnapshot, DocumentPolicy, DocumentState, EntityRef, SyncSegmentRequest
from eap_kb_sources.adapter import LedgerDocument
from eap_kb_sources.errors import SECRET_MISSING, SourceError
from eap_kb_sources.providers.github import GithubRepositoryDocsAdapter
from eap_kb_sources.providers.github.fixtures import TOKEN, FakeGithub

REPO = "TexasInstruments/mcupsdk-core"
CONFIG = {
    "repository": REPO,
    "include": ["docs/**"],
    "exclude": ["docs/big/**"],
    "map": [
        {"match": "docs/api_guide/device/am263x/**", "metadata": {"sdk_family": "mcu-plus-sdk", "device": "AM263x"}},
        {"match": "**", "metadata": {"sdk_family": "mcu-plus-sdk"}},
    ],
}


class Secrets:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = values if values is not None else {"gh-token": TOKEN}
        self.resolved: list[str] = []

    def resolve(self, ref: str) -> str:
        name = ref.removeprefix("secret:")
        self.resolved.append(name)
        if name not in self.values:
            raise SourceError(SECRET_MISSING, f"secret {name} is not set")
        return self.values[name]


class MemoryStore:
    """The facade's contract in memory: records, one entity, a ledger keyed by document key."""

    def __init__(self) -> None:
        self.records: dict[tuple[str, str], dict[str, Any]] = {}
        self.entities: dict[str, dict[str, Any]] = {}
        self.docs: dict[str, dict[str, Any]] = {}  # key -> {data, status, source_revision, url, metadata, kb}
        self.submits: list[str] = []
        self.kb_deleted: list[str] = []
        self.entity_ref: EntityRef | None = None

    def upsert_record(self, external_id, record_type, payload, *, source_updated_at=None):
        old = self.records.get((external_id, record_type))
        self.records[(external_id, record_type)] = payload
        return "inserted" if old is None else ("unchanged" if old == payload else "updated")

    def record_unchanged(self, external_id, record_type, payload):
        return self.records.get((external_id, record_type)) == payload

    def upsert_entity(self, external_id, *, entity_type="product", lifecycle_status=None, attributes=None,
                      identifiers=(), parameters=()):
        self.entities[external_id] = {"entity_type": entity_type, "attributes": dict(attributes or {}),
                                      "identifiers": list(identifiers), "parameters": list(parameters)}
        self.entity_ref = EntityRef(f"ent-{external_id}", external_id, entity_type)
        return self.entity_ref

    def document_state(self, entity, relationship, url, *, key=None):
        d = self.docs.get(key)
        if d is None or d["status"] != "queued":
            return None
        return DocumentState(content_hash=d["hash"], status="queued", source_revision=d["source_revision"])

    def list_documents(self, entity, relationship):
        return [LedgerDocument(k, d["url"], d["source_revision"], d["status"]) for k, d in sorted(self.docs.items())]

    def remove_document(self, entity, key, *, delete):
        if delete:
            self.kb_deleted.append(key)
            self.docs.pop(key, None)  # the ledger row follows the knowledge-base document
        elif key in self.docs:
            self.docs[key]["status"] = "removed"

    def submit_document(self, entity, relationship, url, data, *, mime_type=None, title=None, etag=None,
                        last_modified=None, source_revision=None, metadata=None, key=None):
        old = self.docs.get(key)
        digest = hashlib.sha256(data).hexdigest()
        same = old is not None and old["hash"] == digest and old["metadata"] == dict(metadata or {})
        self.submits.append(key)
        self.docs[key] = {
            "data": data, "hash": digest, "status": "queued", "source_revision": source_revision, "url": url,
            "metadata": dict(metadata or {}), "mime": mime_type, "title": title,
        }
        return "unchanged" if same else "queued"


def conn(*, token: str | None = "secret:gh-token", base_url: str | None = None) -> ConnectionSnapshot:
    return ConnectionSnapshot("t1", "c1", "gh", "github", {}, {"token": token} if token else {}, base_url)


class Clock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def make_adapter(server: FakeGithub, *, clock: Clock | None = None) -> GithubRepositoryDocsAdapter:
    async def no_sleep(_: float) -> None: ...

    return GithubRepositoryDocsAdapter(
        http_factory=lambda: httpx2.AsyncClient(transport=httpx2.MockTransport(server)),
        sleep=no_sleep, clock=clock or Clock(), jitter=lambda: 0.0,
    )


def run_segment(adapter, store, *, mode="full", cursor=None, parts=(), config=None, connection=None, secrets=None,
                call_budget=200, source_id="s1"):
    request = SyncSegmentRequest(
        source_id, mode, cursor or {}, tuple(parts), 10, call_budget, {**CONFIG, **(config or {})}, DocumentPolicy(),
    )
    return asyncio.run(adapter.sync_segment(request, connection or conn(), secrets or Secrets(), store))


def walk_all(adapter, store, *, mode="full", cursor=None, config=None, call_budget=200, limit=50, **kw):
    """Segments until complete, like the worker's loop; (final result, results)."""
    results = []
    for _ in range(limit):
        result = run_segment(adapter, store, mode=mode, cursor=cursor, config=config, call_budget=call_budget, **kw)
        results.append(result)
        cursor = result.next_cursor
        if result.complete:
            return result, results
    raise AssertionError("the walk did not complete")
