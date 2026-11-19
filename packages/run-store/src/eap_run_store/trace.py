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

Agent capability spans (AC.17). Attributes never carry error, tool argument or output text:
- `model_request` attributes add `answering_model` (every request since AC.5) and, for a request on
  a fallback chain, `requested_model_id`, `fallback_index` and `failed_attempts` ([{model_id,
  outcome, error_type, http_status}]); `output_candidate: true` for a structured output candidate.
- `fallback_attempt`: one per failed attempt k of request n (`{run_id}:model:{n}:failed:{k}`), under
  its request, from the stored response's attempt timestamp for its duration (`timed: false` and
  zero length at the request's start when the response was not stored); status `error`; usage and
  cost from its `model:{n}:failed:{k}` row when the attempt reported usage.
- An exhausted chain (run_failed `fallback_exhausted` with `model_request` and `failed_attempts`):
  an error `model_request` span `{run_id}:model:{n}` (`exhausted: true`, no usage), from the
  previous event to the failure, with its `fallback_attempt` spans (timed from the attempt's
  `timestamp` / `duration_ms` when recorded). Totals still count recorded requests only.
- `tool_approval`: a gated call's wait (`{run_id}:tool:{id}:approval`), under its tool call, from
  `tool:{id}:approval:waiting` to `tool:{id}:approval:decision`: `waiting` while undecided in a
  running run, `incomplete` once the run ended undecided, `error` on a timeout, else `ok`; tool
  name, deadline and the decision (type, decided_by, comment, reason, at). A call denied without a
  wait (eval runs, `decided_by: "eval"`) is zero length at its decision (`waited: false`). A denied
  call's `tool_call` span is `ok` with `result_status: "denied"`.
- `output_retry`: an `output:{n}:retry` event, zero length: model_request, the JSON paths of the
  rejected candidate (`errors`, never values), `error_count`, cause.
- `eval_case`: an eval run's `eval_case_result` event (`{run_id}:case:{case_id}`), from its case
  run's start to the record; `ok` when the case passed, else `error`; status, counts, duration and
  cost. The case's child run (parent_step_id `case:{case_id}`) nests under it, and each judge usage
  row (`judge:{case_id}:{i}`) is a zero-length `judge` span at the record (the rows carry no timing).
- A run with an open approval has `display_status: "waiting_approval"` and span status `waiting`.
- Totals add `judge_requests`, `judge_input_tokens`, `judge_output_tokens` and `judge_cost_usd`
  (request_operation `judge`), so `chat_*` stays the agent's model only.

Workflow loop spans (LP.9, loops D4). Every body event is in the workflow run's own log under a
scoped key; a scope `S` is the chain of `(loop, index)` frames (`each.3`, `outer.2/inner.0`):
- `loop`: a `foreach` or `loop` step (`step_type` says which), at any depth, instead of a
  `workflow_step` span, with the same id, start, end, status, input and output. Attributes add
  `iterations_started`, `iterations_completed`, `iterations_failed` (from its iteration events),
  `iterations_omitted` (past the span cap below), and for a `loop` step's output `iterations` and
  `exited`.
- `loop_iteration`: one iteration (`loop:{S}:started` → `loop:{S}:completed|failed`), id
  `{run_id}:loop:{S}`, under its loop span; overlapping foreach iterations are siblings.
  Attributes: `step_id` (the loop), `scope` (`S`), `index`, `iteration_status`, `error_code`,
  `error`, `failed_step_id`, `until`, `last_key`. Input: the foreach item or the loop state the
  iteration started with; output: a loop iteration's carried state.
- A body step's span (`loop:{S}:step:{id}:started` → `...:completed|failed`) has id
  `{run_id}:step:{instance_token(S, id)}` (`r1:step:each.3~triage`) and nests under its iteration;
  its child run (parent_step_id `{S}/{id}`) nests under it. Top-level step ids are unchanged.
- At most MAX_LOOP_ITERATION_SPANS iterations of each loop instance are shown (indices
  0..MAX-1), and iterations are admitted in event order only while the run has kept fewer than
  MAX_LOOP_TRACE_EVENTS loop-scoped events (so a 100x100 nested loop cannot use up
  MAX_TRACE_EVENTS before the run's terminal events and the body child runs). Iterations left
  out, their body spans, nested iterations and child runs are dropped while the events are read
  (LoopEvents; a left-out iteration's events are never loaded with their payload, and its child
  runs are not loaded), counted on the loop span (`iterations_omitted`) and in
  `truncated.iterations`. The workflow run's log is scanned (keys only) up to
  MAX_SCANNED_EVENTS_PER_RUN rows so these counts stay right past the kept events.
- Scoped spans are never invented or re-hung: a `loop:` key whose scope does not parse is dropped;
  an iteration or body step whose parent span is missing waits until the log is walked and is
  dropped if the parent never appears; a body step's child run whose step span is missing is
  dropped. An iteration that failed before writing `loop:{S}:started` (a missing workflow
  version, an invalid spec) has no span: it shows only in the loop span's `iterations_failed`
  and the loop step's error.

Evaluate spans (LP.15, loops D15). Attributes never carry the judged value, a judge's reason or any
other output text:
- `evaluate`: an `evaluate` step (top level or in a body), instead of a `workflow_step` span, with
  the same id, start, end, status, input and output (the step's recorded `{passed, score, reason,
  results}`, as every step span shows its output). Attributes add `passed`, `score`, `evaluators`,
  `evaluators_passed`, `evaluators_failed`, `evaluators_skipped`, `results` (per evaluator: name,
  kind, passed, score, skipped, error_type, error_code; never its reason) and the step's judge
  usage: `judge_requests`, `judge_input_tokens`, `judge_output_tokens` and `judge_cost_usd`
  (null when a judge row is unpriced or there is none).
- `judge`: one zero-length span per judge usage row of the step on the workflow run
  (`judge:{id}:{k}`, `judge:{S}:{id}:{k}` in a body), id `{run_id}:{request_key}`, under the
  evaluate span at its end (the rows carry no timing): request_key, step_id, scope,
  evaluator_index, evaluator_name, model, usage and cost. A left-out iteration's evaluate steps
  have no span, so their judges have none either; their cost still counts in the totals.

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
`moderation_cost_usd`, plus the embedding fields above and the judge fields (AC.17); chat + memory +
moderation + embedding + judge input tokens equal `input_tokens`. A run span carries the same token split for its own requests.

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
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Callable
from uuid import UUID

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import case, not_, or_, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from eap_schemas.workflow_scope import (
    EMPTY_SCOPE,
    Scope,
    ScopeFrame,
    ScopeInvalid,
    instance_token,
    iteration_event_key,
    parse_scope,
    scope_string,
    scoped_event_key,
    scoped_judge_key,
    scoped_parent_step_id,
)

from eap_run_store.store import (
    ExecutionEventModel,
    RunModel,
    RunModelRequestModel,
    TERMINAL_RUN_STATUSES,
    _run_dict,
    _run_select,
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
# LP.9: iterations shown per loop instance (the default EAP_WORKFLOW_MAX_LOOP_ITERATIONS); a loop
# allowed more (up to the schema's 1,000) shows its first ones and counts the rest.
MAX_LOOP_ITERATION_SPANS = 100
# LP.9 review: loop-scoped events (`loop:{S}:...`) a trace keeps in all. Iterations are admitted in
# event order while this budget lasts; later ones are left out and counted like those past
# MAX_LOOP_ITERATION_SPANS, so a 100x100 nested loop cannot spend MAX_TRACE_EVENTS (and starve the
# run's terminal events and the body child runs) before the per-instance cap applies.
MAX_LOOP_TRACE_EVENTS = MAX_SPANS // 2
# Rows of the workflow run's own event log scanned (key and type only, no payload) to keep loop
# counts right past the kept events: bounds the cost of a 10k-iteration run.
MAX_SCANNED_EVENTS_PER_RUN = 100_000

SPAN_KINDS = (
    "run", "model_request", "tool_call", "processor", "workflow_step", "child_run",
    # RM.19
    "retrieve", "embedding", "memory_recall", "memory_index", "observer", "reflector", "working_memory", "ingest",
    # AC.17
    "fallback_attempt", "tool_approval", "output_retry", "eval_case", "judge",
    # LP.9
    "loop", "loop_iteration",
    # LP.15
    "evaluate",
    # External OTLP spans (0032_external_spans): listed by GET /spans, never part of a run's trace.
    "external",
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
    # LP.9: which loop-scoped events were kept and every loop instance's iteration counts; None
    # until filtered (build_trace filters `events` itself when the reader has not).
    loops: "LoopEvents | None" = None
    # LP.15: the judge usage rows grouped by request-key prefix, built once (see `_judge_rows`).
    judge_index: "dict[str, list[tuple[int, dict[str, Any]]]] | None" = field(
        default=None, init=False, repr=False, compare=False,
    )


@dataclass
class TraceInput:
    root: RunData
    children: list[RunData] = field(default_factory=list)
    children_truncated: bool = False
    workflow_spec: dict[str, Any] | None = None
    # (agent_id, agent_version) -> the spec's knowledge base refs, for runs with a retrieve call.
    knowledge_refs: dict[tuple[str, int], list[dict[str, Any]]] = field(default_factory=dict)


class LoopEvents:
    """LP.9: which loop-scoped events (`loop:{S}:...`) of one run a trace keeps, decided as the
    events go by in seq order, plus every shown loop instance's iteration counts.

    An iteration is shown when its index is below MAX_LOOP_ITERATION_SPANS, every iteration around
    it is shown, and when it is first seen the run has kept fewer than `budget` loop-scoped events.
    The decision is made once, at the first event of the iteration's scope (its `started`, or a body
    event that came before it), so every event inside an iteration follows it. Events of iterations
    left out are dropped, but the `started|completed|failed` of an iteration directly under a
    shown one is still counted. A `loop:` key whose scope does not parse is dropped (`malformed`).
    """

    def __init__(self, budget: int) -> None:
        self.budget = budget
        self.kept = 0
        self.malformed = 0
        self._shown: dict[str, bool] = {}
        # (outer scope, loop id) -> index -> phases seen
        self._iterations: dict[tuple[Scope, str], dict[int, set[str]]] = {}

    def keep(self, key: str, etype: str) -> bool:
        if not key.startswith("loop:"):
            return True
        parsed = _loop_key(key)
        if parsed is None:
            self.malformed += 1
            return False
        scope, rest = parsed
        outer, frame = scope[:-1], scope[-1]
        if etype in ITERATION_EVENT_TYPES and rest in ("started", "completed", "failed"):
            if not outer or self._decide(outer):
                self._iterations.setdefault((outer, frame.loop), {}).setdefault(frame.index, set()).add(rest)
        if not self._decide(scope):
            return False
        self.kept += 1
        return True

    def _decide(self, scope: Scope) -> bool:
        for i, frame in enumerate(scope):
            text = scope_string(scope[: i + 1])
            shown = self._shown.get(text)
            if shown is None:
                shown = frame.index < MAX_LOOP_ITERATION_SPANS and self.kept < self.budget
                self._shown[text] = shown
            if not shown:
                return False
        return True

    def omitted(self, scope: Scope) -> bool:
        """Whether `scope` is, or is inside, an iteration left out (read only)."""
        for i, frame in enumerate(scope):
            if frame.index >= MAX_LOOP_ITERATION_SPANS or self._shown.get(scope_string(scope[: i + 1])) is False:
                return True
        return False

    def counts(self, outer: Scope, loop_id: str) -> dict[str, int]:
        """Loop instance (`outer`, `loop_id`)'s iterations started, completed, failed and left out."""
        iterations = self._iterations.get((outer, loop_id), {})
        return {
            "iterations_started": sum(1 for p in iterations.values() if "started" in p),
            "iterations_completed": sum(1 for p in iterations.values() if "completed" in p),
            "iterations_failed": sum(1 for p in iterations.values() if "failed" in p),
            "iterations_omitted": sum(
                1 for i, p in iterations.items()
                if "started" in p and self.omitted((*outer, ScopeFrame(loop_id, i)))
            ),
        }

    def omitted_total(self) -> int:
        """Iterations left out whose loop instance is shown (nested ones inside them not again)."""
        return sum(self.counts(outer, loop_id)["iterations_omitted"] for outer, loop_id in self._iterations)


def _loop_key(key: str) -> tuple[Scope, str] | None:
    """(scope, the rest of the key) of a `loop:{S}:...` key; None when `S` is empty or malformed."""
    parts = key.split(":", 2)
    if len(parts) < 3 or parts[0] != "loop":
        return None
    try:
        scope = parse_scope(parts[1])
    except ScopeInvalid:
        return None
    return (scope, parts[2]) if scope else None


def _kept_body_steps(events: list[dict[str, Any]]) -> set[str]:
    """`{S}/{id}` (a child run's parent_step_id) of every body step whose step_started was kept."""
    steps = set()
    for event in events:
        if event["type"] != "step_started":
            continue
        parsed = _loop_key(event["event_key"])
        if parsed is None:
            continue
        parts = parsed[1].split(":")
        if len(parts) == 3 and parts[0] == "step" and parts[2] == "started":
            steps.add(f"{scope_string(parsed[0])}/{parts[1]}")
    return steps


def _loops(rd: RunData) -> LoopEvents:
    return rd.loops if rd.loops is not None else LoopEvents(MAX_LOOP_TRACE_EVENTS)


def _filter_loops(rd: RunData) -> RunData:
    if rd.loops is not None:
        return rd
    loops = LoopEvents(MAX_LOOP_TRACE_EVENTS)
    events = [e for e in rd.events if loops.keep(e["event_key"], e["type"])]
    return replace(rd, events=events, loops=loops)


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
            # AC.17: with the open-approval flag, so a waiting run shows `waiting_approval` (as
            # RunStore.get_run does).
            root = conn.execute(
                _run_select().where(RunModel.tenant_id == tenant_id, RunModel.id == run_id)
            ).first()
            if root is None:
                return None
            root_run = _run_dict(root[0], bool(root[1]))
            # One event budget for the whole trace: the run's own events first, then each child's
            # (creation order) while budget is left; children past it are not loaded at all.
            budget = MAX_TRACE_EVENTS
            root_data = self._events(
                conn, tenant_id, root_run, min(MAX_EVENTS_PER_RUN, budget), scan=root_run["kind"] == "workflow"
            )
            budget -= len(root_data.events)
            children_query = (
                _run_select()
                .where(RunModel.tenant_id == tenant_id, RunModel.parent_run_id == run_id)
                .order_by(RunModel.created_at, RunModel.id)
                .limit(MAX_CHILD_RUNS + 1)
            )
            if root_run["kind"] == "workflow":
                # LP.9: a body step's child run (parent_step_id `{S}/{id}`) is loaded only when that
                # step's events were kept, so left-out iterations do not use up MAX_CHILD_RUNS.
                shown = sorted(_kept_body_steps(root_data.events))
                children_query = children_query.where(
                    or_(
                        RunModel.parent_step_id.is_(None),
                        not_(RunModel.parent_step_id.contains("/", autoescape=True)),
                        RunModel.parent_step_id.in_(shown),
                    )
                )
            child_rows = conn.execute(children_query).all()
            children_truncated = len(child_rows) > MAX_CHILD_RUNS
            child_rows = child_rows[:MAX_CHILD_RUNS]
            runs = [root_run] + [_run_dict(r[0], bool(r[1])) for r in child_rows]
            ids = [r["id"] for r in runs]
            requests: dict[str, dict[str, dict[str, Any]]] = {i: {} for i in ids}
            for row in conn.execute(
                select(*RunModelRequestModel.__table__.columns).where(
                    RunModelRequestModel.tenant_id == tenant_id, RunModelRequestModel.run_id.in_(ids)
                )
            ).mappings():
                requests[row["run_id"]][row["request_key"]] = dict(row)
            root_data.requests = requests[root_run["id"]]
            loaded: list[RunData] = [root_data]
            for run in runs[1:]:
                if budget <= 0:
                    children_truncated = True
                    break
                child = self._events(conn, tenant_id, run, min(MAX_EVENTS_PER_RUN, budget), scan=False)
                child.requests = requests[run["id"]]
                budget -= len(child.events)
                loaded.append(child)
        return TraceInput(root=loaded[0], children=loaded[1:], children_truncated=children_truncated)

    def _events(self, conn: Session, tenant_id: str, run: dict[str, Any], limit: int, *, scan: bool) -> RunData:
        """Up to `limit` events of `run` in seq order, loop-scoped ones filtered by LoopEvents while
        they stream (a loop-scoped row is read without its payload, which is fetched afterwards for
        the kept rows only). With `scan` (the trace's own run) reading goes on past `limit`, up to
        MAX_SCANNED_EVENTS_PER_RUN rows, so iteration counts stay right."""
        loops = LoopEvents(MAX_LOOP_TRACE_EVENTS)
        cap = MAX_SCANNED_EVENTS_PER_RUN if scan else limit
        kept: list[dict[str, Any]] = []
        truncated = False
        scanned = 0
        statement = (
            select(
                ExecutionEventModel.seq,
                ExecutionEventModel.event_key,
                ExecutionEventModel.type,
                case(
                    (ExecutionEventModel.event_key.startswith("loop:", autoescape=True), None),
                    else_=ExecutionEventModel.data,
                ).label("data"),
                ExecutionEventModel.ts,
            )
            .where(ExecutionEventModel.tenant_id == tenant_id, ExecutionEventModel.run_id == run["id"])
            .order_by(ExecutionEventModel.seq)
            .limit(cap + 1)
        )
        for row in conn.execute(statement, execution_options={"yield_per": 2000}).mappings():
            scanned += 1
            if scanned > cap:
                truncated = True
                break
            if not loops.keep(row["event_key"], row["type"]):
                continue
            if len(kept) >= limit:
                # Past the run's event limit: keep counting iterations, admit no new ones.
                truncated = True
                loops.budget = 0
                if not scan:
                    break
                continue
            kept.append(dict(row))
        payloads = [e["seq"] for e in kept if e["event_key"].startswith("loop:")]
        if payloads:
            by_seq = {e["seq"]: e for e in kept}
            for i in range(0, len(payloads), 1000):
                for row in conn.execute(
                    select(ExecutionEventModel.seq, ExecutionEventModel.data).where(
                        ExecutionEventModel.tenant_id == tenant_id,
                        ExecutionEventModel.run_id == run["id"],
                        ExecutionEventModel.seq.in_(payloads[i : i + 1000]),
                    )
                ):
                    by_seq[row[0]]["data"] = row[1]
        return RunData(run=run, events=kept, requests={}, events_truncated=truncated, loops=loops)


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
_MARKER_EVENTS = frozenset({"post_turn_done", "memory_observe_trimmed", "score"})
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
        # LP.9: scoped spans (iterations, body steps) seen before their parent span, retried after
        # the run's events are walked; those whose parent never appears are dropped.
        self._pending: list[Callable[[], None]] = []

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
            # The model that answered (a fallback model, when one did); `model_id` is the one asked.
            "answering_model_id": answering_model_id(rd.requests),
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
        if run.get("display_status") == "waiting_approval":
            status = "waiting"
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
        saved_pending, self._pending = self._pending, []
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
                if data.get("output_candidate") is True:
                    attrs["output_candidate"] = True
                attempts = _fallback_attributes(data, attrs)
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
                if attempts:
                    self._fallback_attempts(rd, f"{rid}:model:{n}", request_key, n, start, attempts, message)
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
                    # AC.17: a denied (or timed-out) gated call completed: the model read the denial.
                    status = {"ok": "ok", "error": "error", "denied": "ok"}.get(rdata.get("status"), "incomplete")
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
            elif etype == "loop_iteration_started":
                self._iteration(rd, event, by_key, open_status)
            elif etype in ("tool_approval_waiting", *TOOL_DECISION_TYPES):
                self._tool_approval(rd, event, by_key, run_span, open_status)
            elif etype == "output_retry":
                self._output_retry(rd, event, run_span)
            elif etype == "eval_case_result":
                self._eval_case(rd, event, run_span)
            elif etype == "run_failed" and data.get("error_code") == "fallback_exhausted":
                self._exhausted(rd, event, previous, run_span)
            # Markers without a span (P1.C1 `post_turn_done`, P1.C6 `memory_observe_trimmed`, written
            # right before the reflection's own event) never start the next span.
            if etype in _MARKER_EVENTS:
                continue
            # An output processor checks a response before its model:{n}:final is written: the model
            # request's span must not start at it (it would collapse to the checks' duration).
            if not (etype == "processor_result" and key.startswith("processor:output:")):
                previous = event
        # LP.9: retry scoped spans whose parent came later in the log, until no more can be placed.
        pending = self._pending
        while pending:
            self._pending = []
            for place in pending:
                place()
            if len(self._pending) == len(pending):
                break
            pending = self._pending
        self._pending = saved_pending
        # Child runs whose step span is missing (cannot happen for a consistent log) hang off the run.
        if rd is self.data.root:
            for child in self.data.children:
                if child.run["id"] not in self.placed_children:
                    self.placed_children.add(child.run["id"])
                    if self._child_scoped(child):
                        continue  # LP.9: a body step's child run never hangs off the run root
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

    # -- agent capabilities (AC.17)

    def _fallback_attempts(
        self,
        rd: RunData,
        model_span: str,
        request_key: str,
        n: int | None,
        start: datetime | None,
        attempts: list[tuple[int, dict[str, Any]]],
        message: dict[str, Any],
    ) -> None:
        """One `fallback_attempt` span per failed attempt of model request n, under its span: from
        the stored response's `failed_attempts[k]` timestamp for its duration (a response that was
        blocked or never stored has none: zero length at the request's start, `timed: false`);
        usage and cost from its `model:{n}:failed:{k}` row when the attempt reported usage. `k` is
        the attempt's raw position in the event's list, so a malformed entry never shifts timings."""
        rid = rd.run["id"]
        timings = message.get("failed_attempts") if isinstance(message.get("failed_attempts"), list) else []
        for k, attempt in attempts:
            timing = timings[k] if k < len(timings) and isinstance(timings[k], dict) else {}
            began = _parse_datetime(timing.get("timestamp"))
            took = _parse_duration(timing.get("duration"))
            timed = began is not None and took is not None
            attempt_start = began if timed else start
            attempt_end = began + took if timed else start
            key = f"{request_key}:failed:{k}"
            attrs: dict[str, Any] = {
                "request_key": key,
                "model_request": n,
                "fallback_position": k,
                "model_id": attempt.get("model_id"),
                "outcome": attempt.get("outcome"),
                "error_type": attempt.get("error_type"),
                "http_status": attempt.get("http_status"),
                "timed": timed,
            }
            row = rd.requests.get(key)
            if row is not None:
                usage, model_attrs = _model_usage(row, {}, {})
                attrs.update(**model_attrs, usage=usage)
            else:
                attrs["usage"] = None
            self.add(
                f"{rid}:model:{n}:failed:{k}", model_span, "fallback_attempt",
                str(attempt.get("model_id") or f"attempt {k}"), attempt_start, attempt_end, "error", attrs,
            )

    def _exhausted(self, rd: RunData, event: dict[str, Any], previous: dict[str, Any] | None, run_span: str) -> None:
        """An exhausted fallback chain (run_failed `fallback_exhausted` with `model_request` and
        `failed_attempts`, AC.17): the request as an error `model_request` span (`exhausted: true`,
        from the previous event to the failure, no usage: nothing answered) and one
        `fallback_attempt` span per model, timed from the attempt's `timestamp` / `duration_ms` when
        recorded. Older runs without `failed_attempts` show the failure on the run span only."""
        data = event.get("data") or {}
        if not isinstance(data.get("failed_attempts"), list):
            return
        rid = rd.run["id"]
        n = data.get("model_request") if isinstance(data.get("model_request"), int) else None
        request_key = f"model:{n}"
        attrs: dict[str, Any] = {"request_key": request_key, "model_request": n, "exhausted": True}
        attempts = _fallback_attributes(data, attrs)
        first = next((a for _k, a in attempts), {})
        attrs.update(model_id=None, requested_model_id=first.get("model_id"), usage=None, tool_calls=0)
        start = (previous or event)["ts"]
        span_id = f"{rid}:model:{n}"
        self.add(span_id, run_span, "model_request", "model request (every model failed)", start, event["ts"], "error", attrs)
        if span_id not in self.ids:
            return
        raw = data["failed_attempts"]
        timings = {
            "failed_attempts": [
                {"timestamp": a.get("timestamp"), "duration": (a["duration_ms"] / 1000)
                 if isinstance(a.get("duration_ms"), int) and not isinstance(a.get("duration_ms"), bool) else None}
                if isinstance(a, dict) else None
                for a in raw
            ]
        }
        self._fallback_attempts(rd, span_id, request_key, n, start, attempts, timings)

    def _tool_approval(
        self, rd: RunData, event: dict[str, Any], by_key: dict[str, Any], run_span: str, open_status: str
    ) -> None:
        """A gated tool call's approval (`tool:{id}:approval:waiting` → `tool:{id}:approval:decision`)
        as a `tool_approval` span under its tool call: `waiting` while undecided in a running run,
        `incomplete` once the run ended undecided, `error` for a timeout, else `ok`. A call denied
        without a wait (an eval run, an unaddressable id) is a zero-length span at its decision."""
        rid = rd.run["id"]
        key, data = event["event_key"], event.get("data") or {}
        parts = key.split(":")
        call_id = data.get("tool_call_id") or (parts[1] if len(parts) > 1 else key)
        waiting = by_key.get(f"tool:{call_id}:approval:waiting")
        decision = by_key.get(f"tool:{call_id}:approval:decision")
        if event["type"] != "tool_approval_waiting" and waiting is not None:
            return  # the waiting event made the span
        wdata = (waiting or {}).get("data") or {}
        attrs: dict[str, Any] = {
            "tool_call_id": call_id,
            "tool_name": wdata.get("tool_name"),
            "deadline": wdata.get("deadline"),
            "waiting_since": _iso(waiting["ts"]) if waiting is not None else None,
            "waited": waiting is not None,
        }
        if decision is not None:
            ddata = decision.get("data") or {}
            attrs["decision"] = {
                "type": decision["type"],
                "decided_by": ddata.get("decided_by"),
                "comment": ddata.get("comment"),
                "reason": ddata.get("reason"),
                "at": _iso(decision["ts"]),
            }
            status = "error" if decision["type"] == "tool_approval_timed_out" else "ok"
            end = decision["ts"]
        else:
            status = "waiting" if open_status == "running" else "incomplete"
            end = None
        start = (waiting or decision or event)["ts"]
        parent = f"{rid}:tool:{call_id}"
        name = f"approval {attrs['tool_name']}" if attrs["tool_name"] else "approval"
        self.add(
            f"{rid}:tool:{call_id}:approval", parent if parent in self.ids else run_span, "tool_approval",
            name, start, end, status, attrs,
        )

    def _output_retry(self, rd: RunData, event: dict[str, Any], run_span: str) -> None:
        """An `output_retry` event (`output:{n}:retry`): a zero-length marker; the JSON paths the
        rejected candidate of model request n broke (never values)."""
        rid = rd.run["id"]
        key, data = event["event_key"], event.get("data") or {}
        n = data.get("model_request") if isinstance(data.get("model_request"), int) else _request_index(key)
        errors = data.get("errors") if isinstance(data.get("errors"), list) else []
        attrs = {"event_key": key, "model_request": n, "errors": errors, "error_count": len(errors), "cause": data.get("cause")}
        self.add(f"{rid}:{key}", run_span, "output_retry", "output retry", event["ts"], event["ts"], "ok", attrs)

    def _eval_case(self, rd: RunData, event: dict[str, Any], run_span: str) -> None:
        """An eval run's `eval_case_result` event (`case:{case_id}`): an `eval_case` span from its
        child run's start to the record, the child run (parent_step_id `case:{case_id}`) under it,
        and one zero-length `judge` span per judge usage row (`judge:{case_id}:{i}`)."""
        rid = rd.run["id"]
        key, data = event["event_key"], event.get("data") or {}
        case_id = data.get("case_id") or key.removeprefix("case:")
        children = self.children_by_step.get(f"case:{case_id}", []) if rd is self.data.root else []
        child = children[0].run if children else None
        start = (child or {}).get("started_at") or (child or {}).get("created_at") or event["ts"]
        case_status = data.get("status")
        attrs: dict[str, Any] = {"case_id": case_id, "case_status": case_status}
        for k in ("position", "assertions", "scores", "labels", "evaluator_failures", "duration_ms", "cost_usd",
                  "error_code", "child_run_id"):
            if k in data:
                attrs[k] = data[k]
        span_id = f"{rid}:{key}"
        self.add(
            span_id, run_span, "eval_case", f"case {data.get('position', case_id)}", start, event["ts"],
            "ok" if case_status == "passed" else "error", attrs,
        )
        if span_id not in self.ids:
            return
        prefix = f"judge:{case_id}:"
        for request_key in sorted(
            (k for k in rd.requests if k.startswith(prefix)), key=lambda k: _int_or(k[len(prefix):])
        ):
            row = rd.requests[request_key]
            usage, model_attrs = _model_usage(row, {}, {})
            jattrs = {
                "request_key": request_key, "case_id": case_id,
                "evaluator_index": _int_or(request_key[len(prefix):], None), **model_attrs, "usage": usage,
            }
            self.add(
                f"{rid}:{request_key}", span_id, "judge", f"judge {model_attrs.get('model_id') or ''}".strip(),
                event["ts"], event["ts"], "ok", jattrs,
            )
        for child_rd in children:
            if child_rd.run["id"] not in self.placed_children:
                self.placed_children.add(child_rd.run["id"])
                self.run(child_rd, span_id, "child_run")

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
        parsed = _step_key_scope(event["event_key"])
        if parsed is None:
            return  # a `loop:` key whose scope does not parse: no span is invented for it
        scope, key_step = parsed
        step_id = data.get("step_id") or key_step
        if not isinstance(step_id, str):
            return
        if scope and self._scope_omitted(rd, scope):
            return  # an iteration left out (MAX_LOOP_ITERATION_SPANS, MAX_LOOP_TRACE_EVENTS)
        phases = ("completed", "failed", "waiting", "decision")
        try:
            keys = {phase: scoped_event_key(scope, step_id, phase) for phase in phases}
            token = instance_token(scope, step_id)
        except ScopeInvalid:
            if scope:
                return  # a malformed body step id: nothing to name the span by
            # A top-level id the helpers refuse (never written by a validated spec): read as before.
            keys = {phase: f"step:{step_id}:{phase}" for phase in phases}
            token = step_id
        done = by_key.get(keys["completed"])
        failed = by_key.get(keys["failed"])
        waiting = by_key.get(keys["waiting"])
        decision = by_key.get(keys["decision"])
        attrs: dict[str, Any] = {"step_id": step_id, "step_type": data.get("step_type")}
        if scope:
            attrs["scope"] = scope_string(scope)
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
        kind = "workflow_step"
        judge_rows: list[tuple[int, dict[str, Any]]] = []
        if data.get("step_type") in LOOP_STEP_TYPES:
            kind = "loop"
            attrs.update(self._loop_attributes(rd, scope, step_id, output))
        elif data.get("step_type") == "evaluate":
            kind = "evaluate"
            judge_rows = _judge_rows(rd, scope, step_id)
            attrs.update(_evaluate_attributes(output, judge_rows))
        if scope:
            parent_span = f"{rid}:loop:{scope_string(scope)}"
            if parent_span not in self.ids:
                # Its iteration's span is not there (yet): retried once the log is walked.
                self._pending.append(lambda: self._step(rd, event, by_key, run_span, open_status))
                return
        else:
            parent = self.parents.get(step_id)
            parent_span = f"{rid}:step:{parent}" if parent is not None else run_span
        span_id = f"{rid}:step:{token}"
        self.add(
            span_id, parent_span, kind, step_id, event["ts"], end, status, attrs,
            data.get("input"), output,
        )
        if span_id not in self.ids:
            return
        if judge_rows:
            self._judges(rd, span_id, scope, step_id, judge_rows, output, end or event["ts"])
        if rd is not self.data.root:
            return
        for child in self.children_by_step.get(scoped_parent_step_id(scope, step_id), ()):
            if child.run["id"] not in self.placed_children:
                self.placed_children.add(child.run["id"])
                self.run(child, span_id, "child_run")

    def _judges(
        self, rd: RunData, span_id: str, scope: Scope, step_id: str,
        rows: list[tuple[int, dict[str, Any]]], output: Any, at: datetime,
    ) -> None:
        """LP.15: one zero-length `judge` span per judge usage row of an evaluate step, under it, at
        the step's end (the rows carry no timing): usage, cost, model and which evaluator; never
        the judge's reason or the judged value."""
        rid = rd.run["id"]
        results = output.get("results") if isinstance(output, dict) else None
        for index, row in rows:
            usage, model_attrs = _model_usage(row, {}, {})
            jattrs: dict[str, Any] = {
                "request_key": row["request_key"], "step_id": step_id, "evaluator_index": index,
            }
            if scope:
                jattrs["scope"] = scope_string(scope)
            entry = results[index] if isinstance(results, list) and index < len(results) else None
            if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                jattrs["evaluator_name"] = entry["name"]
            jattrs.update(**model_attrs, usage=usage)
            self.add(
                f"{rid}:{row['request_key']}", span_id, "judge",
                f"judge {model_attrs.get('model_id') or ''}".strip(), at, at, "ok", jattrs,
            )

    # -- workflow loops (LP.9)

    def _loop_attributes(self, rd: RunData, scope: Scope, step_id: str, output: Any) -> dict[str, Any]:
        """A loop span's iteration counts (from its iteration events) and its output summary."""
        attrs: dict[str, Any] = _loops(rd).counts(scope, step_id)
        if isinstance(output, dict):
            for k in ("iterations", "exited"):
                if k in output:
                    attrs[k] = output[k]
        return attrs

    def _iteration(self, rd: RunData, event: dict[str, Any], by_key: dict[str, Any], open_status: str) -> None:
        """A `loop:{S}:started` event: one `loop_iteration` span under its loop step's span, from
        it to `loop:{S}:completed|failed`. Iterations LoopEvents left out (past
        MAX_LOOP_ITERATION_SPANS by index, or past MAX_LOOP_TRACE_EVENTS) and every iteration inside
        one are not shown; their counts come from LoopEvents."""
        rid = rd.run["id"]
        parsed = _iteration_key_scope(event["event_key"])
        if parsed is None or parsed[1] != "started":
            return
        scope = parsed[0]
        outer, frame = scope[:-1], scope[-1]
        text = scope_string(scope)
        if self._scope_omitted(rd, scope):
            return
        parent_span = f"{rid}:step:{instance_token(outer, frame.loop)}"
        if parent_span not in self.ids:
            # Its loop step's span is not there (yet): retried once the log is walked.
            self._pending.append(lambda: self._iteration(rd, event, by_key, open_status))
            return
        data = event.get("data") or {}
        completed = by_key.get(iteration_event_key(scope, "completed"))
        failed = by_key.get(iteration_event_key(scope, "failed"))
        attrs: dict[str, Any] = {"step_id": frame.loop, "scope": text, "index": frame.index}
        output = None
        end = None
        if completed is not None:
            cdata = completed.get("data") or {}
            status, end = "ok", completed["ts"]
            attrs["iteration_status"] = "completed"
            for k in ("until", "last_key"):
                if k in cdata:
                    attrs[k] = cdata[k]
            output = cdata.get("state")
        elif failed is not None:
            fdata = failed.get("data") or {}
            status, end = "error", failed["ts"]
            attrs.update(
                iteration_status=fdata.get("status"), error_code=fdata.get("error_code"), error=fdata.get("error"),
                failed_step_id=fdata.get("failed_step_id"),
            )
        else:
            status = open_status
        iteration_input = data["item"] if "item" in data else data.get("state")
        self.add(
            f"{rid}:loop:{text}", parent_span, "loop_iteration",
            f"{frame.loop} #{frame.index}", event["ts"], end, status, attrs, iteration_input, output,
        )

    def _scope_omitted(self, rd: RunData, scope: Scope) -> bool:
        """Whether `scope` is, or is inside, an iteration left out."""
        return _loops(rd).omitted(scope)

    def _child_scoped(self, child: RunData) -> bool:
        """Whether a child run belongs to a body step (its parent_step_id is `{S}/{id}`)."""
        parent_step = child.run.get("parent_step_id")
        if not isinstance(parent_step, str) or "/" not in parent_step:
            return False
        try:
            return bool(parse_scope(parent_step.rsplit("/", 1)[0]))
        except ScopeInvalid:
            return False

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
    if request_key.startswith("judge:"):
        return "judge"
    return "chat"


# The split of a run's (or trace's) usage (RM.19 follow-up): the agent's chat model, observational
# memory (observer + reflector), moderation and embedding requests apart, so the chat model's tokens
# are never labelled with embedding or observer tokens. Every request is in exactly one group, so
# chat + memory + moderation + embedding input tokens == input_tokens (and the same for output).
_SPLIT_GROUPS = {"chat": "chat", "observer": "memory", "reflector": "memory", "moderation": "moderation",
                 "embedding": "embedding", "judge": "judge"}


class _UsageSplit:
    def __init__(self) -> None:
        self.tokens = {g: {"input": 0, "output": 0} for g in ("chat", "memory", "moderation", "embedding", "judge")}
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
        out: dict[str, Any] = {"chat_requests": self.requests["chat"], "judge_requests": self.requests["judge"]}
        for g in ("chat", "memory", "moderation", "judge"):
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


_ANSWER_KEY = re.compile(r"^model:([0-9]{1,18})$")


def answering_model_id(requests: dict[str, dict[str, Any]]) -> str | None:
    """The model that answered the run's last chat request: the `model_id` of the `model:{n}`
    usage row with the highest n (a failed fallback attempt's `model:{n}:failed:{k}` row is not
    an answer); None without one. RunStore.chat_usage's `answering_model_id` uses the same rule."""
    best: tuple[int, str] | None = None
    for key, row in requests.items():
        match = _ANSWER_KEY.match(key)
        if match is None or (row.get("operation") or "chat") != "chat" or not row.get("model_id"):
            continue
        n = int(match.group(1))
        if best is None or n > best[0]:
            best = (n, row["model_id"])
    return best[1] if best else None


def _run_name(run: dict[str, Any]) -> str:
    if run["kind"] == "kb_ingest":
        return f"knowledge base {run.get('knowledge_base_id')} ingest"
    if run["kind"] == "kb_source_sync":
        return f"knowledge base {run.get('knowledge_base_id')} source sync"
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


_DATETIME = TypeAdapter(datetime)
_DURATION = TypeAdapter(timedelta)


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = _DATETIME.validate_python(value)
    except ValidationError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _parse_duration(value: Any) -> timedelta | None:
    """A stored timedelta: Pydantic's JSON form (ISO 8601, e.g. `PT1.5S`) or seconds."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        parsed = _DURATION.validate_python(value)
    except ValidationError:
        return None
    return parsed if parsed >= timedelta(0) else None


# LP.15: an evaluate step's result fields copied into its span (never `reason`: model text).
EVALUATE_RESULT_ATTRIBUTES = ("name", "kind", "passed", "score", "skipped", "error_type", "error_code")
# At most this many result entries are read (a spec allows 1..8 evaluators).
MAX_EVALUATE_RESULTS = 8


def _judge_rows(rd: RunData, scope: Scope, step_id: str) -> list[tuple[int, dict[str, Any]]]:
    """(evaluator index, usage row) of an evaluate step's judge requests on the workflow run
    (`judge:{id}:{k}`, `judge:{S}:{id}:{k}` in a body; scoped_judge_key), by index."""
    try:
        prefix = scoped_judge_key(scope, step_id, 0)[:-1]
    except ScopeInvalid:
        return []
    return _judge_index(rd).get(prefix, [])


def _judge_index(rd: RunData) -> dict[str, list[tuple[int, dict[str, Any]]]]:
    """The run's `judge:` usage rows grouped once by key prefix (everything up to and including the
    last `:`), each group sorted by its decimal evaluator index, so an evaluate span looks its rows
    up instead of scanning every request (one scan per run, not per iteration)."""
    if rd.judge_index is None:
        index: dict[str, list[tuple[int, dict[str, Any]]]] = {}
        for key, row in rd.requests.items():
            if not key.startswith("judge:"):
                continue
            head, sep, k = key.rpartition(":")
            if sep and k.isdecimal():
                index.setdefault(head + ":", []).append((int(k), row))
        for rows in index.values():
            rows.sort(key=lambda r: r[0])
        rd.judge_index = index
    return rd.judge_index


def _evaluate_attributes(output: Any, rows: list[tuple[int, dict[str, Any]]]) -> dict[str, Any]:
    """An evaluate span's attributes (LP.15): the verdict (`passed`, `score`), each evaluator's
    allowlisted result fields (EVALUATE_RESULT_ATTRIBUTES, never its reason), the counts, and the
    step's judge usage: `judge_requests`, `judge_input_tokens`, `judge_output_tokens` and
    `judge_cost_usd` (null when a judge row is unpriced)."""
    attrs: dict[str, Any] = {}
    if isinstance(output, dict):
        if isinstance(output.get("passed"), bool):
            attrs["passed"] = output["passed"]
        score = output.get("score")
        if isinstance(score, (int, float)) and not isinstance(score, bool):
            attrs["score"] = score
        raw = output.get("results")
        if isinstance(raw, list):
            results = [
                {k: r[k] for k in EVALUATE_RESULT_ATTRIBUTES if k in r and not isinstance(r[k], (dict, list))}
                for r in raw[:MAX_EVALUATE_RESULTS] if isinstance(r, dict)
            ]
            attrs["results"] = results
            # The entries read (dicts, at most MAX_EVALUATE_RESULTS): the counts below are of these.
            attrs["evaluators"] = len(results)
            attrs["evaluators_passed"] = sum(1 for r in results if r.get("passed") is True)
            attrs["evaluators_failed"] = sum(1 for r in results if r.get("passed") is False)
            attrs["evaluators_skipped"] = sum(1 for r in results if r.get("skipped") is True)
    tokens_in = tokens_out = 0
    cost = Decimal(0)
    unpriced = False
    for _index, row in rows:
        tokens_in += int(row.get("input_tokens") or 0)
        tokens_out += int(row.get("output_tokens") or 0)
        if row.get("cost_usd") is None:
            unpriced = True
        else:
            cost += Decimal(str(row["cost_usd"]))
    attrs.update(
        judge_requests=len(rows),
        judge_input_tokens=tokens_in if rows else None,
        judge_output_tokens=tokens_out if rows else None,
        judge_cost_usd=None if (unpriced or not rows) else format(cost, "f"),
    )
    return attrs


def _int_or(value: str, default: Any = 1 << 30) -> Any:
    return int(value) if value.isdecimal() else default


TOOL_DECISION_TYPES = ("tool_approved", "tool_denied", "tool_approval_timed_out")
LOOP_STEP_TYPES = ("loop", "foreach")
ITERATION_EVENT_TYPES = ("loop_iteration_started", "loop_iteration_completed", "loop_iteration_failed")


def _step_key_scope(key: str) -> tuple[Scope, str | None] | None:
    """(scope, step id) of a step event key: `step:{id}:{phase}` (the empty scope) or
    `loop:{S}:step:{id}:{phase}`; None for any other `loop:` key (a malformed or empty scope, or
    not a step key), which must not be read as a top-level step."""
    parts = key.split(":")
    if parts[0] == "loop":
        if len(parts) != 5 or parts[2] != "step":
            return None
        try:
            scope = parse_scope(parts[1])
        except ScopeInvalid:
            return None
        return (scope, parts[3]) if scope else None
    return EMPTY_SCOPE, parts[1] if len(parts) > 1 else None


def _iteration_key_scope(key: str) -> tuple[Scope, str] | None:
    """(the iteration's scope, phase) of `loop:{S}:started|completed|failed`, else None."""
    parts = key.split(":")
    if len(parts) != 3 or parts[0] != "loop" or parts[2] not in ("started", "completed", "failed"):
        return None
    try:
        scope = parse_scope(parts[1])
    except ScopeInvalid:
        return None
    return (scope, parts[2]) if scope else None
# model_message fields a request on a fallback chain carries (AC.5); `answering_model` is on every one.
FALLBACK_ATTRIBUTES = ("answering_model", "requested_model_id", "fallback_index")


def _fallback_attributes(data: dict[str, Any], attrs: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
    """Copy a model_message's (or an exhausted run_failed's) answering model and fallback fields into
    `attrs` (failed attempts as `{model_id, outcome, error_type, http_status}`, never error text);
    returns the failed attempts with their raw positions (non-dict entries are skipped, never
    renumbered)."""
    for k in FALLBACK_ATTRIBUTES:
        if k in data:
            attrs[k] = data[k]
    raw = data.get("failed_attempts")
    attempts = [
        (k, {f: a.get(f) for f in ("model_id", "outcome", "error_type", "http_status")})
        for k, a in enumerate(raw if isinstance(raw, list) else ())
        if isinstance(a, dict)
    ]
    if "failed_attempts" in data:
        attrs["failed_attempts"] = [a for _k, a in attempts]
    return attempts


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
    # LP.9: loop-scoped events the reader has not filtered (TraceInput built in memory) go through
    # the same LoopEvents filter.
    data = replace(data, root=_filter_loops(data.root), children=[_filter_loops(c) for c in data.children])
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
            # LP.9: loop iterations left out (past max_loop_iteration_spans per loop instance, or
            # past max_loop_trace_events in all), counted once per left-out iteration.
            "iterations": _loops(data.root).omitted_total(),
        },
        "limits": {
            "max_spans": MAX_SPANS,
            "max_events_per_run": MAX_EVENTS_PER_RUN,
            "max_trace_events": MAX_TRACE_EVENTS,
            "max_child_runs": MAX_CHILD_RUNS,
            "field_max_bytes": FIELD_MAX_BYTES,
            "payload_budget_bytes": PAYLOAD_BUDGET_BYTES,
            "max_loop_iteration_spans": MAX_LOOP_ITERATION_SPANS,
            "max_loop_trace_events": MAX_LOOP_TRACE_EVENTS,
        },
    }


def flat_spans(trace: dict[str, Any]) -> list[dict[str, Any]]:
    """The trace's spans as flat rows for the `spans` table (parents first, as the trace lists
    them): span_id, parent_id, kind, name, start, end (datetimes), status and the run's agent_id and
    workflow_id plus the span's model. No inputs, outputs or attributes: the table is a filterable
    index, the trace itself stays the place to read a span. A span without a start takes its
    parent's (the table's start is not null), one with no parent either is left out."""
    run = trace["run"]
    starts: dict[str, datetime | None] = {}
    rows: list[dict[str, Any]] = []
    for span in trace["spans"]:
        start = _parse_datetime(span.get("start")) or starts.get(span.get("parent_id") or "")
        starts[span["id"]] = start
        if start is None:
            continue
        attrs = span.get("attributes") or {}
        model = attrs.get("model_id") or attrs.get("model")
        if span["kind"] == "run" and span["id"] == f"run:{run['id']}":
            model = model or run.get("model_id")
        rows.append({
            "span_id": span["id"],
            "parent_id": span.get("parent_id"),
            "kind": span["kind"],
            "name": span["name"],
            "start": start,
            "end": _parse_datetime(span.get("end")),
            "status": span["status"],
            "agent_id": run.get("agent_id"),
            "workflow_id": run.get("workflow_id"),
            "model": model if isinstance(model, str) else None,
        })
    return rows


__all__ = [
    "FIELD_MAX_BYTES",
    "MAX_CHILD_RUNS",
    "MAX_EVENTS_PER_RUN",
    "MAX_LOOP_ITERATION_SPANS",
    "MAX_LOOP_TRACE_EVENTS",
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
    "flat_spans",
    "step_parents",
]
