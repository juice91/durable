"""FastAPI dependency providers: the only module (besides `app.py`, the composition root) that touches `app.state`.

`create_app()` puts the long-lived objects (engine, stores, launcher, limiters) on `app.state`. Everything
else gets them from here: a route takes a `<Name>Dep` parameter, and a helper that only has a `Request`
calls the matching `get_<name>(request)`. Code that has an app but no request (the MCP control endpoint)
uses the `*_of(app)` functions. A test replaces a provider with `app.dependency_overrides[get_<name>]`.

Stores that `create_app` does not wire are made on first use and kept on the app, as they always were.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Generator
from typing import Annotated, Any, TypeVar

from eap_blob_store import BlobStore
from eap_iam import IamRuntime, load_iam
from eap_run_store import (
    AgUiStore,
    ApiKeyStore,
    AuditStore,
    ApprovalStore,
    EventLog,
    MetricsStore,
    RunStore,
    ScheduleStore,
)
from eap_run_store.chat_attachments import ChatAttachmentStore, MessageFeedbackStore
from eap_run_store.evals import EvalStore
from eap_run_store.external_access import ExternalAccessStore
from eap_run_store.knowledge import KnowledgeStore
from eap_run_store.memory import MemoryStore
from eap_run_store.scorers import ScorerStore
from eap_run_store.spans import SpanStore
from eap_run_store.trace_feedback import TraceFeedbackStore
from eap_run_store.webhooks import WebhookStore
from fastapi import Depends, Request
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from eap_api.launcher import RunLauncher
from eap_api.inbound_guard import InboundGuard, ip_rate_from_env
from eap_api.otlp_limits import TokenBucketLimiter, rate_limit_from_env
from eap_api.sse import DeltaSource, StreamLimiter

T = TypeVar("T")

_LIMITER_INIT_LOCK = threading.Lock()


# --- app-level accessors (an app, no request) -------------------------------------------------------


def database_engine_of(app: Any) -> Engine:
    return app.state.database_engine


def open_session(engine: Engine) -> Session:
    """A new database session on `engine`; the caller closes it (for entry points that hold an app, not a request)."""
    return Session(engine)


def run_stores_of(app: Any) -> tuple[RunStore, EventLog]:
    return app.state.run_stores


def run_launcher_of(app: Any) -> RunLauncher:
    return app.state.run_launcher


def _lazy(app: Any, name: str, factory: Callable[[], T]) -> T:
    """The object kept on the app under `name`, made by `factory` on first use."""
    value = getattr(app.state, name, None)
    if value is None:
        value = factory()
        setattr(app.state, name, value)
    return value


def audit_store_of(app: Any) -> AuditStore:
    """The AuditStore on the app's database engine, made once per app."""
    return _lazy(app, "audit_store", lambda: AuditStore(database_engine_of(app)))


def api_key_store_of(app: Any) -> ApiKeyStore:
    """The ApiKeyStore on the app's database engine, made once per app."""
    return _lazy(app, "api_key_store", lambda: ApiKeyStore(database_engine_of(app)))


def iam_of(app: Any) -> IamRuntime:
    """The IAM runtime (identity chain, policy engine, membership source): built by `create_app`, or on first use for an
    app assembled without it (a test's bare FastAPI app), from the same configuration."""
    return _lazy(app, "iam", lambda: load_iam(getattr(app.state, "database_engine", None)))


def mcp_limiter_of(app: Any) -> StreamLimiter:
    """The MCP control endpoint's stream limiter (EAP_MCP_MAX_CONCURRENT_*), made once per app."""
    import os

    return _lazy(
        app,
        "mcp_limiter",
        lambda: StreamLimiter(
            max_global=int(os.environ.get("EAP_MCP_MAX_CONCURRENT_GLOBAL", "200")),
            max_per_principal=int(os.environ.get("EAP_MCP_MAX_CONCURRENT_PER_KEY", "10")),
        ),
    )


def external_guard_of(app: Any, ip_per_minute: int) -> InboundGuard:
    """The external surface's request limiter (per address and per key; in memory, per process), made once per app."""
    return _lazy(app, "external_guard", lambda: InboundGuard(ip_per_minute))


def external_limiter_of(app: Any, max_global: int, max_per_key: int) -> StreamLimiter:
    """The external surface's calls-in-flight limiter, made once per app."""
    return _lazy(app, "external_limiter", lambda: StreamLimiter(max_global=max_global, max_per_principal=max_per_key))


def mcp_control_app_of(app: Any) -> Any | None:
    """The mounted MCP control ASGI app, or None when the endpoint is not mounted."""
    return getattr(app.state, "mcp_control_app", None)


# --- database ---------------------------------------------------------------------------------------


def get_database_engine(request: Request) -> Engine:
    return database_engine_of(request.app)


EngineDep = Annotated[Engine, Depends(get_database_engine)]


def get_session(request: Request) -> Generator[Session, None, None]:
    """Create a new database session using the engine from app state."""
    session = Session(get_database_engine(request))
    try:
        yield session
    finally:
        session.close()


SessionDep = Annotated[Session, Depends(get_session)]


# --- runs, events, launcher -------------------------------------------------------------------------


def require_run_storage(request: Request) -> tuple[RunStore, EventLog]:
    return run_stores_of(request.app)


StoresDep = Annotated[tuple[RunStore, EventLog], Depends(require_run_storage)]


def get_run_launcher(request: Request) -> RunLauncher:
    return run_launcher_of(request.app)


RunLauncherDep = Annotated[RunLauncher, Depends(get_run_launcher)]


def get_delta_source(request: Request) -> DeltaSource:
    return request.app.state.delta_source


DeltaSourceDep = Annotated[DeltaSource, Depends(get_delta_source)]


def get_stream_limiter(request: Request) -> StreamLimiter:
    app = request.app
    limiter = getattr(app.state, "stream_limiter", None)
    if limiter is None:
        with _LIMITER_INIT_LOCK:
            limiter = getattr(app.state, "stream_limiter", None)
            if limiter is None:
                limiter = app.state.stream_limiter = StreamLimiter.from_env()
    return limiter


StreamLimiterDep = Annotated[StreamLimiter, Depends(get_stream_limiter)]


# --- stores built on the run stores' engine ---------------------------------------------------------


def approval_store_of(app: Any) -> ApprovalStore:
    """The ApprovalStore on the run stores' engine, made once per app."""
    return _lazy(app, "approval_store", lambda: ApprovalStore(run_stores_of(app)[0].engine))


def get_approval_store(request: Request) -> ApprovalStore:
    return approval_store_of(request.app)


ApprovalStoreDep = Annotated[ApprovalStore, Depends(get_approval_store)]


def eval_store_of(app: Any) -> EvalStore:
    """The EvalStore on the run stores' engine, made once per app."""
    return _lazy(app, "eval_store", lambda: EvalStore(run_stores_of(app)[0].engine))


def get_eval_store(request: Request) -> EvalStore:
    return eval_store_of(request.app)


EvalStoreDep = Annotated[EvalStore, Depends(get_eval_store)]


def get_span_store(request: Request) -> SpanStore:
    """The tenant-agnostic span store (every call takes the tenant), one per app."""
    return _lazy(request.app, "span_store", lambda: SpanStore(require_run_storage(request)[0].engine))


SpanStoreDep = Annotated[SpanStore, Depends(get_span_store)]


def schedule_store_of(app: Any) -> ScheduleStore:
    return _lazy(app, "schedule_store", lambda: ScheduleStore(run_stores_of(app)[0].engine))


def get_schedule_store(request: Request) -> ScheduleStore:
    return schedule_store_of(request.app)


ScheduleStoreDep = Annotated[ScheduleStore, Depends(get_schedule_store)]


def scorer_store_of(app: Any) -> ScorerStore:
    return _lazy(app, "scorer_store", lambda: ScorerStore(run_stores_of(app)[0].engine))


def get_scorer_store(request: Request) -> ScorerStore:
    return scorer_store_of(request.app)


ScorerStoreDep = Annotated[ScorerStore, Depends(get_scorer_store)]


def get_trace_feedback_store(request: Request) -> TraceFeedbackStore:
    return _lazy(
        request.app, "trace_feedback_store", lambda: TraceFeedbackStore(require_run_storage(request)[0].engine)
    )


TraceFeedbackStoreDep = Annotated[TraceFeedbackStore, Depends(get_trace_feedback_store)]


# --- stores built on the database engine ------------------------------------------------------------


def knowledge_store_of(app: Any) -> KnowledgeStore:
    return _lazy(app, "knowledge_store", lambda: KnowledgeStore(database_engine_of(app)))


def get_knowledge_store(request: Request) -> KnowledgeStore:
    # create_app wires it; an app built without that wiring (tests) gets one on first use.
    return _lazy(request.app, "knowledge_store", lambda: KnowledgeStore(get_database_engine(request)))


KnowledgeStoreDep = Annotated[KnowledgeStore, Depends(get_knowledge_store)]


def get_memory_store(request: Request) -> MemoryStore:
    # Built lazily on the API's engine (create_app does not wire it).
    return _lazy(request.app, "memory_store", lambda: MemoryStore(get_database_engine(request)))


MemoryStoreDep = Annotated[MemoryStore, Depends(get_memory_store)]


def get_metrics_store(request: Request) -> MetricsStore:
    """The app's MetricsStore (created on first use; tests may set `metrics_store` on the app state)."""
    return _lazy(request.app, "metrics_store", lambda: MetricsStore(get_database_engine(request)))


MetricsStoreDep = Annotated[MetricsStore, Depends(get_metrics_store)]


def get_message_feedback_store(request: Request) -> MessageFeedbackStore:
    return _lazy(request.app, "message_feedback_store", lambda: MessageFeedbackStore(get_database_engine(request)))


MessageFeedbackStoreDep = Annotated[MessageFeedbackStore, Depends(get_message_feedback_store)]


def get_chat_attachment_store(request: Request) -> ChatAttachmentStore:
    return _lazy(request.app, "chat_attachment_store", lambda: ChatAttachmentStore(get_database_engine(request)))


ChatAttachmentStoreDep = Annotated[ChatAttachmentStore, Depends(get_chat_attachment_store)]


def get_ag_ui_store(request: Request) -> AgUiStore:
    return _lazy(request.app, "ag_ui_store", lambda: AgUiStore(get_database_engine(request)))


AgUiStoreDep = Annotated[AgUiStore, Depends(get_ag_ui_store)]


def get_iam(request: Request) -> IamRuntime:
    return iam_of(request.app)


IamDep = Annotated[IamRuntime, Depends(get_iam)]


def get_api_key_store(request: Request) -> ApiKeyStore:
    """The ApiKeyStore on the app's database engine, made once per app."""
    return api_key_store_of(request.app)


ApiKeyStoreDep = Annotated[ApiKeyStore, Depends(get_api_key_store)]


def get_audit_store(request: Request) -> AuditStore:
    return audit_store_of(request.app)


AuditStoreDep = Annotated[AuditStore, Depends(get_audit_store)]


# --- blobs and limiters -----------------------------------------------------------------------------


def get_blob_store(request: Request) -> BlobStore:
    return request.app.state.blob_store


BlobStoreDep = Annotated[BlobStore, Depends(get_blob_store)]


def get_otlp_limiter(request: Request) -> TokenBucketLimiter:
    return _lazy(request.app, "otlp_limiter", lambda: TokenBucketLimiter(rate_limit_from_env()))


OtlpLimiterDep = Annotated[TokenBucketLimiter, Depends(get_otlp_limiter)]


def get_webhook_guard(request: Request) -> InboundGuard:
    """The inbound webhook limiter and refusal counters (in memory, per process)."""
    return _lazy(request.app, "webhook_guard", lambda: InboundGuard(ip_rate_from_env()))


WebhookGuardDep = Annotated[InboundGuard, Depends(get_webhook_guard)]


def webhook_store_of(app: Any) -> WebhookStore:
    return _lazy(app, "webhook_store", lambda: WebhookStore(run_stores_of(app)[0].engine))


def get_webhook_store(request: Request) -> WebhookStore:
    return webhook_store_of(request.app)


WebhookStoreDep = Annotated[WebhookStore, Depends(get_webhook_store)]


def external_access_store_of(app: Any) -> ExternalAccessStore:
    return _lazy(app, "external_access_store", lambda: ExternalAccessStore(run_stores_of(app)[0].engine))


def get_external_access_store(request: Request) -> ExternalAccessStore:
    return external_access_store_of(request.app)


ExternalAccessStoreDep = Annotated[ExternalAccessStore, Depends(get_external_access_store)]
