"""OTLP trace ingest: store the spans an external exporter sent, for the key's tenant."""

from __future__ import annotations

from typing import Any

from eap_run_store.spans import SpanStore


class OtlpIngestService:
    def __init__(self, spans: SpanStore) -> None:
        self._spans = spans

    def store_spans(self, tenant_id: str, key_id: str | None, spans: list[Any]) -> None:
        if spans:
            self._spans.insert_external(tenant_id, key_id, spans)
