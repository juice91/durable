"""OTLP trace ingest: store the spans an external exporter sent, for the key's tenant."""

from __future__ import annotations

from typing import Any

from eap_api.services import access as acl
from eap_api.services.access import Access, AccessBound
from eap_run_store.spans import SpanStore


class OtlpIngestService(AccessBound):
    """Ingest is API-key scoped (`otlp`, eap_iam.permits), which the policy engine leaves to the key's constraints; for a
    person (the dev bearer) it is the tenant's `otlp` action (admin, as the scope is)."""

    def __init__(self, spans: SpanStore, access: Access | None = None) -> None:
        self._spans = spans
        self._access = access  # None: no caller to check

    def store_spans(self, tenant_id: str, key_id: str | None, spans: list[Any]) -> None:
        acl.require(self._access, "ingest.otlp", "tenant", None)
        if spans:
            self._spans.insert_external(tenant_id, key_id, spans)
