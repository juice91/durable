"""Knowledge-base sources and their syncs: the business logic behind `eap_api.routes.kb_sources`.

A source row, run or sync of another tenant is `NotFound`, never `Forbidden`. A refusal carries the stable
`code` the API has always answered with (`{"detail", "code"}`).
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError

from eap_api.ports import RunLauncher
from eap_api.services import access as acl
from eap_api.services.access import Access, AccessBound
from eap_api.services.errors import Conflict, InvalidInput, NotFound, ServiceError, Unavailable
from eap_observability.context import current_trace_context
from eap_kb_sources.check import load_providers
from eap_kb_sources.errors import SOURCE_ADAPTER_NOT_FOUND, SourceError
from eap_kb_sources.registry import adapter_for, list_adapters
from eap_run_store import RunStore
from eap_run_store.definition_errors import DefinitionError
from eap_run_store.kb_source_launch import LaunchFailed, SourceLaunchError, launch_sync, source_view
from eap_run_store.kb_sources import SOURCE_KINDS, ConnectionStore, Source, SourceNameConflict, SourceStore, SyncStore
from eap_run_store.knowledge import KnowledgeStore
from eap_run_store.platform_reads import sync_view  # the shape the Architect's platform.* tools return too

# The example workflow Studio publishes on request (the repository ships `examples/` in the image).
TEMPLATE_PATH = Path(
    os.environ.get("EAP_KB_SOURCE_WORKFLOW_TEMPLATE")
    or Path(__file__).resolve().parents[5] / "examples" / "workflows" / "kb-source-sync.json"
)
DEFAULT_CONNECTION_KIND = "oauth2_client_credentials"
SYNC_FINISHED = ("completed", "failed", "needs_attention")


class SourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_id: str
    name: Annotated[str, Field(min_length=1, max_length=100)]
    kind: str = SOURCE_KINDS[0]
    provider: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class SourcePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    config: dict[str, Any] | None = None
    enabled: bool | None = None


class SyncCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["targeted", "incremental", "full"]
    part_numbers: list[str] = Field(default_factory=list, max_length=1000)
    restart: StrictBool = False  # full / incremental: a new walk instead of resuming the stored cursor
    run_id: uuid.UUID | None = None


def parse_payload(payload: Any, model: type[BaseModel]) -> Any:
    """The validated model; InvalidInput `invalid_request` built from locations and fixed messages (never the input)."""
    if not isinstance(payload, dict):
        raise InvalidInput("body must be a JSON object", code="invalid_request")
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        parts = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors(include_input=False, include_url=False)]
        raise InvalidInput("; ".join(parts), code="invalid_request") from None


def parse_uuid(value: str) -> str | None:
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def load_workflow_template() -> dict[str, Any] | None:
    """The `spec` of the "KB Source Synchronization" example, or None when it is not shipped or unreadable."""
    try:
        spec = json.loads(TEMPLATE_PATH.read_text()).get("spec")
    except (OSError, ValueError, AttributeError):
        return None
    return spec if isinstance(spec, dict) else None


def adapter_view(info: Any) -> dict[str, Any]:
    """One registered adapter for Studio's forms. Display data is optional on an adapter (`display_name`,
    `connection_kind`, `default_base_url`, `default_token_url`, `config_schema`); a plug-in that exposes
    none still lists, with the provider name and OAuth client credentials."""
    try:
        adapter = adapter_for(info.kind, info.provider)
    except Exception:  # a factory that cannot build without a store must not hide the adapter
        adapter = None

    def attr(name: str) -> Any:
        return getattr(adapter, name, None)

    schema = attr("config_schema")
    return {
        "kind": info.kind,
        "provider": info.provider,
        "distribution": info.distribution,
        "display_name": attr("display_name") or info.provider,
        "connection_kind": attr("connection_kind") or DEFAULT_CONNECTION_KIND,
        "base_url": attr("default_base_url"),
        "token_url": attr("default_token_url"),
        "config_schema": schema if isinstance(schema, dict) else None,
    }


def _launch_refusal(exc: SourceLaunchError) -> ServiceError:
    kind = {404: NotFound, 409: Conflict, 422: InvalidInput}.get(exc.status, InvalidInput)
    return kind(exc.message, code=exc.code)


def _validate_config(kind: str, provider: str, config: dict[str, Any]) -> None:
    try:
        load_providers()
        adapter = adapter_for(kind, provider)
    except SourceError as exc:
        if exc.code == SOURCE_ADAPTER_NOT_FOUND:
            raise InvalidInput("no adapter for this kind and provider", code=SOURCE_ADAPTER_NOT_FOUND) from None
        raise
    try:
        adapter.validate_config(config)
    except ValueError as exc:
        raise InvalidInput(str(exc)[:300], code="invalid_source_config") from None


_KB_NOT_FOUND = ("knowledge base not found", "knowledge_base_not_found")
_SOURCE_NOT_FOUND = ("source not found", "source_not_found")


class KbSourceService(AccessBound):
    """Sources of a knowledge base and their syncs (tenant-scoped)."""

    def __init__(
        self,
        kb_store: KnowledgeStore,
        sources: SourceStore,
        connections: ConnectionStore,
        syncs: SyncStore,
        run_store: RunStore,
        engine: Any,
        launcher: RunLauncher,
        access: Access | None = None,
    ) -> None:
        self._access = access  # None: no caller to check
        self.kb_store = kb_store
        self.sources = sources
        self.connections = connections
        self.syncs = syncs
        self.run_store = run_store
        self.engine = engine
        self.launcher = launcher

    # ------------------------------------------------------------------ catalogue

    def workflow_template(self) -> dict[str, Any]:
        acl.require(self._access, "read", "tenant", None)
        spec = load_workflow_template()
        if spec is None:
            raise Unavailable("the sync workflow template is not available", code="workflow_template_unavailable")
        return spec

    def adapters(self) -> list[dict[str, Any]]:
        acl.require(self._access, "read", "tenant", None)
        return [adapter_view(info) for info in list_adapters()]

    # ------------------------------------------------------------------ lookups

    def _require_kb(self, tenant_id: str, kb_id: str, action: str = "read") -> None:
        # A source and its syncs are checked through their knowledge base (read to see, edit to change).
        acl.require(self._access, action, "knowledge_base", str(kb_id))
        if self.kb_store.get_knowledge_base(tenant_id, kb_id) is None:
            raise NotFound(_KB_NOT_FOUND[0], code=_KB_NOT_FOUND[1])

    def _source_in_kb(self, tenant_id: str, kb_id: str, source_id: str) -> Source | None:
        sid, kid = parse_uuid(source_id), parse_uuid(kb_id)
        if sid is None or kid is None:
            return None
        source = self.sources.get(tenant_id, sid)
        return source if source is not None and source.knowledge_base_id == kid else None

    def _require_source(self, tenant_id: str, kb_id: str, source_id: str, action: str = "read") -> Source:
        acl.require(self._access, action, "knowledge_base", str(kb_id))
        source = self._source_in_kb(tenant_id, kb_id, source_id)
        if source is None:
            raise NotFound(_SOURCE_NOT_FOUND[0], code=_SOURCE_NOT_FOUND[1])
        return source

    # ------------------------------------------------------------------ sources

    def create_source(self, tenant_id: str, kb_id: str, body: SourceCreate) -> tuple[dict[str, Any], int]:
        """The new source's view and revision."""
        self._require_kb(tenant_id, kb_id, "edit")
        cid = parse_uuid(body.connection_id)
        if cid is None or self.connections.get(tenant_id, cid) is None:
            raise NotFound("connection not found", code="connection_not_found")
        if body.kind not in SOURCE_KINDS:
            raise InvalidInput("unknown source kind", code="invalid_request")
        _validate_config(body.kind, body.provider, body.config)
        try:
            made = self.sources.create(
                tenant_id, knowledge_base_id=str(uuid.UUID(kb_id)), connection_id=cid, name=body.name,
                kind=body.kind, provider=body.provider, config=body.config, enabled=body.enabled,
            )
        except SourceNameConflict:
            raise Conflict("a source with this name exists", code="source_name_conflict") from None
        return source_view(made), made.revision

    def list_sources(self, tenant_id: str, kb_id: str) -> dict[str, Any]:
        self._require_kb(tenant_id, kb_id)
        rows = self.sources.list_for_kb(tenant_id, str(uuid.UUID(kb_id)))
        return {"items": [source_view(s) for s in rows]}

    def get_source(self, tenant_id: str, kb_id: str, source_id: str) -> tuple[dict[str, Any], int]:
        source = self._require_source(tenant_id, kb_id, source_id)
        return source_view(source), source.revision

    def update_source(
        self, tenant_id: str, kb_id: str, source_id: str, body: SourcePatch, expected_revision: int | None
    ) -> tuple[dict[str, Any], int]:
        """Change a source. A stale `expected_revision` is a `Conflict` `revision_mismatch` carrying the
        current source (the API answers 412 with its ETag)."""
        from eap_run_store.revisions import RevisionMismatch

        source = self._require_source(tenant_id, kb_id, source_id, "edit")
        if body.config is not None:
            _validate_config(source.kind, source.provider, body.config)

        def stale() -> Conflict:
            found = self.sources.get(tenant_id, source.id) or source
            return Conflict(
                f"this resource changed since you read it (revision {found.revision}, you sent "
                f"{expected_revision}); re-read it or send the current ETag to overwrite",
                code="revision_mismatch", current=source_view(found), current_revision=found.revision,
            )

        if expected_revision is not None and source.revision != expected_revision:
            raise stale()
        try:
            changed = self.sources.update(
                tenant_id, source.id, name=body.name, config=body.config, enabled=body.enabled,
                expected_revision=expected_revision,
            )
        except RevisionMismatch:
            raise stale() from None
        except SourceNameConflict:
            raise Conflict("a source with this name exists", code="source_name_conflict") from None
        if changed is None:
            raise NotFound(_SOURCE_NOT_FOUND[0], code=_SOURCE_NOT_FOUND[1])
        return source_view(changed), changed.revision

    # ------------------------------------------------------------------ syncs

    def start_sync(
        self, tenant_id: str, kb_id: str, source_id: str, body: SyncCreate, created_by: str | None
    ) -> dict[str, Any]:
        source = self._require_source(tenant_id, kb_id, source_id, "edit")
        trace_id, traceparent = current_trace_context()
        try:
            return launch_sync(
                self.engine, self.launcher, tenant_id=tenant_id, source_id=source.id, mode=body.mode,
                part_numbers=body.part_numbers, restart=body.restart, run_id=body.run_id or uuid.uuid4(),
                created_by=created_by, traceparent=traceparent, trace_id=trace_id,
            )
        except SourceLaunchError as exc:
            raise _launch_refusal(exc) from None
        except LaunchFailed as exc:
            err = DefinitionError(exc.detail, run_id=exc.run_id)
            err.status_code = 503
            raise err from None

    def list_syncs(self, tenant_id: str, kb_id: str, source_id: str, limit: int) -> dict[str, Any]:
        source = self._require_source(tenant_id, kb_id, source_id)
        rows = self.syncs.list_for_source(tenant_id, source.id, limit)
        return {"items": [sync_view(s) for s in rows]}

    def get_sync(self, tenant_id: str, kb_id: str, source_id: str, run_id: str) -> dict[str, Any]:
        acl.require(self._access, "read", "knowledge_base", str(kb_id))
        source = self._source_in_kb(tenant_id, kb_id, source_id)
        rid = parse_uuid(run_id)
        missing = NotFound("sync not found", code="sync_not_found")
        if source is None or rid is None:
            raise missing
        sync = self.syncs.get_by_run(tenant_id, rid)
        if sync is None or sync.source_id != source.id:
            raise missing
        run = self.run_store.get_run(tenant_id, uuid.UUID(rid))
        report: Any = None
        if run is not None and run["status"] in SYNC_FINISHED and run.get("output"):
            try:
                report = json.loads(run["output"])
            except (TypeError, ValueError):
                report = None
        return {**sync_view(sync), "report": report, "run_status": run["status"] if run else None}
