"""Built-in trace of one run, derived from its durable events (P0.7).

`TraceReader.read(tenant_id, run_id)` returns `{run, spans, totals, truncated, limits}` or None
when the tenant owns no such run (unknown ids and other tenants' runs look the same). Everything is
read from one REPEATABLE READ snapshot and every query is filtered by `tenant_id`.

Spans (kind, where they come from, start → end):

- `run` (the requested run) and `child_run` (an agent step's child run of a workflow run, nested
  under its `workflow_step`): `started_at` (else `created_at`) → `finished_at` (else the `end`
  event); end null while the run is not terminal.
- `model_request`: request `n` from the run's previous durable event to its `model:{n}:final`
  event (output `processor_result` events, written between a response and its `model:{n}:final`,
  do not count as that previous event: the request spans up to them); tokens and cost from that request's `run_model_requests` row (the event's own `usage`,
  `model_id`, `cost_usd` fields when the row is missing), `usage: null` when neither exists (runs
  recorded before usage existed).
- `tool_call`: `tool:{id}:call` → `tool:{id}:result` (agent and tool runs), a sibling of the model
  requests under the run, with `attributes.model_request` naming the request that issued it.
- `processor`: one `processor_result` event (`processor:input:{i}`, or
  `processor:output:{i}:model:{n}` for model request n, `attributes.model_request` = n; older
  `processor:output:{i}` keys are still read),
  zero length, named by `processor_type`; attributes phase, index, processor_type, action, reason,
  match_counts and categories, never text; a moderation check also carries model_id, usage and
  cost_usd from its usage row (`moderation:input:{i}` or `moderation:output:{i}:model:{n}`, the
  event key's `:model:{n}` suffix carried over). Status `error` for a block.
- `workflow_step`: `step:{id}:started` → `step:{id}:completed|failed`; approval steps carry the
  waiting data and the decision. Steps a parallel step's branches ran are nested under that
  parallel step (from the workflow spec, when the caller provides it); all others under the run.

RAG and memory spans (RM.19). Attributes are ids, counts, scores, model, usage and cost only, never
memory or chunk text; the retrieve span's input and output are the tool call's arguments and
result, exactly what the `tool_call` span showed before (the model's query and the text it saw):
- `retrieve`: a `tool_call` of the auto-added `retrieve` tool, or of the hidden `knowledge.search`
  tool (the search tester's tool run), instead of a `tool_call` span. Attributes add `query`,
  `knowledge_base` (the model's choice, a name), `top_k` (the tool run's argument, else the only
  attached knowledge base's), `knowledge_bases` (`[{id, top_k, min_score}]` from the agent version's
  spec, when the caller provides it), `hits` (the structured sources: n, title, url, ref, kb_id,
  document_id, document_key, chunk_id, doc_version, score) and `hit_count`.
- `embedding`: an `embedding` event (`embed:retrieve:{call}` nested under its retrieve span) or an
  ingest batch (`ingest_progress` `embed:batch:{i}`), from the previous event to it: model,
  dimensions, inputs, input type, usage and cost from its usage row.
- `memory_recall` / `memory_index`: the `embedding` event of semantic recall (`embed:recall:{turn}`:
  hits, turns, groups, messages, counts only) or indexing (`embed:index:{turn}`: inserted,
  existing, redacted), the same way; `…:failed` keys give an `error` span with its `error_code`.
- `observer` / `reflector`: a `memory_updated` (`memory:observe|reflect:{turn}`) or
  `memory_observe_failed` (`…:failed`) event, from the previous event to it, with the chat usage of
  `memory:{stage}:{turn}`: memory kind, covered turns, approx tokens, notes, redacted, superseded.
- `working_memory`: a `memory_updated` event of `update_working_memory` (`memory:wm:{call}`),
  zero length, under its tool call: scope, format, version and the changed keys (schema names).
- `ingest`: the `chunked` and `finalized` phases of a kb_ingest run (zero length): generation,
  embedded, total, pending tokens, document id and version.
- No span: `post_turn_done` (`memory:post_turn:{turn}`, P1.C1) is a marker on its run span,
  attribute `post_turn` = {observe, index, done_at}; `memory_observe_trimmed` (P1.C6, the reflector
  input cap) is skipped. Neither is the start of the next span.

Ids are stable strings (`run:{run_id}`, `{run_id}:model:{n}`, `{run_id}:tool:{call_id}`,
`{run_id}:processor:{phase}:{i}`, `{run_id}:step:{step_id}`), so a run recovered after a crash
(whose events are idempotent by key) never shows a duplicate span. Spans are listed parents first.

Totals count every model request of the run and its child runs, moderation requests included
(`moderation_requests` says how many of them were moderation checks), embedding calls included
(`embedding_requests`, with `embedding_input_tokens` and `embedding_cost_usd`; RM.19) and observer
and reflector requests included (`memory_requests`). `input_tokens`, `output_tokens` and
`cost_usd` are over all of them; the split by operation (request_operation, as Metrics classifies
requests) is `chat_requests`, `chat_input_tokens`, `chat_output_tokens`, `chat_cost_usd` (the
agent's model only: what a chat turn shows), `memory_input_tokens`, `memory_output_tokens`,
`memory_cost_usd` (observer and reflector), `moderation_input_tokens`, `moderation_output_tokens`,
`moderation_cost_usd`, plus the embedding fields above; chat + memory + moderation + embedding
input tokens equal `input_tokens`. A run span carries the same token split for its own requests.

Status: `ok`, `error`, `running` (not finished yet), `waiting` (an approval step waiting for a
decision) or `incomplete` (never finished, and the run has ended). Run spans keep the run's own
status in `attributes.run_status`.

Bounds: at most MAX_SPANS spans (the first ones in tree order are kept); at most MAX_CHILD_RUNS
child runs, MAX_EVENTS_PER_RUN events of one run and MAX_TRACE_EVENTS events in all are read (the
run's own first, then each child's in creation order; children past the budget are not loaded:
`truncated.child_runs`, and a run cut short sets `truncated.events`); each span's `input` and `output` above FIELD_MAX_BYTES of
JSON (or once PAYLOAD_BUDGET_BYTES has been spent on the trace) becomes a string preview of its
JSON text, and the span's `truncated` names the field with its original size in bytes. Every
query runs under a statement timeout (STATEMENT_TIMEOUT_MS): TraceQueryTimeout, a 503 in the API.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from eap_run_store.store import (
    ExecutionEventModel,
    RunModel,
    RunModelRequestModel,
    TERMINAL_RUN_STATUSES,
    _run_dict,
)

MAX_SPANS = 2000
MAX_EVENTS_PER_RUN = 10_000
# Events read for the whole trace (the run and all its child runs): each event makes at most one
# span, so this bounds what is loaded before MAX_SPANS and the payload budget apply.
MAX_TRACE_EVENTS = 4 * MAX_SPANS
MAX_CHILD_RUNS = 500
FIELD_MAX_BYTES = 8 * 1024
PAYLOAD_BUDGET_BYTES = 2 * 1024 * 1024
# The preview size once the payload budget is spent.
MIN_PREVIEW_BYTES = 256
STATEMENT_TIMEOUT_MS = 5000

SPAN_KINDS = (
    "run", "model_request", "tool_call", "processor", "workflow_step", "child_run",
    # RM.19
    "retrieve", "embedding", "memory_recall", "memory_index", "observer", "reflector", "working_memory", "ingest",
)
SPAN_STATUSES = ("ok", "error", "running", "waiting", "incomplete")

class TraceQueryTimeout(Exception):
    """Reading a trace hit the statement timeout (the API answers 503)."""

    def __init__(self, timeout_ms: int) -> None:
        self.timeout_ms = timeout_ms
        super().__init__(timeout_ms)  # pickle-safe: the constructor args

    def __str__(self) -> str:
        return f"trace query exceeded the {self.timeout_ms} ms statement timeout"


# (workflow_id, workflow_version) -> that version's spec as stored (a dict), or None.
WorkflowSpecLoader = Callable[[str, int], "dict[str, Any] | None"]
# (agent_id, agent_version) -> that version's spec as stored (a dict), or None.
AgentSpecLoader = Callable[[str, int], "dict[str, Any] | None"]

RETRIEVE_TOOL = "retrieve"
KNOWLEDGE_SEARCH_TOOL = "knowledge.search"


# ---------------------------------------------------------------------------- loading


@dataclass
class RunData:
    """One run's row (store._run_dict shape), its events (seq order) and its usage rows."""

    run: dict[str, Any]
    events: list[dict[str, Any]]
    requests: dict[str, dict[str, Any]]  # request_key -> run_model_requests row
    events_truncated: bool = False


@dataclass
class TraceInput:
    root: RunData
    children: list[RunData] = field(default_factory=list)
    children_truncated: bool = False
    workflow_spec: dict[str, Any] | None = None
    # (agent_id, agent_version) -> the spec's knowledge base refs, for runs with a retrieve call.
    knowledge_refs: dict[tuple[str, int], list[dict[str, Any]]] = field(default_factory=dict)


class TraceReader:
    """Reads a run's trace from Postgres (see the module docstring)."""

    def __init__(self, engine, statement_timeout_ms: int | None = None):
        self.engine = engine
        self.statement_timeout_ms = int(STATEMENT_TIMEOUT_MS if statement_timeout_ms is None else statement_timeout_ms)

    def read(
        self,
        tenant_id: str,
        run_id: UUID | str,
        workflow_spec: WorkflowSpecLoader | None = None,
        agent_spec: AgentSpecLoader | None = None,
    ) -> dict[str, Any] | None:
        data = self.load(tenant_id, run_id)
        if data is None:
            return None
        run = data.root.run
        if run["kind"] == "workflow" and workflow_spec is not None and run.get("workflow_id"):
            data.workflow_spec = workflow_spec(run["workflow_id"], run["workflow_version"])
        if agent_spec is not None:
            data.knowledge_refs = load_knowledge_refs(data, agent_spec)
        return build_trace(data)

    def load(self, tenant_id: str, run_id: UUID | str) -> TraceInput | None:
        """The run and its children, their events and usage rows, or None. Raises
        TraceQueryTimeout when a query hits the statement timeout."""
        if not tenant_id:
            raise ValueError("tenant_id is required")
        try:
            return self._load(tenant_id, str(run_id))
        except DBAPIError as exc:
            if getattr(getattr(exc, "orig", None), "sqlstate", None) == "57014":  # query_canceled
                raise TraceQueryTimeout(self.statement_timeout_ms) from None
            raise

    def _load(self, tenant_id: str, run_id: str) -> TraceInput | None:
        # One snapshot for the run, its children, their events and usage rows.
        with Session(self.engine) as conn:
            conn.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            conn.execute(text(f"SET LOCAL statement_timeout = {self.statement_timeout_ms}"))
            root = conn.scalars(
                select(RunModel).where(RunModel.tenant_id == tenant_id, RunModel.id == run_id)
            ).first()
            if root is None:
                return None
            child_rows = conn.scalars(
                select(RunModel)
                .where(RunModel.tenant_id == tenant_id, RunModel.parent_run_id == run_id)
                .order_by(RunModel.created_at, RunModel.id)
                .limit(MAX_CHILD_RUNS + 1)
            ).all()
            children_truncated = len(child_rows) > MAX_CHILD_RUNS
            child_rows = child_rows[:MAX_CHILD_RUNS]
            runs = [_run_dict(root)] + [_run_dict(r) for r in child_rows]
            ids = [r["id"] for r in runs]
            requests: dict[str, dict[str, dict[str, Any]]] = {i: {} for i in ids}
            for row in conn.execute(
                select(*RunModelRequestModel.__table__.columns).where(
                    RunModelRequestModel.tenant_id == tenant_id, RunModelRequestModel.run_id.in_(ids)
                )
            ).mappings():
                requests[row["run_id"]][row["request_key"]] = dict(row)
            loaded: list[RunData] = []
            # One event budget for the whole trace: the run's own events first, then each child's
            # (creation order) while budget is left; children past it are not loaded at all.
            budget = MAX_TRACE_EVENTS
            for run in runs:
                if budget <= 0:
                    children_truncated = True
                    break
                limit = min(MAX_EVENTS_PER_RUN, budget)
                events = [
                    dict(e)
                    for e in conn.execute(
                        select(
                            ExecutionEventModel.seq,
                            ExecutionEventModel.event_key,
                            ExecutionEventModel.type,
                            ExecutionEventModel.data,
                            ExecutionEventModel.ts,
                        )
                        .where(
                            ExecutionEventModel.tenant_id == tenant_id,
                            ExecutionEventModel.run_id == run["id"],
                        )
                        .order_by(ExecutionEventModel.seq)
                        .limit(limit + 1)
                    ).mappings()
                ]
                budget -= min(len(events), limit)
                loaded.append(
                    RunData(
                        run=run,
                        events=events[:limit],
                        requests=requests[run["id"]],
                        events_truncated=len(events) > limit,
                    )
                )
        return TraceInput(root=loaded[0], children=loaded[1:], children_truncated=children_truncated)


def _has_retrieve(rd: RunData) -> bool:
    return any(e["type"] == "tool_call" and (e.get("data") or {}).get("tool_name") == RETRIEVE_TOOL for e in rd.events)


def knowledge_refs_of(spec: Any) -> list[dict[str, Any]]:
    """`[{id, top_k, min_score}]` of an agent spec's `knowledge_bases` (tolerant of a malformed spec)."""
    refs = spec.get("knowledge_bases") if isinstance(spec, dict) else None
    out = []
    for ref in refs or ():
        if isinstance(ref, dict) and isinstance(ref.get("id"), str):
            out.append({"id": ref["id"], "top_k": ref.get("top_k", 8), "min_score": ref.get("min_score")})
    return out


def load_knowledge_refs(data: TraceInput, agent_spec: AgentSpecLoader) -> dict[tuple[str, int], list[dict[str, Any]]]:
    """The knowledge base refs of each agent version whose run in the trace called `retrieve` (one
    loader call per distinct version; runs without a retrieve call cost nothing)."""
    refs: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for rd in [data.root, *data.children]:
        agent_id, version = rd.run.get("agent_id"), rd.run.get("agent_version")
        if agent_id is None or not isinstance(version, int) or not _has_retrieve(rd):
            continue
        key = (str(agent_id), version)
        if key not in refs:
            refs[key] = knowledge_refs_of(agent_spec(str(agent_id), version))
    return refs


# ---------------------------------------------------------------------------- building


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _dec(value: Any) -> str | None:
    if value is None:
        return None
    return format(value if isinstance(value, Decimal) else Decimal(str(value)), "f")


class _Budget:
    def __init__(self) -> None:
        self.remaining = PAYLOAD_BUDGET_BYTES
        self.fields_truncated = 0


def _bounded(value: Any, budget: _Budget) -> tuple[Any, int | None]:
    """(value, None) when it fits; else (a string preview of its JSON text, its size in bytes)."""
    if value is None:
        return None, None
    as_text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    raw = as_text.encode("utf-8")
    size = len(raw)
    if size <= FIELD_MAX_BYTES and (size <= budget.remaining or size <= MIN_PREVIEW_BYTES):
        # A value no larger than a preview is never cut, even once the budget is spent.
        budget.remaining = max(0, budget.remaining - size)
        return value, None
    limit = min(FIELD_MAX_BYTES, max(budget.remaining, MIN_PREVIEW_BYTES))
    preview = raw[:limit].decode("utf-8", "ignore")
    budget.remaining = max(0, budget.remaining - len(preview.encode("utf-8")))
    budget.fields_truncated += 1
    return preview, size


def step_parents(spec: dict[str, Any] | None) -> dict[str, str]:
    """step id -> the id of the parallel step whose branch ran it (innermost), from a stored
    workflow spec. Steps outside every branch are absent. Tolerant of a malformed spec."""
    if not isinstance(spec, dict):
        return {}
    steps = {s.get("id"): s for s in spec.get("steps") or () if isinstance(s, dict) and isinstance(s.get("id"), str)}
    parents: dict[str, str] = {}

    def successors(step: dict[str, Any]) -> list[str]:
        kind = step.get("type")
        if kind == "branch":
            out = [c.get("next") for c in step.get("cases") or () if isinstance(c, dict)]
            return [s for s in [*out, step.get("default")] if isinstance(s, str)]
        if kind == "parallel":
            join = step.get("join")
            return [join] if isinstance(join, str) else []
        nxt = step.get("next")
        return [nxt] if isinstance(nxt, str) else []

    for pid, pstep in steps.items():
        if pstep.get("type") != "parallel":
            continue
        join = pstep.get("join")
        stack = [b for b in pstep.get("branches") or () if isinstance(b, str)]
        seen: set[str] = set()
        while stack:
            sid = stack.pop()
            if sid == join or sid == pid or sid in seen or sid not in steps:
                continue
            seen.add(sid)
            # The walk never enters a nested parallel's branches (its successor is its join), so
            # those are claimed by the nested parallel itself: the innermost parallel wins.
            parents.setdefault(sid, pid)
            stack.extend(successors(steps[sid]))
    return parents


# processor_result fields shown as span attributes (never the processed or matched text).
PROCESSOR_ATTRIBUTES = ("processor_type", "action", "reason", "match_counts", "categories")
# Fields of a retrieve source shown in `hits` (never the chunk text of a knowledge.search hit). `n`
# is the number the model saw: unique across the run when `numbering` is "run" (each call continues
# from the run's earlier retrieve calls), per call from 1 in runs recorded before.
HIT_ATTRIBUTES = ("n", "numbering", "title", "url", "ref", "kb_id", "document_id", "document_key", "chunk_id", "doc_version", "score")
# `embedding` and `ingest_progress` event fields shown as span attributes (ids and counts only).
EMBEDDING_ATTRIBUTES = (
    "stage", "model_id", "provider_model_name", "dimensions", "inputs", "input_type", "requests",
    "usage_measured", "price_source", "hits", "turns", "groups", "messages", "turn", "inserted",
    "existing", "redacted", "error_code", "batch", "chunks", "filled", "written", "approx_tokens",
    "embedded", "total",
)
INGEST_ATTRIBUTES = ("phase", "generation", "embedded", "total", "pending_tokens", "document_id", "doc_version")
# `memory_updated` / `memory_observe_failed` fields shown as span attributes (never memory text).
OBSERVER_ATTRIBUTES = (
    "stage", "covers_from_turn", "covers_to_turn", "approx_tokens", "notes", "redacted", "prompt_version",
    "superseded", "error_code",
)
# `post_turn_done` (P1.C1): shown as the run span's `post_turn` attribute (no span of its own).
POST_TURN_ATTRIBUTES = ("observe", "index")
_MARKER_EVENTS = frozenset({"post_turn_done", "memory_observe_trimmed"})
WORKING_MEMORY_ATTRIBUTES = ("tool_call_id", "scope", "format", "version", "changed_keys", "other_changed_keys", "redacted")
_EMBED_STAGE_KINDS = {"recall": "memory_recall", "index": "memory_index"}

_RUN_STATUS = {"completed": "ok", "failed": "error", "needs_attention": "error"}


class _Builder:
    def __init__(self, data: TraceInput) -> None:
        self.data = data
        self.budget = _Budget()
        self.spans: list[dict[str, Any]] = []
        self.ids: set[str] = set()
        self.spans_truncated = False
        self.parents = step_parents(data.workflow_spec)
        self.children_by_step: dict[str, list[RunData]] = {}
        for child in data.children:
            self.children_by_step.setdefault(child.run.get("parent_step_id") or "", []).append(child)
        self.placed_children: set[str] = set()

    # -- spans

    def add(
        self,
        span_id: str,
        parent_id: str | None,
        kind: str,
        name: str,
        start: datetime | None,
        end: datetime | None,
        status: str,
        attributes: dict[str, Any],
        input: Any = None,
        output: Any = None,
    ) -> dict[str, Any] | None:
        if span_id in self.ids:
            return None
        if len(self.spans) >= MAX_SPANS:
            self.spans_truncated = True
            return None
        if parent_id is not None and parent_id not in self.ids:
            parent_id = self.spans[0]["id"] if self.spans else None
        input_value, input_size = _bounded(input, self.budget)
        output_value, output_size = _bounded(output, self.budget)
        truncated = {}
        if input_size is not None:
            truncated["input"] = input_size
        if output_size is not None:
            truncated["output"] = output_size
        span = {
            "id": span_id,
            "parent_id": parent_id,
            "kind": kind,
            "name": name,
            "start": _iso(start),
            "end": _iso(end),
            "status": status,
            "attributes": attributes,
            "input": input_value,
            "output": output_value,
            "truncated": truncated,
        }
        self.spans.append(span)
        self.ids.add(span_id)
        return span

    # -- one run

    def run(self, rd: RunData, parent_id: str | None, kind: str) -> None:
        run = rd.run
        events = rd.events
        by_key = {e["event_key"]: e for e in events}
        start_event = by_key.get("start")
        end_event = by_key.get("end")
        terminal = run["status"] in TERMINAL_RUN_STATUSES
        span_id = f"run:{run['id']}"
        start = run.get("started_at") or run.get("created_at") or (start_event or {}).get("ts")
        end = None
        if terminal:
            end = run.get("finished_at") or (end_event or {}).get("ts") or run.get("updated_at")
        attributes: dict[str, Any] = {
            "run_id": run["id"],
            "run_kind": run["kind"],
            "run_status": run["status"],
            "display_status": run.get("display_status"),
            "agent_id": run.get("agent_id"),
            "agent_version": run.get("agent_version"),
            # A draft test run (or draft chat turn) has no version: Studio labels it "Draft".
            "draft": run.get("draft_spec") is not None,
            "tool_id": run.get("tool_id"),
            "workflow_id": run.get("workflow_id"),
            "workflow_version": run.get("workflow_version"),
            "parent_run_id": run.get("parent_run_id"),
            "parent_step_id": run.get("parent_step_id"),
            "conversation_id": run.get("conversation_id"),
            "turn": run.get("turn"),
            "model_id": run.get("model_id"),
            "input_tokens": run.get("input_tokens"),
            "output_tokens": run.get("output_tokens"),
            # The run's own requests split by operation: `chat_*` is the agent's model (`model_id`)
            # only; memory (observer, reflector), moderation and embedding tokens apart.
            **_run_usage_split(rd),
            "cost_usd": _dec(run.get("cost_usd")),
            "trace_id": run.get("trace_id"),
            "error": run.get("error"),
        }
        if run["kind"] == "kb_ingest" or run.get("knowledge_base_id"):
            attributes["knowledge_base_id"] = run.get("knowledge_base_id")
            if start_event is not None and isinstance((start_event.get("data") or {}).get("mode"), str):
                attributes["ingest_mode"] = start_event["data"]["mode"]
        if end_event is not None and end_event["type"] == "run_failed":
            data = end_event.get("data") or {}
            attributes["error_code"] = data.get("error_code")
            if data.get("step_id") is not None:
                attributes["failed_step_id"] = data.get("step_id")
        if rd.events_truncated:
            attributes["events_truncated"] = True
        post_turn = next((e for e in events if e["type"] == "post_turn_done"), None)
        if post_turn is not None:
            # P1.C1: a marker on the run span, not a span: the post-turn memory steps have reported.
            pdata = post_turn.get("data") or {}
            attributes["post_turn"] = {
                **{k: pdata[k] for k in POST_TURN_ATTRIBUTES if isinstance(pdata.get(k), str)},
                "done_at": ts.isoformat() if hasattr(ts := post_turn["ts"], "isoformat") else str(ts),
            }
        run_input: Any = run.get("input")
        if start_event is not None and "input" in (start_event.get("data") or {}):
            run_input = start_event["data"]["input"]
        run_output: Any = run.get("output")
        if end_event is not None and end_event["type"] == "run_completed":
            data = end_event.get("data") or {}
            run_output = data["result"] if "result" in data else data.get("output", run_output)
        status = _RUN_STATUS.get(run["status"], "running")
        self.add(
            span_id, parent_id, kind, _run_name(run), start, end, status, attributes, run_input, run_output
        )
        if span_id not in self.ids:
            return
        self._events(rd, span_id, terminal)

    def _events(self, rd: RunData, run_span: str, terminal: bool) -> None:
        run = rd.run
        rid = run["id"]
        events = rd.events
        by_key = {e["event_key"]: e for e in events}
        open_status = "incomplete" if terminal else "running"
        last_tool_results: list[dict[str, Any]] = []
        last_request: int | None = None  # the request that issued the tool calls that follow it
        previous: dict[str, Any] | None = None
        for event in events:
            etype, key, data = event["type"], event["event_key"], event.get("data") or {}
            if etype == "model_message":
                n = data.get("model_request")
                if not isinstance(n, int):
                    n = _request_index(key)
                request_key = key.removesuffix(":final")
                start = (previous or event)["ts"]
                usage, attrs = _model_usage(rd.requests.get(request_key), data, run)
                attrs = {"request_key": request_key, "model_request": n, **attrs, "usage": usage}
                message = data.get("message") if isinstance(data.get("message"), dict) else {}
                calls = [
                    {"tool_call_id": p.get("tool_call_id"), "tool_name": p.get("tool_name"), "args": p.get("args")}
                    for p in message.get("parts") or ()
                    if isinstance(p, dict) and p.get("part_kind") == "tool-call"
                ]
                attrs["tool_calls"] = len(calls)
                model_input: Any = (
                    _run_input_text(rd) if n == 0 else {"tool_results": last_tool_results}
                )
                last_tool_results = []
                last_request = n
                self.add(
                    f"{rid}:model:{n}", run_span, "model_request", attrs.get("model_id") or "model request",
                    start, event["ts"], "ok", attrs, model_input,
                    {"text": data.get("text"), "tool_calls": calls},
                )
            elif etype == "tool_call":
                call_id = data.get("tool_call_id") or key.split(":")[1]
                result = by_key.get(f"tool:{call_id}:result")
                rdata = (result or {}).get("data") or {}
                attrs = {
                    "tool_call_id": call_id,
                    "tool_name": data.get("tool_name"),
                    "tool_id": data.get("tool_id"),
                    "model_request": last_request,
                }
                if result is None:
                    status, out = open_status, None
                else:
                    attrs["result_status"] = rdata.get("status")
                    status = {"ok": "ok", "error": "error"}.get(rdata.get("status"), "incomplete")
                    out = rdata["content"] if "content" in rdata else None
                    for k in ("error", "error_code"):
                        if rdata.get(k) is not None:
                            attrs[k] = rdata[k]
                span_kind = "tool_call"
                if data.get("tool_name") == RETRIEVE_TOOL or data.get("tool_id") == KNOWLEDGE_SEARCH_TOOL:
                    span_kind = "retrieve"
                    attrs.update(self._retrieve_attributes(rd, data, rdata))
                self.add(
                    f"{rid}:tool:{call_id}", run_span, span_kind, str(data.get("tool_name") or call_id),
                    event["ts"], (result or {}).get("ts"), status, attrs, data.get("args"), out,
                )
            elif etype == "tool_result":
                last_tool_results.append(
                    {k: data.get(k) for k in ("tool_call_id", "tool_name", "status", "content") if k in data}
                )
            elif etype == "processor_result":
                self._processor(rd, event, run_span)
            elif etype == "embedding":
                self._embedding(rd, event, previous, run_span)
            elif etype == "ingest_progress":
                self._ingest(rd, event, previous, run_span)
            elif etype in ("memory_updated", "memory_observe_failed"):
                self._memory(rd, event, previous, run_span)
            elif etype == "step_started":
                self._step(rd, event, by_key, run_span, open_status)
            # Markers without a span (P1.C1 `post_turn_done`, P1.C6 `memory_observe_trimmed`, written
            # right before the reflection's own event) never start the next span.
            if etype in _MARKER_EVENTS:
                continue
            # An output processor checks a response before its model:{n}:final is written: the model
            # request's span must not start at it (it would collapse to the checks' duration).
            if not (etype == "processor_result" and key.startswith("processor:output:")):
                previous = event
        # Child runs whose step span is missing (cannot happen for a consistent log) hang off the run.
        if rd is self.data.root:
            for child in self.data.children:
                if child.run["id"] not in self.placed_children:
                    self.placed_children.add(child.run["id"])
                    self.run(child, run_span, "child_run")

    def _processor(self, rd: RunData, event: dict[str, Any], run_span: str) -> None:
        """One `processor_result` event (`processor:{phase}:{i}` or `processor:output:{i}:model:{n}`)
        as a zero-length span named by its processor type. Only allowlisted fields become attributes
        (never text); a moderation outcome carries its model request's usage and cost, from the
        `moderation:{phase}:{i}[:model:{n}]` usage row (else the event's own usage fields)."""
        rid = rd.run["id"]
        key, data = event["event_key"], event.get("data") or {}
        parts = key.split(":")
        phase = data.get("phase") or (parts[1] if len(parts) > 1 else None)
        index = data.get("index")
        if not isinstance(index, int):
            index = _request_index(key)
        attrs: dict[str, Any] = {"processor_key": key, "phase": phase, "index": index}
        for k in PROCESSOR_ATTRIBUTES:
            if k in data:
                attrs[k] = data[k]
        if len(parts) >= 5 and parts[3] == "model":
            attrs["model_request"] = _request_index(":".join(parts[3:]))
        is_model_call = data.get("processor_type") == "moderation" or "usage" in data or "model_id" in data
        if is_model_call:
            # `processor:output:{i}:model:{n}` -> `moderation:output:{i}:model:{n}` (the worker's key).
            suffix = ":" + ":".join(parts[3:]) if len(parts) >= 5 and parts[3] == "model" else ""
            request_key = f"moderation:{phase}:{index}{suffix}"
            row = rd.requests.get(request_key)
            if row is not None or "usage" in data:
                usage, model_attrs = _model_usage(row, data, {})
                attrs.update(request_key=request_key, **model_attrs, usage=usage)
        status = "error" if data.get("action") == "block" else "ok"
        self.add(
            f"{rid}:{key}", run_span, "processor", str(data.get("processor_type") or data.get("type") or key),
            event["ts"], event["ts"], status, attrs,
        )

    # -- RAG and memory (RM.19)

    def _retrieve_attributes(self, rd: RunData, call: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        """Query, knowledge base choice, top_k, the spec's refs and the hits (allowlisted fields)."""
        args = call.get("args") if isinstance(call.get("args"), dict) else {}
        run = rd.run
        refs = self.data.knowledge_refs.get((str(run.get("agent_id")), run.get("agent_version")))
        query = args.get("search_query", args.get("query"))
        top_k = args.get("top_k")
        if not isinstance(top_k, int) and refs is not None and len(refs) == 1:
            top_k = refs[0].get("top_k")
        sources = result.get("sources")
        if not isinstance(sources, list):
            content = result.get("content")
            sources = content.get("hits") if isinstance(content, dict) else None
        hits = [
            {k: h.get(k) for k in HIT_ATTRIBUTES if k in h}
            for h in (sources if isinstance(sources, list) else ())
            if isinstance(h, dict)
        ]
        attrs: dict[str, Any] = {
            "query": query if isinstance(query, str) else None,
            "knowledge_base": args.get("knowledge_base") if isinstance(args.get("knowledge_base"), str) else None,
            "top_k": top_k if isinstance(top_k, int) else None,
            "hits": hits,
            "hit_count": len(hits),
        }
        if refs is not None:
            attrs["knowledge_bases"] = refs
        return attrs

    def _embedding_usage(self, rd: RunData, key: str, data: dict[str, Any]) -> dict[str, Any]:
        """`request_key`, model attributes and usage of an embedding call: from its usage row (the
        event key, without a `:failed` suffix), else the event's own `input_tokens` / `cost_usd`."""
        base = key.removesuffix(":failed")
        row = rd.requests.get(key) or rd.requests.get(base)
        if row is not None:
            usage, model_attrs = _model_usage(row, data, {})
            return {"request_key": row["request_key"], **model_attrs, "usage": usage}
        usage = None
        if isinstance(data.get("input_tokens"), int) and data.get("status") != "failed":
            usage = {
                "input_tokens": data["input_tokens"], "output_tokens": 0, "cache_read_tokens": 0,
                "cache_write_tokens": 0, "usage_measured": bool(data.get("usage_measured", True)),
            }
        return {
            "request_key": base,
            "model_id": data.get("model_id"),
            "provider_model_name": data.get("provider_model_name"),
            "cost_usd": data.get("cost_usd") if usage is not None else None,
            "price_source": data.get("price_source"),
            "usage": usage,
        }

    def _embedding(self, rd: RunData, event: dict[str, Any], previous: dict[str, Any] | None, run_span: str) -> None:
        """An `embedding` event: retrieve and search tester (`embedding`, under the retrieve span),
        semantic recall (`memory_recall`) or indexing (`memory_index`); `:failed` keys are errors."""
        rid = rd.run["id"]
        key, data = event["event_key"], event.get("data") or {}
        stage = data.get("stage")
        kind = _EMBED_STAGE_KINDS.get(stage, "embedding")
        failed = data.get("status") == "failed" or key.endswith(":failed")
        parent = run_span
        parts = key.split(":")
        if kind == "embedding" and len(parts) >= 3 and parts[1] == "retrieve":
            candidate = f"{rid}:tool:{parts[2]}"
            parent = candidate if candidate in self.ids else run_span
        attrs: dict[str, Any] = {"event_key": key}
        attrs.update({k: data[k] for k in EMBEDDING_ATTRIBUTES if k in data})
        attrs.update(self._embedding_usage(rd, key, data))
        if failed:
            attrs["error_code"] = data.get("error_code")
        name = {"memory_recall": "semantic recall", "memory_index": "memory index"}.get(kind, "embedding")
        self.add(
            f"{rid}:{key}", parent, kind, name, (previous or event)["ts"], event["ts"],
            "error" if failed else "ok", attrs,
        )

    def _ingest(self, rd: RunData, event: dict[str, Any], previous: dict[str, Any] | None, run_span: str) -> None:
        """A kb_ingest `ingest_progress` event: an embedding batch (`embedding`, from the previous
        event) or the chunked / finalized phase (`ingest`, zero length)."""
        rid = rd.run["id"]
        key, data = event["event_key"], event.get("data") or {}
        phase = data.get("phase")
        if phase == "embedding" or key.startswith("embed:batch:"):
            attrs: dict[str, Any] = {"event_key": key, "phase": "embedding"}
            attrs.update({k: data[k] for k in EMBEDDING_ATTRIBUTES if k in data})
            attrs.update(self._embedding_usage(rd, key, data))
            batch = data.get("batch") if isinstance(data.get("batch"), int) else _request_index(key.removeprefix("embed:"))
            self.add(
                f"{rid}:{key}", run_span, "embedding", f"embedding batch {batch}" if batch is not None else "embedding",
                (previous or event)["ts"], event["ts"], "ok", attrs,
            )
            return
        attrs = {"event_key": key, **{k: data[k] for k in INGEST_ATTRIBUTES if k in data}}
        self.add(f"{rid}:{key}", run_span, "ingest", str(phase or key), event["ts"], event["ts"], "ok", attrs)

    def _memory(self, rd: RunData, event: dict[str, Any], previous: dict[str, Any] | None, run_span: str) -> None:
        """`memory_updated` / `memory_observe_failed`: working memory (`memory:wm:{call}`, under its
        tool call) or the observer / reflector (`memory:observe|reflect:{turn}[:failed]`)."""
        rid = rd.run["id"]
        key, data = event["event_key"], event.get("data") or {}
        parts = key.split(":")
        if len(parts) >= 3 and parts[1] == "wm":
            parent = f"{rid}:tool:{parts[2]}"
            attrs = {"event_key": key, **{k: data[k] for k in WORKING_MEMORY_ATTRIBUTES if k in data}}
            self.add(
                f"{rid}:{key}", parent if parent in self.ids else run_span, "working_memory", "working memory",
                event["ts"], event["ts"], "ok", attrs,
            )
            return
        stage = data.get("stage") if data.get("stage") in ("observe", "reflect") else (parts[1] if len(parts) > 1 else None)
        kind = "reflector" if stage == "reflect" else "observer"
        failed = event["type"] == "memory_observe_failed" or key.endswith(":failed")
        attrs = {"event_key": key, **{k: data[k] for k in OBSERVER_ATTRIBUTES if k in data}}
        if isinstance(data.get("kind"), str):
            attrs["memory_kind"] = data["kind"]
        request_key = key.removesuffix(":failed")
        row = rd.requests.get(request_key)
        if row is not None or "usage" in data:
            usage, model_attrs = _model_usage(row, data, {})
            attrs.update(request_key=request_key, **model_attrs, usage=usage)
        self.add(
            f"{rid}:{key}", run_span, kind, kind, (previous or event)["ts"], event["ts"],
            "error" if failed else "ok", attrs,
        )

    def _step(
        self, rd: RunData, event: dict[str, Any], by_key: dict[str, Any], run_span: str, open_status: str
    ) -> None:
        rid = rd.run["id"]
        data = event.get("data") or {}
        step_id = data.get("step_id") or event["event_key"].split(":")[1]
        done = by_key.get(f"step:{step_id}:completed")
        failed = by_key.get(f"step:{step_id}:failed")
        waiting = by_key.get(f"step:{step_id}:waiting")
        decision = by_key.get(f"step:{step_id}:decision")
        attrs: dict[str, Any] = {"step_id": step_id, "step_type": data.get("step_type")}
        for k in ("agent_id", "agent_version", "child_run_id", "tool_id"):
            if k in data:
                attrs[k] = data[k]
        output = None
        end = None
        if done is not None:
            status, end = "ok", done["ts"]
            output = (done.get("data") or {}).get("output")
        elif failed is not None:
            fdata = failed.get("data") or {}
            status, end = "error", failed["ts"]
            attrs.update(step_status=fdata.get("status"), error=fdata.get("error"), error_code=fdata.get("error_code"))
        elif waiting is not None and decision is None and open_status == "running":
            status = "waiting"
        else:
            status = open_status
        if waiting is not None:
            wdata = waiting.get("data") or {}
            attrs["approval"] = {
                "prompt": wdata.get("prompt"),
                "timeout_hours": wdata.get("timeout_hours"),
                "deadline": wdata.get("deadline"),
                "waiting_since": _iso(waiting["ts"]),
            }
        if decision is not None:
            ddata = decision.get("data") or {}
            attrs["decision"] = {
                "type": decision["type"],
                "decided_by": ddata.get("decided_by"),
                "comment": ddata.get("comment"),
                "at": _iso(decision["ts"]),
            }
        parent = self.parents.get(step_id)
        parent_span = f"{rid}:step:{parent}" if parent is not None else run_span
        span_id = f"{rid}:step:{step_id}"
        self.add(
            span_id, parent_span, "workflow_step", step_id, event["ts"], end, status, attrs,
            data.get("input"), output,
        )
        if span_id not in self.ids or rd is not self.data.root:
            return
        for child in self.children_by_step.get(step_id, ()):
            if child.run["id"] not in self.placed_children:
                self.placed_children.add(child.run["id"])
                self.run(child, span_id, "child_run")

    # -- totals

    def totals(self) -> dict[str, Any]:
        model_requests = recorded = unpriced = moderation = embeddings = memory = embed_unpriced = 0
        embed_tokens = 0
        embed_cost = Decimal(0)
        sums = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0}
        cost = Decimal(0)
        split = _UsageSplit()
        for rd in [self.data.root, *self.data.children]:
            keys = {e["event_key"].removesuffix(":final") for e in rd.events if e["type"] == "model_message"}
            keys |= set(rd.requests)
            for request_key in keys:
                model_requests += 1
                if request_key.startswith("moderation:"):
                    moderation += 1
                if request_key.startswith(("memory:observe:", "memory:reflect:")):
                    memory += 1
                row = rd.requests.get(request_key)
                split.add(request_key, row)
                if row is None:
                    continue
                recorded += 1
                for k in sums:
                    sums[k] += int(row.get(k) or 0)
                is_embedding = row.get("operation") == "embedding"
                if is_embedding:
                    embeddings += 1
                    embed_tokens += int(row.get("input_tokens") or 0)
                if row.get("cost_usd") is None:
                    unpriced += 1
                    embed_unpriced += int(is_embedding)
                else:
                    cost += Decimal(str(row["cost_usd"]))
                    if is_embedding:
                        embed_cost += Decimal(str(row["cost_usd"]))
        unrecorded = model_requests - recorded
        no_usage = model_requests > 0 and recorded == 0
        root = self.data.root.run
        root_span = self.spans[0] if self.spans else None
        duration_ms = None
        if root_span is not None and root_span["start"] and root_span["end"]:
            delta = datetime.fromisoformat(root_span["end"]) - datetime.fromisoformat(root_span["start"])
            duration_ms = max(0, int(delta.total_seconds() * 1000))
        return {
            "runs": 1 + len(self.data.children),
            "model_requests": model_requests,
            "moderation_requests": moderation,
            "embedding_requests": embeddings,
            "memory_requests": memory,
            "embedding_input_tokens": embed_tokens,
            "embedding_cost_usd": None if embed_unpriced else format(embed_cost, "f"),
            **split.totals(),
            "tool_calls": sum(1 for s in self.spans if s["kind"] in ("tool_call", "retrieve")),
            **{k: (None if no_usage else v) for k, v in sums.items()},
            "cost_usd": None if (unpriced or unrecorded) else format(cost, "f"),
            "unpriced_requests": unpriced,
            "unrecorded_requests": unrecorded,
            "duration_ms": duration_ms,
            "status": root["status"],
        }


def request_operation(request_key: str, row: dict[str, Any] | None) -> str:
    """The request's operation, as eap_run_store.metrics.operation_key classifies it: `embedding`
    (its row's operation), `moderation`, `observer`, `reflector`, else `chat` (the agent's model).
    A request without a usage row is classified by its key (an embedding always has a row)."""
    if row is not None and row.get("operation") == "embedding":
        return "embedding"
    if request_key.startswith("moderation:"):
        return "moderation"
    if request_key.startswith("memory:observe:"):
        return "observer"
    if request_key.startswith("memory:reflect:"):
        return "reflector"
    return "chat"


# The split of a run's (or trace's) usage (RM.19 follow-up): the agent's chat model, observational
# memory (observer + reflector), moderation and embedding requests apart, so the chat model's tokens
# are never labelled with embedding or observer tokens. Every request is in exactly one group, so
# chat + memory + moderation + embedding input tokens == input_tokens (and the same for output).
_SPLIT_GROUPS = {"chat": "chat", "observer": "memory", "reflector": "memory", "moderation": "moderation",
                 "embedding": "embedding"}


class _UsageSplit:
    def __init__(self) -> None:
        self.tokens = {g: {"input": 0, "output": 0} for g in ("chat", "memory", "moderation", "embedding")}
        self.cost = {g: Decimal(0) for g in self.tokens}
        self.requests = {g: 0 for g in self.tokens}
        self.recorded = {g: 0 for g in self.tokens}
        self.unpriced = {g: 0 for g in self.tokens}

    def add(self, request_key: str, row: dict[str, Any] | None) -> None:
        group = _SPLIT_GROUPS[request_operation(request_key, row)]
        self.requests[group] += 1
        if row is None:
            return
        self.recorded[group] += 1
        self.tokens[group]["input"] += int(row.get("input_tokens") or 0)
        self.tokens[group]["output"] += int(row.get("output_tokens") or 0)
        if row.get("cost_usd") is None:
            self.unpriced[group] += 1
        else:
            self.cost[group] += Decimal(str(row["cost_usd"]))

    def totals(self, *, cost: bool = True) -> dict[str, Any]:
        """`{chat,memory,moderation}_{input,output}_tokens` and `chat_requests`; with `cost`, also
        `{chat,memory,moderation}_cost_usd` (null when one of the group's requests is unpriced or has
        no usage row). Tokens are null when the group has requests but none was recorded. The
        embedding group's input tokens and cost are `embedding_input_tokens` / `embedding_cost_usd`
        (added by the caller; embeddings have no output tokens)."""
        out: dict[str, Any] = {"chat_requests": self.requests["chat"]}
        for g in ("chat", "memory", "moderation"):
            none = self.requests[g] > 0 and self.recorded[g] == 0
            out[f"{g}_input_tokens"] = None if none else self.tokens[g]["input"]
            out[f"{g}_output_tokens"] = None if none else self.tokens[g]["output"]
            if cost:
                missing = self.unpriced[g] or self.requests[g] - self.recorded[g]
                out[f"{g}_cost_usd"] = None if missing else format(self.cost[g], "f")
        return out

    def embedding_input_tokens(self) -> int:
        return self.tokens["embedding"]["input"]


def _run_usage_split(rd: RunData) -> dict[str, Any]:
    """The run span's usage split (its own requests only, not its child runs'): the chat model's
    tokens apart from memory, moderation and embedding tokens. Costs stay on the trace totals."""
    split = _UsageSplit()
    keys = {e["event_key"].removesuffix(":final") for e in rd.events if e["type"] == "model_message"}
    for request_key in keys | set(rd.requests):
        split.add(request_key, rd.requests.get(request_key))
    return {**split.totals(cost=False), "embedding_input_tokens": split.embedding_input_tokens()}


def _run_name(run: dict[str, Any]) -> str:
    if run["kind"] == "kb_ingest":
        return f"knowledge base {run.get('knowledge_base_id')} ingest"
    if run["kind"] == "tool":
        return f"tool {run.get('tool_id')}"
    if run["kind"] == "workflow":
        return f"workflow {run.get('workflow_id')} v{run.get('workflow_version')}"
    name = f"agent {run.get('agent_id')} v{run.get('agent_version')}"
    if run.get("parent_step_id"):
        name = f"{name} (step {run['parent_step_id']})"
    if run.get("turn") is not None:
        name = f"{name} turn {run['turn']}"
    return name


def _run_input_text(rd: RunData) -> Any:
    for e in rd.events:
        if e["event_key"] == "start" and "input" in (e.get("data") or {}):
            return e["data"]["input"]
    return rd.run.get("input")


def _request_index(key: str) -> int | None:
    parts = key.split(":")
    try:
        return int(parts[1])
    except (IndexError, ValueError):
        return None


def _model_usage(
    row: dict[str, Any] | None, data: dict[str, Any], run: dict[str, Any]
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """(usage or None, model attributes) from the usage row, else the event's own usage fields."""
    if row is not None:
        usage = {
            "input_tokens": row["input_tokens"],
            "output_tokens": row["output_tokens"],
            "cache_read_tokens": row["cache_read_tokens"],
            "cache_write_tokens": row["cache_write_tokens"],
            "usage_measured": bool(row["usage_measured"]),
        }
        return usage, {
            "model_id": row["model_id"],
            "provider_model_name": row.get("provider_model_name"),
            "cost_usd": _dec(row.get("cost_usd")),
            "price_source": row.get("price_source"),
        }
    event_usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
    attrs = {
        "model_id": data.get("model_id") or run.get("model_id"),
        "provider_model_name": (event_usage or {}).get("provider_model_name"),
        "cost_usd": data.get("cost_usd") if event_usage is not None else None,
        "price_source": (event_usage or {}).get("price_source"),
    }
    if event_usage is None:
        return None, attrs
    usage = {
        "input_tokens": event_usage.get("input_tokens"),
        "output_tokens": event_usage.get("output_tokens"),
        "cache_read_tokens": event_usage.get("cache_read_tokens"),
        "cache_write_tokens": event_usage.get("cache_write_tokens"),
        "usage_measured": bool(event_usage.get("measured")),
    }
    return usage, attrs


def build_trace(data: TraceInput) -> dict[str, Any]:
    """The trace response body except `run`'s API shape: `run` is the store's run dict."""
    builder = _Builder(data)
    builder.run(data.root, None, "run")
    return {
        "run": data.root.run,
        "spans": builder.spans,
        "totals": builder.totals(),
        "truncated": {
            "spans": builder.spans_truncated,
            "events": data.root.events_truncated or any(c.events_truncated for c in data.children),
            "child_runs": data.children_truncated,
            "payload_fields": builder.budget.fields_truncated,
        },
        "limits": {
            "max_spans": MAX_SPANS,
            "max_events_per_run": MAX_EVENTS_PER_RUN,
            "max_trace_events": MAX_TRACE_EVENTS,
            "max_child_runs": MAX_CHILD_RUNS,
            "field_max_bytes": FIELD_MAX_BYTES,
            "payload_budget_bytes": PAYLOAD_BUDGET_BYTES,
        },
    }


__all__ = [
    "FIELD_MAX_BYTES",
    "MAX_CHILD_RUNS",
    "MAX_EVENTS_PER_RUN",
    "MAX_SPANS",
    "MAX_TRACE_EVENTS",
    "PAYLOAD_BUDGET_BYTES",
    "RunData",
    "SPAN_KINDS",
    "SPAN_STATUSES",
    "TraceInput",
    "TraceQueryTimeout",
    "AgentSpecLoader",
    "knowledge_refs_of",
    "load_knowledge_refs",
    "TraceReader",
    "build_trace",
    "step_parents",
]
