"""Runs: start a run, read and list runs, read events, wait for a run (the business logic behind the run routes).

`RunService` implements `eap_api.ports.RunStarter`, so other services (the Architect's drafters, scorer
suggestions, workflow explanations) start and await runs through it instead of calling the route.
Every lookup is tenant-scoped: an unknown id or another tenant's is `NotFound`.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from eap_api.models import AgentModel
from eap_api.services import access as acl
from eap_api.services.access import Access, AccessBound
from eap_api.services.run_access import authorize_run
from eap_api.ports import RunLauncher, RunWaitTimeout, StartedRun
from eap_api.services.agent_archive import refuse_if_archived
from eap_api.services.run_filters import plain_refusals
from eap_api.services.errors import Conflict, InvalidInput, NotFound, Unavailable
from eap_api.services.run_filters import (
    BAD_CURSOR,
    RUN_LIST_TIMEOUT_DETAIL,
    list_param,
    metadata_filter,
    status_filter,
    text_query,
    time_window,
)
from eap_api.services.run_options import (
    DRAFT_HANDOFF_VERSION,
    check_spec_payload_size,  # noqa: F401 - re-exported
    resolve_run_options,
    same_options,
    version_row,
)
from eap_observability.context import current_trace_context
from eap_observability.redaction import log_failure
from eap_run_store import (
    ENQUEUE_FAILED_ERROR,
    TERMINAL_RUN_STATUSES,
    EventLog,
    InvalidRunCursor,
    MetricsQueryTimeout,
    RunCursor,
    RunIdConflict,
    RunStore,
)
from eap_run_store.chat_attachments import ATTACHMENT_CAPABILITIES_KEY
from eap_run_store.cursor import MAX_CURSOR_LENGTH  # noqa: F401 - re-exported for the routes' query limits
from eap_run_store.store import run_tags
from eap_schemas import ModelRef, ModelSettings

log = logging.getLogger("eap_api")

RUN_NOT_FOUND = "Run not found"
RUN_ID_IN_USE = "run_id is already in use"
RUN_ID_DIFFERENT = "run_id is already used by a different run request"
NOT_STARTED = "Run recorded but could not be started; retry the same request (same run_id)"
TURN_SUPERSEDED = "This turn could not be started and a later turn has started since; start a new turn"


def run_json(
    run: dict[str, Any], chat: dict[str, Any] | None = None, recovery_attempts: int = 0
) -> dict[str, Any]:
    """The API's run shape: `run_id`/`version` match the POST response; timestamps as ISO strings.

    `kind` is 'agent', 'tool', 'workflow' or 'kb_ingest' (a knowledge base ingest or reindex run:
    `knowledge_base_id` set, `input` its JSON mode, see eap_api.launcher). An agent run has `agent_id`/`version` and `tool_id`
    null; a tool run has `tool_id`, `agent_id`/`version` null, and `input`/`output` as JSON text; a
    workflow run has `workflow_id`/`workflow_version`, input/output as JSON text. A workflow's agent
    step runs as an agent run with `parent_run_id`/`parent_step_id` set (null otherwise).

    A structured-output run (`runs.output_format` json, agent capabilities milestone) also has
    `output_json`, the validated object; `output` stays its canonical JSON text. The key is absent
    for text runs and for runs without output.

    `chat` is the run's entry of RunStore.chat_usage (the agent's chat model only, as the trace's
    `chat_*` split); without one, `chat_input_tokens`, `chat_output_tokens` and `chat_cost_usd`
    are null (no usage recorded).

    `recovery_attempts` is DBOS's count of how often the run's workflow was resumed after a worker crash or
    a deploy (0 when DBOS has no row yet; RunLauncher.recovery_attempts). DBOS increments this on the first
    start, so a run that was never recovered shows `recovery_attempts == 1`.
    `recovered` is `True` only when the run was actually resumed (`recovery_attempts > 1`).
    `restarts` is the number of times the run was resumed (`max(recovery_attempts - 1, 0)`).
    The callers that have a launcher pass it; the others show 0 for all three."""
    iso = lambda v: v.isoformat() if v is not None else None  # noqa: E731
    restarts = max(recovery_attempts - 1, 0)
    return {
        "run_id": str(run["id"]),
        "kind": run.get("kind") or "agent",
        "agent_id": str(run["agent_id"]) if run["agent_id"] is not None else None,
        "version": run["agent_version"],
        "tool_id": run.get("tool_id"),
        "workflow_id": run.get("workflow_id"),
        "workflow_version": run.get("workflow_version"),
        # The knowledge base of a kb_ingest run, or of a tool run of the knowledge base search tester.
        "knowledge_base_id": str(run["knowledge_base_id"]) if run.get("knowledge_base_id") is not None else None,
        "parent_run_id": run.get("parent_run_id"),
        "parent_step_id": run.get("parent_step_id"),
        # The earlier run this one repeats ("Run again"); null otherwise.
        "rerun_of": run.get("rerun_of"),
        # A step-by-step run: the worker waits after every top-level step.
        "pause_after_each_step": bool(run.get("pause_after_each_step")),
        "recovery_attempts": recovery_attempts,
        "recovered": recovery_attempts > 1,
        "restarts": restarts,
        "status": run["status"],
        "display_status": run["display_status"],
        "input": run["input"],
        "output": run["output"],
        **output_json(run),
        "error": run["error"],
        "heartbeat_at": iso(run["heartbeat_at"]),
        "created_at": iso(run["created_at"]),
        "updated_at": iso(run["updated_at"]),
        "conversation_id": run.get("conversation_id"),
        "turn": run.get("turn"),
        "trace_id": run.get("trace_id"),
        # Usage and cost (P0.1). Totals are null until the run finishes, and for runs recorded
        # before usage existed ("not recorded"); cost_usd is a decimal string, null when any of the
        # run's model requests had no price.
        "model_id": run.get("model_id"),
        "model_overridden": bool(run.get("model_overridden") or False),
        "input_tokens": run.get("input_tokens"),
        "output_tokens": run.get("output_tokens"),
        "cost_usd": decimal_str(run.get("cost_usd")),
        # The agent's chat model alone (follow-up 3): the totals above also count moderation,
        # observer / reflector and embedding requests. Null when no usage was recorded.
        "chat_input_tokens": (chat or {}).get("chat_input_tokens"),
        "chat_output_tokens": (chat or {}).get("chat_output_tokens"),
        "chat_cost_usd": (chat or {}).get("chat_cost_usd"),
        # The model that answered the run's last chat request: a fallback model when one did, so
        # it differs from `model_id` (the requested model). Null before any chat request is recorded.
        "answering_model_id": (chat or {}).get("answering_model_id"),
        "started_at": iso(run.get("started_at")),
        "finished_at": iso(run.get("finished_at")),
        # Run options (P0.8): a draft test run has `version` null and `draft` true; the validated
        # request context ({} when none).
        "draft": run.get("draft_spec") is not None,
        "request_context": run.get("request_context") or {},
        # The settings as the caller sent them (re-postable as they are); the server's own attachment
        # decision rides beside them, never inside (ModelSettings is extra="forbid").
        "model_settings": {k: v for k, v in (run.get("model_settings") or {}).items() if k != ATTACHMENT_CAPABILITIES_KEY},
        "attachment_capabilities": (run.get("model_settings") or {}).get(ATTACHMENT_CAPABILITIES_KEY),
        # W2.2: tags derived from what the run records (eap_run_store.store.run_tags).
        "tags": run_tags({**run, "kind": run.get("kind") or "agent"}),
    }


def recovery_for(launcher: RunLauncher, run_ids: list[str]) -> dict[str, int]:
    """`recovery_attempts` per run id (one DBOS query for all of them); {} when the launcher cannot say."""
    lookup = getattr(launcher, "recovery_attempts", None)
    return lookup(run_ids) if lookup is not None else {}


def output_json(run: dict[str, Any]) -> dict[str, Any]:
    """`{"output_json": <object>}` for a run whose output is structured (`output_format` json:
    `output` is the canonical JSON text of the validated object), else `{}`, so the key is present
    only for json runs. Output that does not parse (never written by the worker; a defence) is
    `output_json: null`; a run without output yet (queued, running, failed) has no key either."""
    if run.get("output_format") != "json" or run.get("output") is None:
        return {}
    try:
        return {"output_json": json.loads(run["output"])}
    except (ValueError, RecursionError):  # RecursionError: nesting deeper than the parser's limit
        log.warning("run %s has output_format json but its output is not JSON", run.get("id"))
        return {"output_json": None}


def decimal_str(value: Any) -> str | None:
    """A Numeric(20, 10) value as a plain decimal string ("0.0001230000"), or None."""
    return None if value is None else format(value, "f")


def get_run_or_404(run_store: RunStore, tenant_id: str, run_id: uuid.UUID) -> dict[str, Any]:
    run = run_store.get_run(tenant_id, run_id)
    if run is None:
        raise NotFound(RUN_NOT_FOUND)
    return run


def lock_agent(db: Session, tenant_id: str, agent_id: uuid.UUID | str) -> AgentModel:
    """The tenant's agent, or NotFound `Agent not found` (another tenant's agent included), locked FOR
    SHARE until the request's transaction ends: an archive (FOR NO KEY UPDATE, like publish) waits
    for a run being recorded, or the request sees `archived` (eap_api.agent_archive)."""
    agent = (
        db.query(AgentModel)
        .filter(AgentModel.id == str(agent_id), AgentModel.tenant_id == tenant_id)
        .with_for_update(read=True)
        .first()
    )
    if agent is None:
        raise NotFound("Agent not found")
    return agent


def launch_recorded_run(
    db: Session, launcher: RunLauncher, run_store: RunStore, tenant_id: str, run: dict[str, Any], created: bool
) -> dict[str, Any]:
    """Hand the stored run to the orchestrator; returns the run as it stands afterwards.

    Enqueues exactly what the stored row says (hand-off contract), so a retry starts the recorded
    run, whether or not this request created the row: DBOS attaches a repeated workflow_id to the
    existing workflow, so a crash between insert and enqueue heals when the client retries.

    Same semantics as tool runs (POST /tools/{id}/runs). This request owns the hand-off of a row it
    created, or of one an earlier failed hand-off left failed and it re-queues now. A failed
    enqueue is `Unavailable`, and an owned run still `queued` is marked failed with ENQUEUE_FAILED_ERROR
    so it is not left queued with no workflow; a retry with the same run_id and input re-queues it
    and enqueues again. If DBOS did accept the enqueue that raised, the workflow runs anyway and
    its start step (mark_running) clears that error. A conversation turn is only re-queued while it
    is the latest turn: once a later turn has started, the retry is a `Conflict`.
    """
    run_id = uuid.UUID(str(run["id"]))
    owns_handoff = created or run_store.requeue_after_enqueue_failure(tenant_id, run_id)
    if not owns_handoff and run["conversation_id"] is not None:
        current = run_store.get_run(tenant_id, run_id) or run
        if current["status"] == "failed" and current["error"] == ENQUEUE_FAILED_ERROR:
            raise Conflict(TURN_SUPERSEDED)
    if run.get("draft_spec") is not None:
        # A draft test run: always its recorded snapshot, never the live draft (P0.8).
        spec, handoff_version = run["draft_spec"], DRAFT_HANDOFF_VERSION
    else:
        row = version_row(db, run["agent_id"], run["agent_version"])
        assert row is not None  # versions are immutable and never deleted
        spec, handoff_version = row.spec, run["agent_version"]
    try:
        launcher.launch(
            run_id=run_id,
            tenant_id=tenant_id,
            agent_id=uuid.UUID(str(run["agent_id"])),
            version=handoff_version,
            spec_json=json.dumps(spec),
            input_text=run["input"],
            traceparent=run.get("traceparent"),
        )
    except Exception as exc:
        # Type and code only: the error can quote the DBOS database URL or the enqueued payload.
        log_failure(
            log, "enqueue failed", exc, code="enqueue_failed", level=logging.ERROR,
            run_id=run["id"], tenant_id=tenant_id,
        )
        if owns_handoff:
            try:
                run_store.mark_enqueue_failed(tenant_id, run_id)
            except Exception as mark_exc:  # noqa: BLE001 - the refusal below is the answer either way
                log_failure(
                    log, "marking enqueue failure failed", mark_exc, code="enqueue_failed_mark",
                    level=logging.ERROR, run_id=run["id"], tenant_id=tenant_id,
                )
        raise Unavailable(NOT_STARTED) from None
    run = run_store.get_run(tenant_id, run_id) or run
    if run["error"] == ENQUEUE_FAILED_ERROR:
        # A concurrent request that owned the hand-off failed and marked the run failed, but this
        # enqueue succeeded: the workflow exists, and its start step clears that error.
        run = {**run, "status": "queued", "display_status": "queued", "error": None}
    log.info("run %s queued: tenant=%s agent=%s version=%s", run["id"], tenant_id, run["agent_id"], run["agent_version"])
    return run


@plain_refusals
class RunService(AccessBound):
    """Runs of agents: start (idempotently), read, list, events, histogram, wait. Implements RunStarter."""

    def __init__(
        self, db: Session, run_store: RunStore, event_log: EventLog, launcher: RunLauncher, access: Access | None = None
    ) -> None:
        self.db = db
        self.run_store = run_store
        self.event_log = event_log
        self.launcher = launcher
        self._access = access  # None: no caller to check (see eap_api.services.access)

    def _authorize(self, run: dict[str, Any], action: str = "read") -> None:
        """A run is as readable as the agent or workflow it belongs to (see `run_access.authorize_run`)."""
        authorize_run(self._access, self.run_store, run, action)

    # -- start ---------------------------------------------------------------------------------

    def start_run(
        self,
        tenant_id: str,
        agent_id: uuid.UUID,
        *,
        run_id: uuid.UUID,
        input_text: str,
        version: int | str | None = None,
        request_context: dict[str, Any] | None = None,
        model: ModelRef | None = None,
        model_settings: ModelSettings | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Insert the run idempotently (client run_id), then enqueue its workflow (durability rule 1).

        `(run, created)`: created when this call inserted the run, not when the tenant already had it. The
        enqueue is attempted in both cases, so a crash between insert and enqueue heals when the client
        retries. The request's trace id and traceparent are stored on the run."""
        acl.require(self._access, "run", "agent", str(agent_id))
        agent = lock_agent(self.db, tenant_id, agent_id)
        # A retry of a recorded run is compared with the row (below) and relaunched as recorded; only a
        # new run validates its options (a draft run against the live draft, then snapshotted).
        run = self.run_store.get_run(tenant_id, run_id)
        created = False
        if run is None:
            refuse_if_archived(agent)  # a new run only; a recorded one is replayed (eap_api.agent_archive)
            options = resolve_run_options(
                self.db, tenant_id, agent, version=version, request_context=request_context, model=model,
                input_text=input_text, model_settings=model_settings,
            )
            # The request span (FastAPI instrumentation) is current here; (None, None) when telemetry is off.
            trace_id, traceparent = current_trace_context()
            try:
                stored_id, created = self.run_store.create_run(
                    tenant_id, agent_id, options.target.version, input_text, run_id,
                    trace_id=trace_id, traceparent=traceparent, **options.store_kwargs(),
                )
            except RunIdConflict:
                raise Conflict(RUN_ID_IN_USE) from None
            run = get_run_or_404(self.run_store, tenant_id, stored_id)

        if not created and (
            run["agent_id"] != str(agent_id)
            or run["input"] != input_text
            or run["conversation_id"] is not None
            or not same_options(
                self.db, run, version=version, request_context=request_context, model=model,
                model_settings=model_settings,
            )
        ):
            raise Conflict(RUN_ID_DIFFERENT)

        run = launch_recorded_run(self.db, self.launcher, self.run_store, tenant_id, run, created)
        return run, created

    def start_agent_run(
        self,
        tenant_id: str,
        agent_id: uuid.UUID,
        *,
        input_text: str,
        run_id: uuid.UUID | None = None,
        version: int | None = None,
        request_context: dict[str, Any] | None = None,
    ) -> StartedRun:
        run, created = self.start_run(
            tenant_id, agent_id, run_id=run_id or uuid.uuid4(), input_text=input_text, version=version,
            request_context=request_context,
        )
        return StartedRun(run=run, created=created)

    # -- read ----------------------------------------------------------------------------------

    def require_run(self, tenant_id: str, run_id: uuid.UUID) -> dict[str, Any]:
        """The run, or NotFound when there is none or the caller may not read its agent or workflow."""
        run = get_run_or_404(self.run_store, tenant_id, run_id)
        self._authorize(run)
        return run

    def read_run(self, tenant_id: str, run_id: uuid.UUID) -> dict[str, Any] | None:
        """The run row for a caller that asks (MCP tools): None when there is none or it may not be read."""
        try:
            return self.require_run(tenant_id, run_id)
        except NotFound:
            return None

    def get_run(self, tenant_id: str, run_id: uuid.UUID) -> dict[str, Any] | None:
        return self.run_store.get_run(tenant_id, run_id)

    def read_run_json(self, tenant_id: str, run_id: uuid.UUID) -> dict[str, Any]:
        run = self.require_run(tenant_id, run_id)
        chat = self.run_store.chat_usage(tenant_id, [run["id"]])
        recovery = recovery_for(self.launcher, [str(run["id"])])
        return run_json(run, chat.get(str(run["id"])), recovery.get(str(run["id"]), 0))

    def wait_for_run(
        self, tenant_id: str, run_id: uuid.UUID, *, timeout: float, poll_interval: float = 0.5
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            run = self.run_store.get_run(tenant_id, run_id)
            if run is not None and run["status"] in TERMINAL_RUN_STATUSES:
                return run
            if time.monotonic() >= deadline:
                try:
                    self.run_store.cancel_run_tree(tenant_id, run_id)
                except Exception:  # noqa: BLE001 - the timeout below is the answer either way
                    log.warning("wait for run: could not cancel run %s", run_id)
                raise RunWaitTimeout(f"run {run_id} did not finish in {timeout} seconds")
            time.sleep(poll_interval)

    def list_events(self, tenant_id: str, run_id: uuid.UUID, after_seq: int) -> list[dict[str, Any]]:
        self.require_run(tenant_id, run_id)
        return self.event_log.list_events(tenant_id, run_id, after_seq=after_seq)

    def events_after(self, tenant_id: str, run_id: uuid.UUID, after_seq: int) -> list[dict[str, Any]]:
        """The events past `after_seq` without the run check (the stream loader, after the route checked it)."""
        return self.event_log.list_events(tenant_id, run_id, after_seq=after_seq)

    # -- list ----------------------------------------------------------------------------------

    def list_runs(
        self,
        tenant_id: str,
        *,
        limit: int,
        before: str | None,
        agent_id: uuid.UUID | None,
        run_status: list[str] | None,
        kind: str | None,
        tool_id: str | None,
        workflow_id: uuid.UUID | None,
        knowledge_base_id: uuid.UUID | None,
        parent_run_id: uuid.UUID | None,
        created_from: datetime | None,
        created_to: datetime | None,
        model: str | None,
        provider: str | None,
        tags: list[str] | None,
        metadata: list[str] | None,
        q: str | None,
    ) -> dict[str, Any]:
        """The tenant's runs, newest first (the Studio Traces and Logs pages), ordered by
    (created_at, run id) descending. `next_before` is an opaque cursor naming the last item's
    position when a full page came back (pass it back verbatim as `before`), else null. A cursor
    this API did not issue is a 422. `kind` ('agent' | 'tool' | 'workflow' | 'kb_ingest' | 'eval' | 'kb_source_sync'; eval
    runs exist from 0015_evals, until then the filter lists nothing), `tool_id`,
    `workflow_id` and `knowledge_base_id` filter (a knowledge base's runs: its kb_ingest runs and its
    search tester tool runs; add `kind=kb_ingest` for the ingest runs only). A workflow's child agent runs are listed only with `parent_run_id`.

    Metrics drill-down (P0.6), same meaning as in GET /metrics/*: `status` may be repeated or
    comma-separated (any of them; at most 10), `from` / `to` bound `created_at` to `[from, to)`
    (ISO 8601, UTC without an offset; `from` must be before `to`), `model` (`provider:name`) and
    `provider` keep runs that used that model, including through a workflow's child runs.

    Trace list filters (W2.2): `tags` (repeated or comma-separated, at most 10, all must match; runs
    carry derived tags, see eap_run_store.store.run_tags: `kind:agent`, `agent:<id>`, `model:<id>`,
    `draft`, `model-override`, `ctx:<key>`), `metadata=key=value` (repeated, at most 10; matches the
    run's request context value as text) and `q`, a case-insensitive substring of the run's input,
    output, error, tool id or model id (span names and trace payloads are not searched). `q` runs
    under the metrics statement timeout (503 when it fires)."""
        agent_ids = acl.sql_ids(acl.visible(self._access, "agent"))  # two engine calls, whatever the page size
        workflow_ids = acl.sql_ids(acl.visible(self._access, "workflow"))
        statuses = status_filter(run_status)
        tag_list = list_param(tags, "tags", split=True)
        metadata_map = metadata_filter(metadata) or {}
        start, end = time_window(created_from, created_to)
        cursor = None
        if before is not None:
            try:
                cursor = RunCursor.decode(before)
            except InvalidRunCursor:
                raise InvalidInput(BAD_CURSOR) from None
        try:
            runs = self.run_store.list_runs(
                tenant_id, limit=limit, before=cursor, agent_id=agent_id, status=statuses, kind=kind,
                tool_id=tool_id, workflow_id=workflow_id, knowledge_base_id=knowledge_base_id,
                parent_run_id=parent_run_id, created_from=start, created_to=end, model=model, provider=provider,
                tags=tag_list, metadata=metadata_map or None, text_query=text_query(q),
                visible_agent_ids=agent_ids, visible_workflow_ids=workflow_ids,
            )
        except MetricsQueryTimeout:
            # Only with `model` / `provider` / `q`: those filters run under the metrics statement timeout.
            raise Unavailable(RUN_LIST_TIMEOUT_DETAIL) from None
        next_before = RunCursor.after(runs[-1]).encode() if len(runs) == limit else None
        chat = self.run_store.chat_usage(tenant_id, [r["id"] for r in runs])
        recovery = recovery_for(self.launcher, [str(r["id"]) for r in runs])
        return {
            "items": [run_json(r, chat.get(str(r["id"])), recovery.get(str(r["id"]), 0)) for r in runs],
            "next_before": next_before,
        }
