"""Shared Pydantic schemas for the Enterprise Agent Platform."""

from datetime import datetime, timezone
from typing import Any, Literal, Tuple
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt, field_validator, model_serializer
from pydantic.functional_validators import model_validator

from eap_schemas.approvals import ApprovalPolicy
from eap_schemas.embedding_policy import (
    EmbeddingDimensionMismatch,
    EmbeddingModel,
    EmbeddingModelNotAllowed,
    EmbeddingModelPolicy,
)
from eap_schemas.knowledge import (
    MAX_KNOWLEDGE_BASES_PER_AGENT,
    ChunkingConfig,
    IngestBudgetExceeded,
    KnowledgeBaseNotFound,
    KnowledgeBaseRef,
    approx_tokens,
    check_knowledge_base_refs,
)
from eap_schemas.json_schema_subset import SchemaSubsetInvalid
from eap_schemas.limits import MAX_REQUESTS, RunBudgetExceeded, UsageLimitsSpec
from eap_schemas.mcp import McpRuntimeKind, McpServerSpec, McpToolRef, mcp_tool_name
from eap_schemas.mcp_catalog import CatalogEntry, CatalogPackage, EnvVarSpec, RuntimeState
from eap_schemas.memory import (
    HistoryConfig,
    MemoryConfig,
    ObservationalMemoryConfig,
    SemanticRecallConfig,
    WorkingMemoryConfig,
    WorkingMemoryInvalid,
    merge_working_memory,
    validate_working_memory_content,
    validate_working_memory_schema,
)
from eap_schemas.model_ref import ModelRef
from eap_schemas.output import OUTPUT_TOOL_NAME, OutputInvalid, OutputSpec
from eap_schemas.processors import (
    MAX_PROCESSORS_PER_PHASE,
    PROCESSOR_DECLARATIONS,
    PROCESSOR_PHASES,
    ModerationVerdict,
    ProcessorDeclaration,
    ProcessorSpec,
)
from eap_schemas.request_context import MAX_CONTEXT_FIELDS, ContextField
from eap_schemas.workflow_scope import (
    LOOP_ERROR_CODES,
    LOOP_ERROR_MESSAGES,
    LOOP_WORKFLOW_FAILURES,
    Scope,
    ScopeFrame,
    ScopeInvalid,
    instance_token,
    iteration_event_key,
    iteration_workflow_id,
    scoped_judge_key,
    scope_string,
    scoped_child_run_id,
    scoped_event_key,
    scoped_guard_key,
    scoped_parent_step_id,
    scoped_tool_call_id,
    status_for_end,
)
from eap_schemas.tools import (
    RESERVED_TOOL_NAMES,
    RETRIEVE_TOOL_NAME,
    TOOL_DECLARATIONS,
    UPDATE_WORKING_MEMORY_TOOL_NAME,
    ToolDeclaration,
    builtin_tool_name,
)

__all__ = (
    "AgentSpec",
    "AgentVersion",
    "ApprovalPolicy",
    "MAX_FALLBACK_MODELS",
    "OUTPUT_TOOL_NAME",
    "OutputInvalid",
    "OutputSpec",
    "RunBudgetExceeded",
    "SchemaSubsetInvalid",
    "UsageLimitsSpec",
    "approval_required_tool_names",
    "ChunkingConfig",
    "ContextField",
    "EmbeddingDimensionMismatch",
    "EmbeddingModel",
    "EmbeddingModelNotAllowed",
    "EmbeddingModelPolicy",
    "HistoryConfig",
    "IngestBudgetExceeded",
    "KnowledgeBaseNotFound",
    "KnowledgeBaseRef",
    "MemoryConfig",
    "ObservationalMemoryConfig",
    "RESERVED_TOOL_NAMES",
    "SemanticRecallConfig",
    "WorkingMemoryConfig",
    "WorkingMemoryInvalid",
    "approx_tokens",
    "merge_working_memory",
    "reserved_tool_name_conflicts",
    "validate_working_memory_content",
    "validate_working_memory_schema",
    "McpToolRef",
    "ModelRef",
    "ModelSettings",
    "ModerationVerdict",
    "PROCESSOR_DECLARATIONS",
    "ProcessorDeclaration",
    "ProcessorSpec",
    "RunEvent",
    "RunEventType",
    "TOOL_DECLARATIONS",
    "ToolDeclaration",
    "canonical_spec_hash",
    "mcp_tool_name",
    "McpRuntimeKind",
    "McpServerSpec",
    "CatalogEntry",
    "CatalogPackage",
    "EnvVarSpec",
    "RuntimeState",
    # Workflow loops (eap_schemas.workflow_scope, LP.2)
    "LOOP_ERROR_CODES",
    "LOOP_ERROR_MESSAGES",
    "LOOP_WORKFLOW_FAILURES",
    "Scope",
    "ScopeFrame",
    "ScopeInvalid",
    "instance_token",
    "iteration_event_key",
    "iteration_workflow_id",
    "scoped_judge_key",
    "scope_string",
    "scoped_child_run_id",
    "scoped_event_key",
    "scoped_guard_key",
    "scoped_parent_step_id",
    "scoped_tool_call_id",
    "status_for_end",
)


MAX_FALLBACK_MODELS = 3


MAX_MODEL_RETRIES = 10
MODEL_SETTINGS_OMIT_WHEN_NONE = (
    "top_p",
    "top_k",
    "frequency_penalty",
    "presence_penalty",
    "seed",
    "max_steps",
    "max_retries",
)


class ModelSettings(BaseModel):
    """Settings for model inference."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, gt=0)
    # Added after specs were first stored: an unset one is left out of every dump (as `mcp_tools` is
    # on AgentSpec), so a spec without them keeps its exact JSON and canonical_spec_hash.
    # `temperature` and `max_tokens` stay in the dump as null, as they always were.
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    top_k: int | None = Field(default=None, ge=1)
    frequency_penalty: float | None = Field(default=None, ge=-2.0, le=2.0)
    presence_penalty: float | None = Field(default=None, ge=-2.0, le=2.0)
    seed: StrictInt | None = Field(default=None, ge=-(2**63), le=2**63 - 1)
    # Caps the agent loop (model requests of one run; Pydantic AI `request_limit`) and sets the
    # agent's tool and output retry budgets (`retries`).
    max_steps: int | None = Field(default=None, ge=1, le=MAX_REQUESTS)
    max_retries: int | None = Field(default=None, ge=0, le=MAX_MODEL_RETRIES)

    @model_serializer(mode="wrap")
    def _omit_unset_new_fields(self, handler):
        data = handler(self)
        if isinstance(data, dict):
            for name in MODEL_SETTINGS_OMIT_WHEN_NONE:
                if getattr(self, name) is None:
                    data.pop(name, None)
        return data

    def set_values(self) -> dict[str, Any]:
        """The settings that are set, by name."""
        return {name: getattr(self, name) for name in type(self).model_fields if getattr(self, name) is not None}


class AgentSpec(BaseModel):
    """Specification for an agent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    spec_version: Literal[1] = 1
    name: str
    instructions: str = ""
    model: ModelRef
    tools: Tuple[str, ...] = ()
    model_settings: ModelSettings = Field(default_factory=ModelSettings)
    # MCP tools of the tenant's registered servers. Left out of every dump while empty, so specs
    # stored before this field existed keep their exact JSON and canonical_spec_hash.
    mcp_tools: Tuple[McpToolRef, ...] = ()
    # Guardrails (eap_schemas.processors) and typed request context (eap_schemas.request_context),
    # milestone P0. Same rule as mcp_tools: left out of every dump while empty, so the JSON and
    # canonical_spec_hash of every spec stored before they existed stay exactly the same.
    input_processors: Tuple[ProcessorSpec, ...] = Field(default=(), max_length=MAX_PROCESSORS_PER_PHASE)
    output_processors: Tuple[ProcessorSpec, ...] = Field(default=(), max_length=MAX_PROCESSORS_PER_PHASE)
    # Step phase (tool-call arguments and results) and stream phase (live deltas, and implicitly the
    # final answer). Same rule: omitted from dumps while empty.
    step_processors: Tuple[ProcessorSpec, ...] = Field(default=(), max_length=MAX_PROCESSORS_PER_PHASE)
    stream_processors: Tuple[ProcessorSpec, ...] = Field(default=(), max_length=MAX_PROCESSORS_PER_PHASE)
    context_fields: Tuple[ContextField, ...] = Field(default=(), max_length=MAX_CONTEXT_FIELDS)
    # Knowledge and memory (RAG and memory milestone, RM.2). Same rule: left out of every dump while
    # empty (no knowledge bases; memory None), so every stored spec keeps its JSON and hash. Inside
    # KnowledgeBaseRef and MemoryConfig every None field is dropped too. `memory: {}` is NOT empty:
    # it turns memory on with the default history limits.
    knowledge_bases: Tuple[KnowledgeBaseRef, ...] = Field(default=(), max_length=MAX_KNOWLEDGE_BASES_PER_AGENT)
    memory: MemoryConfig | None = None
    # Agent capabilities milestone (AC.1): structured output, per-tool approval, usage limits and
    # fallback models. Same rule: left out of every dump while empty (None, or no fallbacks), and
    # every None field inside the sub-configs is dropped too, so every stored spec keeps its JSON
    # and hash. A `limits` object with every cap None is turned into None.
    output: OutputSpec | None = None
    approvals: ApprovalPolicy | None = None
    limits: UsageLimitsSpec | None = None
    fallback_models: Tuple[ModelRef, ...] = Field(default=(), max_length=MAX_FALLBACK_MODELS)

    @model_serializer(mode="wrap")
    def _omit_empty_optional_fields(self, handler):
        data = handler(self)
        if isinstance(data, dict):
            for name in _OMIT_WHEN_EMPTY:
                if not getattr(self, name):
                    data.pop(name, None)
        return data

    @model_validator(mode="after")
    def check_processor_phases(self):
        """A processor type runs only in the phases its declaration lists (moderation not in step or stream)."""
        for phase in PROCESSOR_PHASES:
            for processor in getattr(self, f"{phase}_processors"):
                if phase not in PROCESSOR_DECLARATIONS[processor.type].phases:
                    raise ValueError(f"processor_phase_unsupported: {processor.type} cannot run in the {phase} phase")
        return self

    @model_validator(mode="after")
    def check_context_fields(self):
        """Context field names are unique."""
        seen: set[str] = set()
        for field in self.context_fields:
            if field.name in seen:
                raise ValueError(f"duplicate context field: {field.name}")
            seen.add(field.name)
        return self

    @model_validator(mode="after")
    def check_knowledge_and_memory(self):
        """Unique knowledge bases; no tool takes the name of a platform tool the spec turns on:
        `retrieve` with knowledge bases, `update_working_memory` with memory, `final_result` with
        `output`.

        Reserved per feature, so no stored spec can stop validating. Publish (RM.11) checks
        `reserved_tool_name_conflicts` (all three names) for every spec.
        """
        check_knowledge_base_refs(self.knowledge_bases)
        active = set()
        if self.knowledge_bases:
            active.add(RETRIEVE_TOOL_NAME)
        if self.memory is not None:
            active.add(UPDATE_WORKING_MEMORY_TOOL_NAME)
        if self.output is not None:
            active.add(OUTPUT_TOOL_NAME)
        conflicts = [name for name in reserved_tool_name_conflicts(self) if name in active]
        if conflicts:
            raise ValueError(f"tool name {conflicts[0]!r} is reserved for a platform tool")
        return self

    @field_validator("limits")
    @classmethod
    def drop_empty_limits(cls, v):
        """`limits: {}` (every cap None) is the same as no limits."""
        return None if v is not None and v.is_empty() else v

    @model_validator(mode="after")
    def check_fallback_models(self):
        """Fallback models are distinct and none is the primary model."""
        seen: set[tuple[str, str]] = {(self.model.provider, self.model.name)}
        for i, ref in enumerate(self.fallback_models):
            key = (ref.provider, ref.name)
            if key == (self.model.provider, self.model.name):
                raise ValueError(
                    f"fallback_models[{i}]: fallback model {ref.provider}:{ref.name} is the agent's primary model"
                )
            if key in seen:
                raise ValueError(f"fallback_models[{i}]: duplicate fallback model: {ref.provider}:{ref.name}")
            seen.add(key)
        return self

    @model_validator(mode="after")
    def check_approvals(self):
        """Every approval entry names a built-in tool id of `tools` or `"<server>/<tool>"` of
        `mcp_tools`; platform tools and the output tool can never require approval."""
        if self.approvals is None:
            return self
        known = set(self.tools) | {f"{ref.server}/{ref.tool}" for ref in self.mcp_tools}
        for i, entry in enumerate(self.approvals.tools):
            if entry in RESERVED_TOOL_NAMES or builtin_tool_name(entry) in RESERVED_TOOL_NAMES:
                raise ValueError(f"approvals.tools[{i}]: {entry!r} is a platform tool and cannot require approval")
            if entry not in known:
                raise ValueError(f"approvals.tools[{i}]: {entry!r} is not a tool of this agent")
        return self

    @model_validator(mode="after")
    def check_tool_names(self):
        """MCP tool pairs are unique and no two tools share a model-facing name."""
        if not self.mcp_tools:
            return self
        names = {tool_id.replace(".", "__"): tool_id for tool_id in self.tools}
        seen: set[tuple[str, str]] = set()
        for ref in self.mcp_tools:
            if (ref.server, ref.tool) in seen:
                raise ValueError(f"duplicate MCP tool: {ref.server}/{ref.tool}")
            seen.add((ref.server, ref.tool))
            name = mcp_tool_name(ref.server, ref.tool)
            if name in names:
                raise ValueError(f"MCP tool {ref.server}/{ref.tool} has the same tool name as {names[name]}: {name}")
            names[name] = f"{ref.server}/{ref.tool}"
        return self

    @field_validator("name", mode="before")
    @classmethod
    def strip_name(cls, v):
        """Strip whitespace from name."""
        if isinstance(v, str):
            return v.strip()
        return v

    @field_validator("name")
    @classmethod
    def validate_name(cls, v):
        """Validate name after stripping."""
        if not v:
            raise ValueError("name cannot be empty")
        if len(v) > 100:
            raise ValueError("name must be at most 100 characters")
        return v

    @field_validator("instructions")
    @classmethod
    def validate_instructions(cls, v):
        """Keep workflow instructions within the durable payload budget."""
        if len(v.encode("utf-8")) > 32 * 1024:
            raise ValueError("instructions must be at most 32 KiB in UTF-8")
        return v

    @field_validator("tools", mode="before")
    @classmethod
    def validate_tools(cls, v):
        """Validate tool IDs and check for duplicates using exact regex."""
        import re

        if not v:
            return ()
        # A "before" validator sees raw input: anything but a list of strings is a ValueError (a 422
        # at the API), never a TypeError from the regex (a 500). A bare string is not split into ids.
        if isinstance(v, (str, bytes)) or not isinstance(v, (list, tuple)):
            raise ValueError("tools must be a list of tool ids")

        tool_ids = list(v)
        seen = set()
        pattern = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")

        for tool_id in tool_ids:
            if not isinstance(tool_id, str):
                raise ValueError(f"tool id must be a string, not {type(tool_id).__name__}")
            if not tool_id:
                raise ValueError("tool id cannot be empty")
            if not pattern.fullmatch(tool_id):
                raise ValueError(f"tool id must match ^[a-z][a-z0-9_.-]{{0,63}}$ (ASCII only, max 64 chars): {tool_id}")
            if tool_id in seen:
                raise ValueError(f"duplicate tool id: {tool_id}")
            seen.add(tool_id)

        return tuple(tool_ids)


# AgentSpec fields added after specs were first stored: omitted from dumps (and so from the hash)
# while empty.
_OMIT_WHEN_EMPTY = (
    "mcp_tools",
    "input_processors",
    "output_processors",
    "step_processors",
    "stream_processors",
    "context_fields",
    "knowledge_bases",
    "memory",
    "output",
    "approvals",
    "limits",
    "fallback_models",
)


def approval_required_tool_names(spec: AgentSpec) -> frozenset[str]:
    """Model-facing names of the spec's tools that need approval (built-in ids with dots as `__`,
    MCP tools as `mcp_tool_name`). Empty without `approvals`."""
    if spec.approvals is None:
        return frozenset()
    gated = set(spec.approvals.tools)
    names = {builtin_tool_name(tool_id) for tool_id in spec.tools if tool_id in gated}
    names |= {mcp_tool_name(ref.server, ref.tool) for ref in spec.mcp_tools if f"{ref.server}/{ref.tool}" in gated}
    return frozenset(names)


def reserved_tool_name_conflicts(spec: AgentSpec) -> tuple[str, ...]:
    """Model-facing names of the spec's built-in and MCP tools that are reserved platform tool
    names (`retrieve`, `update_working_memory`, `final_result`), in spec order."""
    names = [builtin_tool_name(tool_id) for tool_id in spec.tools]
    names += [mcp_tool_name(ref.server, ref.tool) for ref in spec.mcp_tools]
    return tuple(name for name in names if name in RESERVED_TOOL_NAMES)


def canonical_spec_hash(spec: AgentSpec) -> str:
    """Compute SHA256 hash of AgentSpec JSON with sorted keys.

    Equal specs give equal hashes; any field change changes the hash.
    """
    import hashlib
    import json

    json_str = json.dumps(
        spec.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(json_str.encode()).hexdigest()


class AgentVersion(BaseModel):
    """Versioned agent specification."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: UUID
    tenant_id: str
    version: int = Field(ge=1)
    spec: AgentSpec
    spec_hash: str
    created_at: AwareDatetime

    @field_validator("agent_id", mode="before")
    @classmethod
    def validate_agent_id(cls, v):
        """Convert to UUID if needed."""
        if isinstance(v, str):
            from uuid import UUID as UUIDType
            return UUIDType(v)
        return v

    @field_validator("tenant_id", mode="before")
    @classmethod
    def validate_tenant_id(cls, v):
        """Validate tenant_id is non-empty string."""
        if isinstance(v, str) and not v:
            raise ValueError("tenant_id cannot be empty")
        return v

    @field_validator("created_at", mode="before")
    @classmethod
    def validate_created_at_datetime(cls, v):
        """Ensure created_at is timezone-aware datetime."""
        if isinstance(v, str):
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                raise ValueError("created_at must be a timezone-aware datetime")
            return dt
        if hasattr(v, "tzinfo") and v.tzinfo is None:
            raise ValueError("created_at must be a timezone-aware datetime")
        if not hasattr(v, "tzinfo"):
            raise ValueError("created_at must be a datetime object")
        return v

    @model_validator(mode="after")
    def verify_spec_hash(self):
        """Verify spec_hash matches canonical hash of spec."""
        expected_hash = canonical_spec_hash(self.spec)
        if self.spec_hash != expected_hash:
            raise ValueError(
                f"spec_hash mismatch: expected {expected_hash}, got {self.spec_hash}"
            )
        return self


RunEventType = Literal[
    "run_started",
    "text_delta",  # transient only: sent over NOTIFY, never stored
    "model_message",
    "tool_call",
    "tool_result",
    "run_completed",
    "run_failed",
    # Agent runs whose spec has processors (P0.17): one per processor that ran, keyed
    # `processor:{phase}:{i}`; type, action, match counts and reason codes, never matched text.
    "processor_result",
    # Workflow runs (eap.workflow_run): one node's lifecycle, keyed `step:{step_id}:<phase>`.
    "step_started",
    "step_waiting",  # an approval step is waiting for a decision
    "step_approved",  # written by the API (POST .../approve), key `step:{step_id}:decision`
    "step_rejected",  # written by the API (POST .../reject), key `step:{step_id}:decision`
    "step_timed_out",  # written by the worker when the approval wait timed out, same key
    "step_completed",
    "step_failed",
    # RAG and memory milestone. Data carries counts, ids, models and costs, never text.
    "ingest_progress",  # kb_ingest runs: {embedded, total} after each embedded batch
    "embedding",  # one per embedding call, key = request key: {model_id, inputs, input_tokens, cost_usd}
    "memory_observe_failed",  # the post-turn observer failed; the run status is unchanged
    "memory_updated",  # working memory or observations changed (Studio "memory updating")
    # Agent capabilities milestone. Data carries ids, names, paths and counts; never output values.
    "output_retry",  # key `output:{n}:retry`: {model_request, errors: [json paths]}
    "tool_approval_waiting",  # key `tool:{id}:approval:waiting`: {tool_call_id, tool_name, args, deadline, workflow_id}
    "tool_approved",  # written by the API, key `tool:{id}:approval:decision`: {tool_call_id, decided_by, comment}
    "tool_denied",  # API (or the worker in eval runs, decided_by "eval"), same key
    "tool_approval_timed_out",  # written by the worker on the deadline, same key (first writer wins)
    "eval_case_result",  # eval runs, key `case:{case_id}`: status, counts, cost; no text
    # The reflector's input was capped (EAP_MEMORY_MAX_ACTIVE_OBSERVATION_TOKENS): the oldest active
    # observations were left out of it. {dropped, kept_tokens, ...}: counts only, key `memory:reflect:{turn}:trimmed`.
    "memory_observe_trimmed",
    # The post-turn memory steps of a completed turn have reported, key `memory:post_turn:{turn}`:
    # {observe: created|skipped|failed|off, index: done|skipped|failed|off}. No text.
    "post_turn_done",
    # RAG P1: a worker-extracted document version was extracted and chunked (eap.kb_extract_chunk),
    # key `extract:{doc_version}`, data `eap_schemas.knowledge.ExtractedEventData`. No text.
    "extracted",
    # Workflow loops milestone (eap_schemas.workflow_scope). Data carries step and loop ids, the
    # scope `[{loop, index}]`, statuses and codes; never item or output values.
    "loop_iteration_started",  # key `loop:{S}:started`: {step_id, scope, index}
    "loop_iteration_completed",  # key `loop:{S}:completed`: {step_id, scope, index, ...}
    "loop_iteration_failed",  # key `loop:{S}:failed`: {step_id, scope, index, error_code, error}
    "cancel_requested",  # written by the API (POST /runs/{id}/cancel), key `cancel`: {requested_by}
    # Reusable scorers (W2.3): written by the worker after a run finished, key `score:{scorer_id}`:
    # {scorer, scorer_id, score, passed, reason}. Never output text.
    "score",
    # Message feedback (W2.4): legacy, no longer written. Feedback is per user, so it lives only in
    # `message_feedback` rows and is replayed to its own author; a shared run event would leak it.
    "message_feedback",
]


class RunEvent(BaseModel):
    """Event in a run execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    seq: int = Field(ge=0)
    type: RunEventType
    data: dict[str, Any] = Field(default_factory=dict)
    ts: AwareDatetime

    @field_validator("run_id", mode="before")
    @classmethod
    def validate_run_id(cls, v):
        """Convert to UUID if needed."""
        if isinstance(v, str):
            from uuid import UUID as UUIDType
            return UUIDType(v)
        return v

    @field_validator("ts", mode="before")
    @classmethod
    def validate_ts_datetime(cls, v):
        """Ensure ts is timezone-aware datetime."""
        if isinstance(v, str):
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                raise ValueError("ts must be a timezone-aware datetime")
            return dt
        if hasattr(v, "tzinfo") and v.tzinfo is None:
            raise ValueError("ts must be a timezone-aware datetime")
        if not hasattr(v, "tzinfo"):
            raise ValueError("ts must be a datetime object")
        return v


# Import after definitions to avoid circular issues
from datetime import datetime, timezone
