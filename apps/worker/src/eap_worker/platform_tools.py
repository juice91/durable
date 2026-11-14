"""Platform tools (RAG and memory milestone, RM.11; RM.12 adds `update_working_memory`, built in
eap_worker.memory).

Platform tools are added to a run by the platform, never listed in `AgentSpec.tools`. They live in
`PlatformToolset`, the third member of the run's `RunToolset` (eap_worker.mcp), and only for a spec
that uses them: a spec without `knowledge_bases` and without a memory turn's writable working memory gets
`platform_toolset_for_run(...) is None`, so its toolset and its DBOS step sequence are exactly what
they were before platform tools existed.

`retrieve` (present when the spec attaches knowledge bases):

- The model sees `retrieve(search_query: str, knowledge_base: str | None = None)`, where
  `knowledge_base` is an enum of the attached knowledge bases' names, and a description listing
  their names and descriptions. The names are read inside the `get_tools` step (a DBOS step, so its
  output is recorded and a replay never reads them again); the recorded definition carries the
  name -> id map in its metadata, so the call step resolves the name the model saw even if the
  knowledge base was renamed meanwhile. Rebuilding the tool for the call step does no I/O.
- Filters, top_k and min_score come ONLY from the spec's `KnowledgeBaseRef`s; the tenant ONLY from
  the run's deps. The model chooses the query and, at most, one of the attached names.
- Searched knowledge bases = the spec's ids (or the named one) ∩ the tenant's existing ids, read at
  call time. None left: a model-visible tool error ("... no longer exists"), never a crash.
- References are grouped by the knowledge base's embedding model (one query embedding per model)
  and then by (filter, top_k, min_score) (one `KnowledgeStore.search` per group). The merged hits
  are ordered by score and cut at the largest top_k of the searched references.
- Embedding usage: request key `embed:retrieve:{tool_call_id}` when the call searches one embedding
  model; with several, each model's key is `embed:retrieve:{tool_call_id}:{model_id}` (models in
  sorted order). The suffix is the model's own id, never its position, so a knowledge base deleted
  or reindexed between a crash and the re-execution cannot move one model's usage onto another's
  key. Each is written with its
  `embedding` event in one transaction (`record_embedding`), so a re-executed call step records it
  once. Retryable provider errors are retried inside the call (read-only, so harmless); any failure
  left becomes a fixed model-visible tool error, logged with a code only.
- The result is at most MAX_RETRIEVE_RESULT_BYTES (24 KiB) of numbered sources,
  `[n] {title} — {anchor_url or "kb:{document_key}"}` each followed by the section text with every
  line quoted as `> ...` (so retrieved text cannot forge a `[n] ... — url` header), then the
  fixed RETRIEVE_INSTRUCTION. Structured sources `{n, title, url, ref, kb_id, document_id,
  document_key, chunk_id, doc_version, score}` travel in the ToolReturn's metadata (not sent to the
  model); the workflow copies them into the `tool_result` event as `sources`.
- The tool is read-only: the call step's result is checkpointed by DBOS, so a replay returns the
  identical result; a re-executed step searches again. The crash point `after_tool_call` (crash.py)
  also fires here, after the search and before that checkpoint.

`knowledge.search` (hidden; eap_schemas.tools.KNOWLEDGE_SEARCH_DECLARATION) runs the same search for
one knowledge base in a tool run (the knowledge base search tester, POST
/knowledge-bases/{id}/search). It is registered in the worker's registry by
`register_platform_tools` and refused in an agent spec's tools (`check_no_hidden_tools`).

Privacy: queries and section text never go to logs or spans from here (logs carry counts and ids);
Pydantic AI's instrumentation is configured with include_content=False (eap_observability).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any

from pydantic import Field
from pydantic_ai import RunContext, ToolFailed
from pydantic_ai.messages import ToolReturn
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import AbstractToolset
from pydantic_ai.toolsets.abstract import ToolsetTool
from pydantic_core import SchemaValidator, core_schema

from eap_observability import log_failure
from eap_run_store import KnowledgeStore, MemoryStore, SearchHit
from eap_runtime import RunDeps, ToolRegistry, UnknownToolError
from eap_schemas import AgentSpec, EmbeddingDimensionMismatch, EmbeddingModelNotAllowed, KnowledgeBaseNotFound
from eap_schemas.knowledge import MAX_RETRIEVE_RESULT_BYTES
from eap_schemas.tools import (
    HIDDEN_TOOL_DECLARATIONS,
    KNOWLEDGE_SEARCH_DECLARATION,
    MAX_SEARCH_QUERY_CHARS,
    RETRIEVE_TOOL_NAME,
)
from eap_worker.crash import crash_point
from eap_worker.embeddings import EmbeddingCall, embed_query, is_retryable, record_embedding

log = logging.getLogger("eap_worker.platform_tools")

HIDDEN_TOOL_IDS: frozenset[str] = frozenset(HIDDEN_TOOL_DECLARATIONS)

RETRIEVE_INSTRUCTION = (
    "Cite every source you use by its number in square brackets, e.g. [1], together with its URL. "
    "Use only these sources for facts from the knowledge base; if they do not answer the question, say so."
)
NO_RESULTS_TEXT = "No matching sections were found in the knowledge base."
KB_GONE_ALL = "The attached knowledge bases no longer exist. Answer without them."
KB_GONE_ONE = "The knowledge base '{name}' no longer exists."
RETRIEVAL_FAILED = "The knowledge base search failed. Details are withheld; answer without it or try again later."
EMBEDDING_NOT_ALLOWED = "The knowledge base search is unavailable: its embedding model is not allowed on this platform."
RETRIEVE_MAX_ARG_RETRIES = 2  # schema (type) errors only: other argument problems are tool errors
EMBED_ATTEMPTS = 3
EMBED_BACKOFF_S = 0.5
MAX_DESCRIPTION_CHARS = 300  # per knowledge base, in the tool description
MAX_SOURCE_TITLE_CHARS = 200
MAX_SEARCH_HIT_CONTENT_CHARS = 4000  # per hit in a knowledge.search result


# ------------------------------------------------------------------------------- the toolset


class PlatformTool:
    """One platform tool of `PlatformToolset`.

    `definition(ctx)` builds the model-visible ToolDefinition; it may read the database because it
    runs only inside the `get_tools` step. Everything else must be I/O-free and work from the
    recorded definition alone: the call step rebuilds the tool with `toolset_tool(...)`."""

    name: str
    max_retries: int = 0
    args_validator: SchemaValidator

    async def definition(self, ctx: RunContext[Any]) -> ToolDefinition:  # pragma: no cover - abstract
        raise NotImplementedError

    def args_check(self, tool_def: ToolDefinition) -> Callable[..., None] | None:
        """An args_validator_func for this definition (ModelRetry before the call), or None.
        Note: DBOSDurability runs a tool with one as an extra `...validate_args` DBOS step, so a
        tool that must add no step checks its arguments inside `call` instead (as retrieve does)."""
        return None

    async def call(self, args: dict[str, Any], ctx: RunContext[Any], tool_def: ToolDefinition) -> Any:  # pragma: no cover
        raise NotImplementedError


class PlatformToolset(AbstractToolset[Any]):
    """The run's platform tools (retrieve; RM.12: update_working_memory). Built without I/O."""

    def __init__(self, tools: Sequence[PlatformTool]) -> None:
        self.tools: dict[str, PlatformTool] = {}
        for tool in tools:
            if tool.name in self.tools:
                raise ValueError(f"platform tool registered twice: {tool.name}")
            self.tools[tool.name] = tool

    @property
    def id(self) -> str | None:
        return None  # always inside the run's DynamicToolset, which carries the durable id

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self.tools)

    def __repr__(self) -> str:
        return f"PlatformToolset(tools={sorted(self.tools)!r})"

    def _toolset_tool(self, tool: PlatformTool, tool_def: ToolDefinition) -> ToolsetTool[Any]:
        return ToolsetTool(
            toolset=self,
            tool_def=tool_def,
            max_retries=tool.max_retries,
            args_validator=tool.args_validator,
            args_validator_func=tool.args_check(tool_def),
        )

    async def get_tools(self, ctx: RunContext[Any]) -> dict[str, ToolsetTool[Any]]:
        return {name: self._toolset_tool(tool, await tool.definition(ctx)) for name, tool in self.tools.items()}

    async def get_tool_for_tool_def(self, tool_def: ToolDefinition, ctx: RunContext[Any]) -> ToolsetTool[Any]:
        """Rebuild from the definition the get_tools step recorded: no I/O."""
        tool = self.tools[tool_def.name]  # KeyError: not ours
        return self._toolset_tool(tool, tool_def)

    async def call_tool(
        self, name: str, tool_args: dict[str, Any], ctx: RunContext[Any], tool: ToolsetTool[Any]
    ) -> Any:
        return await self.tools[name].call(tool_args, ctx, tool.tool_def)


def platform_toolset_for_run(
    deps: Any,
    *,
    knowledge: Callable[[], KnowledgeStore],
    memory: Callable[[], MemoryStore] | None = None,
) -> PlatformToolset | None:
    """The run's platform toolset, or None when the spec uses no platform tool (then the run's
    toolset is exactly what it was before platform tools existed). No I/O here: it is built inside
    every step that evaluates the toolset factory. `knowledge` / `memory` return the stores (called
    only inside a step).

    `update_working_memory` (RM.12, eap_worker.memory) is added only when the deps carry a memory
    turn's context (`MemoryWorkerDeps.memory`, from the recorded `eap.load_memory` output) with
    writable working memory and a scope key: never for a spec without memory, a standalone run, a
    `read_only` config, or a resource-scoped note of a conversation without a resource id."""
    from eap_worker.memory import working_memory_tool  # eap_worker.memory imports this module

    spec: AgentSpec = deps.spec
    tools: list[PlatformTool] = []
    if spec.knowledge_bases:
        tools.append(RetrieveTool(spec.knowledge_bases, knowledge))
    wm_tool = working_memory_tool(deps, memory)
    if wm_tool is not None:
        tools.append(wm_tool)
    return PlatformToolset(tools) if tools else None


def check_no_hidden_tools(spec: AgentSpec) -> None:
    """Hidden platform tools (knowledge.search) are for tool runs only: an agent spec that lists one
    fails like a spec naming an unknown tool (UnknownToolError, pickle-safe)."""
    for tool_id in spec.tools:
        if tool_id in HIDDEN_TOOL_IDS:
            raise UnknownToolError(tool_id)


# ------------------------------------------------------------------------------- search core


@dataclass(frozen=True)
class SearchTarget:
    """One knowledge base to search with its spec settings (or the tester's)."""

    kb_id: str
    top_k: int
    min_score: float | None = None
    filter: Mapping[str, Any] | None = None


def _filter_key(value: Mapping[str, Any] | None) -> str:
    return json.dumps(
        {k: list(v) if isinstance(v, (list, tuple)) else v for k, v in (value or {}).items()},
        sort_keys=True, separators=(",", ":"),
    )


def plan_searches(
    targets: Sequence[SearchTarget], models: Mapping[str, str]
) -> list[tuple[str, list[tuple[SearchTarget, list[str]]]]]:
    """[(embedding_model, [(settings of the group, [kb ids])...])...]: one entry per embedding model
    (sorted by model id), and per model one search per distinct (filter, top_k, min_score). Targets
    whose knowledge base has no model in `models` (deleted) are left out. Deterministic."""
    by_model: dict[str, dict[tuple[str, int, float | None], tuple[SearchTarget, list[str]]]] = {}
    for t in targets:
        model = models.get(t.kb_id)
        if model is None:
            continue
        groups = by_model.setdefault(model, {})
        key = (_filter_key(t.filter), t.top_k, t.min_score)
        if key not in groups:
            groups[key] = (t, [])
        if t.kb_id not in groups[key][1]:
            groups[key][1].append(t.kb_id)
    return [(model, list(by_model[model].values())) for model in sorted(by_model)]


def embedding_request_key(tool_call_id: str, model_id: str | None = None) -> str:
    """`embed:retrieve:{tool_call_id}` for a call that searches one embedding model (`model_id`
    None), else `embed:retrieve:{tool_call_id}:{model_id}`: stable per model, whatever other models
    the call searches or in which order."""
    base = f"embed:retrieve:{tool_call_id}"
    return base if model_id is None else f"{base}:{model_id}"


async def _embed_query_retrying(
    settings: Any, *, tenant_id: str, run_id: str, request_key: str, model_id: str, text: str
) -> EmbeddingCall:
    """embed_query with up to EMBED_ATTEMPTS attempts for retryable errors (the tool step itself is
    not retried by DBOS; the search is read-only, so another attempt is harmless)."""
    for attempt in range(EMBED_ATTEMPTS):
        try:
            return await embed_query(
                settings, tenant_id=tenant_id, run_id=run_id, request_key=request_key, model_id=model_id, text=text
            )
        except Exception as exc:  # noqa: BLE001 - re-raised unless retryable
            if attempt + 1 >= EMBED_ATTEMPTS or not is_retryable(exc):
                raise
            await asyncio.sleep(EMBED_BACKOFF_S * (2**attempt))
    raise AssertionError("unreachable")  # pragma: no cover


async def search_knowledge(
    *,
    settings: Any,
    store: KnowledgeStore,
    event_log: Any,
    tenant_id: str,
    run_id: str,
    tool_call_id: str,
    query: str,
    targets: Sequence[SearchTarget],
    models: Mapping[str, str],
) -> list[SearchHit]:
    """Embed `query` once per embedding model, record each call's usage with its `embedding` event
    (exactly once per request key), search every group, merge by score and cut at the largest top_k
    of the searched targets. `models` maps the live knowledge bases' ids to their embedding model
    (from the tenant's rows: the tenant comes from the caller's trusted deps)."""
    plan = plan_searches(targets, models)
    hits: dict[str, SearchHit] = {}
    limit = 0
    several = len(plan) > 1
    for model, groups in plan:
        call = await _embed_query_retrying(
            settings, tenant_id=tenant_id, run_id=run_id,
            request_key=embedding_request_key(tool_call_id, model if several else None),
            model_id=model, text=query,
        )
        await asyncio.to_thread(record_embedding, event_log, tenant_id, run_id, call)
        for settings_of, kb_ids in groups:
            limit = max(limit, settings_of.top_k)
            found = await asyncio.to_thread(
                store.search, tenant_id, kb_ids, list(call.vector), embedding_model=model,
                top_k=settings_of.top_k, min_score=settings_of.min_score,
                metadata_filter=dict(settings_of.filter) if settings_of.filter else None,
            )
            for hit in found:
                if hit.chunk_id not in hits or hits[hit.chunk_id].score < hit.score:
                    hits[hit.chunk_id] = hit
    ordered = sorted(hits.values(), key=lambda h: (-h.score, h.chunk_id))
    return ordered[:limit]


# ------------------------------------------------------------------------------- formatting


def _one_line(text: str | None, limit: int) -> str:
    return " ".join((text or "").split())[:limit]


def source_title(hit: SearchHit) -> str:
    return _one_line(hit.title or hit.document_title or hit.document_key, MAX_SOURCE_TITLE_CHARS) or "Untitled"


def source_ref(hit: SearchHit) -> str:
    """The citation target: the section's anchor URL, else `kb:{document_key}`."""
    return hit.anchor_url or f"kb:{hit.document_key}"


def _cut_utf8(text: str, max_bytes: int) -> str:
    if max_bytes <= 0:
        return ""
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text
    return data[:max_bytes].decode("utf-8", errors="ignore")


def quote_section(content: str) -> str:
    """Section text with every line prefixed `> `, so no line of retrieved (tenant-uploaded) text can
    start like a source header `[n] ... — url` and forge a citation. The structured sources stay
    authoritative: clients link `[n]` from `sources[n]`, never from a URL in the model's text.

    Lines are split on every line boundary str.splitlines knows (\\r\\n, \\n, \\r, \\v, \\f,
    \\x1c-\\x1e, \\x85, U+2028, U+2029), not only \\n, so no renderer or model can see a line that
    starts unquoted; the output uses \\n only."""
    return "\n".join("> " + line for line in _LINE_BREAKS.split(content))


# str.splitlines' boundaries, as a split (a trailing break still yields a final empty line).
_LINE_BREAKS = re.compile("\r\n|[\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029]")


def source_record(n: int, hit: SearchHit) -> dict[str, Any]:
    """The structured source of `[n]` (tool_result event `sources`; no section text)."""
    return {
        "n": n,
        "title": source_title(hit),
        "url": hit.anchor_url,
        "ref": source_ref(hit),
        "kb_id": hit.kb_id,
        "document_id": hit.document_id,
        "document_key": hit.document_key,
        "chunk_id": hit.chunk_id,
        "doc_version": hit.doc_version,
        "score": round(hit.score, 6),
    }


def format_results(hits: Sequence[SearchHit], max_bytes: int = MAX_RETRIEVE_RESULT_BYTES) -> tuple[str, list[dict[str, Any]]]:
    """(model-visible text, structured sources). At most `max_bytes` UTF-8 bytes in total: numbered
    sources in score order, each `[n] {title} — {ref}` and its section text quoted line by line
    with `> ` (quote_section: retrieved text cannot forge a source header), then the fixed
    instruction. A source that does not fit whole is cut (with `…`) when there is useful room, and
    nothing after it is included. No hits: NO_RESULTS_TEXT and no sources."""
    if not hits:
        return NO_RESULTS_TEXT, []
    suffix = "\n\n" + RETRIEVE_INSTRUCTION
    budget = max_bytes - len(suffix.encode("utf-8"))
    blocks: list[str] = []
    sources: list[dict[str, Any]] = []
    used = 0
    for hit in hits:
        n = len(blocks) + 1
        head = f"[{n}] {source_title(hit)} — {source_ref(hit)}\n"
        sep = 2 if blocks else 0
        body = quote_section(hit.content)
        block = head + body
        size = len(block.encode("utf-8")) + sep
        if used + size <= budget:
            blocks.append(block)
            sources.append(source_record(n, hit))
            used += size
            continue
        room = budget - used - sep - len(head.encode("utf-8")) - len("…".encode("utf-8"))
        if room >= 200 or (not blocks and room > 0):
            blocks.append(head + _cut_utf8(body, room) + "…")
            sources.append(source_record(n, hit))
        break
    if not blocks:  # pragma: no cover - only with an absurdly small max_bytes
        return NO_RESULTS_TEXT, []
    text = "\n\n".join(blocks) + suffix
    assert len(text.encode("utf-8")) <= max_bytes
    return text, sources


# ------------------------------------------------------------------------------- retrieve


_RETRIEVE_ARGS = SchemaValidator(
    core_schema.typed_dict_schema(
        {
            "search_query": core_schema.typed_dict_field(core_schema.str_schema()),
            "knowledge_base": core_schema.typed_dict_field(
                core_schema.nullable_schema(core_schema.str_schema()), required=False
            ),
        },
        extra_behavior="forbid",
    )
)


def _failure_message(exc: BaseException) -> tuple[str, str]:
    """(log code, model-visible message) for a failed search. Never exception text."""
    if isinstance(exc, EmbeddingModelNotAllowed):
        return "embedding_model_not_allowed", EMBEDDING_NOT_ALLOWED
    if isinstance(exc, EmbeddingDimensionMismatch):
        return "embedding_dimension_mismatch", RETRIEVAL_FAILED
    code = getattr(exc, "code", None)
    return (code if isinstance(code, str) else "retrieval_failed"), RETRIEVAL_FAILED


class RetrieveTool(PlatformTool):
    """`retrieve(search_query, knowledge_base=None)` over the spec's knowledge bases."""

    name = RETRIEVE_TOOL_NAME
    max_retries = RETRIEVE_MAX_ARG_RETRIES
    args_validator = _RETRIEVE_ARGS

    def __init__(self, refs: Sequence[Any], knowledge: Callable[[], KnowledgeStore]) -> None:
        self.refs = tuple(refs)  # KnowledgeBaseRef, in spec order
        self._knowledge = knowledge

    async def definition(self, ctx: RunContext[Any]) -> ToolDefinition:
        store = self._knowledge()
        kbs = await asyncio.to_thread(store.get_knowledge_bases, ctx.deps.tenant_id, [r.id for r in self.refs])
        return retrieve_definition(kbs)

    @staticmethod
    def argument_problem(args: Mapping[str, Any], tool_def: ToolDefinition) -> str | None:
        """A model-visible message for arguments the schema accepts but the tool cannot use, else
        None. Checked inside the call (not as an args_validator_func, which DBOSDurability runs as an
        extra `validate_args` step: retrieval must add no step), answered as a final tool error
        before anything is embedded or searched; the model may simply call again."""
        query = args.get("search_query") or ""
        if not query.strip():
            return "search_query must not be empty."
        if len(query) > MAX_SEARCH_QUERY_CHARS:
            return f"search_query must be at most {MAX_SEARCH_QUERY_CHARS} characters."
        name = args.get("knowledge_base")
        names = list(((tool_def.metadata or {}).get("knowledge_bases") or {}).keys())
        if name is not None and name not in names:
            choices = ", ".join(repr(n) for n in names) or "none"
            return f"knowledge_base must be one of {choices}, or omitted to search all of them."
        return None

    async def call(self, args: dict[str, Any], ctx: RunContext[Any], tool_def: ToolDefinition) -> Any:
        from eap_worker.runtime import get_runtime  # runtime imports this module (registration)

        if ctx.tool_call_id is None:
            raise ValueError("tool_call_id is required for retrieve")
        problem = self.argument_problem(args, tool_def)
        if problem is not None:
            raise ToolFailed(problem)
        deps = ctx.deps
        tenant_id: str = deps.tenant_id  # trusted: never a model argument
        run_id = str(deps.run_id)
        query: str = args["search_query"]
        name: str | None = args.get("knowledge_base")
        refs = self.refs
        if name is not None:
            kb_id = ((tool_def.metadata or {}).get("knowledge_bases") or {}).get(name)
            refs = tuple(r for r in self.refs if r.id == kb_id)
            if not refs:  # pragma: no cover - argument_problem refuses names this run did not offer
                raise ToolFailed(KB_GONE_ONE.format(name=_one_line(name, 100)))
        rt = get_runtime()
        store = self._knowledge()
        live = await asyncio.to_thread(store.get_knowledge_bases, tenant_id, [r.id for r in refs])
        models = {kb["id"]: kb["embedding_model"] for kb in live}
        if not models:
            raise ToolFailed(KB_GONE_ONE.format(name=_one_line(name, 100)) if name is not None else KB_GONE_ALL)
        targets = [SearchTarget(r.id, r.top_k, r.min_score, r.filter) for r in refs if r.id in models]
        try:
            hits = await search_knowledge(
                settings=rt.settings, store=store, event_log=rt.event_log, tenant_id=tenant_id, run_id=run_id,
                tool_call_id=ctx.tool_call_id, query=query, targets=targets, models=models,
            )
        except Exception as exc:  # noqa: BLE001 - a model-visible error, never exception text
            code, message = _failure_message(exc)
            log_failure(log, "retrieve failed", exc, code=code, run_id=run_id, tenant_id=tenant_id)
            raise ToolFailed(message) from None
        text, sources = format_results(hits)
        # Crash-injection tests: the query was embedded (its usage recorded) and searched, and the
        # call step's result is not yet checkpointed; recovery re-executes the step.
        crash_point("after_tool_call")
        log.info(
            "retrieve run=%s tool_call=%s knowledge_bases=%d hits=%d sources=%d bytes=%d",
            run_id, ctx.tool_call_id, len(targets), len(hits), len(sources), len(text.encode("utf-8")),
        )
        return ToolReturn(return_value=text, metadata={"sources": sources})


def retrieve_definition(kbs: Sequence[Mapping[str, Any]]) -> ToolDefinition:
    """The model-visible definition for the tenant's live knowledge bases among the spec's (in spec
    order). With none left the tool is still offered without a `knowledge_base` parameter: its call
    then answers the clear "no longer exist" error."""
    names = [str(kb["name"]) for kb in kbs]
    lines = [
        f"- {_one_line(kb['name'], 100)}: {_one_line(kb.get('description'), MAX_DESCRIPTION_CHARS) or 'no description'}"
        for kb in kbs
    ]
    description = (
        "Search the knowledge bases attached to this agent and return the most relevant sections as "
        "numbered sources [n], each with its title and URL. Use it before answering questions that "
        "these knowledge bases cover, and cite the sources you use."
    )
    description += "\n\nKnowledge bases:\n" + "\n".join(lines) if lines else "\n\nNone of the attached knowledge bases exists any more."
    properties: dict[str, Any] = {
        "search_query": {
            "type": "string",
            "description": "What to search for, as a natural-language query.",
        }
    }
    if names:
        properties["knowledge_base"] = {
            "anyOf": [{"type": "string", "enum": names}, {"type": "null"}],
            "default": None,
            "description": "Search only this knowledge base. Omit to search all of them.",
        }
    return ToolDefinition(
        name=RETRIEVE_TOOL_NAME,
        description=description,
        parameters_json_schema={
            "type": "object",
            "properties": properties,
            "required": ["search_query"],
            "additionalProperties": False,
        },
        metadata={"knowledge_bases": {str(kb["name"]): str(kb["id"]) for kb in kbs}},
    )


# ------------------------------------------------------------------------------- knowledge.search


async def knowledge_search(
    ctx: RunContext[RunDeps],
    knowledge_base_id: str,
    query: Annotated[str, Field(min_length=1, max_length=MAX_SEARCH_QUERY_CHARS)],
    top_k: Annotated[int, Field(ge=1, le=20)] = 8,
    min_score: Annotated[float | None, Field(ge=0.0, le=1.0)] = None,
    filter: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The hidden `knowledge.search` tool (tool runs only): the retrieve search over one knowledge
    base of the run's tenant, with the tester's settings. Its embedding usage is recorded on the
    tool run (request key `embed:retrieve:{tool_call_id}`). Raises KnowledgeBaseNotFound when the
    knowledge base is not the tenant's (the run fails with that code)."""
    from eap_worker.runtime import get_runtime

    rt = get_runtime()
    tenant_id = ctx.deps.tenant_id
    run_id = str(ctx.deps.run_id)
    store = KnowledgeStore(rt.engine)
    live = await asyncio.to_thread(store.get_knowledge_bases, tenant_id, [knowledge_base_id])
    if not live:
        raise KnowledgeBaseNotFound(knowledge_base_id)
    kb = live[0]
    hits = await search_knowledge(
        settings=rt.settings, store=store, event_log=rt.event_log, tenant_id=tenant_id, run_id=run_id,
        tool_call_id=ctx.tool_call_id or "tool_run", query=query,
        targets=[SearchTarget(kb["id"], top_k, min_score, filter)], models={kb["id"]: kb["embedding_model"]},
    )
    text, sources = format_results(hits)
    log.info("knowledge.search run=%s hits=%d", run_id, len(hits))
    return {
        "knowledge_base_id": kb["id"],
        "embedding_model": kb["embedding_model"],
        "hits": [
            {
                **source_record(n, hit),
                "document_title": hit.document_title,
                "heading_path": list(hit.heading_path),
                "metadata": hit.metadata,
                "content": hit.content[:MAX_SEARCH_HIT_CONTENT_CHARS],
                "content_truncated": len(hit.content) > MAX_SEARCH_HIT_CONTENT_CHARS,
            }
            for n, hit in enumerate(hits, start=1)
        ],
        "text": text,  # exactly what `retrieve` would show the model for these hits
    }


def register_platform_tools(registry: ToolRegistry) -> None:
    """Register the hidden platform tools (tool runs only) in the worker's registry."""
    registry.register(KNOWLEDGE_SEARCH_DECLARATION, knowledge_search)


__all__ = [
    "HIDDEN_TOOL_IDS",
    "KB_GONE_ALL",
    "KB_GONE_ONE",
    "NO_RESULTS_TEXT",
    "RETRIEVE_INSTRUCTION",
    "RETRIEVAL_FAILED",
    "PlatformTool",
    "PlatformToolset",
    "RetrieveTool",
    "SearchTarget",
    "check_no_hidden_tools",
    "embedding_request_key",
    "format_results",
    "knowledge_search",
    "plan_searches",
    "platform_toolset_for_run",
    "register_platform_tools",
    "retrieve_definition",
    "search_knowledge",
]
