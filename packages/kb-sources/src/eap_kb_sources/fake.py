"""A deterministic fake catalogue for tests of the sync workflow.

`size` generic parts `FAKE0001`..., each with `orderables` orderable records (`FAKE0001-A`, `-B`),
parametrics and quality records and one document per configured type (Markdown, so the knowledge
base can really ingest it). `sync_segment` honours the protocol: `page_budget` pages of `page_size`
keys (or the targeted part numbers), `call_budget` and the daily quota in the cursor. Every call is
recorded in `calls` (method, key); secrets are recorded by NAME (`secrets_seen`), and, test-only, by
value in `secret_values`. `bump(key)` raises a record's revision (its payload, its document bytes,
its ETag and what an incremental walk returns change with it). `fail` maps a key to an error code,
`unauthorized` makes the segment fail `connection_auth_failed`, `hook` runs at the start of every call.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date
from typing import Any

from eap_kb_sources.adapter import (
    ConnectionCheck,
    ConnectionSnapshot,
    DocumentPolicy,
    SecretResolver,
    SegmentCounters,
    SourceStoreFacade,
    SyncSegmentRequest,
    SyncSegmentResult,
)
from eap_kb_sources.budget import DailyBudget
from eap_kb_sources.errors import (
    CONNECTION_AUTH_FAILED,
    BudgetExhausted,
    SourceError,
    error_entry,
)

BUCKET = "product"


class FakeCatalogAdapter:
    kind = "product_catalog"
    provider = "fake"

    def __init__(
        self,
        *,
        size: int = 20,
        page_size: int = 5,
        orderables: int = 2,
        documents: tuple[str, ...] = ("datasheet",),
        fail: Mapping[str, str] | None = None,
        unauthorized: bool = False,
        daily_limit: int = 1000,
        sweep_ok: bool = True,
    ) -> None:
        self.size = size
        self.page_size = page_size
        self.orderables = orderables
        self.documents = documents
        self.fail = dict(fail or {})
        self.unauthorized = unauthorized
        self.daily_limit = daily_limit
        self.sweep_ok = sweep_ok  # what a complete walk tells the worker about removing unobserved records
        self.today: date | None = None  # tests pin the quota day
        self.revision: dict[str, int] = {self._key(n): 1 for n in range(1, size + 1)}
        self._clock = 1
        self.calls: list[tuple[str, str | None]] = []
        self.secrets_seen: list[set[str]] = []
        self.secret_values: list[dict[str, str]] = []  # test-only: what the adapter was given
        self.hook: Callable[[str, str | None], None] | None = None

    @staticmethod
    def _key(n: int) -> str:
        return f"FAKE{n:04d}"

    def bump(self, key: str) -> None:
        self._clock += 1
        self.revision[key] = self._clock

    def remove(self, key: str) -> None:
        """The product vanishes from the catalogue (it is no longer listed)."""
        self.revision.pop(key, None)

    def restore(self, key: str) -> None:
        self._clock += 1
        self.revision[key] = self._clock

    def count(self, method: str) -> int:
        return sum(1 for name, _ in self.calls if name == method)

    def _call(self, method: str, key: str | None) -> None:
        self.calls.append((method, key))
        if self.hook is not None:
            self.hook(method, key)

    def _resolve(self, connection: ConnectionSnapshot, secrets: SecretResolver) -> None:
        values = {logical: secrets.resolve(ref) for logical, ref in connection.secret_refs.items()}
        self.secrets_seen.append(set(values))
        self.secret_values.append(values)
        if self.unauthorized:
            raise SourceError(CONNECTION_AUTH_FAILED, "the fake credentials are refused")

    # ------------------------------------------------------------------------------- the protocol

    def validate_config(self, config: Mapping[str, Any]) -> None:
        limit = config.get("daily_limit")
        if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit < 0):
            raise ValueError("daily_limit must be a non-negative integer")

    async def check_connection(self, connection: ConnectionSnapshot, secrets: SecretResolver) -> ConnectionCheck:
        self._call("check_connection", None)
        try:
            self._resolve(connection, secrets)
        except SourceError as exc:
            return ConnectionCheck(False, exc.code)
        return ConnectionCheck(True)

    def _limit(self, request: SyncSegmentRequest) -> int:
        value = request.config.get("daily_limit")
        return value if isinstance(value, int) and not isinstance(value, bool) else self.daily_limit

    def _keys(self, request: SyncSegmentRequest) -> tuple[list[str], dict[str, Any], bool]:
        """The segment's keys, the cursor after them and whether the walk is done."""
        cursor = request.cursor
        if request.mode == "targeted":
            return list(request.part_numbers), dict(cursor), True
        all_keys = sorted(self.revision)
        since = int(cursor.get("changed_since", 0)) if request.mode == "incremental" else 0
        target = int(cursor.get("target_rev", self._clock))
        index = int(cursor.get("cursor", 0))
        keys: list[str] = []
        for _ in range(request.page_budget):
            page = 0
            while index < len(all_keys) and page < self.page_size:
                if self.revision[all_keys[index]] > since:
                    keys.append(all_keys[index])
                index += 1
                page += 1
        if index >= len(all_keys):
            return keys, {"changed_since": target}, True
        return keys, {"cursor": index, "changed_since": since, "target_rev": target}, False

    async def sync_segment(
        self,
        request: SyncSegmentRequest,
        connection: ConnectionSnapshot,
        secrets: SecretResolver,
        store: SourceStoreFacade,
    ) -> SyncSegmentResult:
        self._call("sync_segment", None)
        self._resolve(connection, secrets)
        daily = DailyBudget.from_checkpoint(request.cursor.get("daily_budget"), {BUCKET: self._limit(request)}, self.today)
        keys, after, done = self._keys(request)
        counters, errors, calls = SegmentCounters(), [], 0
        consumed = 0
        stopped_early = False
        for key in keys:
            if calls >= request.call_budget:
                stopped_early = True
                break
            self._call("fetch_record", key)
            code = self.fail.get(key)
            if code:
                if request.mode == "full":
                    store.upsert_record(key, "listing", {"key": key, "detail_pending": True})  # observed, not swept
                counters.failed += 1
                errors.append(error_entry(key, SourceError(code, f"{key} failed")))
                consumed += 1
                continue
            try:
                daily.consume(BUCKET)
            except BudgetExhausted:
                stopped_early = True
                break
            calls += 1
            consumed += 1
            self._store(key, request, store, counters, errors)
        # A segment that stopped inside its page budget re-walks from the segment's own start
        # (upserts are hash-guarded): the cursor does not advance, the daily counts do.
        cursor_out = {**request.cursor, "daily_budget": daily.as_state()} if stopped_early else {**after, "daily_budget": daily.as_state()}
        complete = done and not stopped_early
        revision = None
        if complete and request.mode != "targeted":
            revision = f"{len(self.revision)}:rev{max(self.revision.values(), default=0)}"
        return SyncSegmentResult(
            cursor_out, complete, counters, errors[:50], calls, sweep_ok=self.sweep_ok, source_revision=revision,
            budget=daily.snapshot(),
        )

    def _store(
        self, key: str, request: SyncSegmentRequest, store: SourceStoreFacade, counters: SegmentCounters,
        errors: list[dict[str, str]],
    ) -> None:
        rev = self.revision.get(key, 1)
        orderables = [f"{key}-{chr(ord('A') + i)}" for i in range(self.orderables)]
        raw = {
            "product": {"part": key, "title": f"Fake part {key}", "revision": rev, "orderables": orderables},
            "parametrics": {"parametrics": [{"name": "Vcc", "min": 3.0, "max": 5.5}]},
            "quality": {"fit": 3, "revision": rev},
        }
        counters.discovered += 1
        for record_type, payload in raw.items():
            result = store.upsert_record(key, record_type, payload)
            setattr(counters, result, getattr(counters, result) + 1)
        entity = store.upsert_entity(
            key,
            lifecycle_status="ACTIVE",
            attributes={"title": f"Fake part {key}", "revision": rev},
            identifiers=[("generic_part_number", key, True)] + [("orderable_part_number", o, False) for o in orderables],
            parameters=[
                {"canonical_name": "supply_voltage", "source_name": "Vcc", "value_type": "range",
                 "min_value": 3.0, "max_value": 5.5, "unit": "V"}
            ],
        )
        policy: DocumentPolicy = request.document_policy
        for doc_type in self.documents:
            if doc_type not in policy.types:
                continue
            url = f"https://fake.example/lit/{key}/{doc_type}.md"
            etag = f"rev{rev}"
            state = store.document_state(entity, doc_type, url)
            if state is not None and state.etag == etag:
                continue  # unchanged at the provider: no download
            if state is None:
                counters.documents_discovered += 1
            self._call("fetch_document", url)
            body = (
                f"# {key} {doc_type}\n\nRevision {rev} of the {doc_type} of {key}.\n\n"
                + "".join(f"Section {i}: parameters of {key}.\n\n" for i in range(1, 6))
            ).encode("utf-8")
            try:
                if store.submit_document(
                    entity, doc_type, url, body, mime_type="text/markdown", title=f"{key} {doc_type}", etag=etag,
                    source_revision=str(rev),
                ) == "queued":
                    counters.documents_queued += 1
            except SourceError as exc:
                counters.failed += 1
                errors.append(error_entry(key, exc))
