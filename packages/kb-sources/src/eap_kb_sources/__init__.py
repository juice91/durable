"""Provider-neutral knowledge-base source sync: the adapter protocol, budgets, errors and a fake."""

from eap_kb_sources.adapter import (
    ConnectionCheck,
    ConnectionSnapshot,
    DocumentPolicy,
    DocumentState,
    EntityRef,
    SecretResolver,
    SegmentCounters,
    SourceAdapter,
    SourceStoreFacade,
    SyncSegmentRequest,
    SyncSegmentResult,
    document_key,
)
from eap_kb_sources.budget import Budget, DailyBudget, TokenBucket
from eap_kb_sources.errors import ERROR_CODES, BudgetExhausted, SourceError, error_entry
from eap_kb_sources.registry import adapter_for, register_adapter

__all__ = [
    "Budget", "BudgetExhausted", "ConnectionCheck", "ConnectionSnapshot", "DailyBudget", "DocumentPolicy",
    "DocumentState", "EntityRef", "ERROR_CODES", "SecretResolver", "SegmentCounters", "SourceAdapter", "SourceError",
    "SourceStoreFacade", "SyncSegmentRequest", "SyncSegmentResult", "TokenBucket", "adapter_for",
    "document_key", "error_entry", "register_adapter",
]
