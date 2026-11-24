"""The Architect's restricted definition formats and the functions that compile them (Task 3).

The Architect (a built-in agent) and the Architect page describe agents and workflows in two small,
strict formats. Everything outside them takes the platform default. Each format is compiled by one
pure, deterministic function into the full `AgentSpec` or `WorkflowSpec`, which is then validated
by the ordinary validators; the compile step refuses anything the tenant's `Allowlist` did not offer.

Ids here are slugs (`^[a-z][a-z0-9-]{1,47}$`), never row ids: a slug is what the model and the page
see; the worker resolves it to the row. The Allowlist's `agents` entries carry the row id only in a
field that is excluded from every dump, so it can reach `compile_workflow` but never the model.

No I/O.
"""

from __future__ import annotations

from collections.abc import Iterable

import hashlib
import re
from typing import Annotated, Any, Literal, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from eap_schemas import AgentSpec, ApprovalPolicy, KnowledgeBaseRef, McpToolRef, MemoryConfig, ModelRef, ModelSettings
from eap_schemas.output import OutputSpec
from eap_schemas.tools import ARCHITECT_GATED_TOOL_IDS, ungated_always_gated_tools
from eap_schemas.workflows import MAX_STEPS, AgentStep, BranchStep, ParallelStep, ToolStep, WorkflowSpec

AGENT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,47}$")
STEP_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
RESERVED_STEP_IDS = frozenset({"run_end"})
# Slugs the Architect may never create or edit: the built-in specs.
RESERVED_AGENT_IDS = frozenset({"architect", "architect-drafter", "architect-workflow-drafter", "scorer-suggester"})
# The code denylist. A tool whose id starts with one of these is never offered to a created agent or
# a created workflow, whatever the tool registry holds: the Architect's own tools, the platform
# tools of the knowledge and memory features (they come with the spec's own fields), and anything
# that touches files, shells, processes or the host.
ARCHITECT_DENIED_TOOL_PREFIXES: tuple[str, ...] = (
    "architect.",
    "knowledge.",
    "kb.",
    "settings.",
    "workflow.",
    "knowledge_search",
    "retrieve",
    "update_working_memory",
    "final_result",
    "file.",
    "files.",
    "fs.",
    "shell.",
    "bash.",
    "exec.",
    "process.",
    "host.",
    "sandbox.",
    "platform.",
    "admin.",
)
# Registry tools offered to created agents but always behind a person's approval, besides every tool
# the registry marks as having a side effect and every MCP tool.
GATED_TOOL_IDS: frozenset[str] = frozenset({"tickets.create"})

# The Architect's tools that change what is live or what runs by itself (Task 4). A built-in agent that
# lists one must list it in `approvals.tools` too: the API refuses the spec (422 approval_required) and
# the worker refuses the run, so no path reaches them without a person's recorded approval.

# Every tool of the Architect agent (Tasks 3 and 4): the read tools, the two draft tools, then the gated
# ones. The seed lists all of them in the Architect's `tools`; the worker registers exactly these ids.
ARCHITECT_TOOL_IDS: tuple[str, ...] = (
    "architect.list_approved_tools",
    "architect.list_created_agents",
    "architect.list_agent_versions",
    "architect.list_created_workflows",
    "architect.list_workflow_versions",
    "architect.list_workflow_step_kinds",
    "architect.list_capabilities",
    "architect.get_capability",
    "architect.save_agent",
    "architect.save_workflow",
    "architect.list_eval_suites",
    "architect.get_eval_run",
    *ARCHITECT_GATED_TOOL_IDS,
)

# The Architect's read-only view of the tenant (not gated: nothing here writes). Like `architect.*` they belong
# to the built-in Architect only; `platform.` is also in ARCHITECT_DENIED_TOOL_PREFIXES, so a created agent is
# never offered one.
ARCHITECT_PLATFORM_TOOL_IDS: tuple[str, ...] = (
    "platform.list_knowledge_bases",
    "platform.get_knowledge_base",
    "platform.list_source_syncs",
    "platform.list_workflows",
    "platform.get_workflow",
    "platform.list_agents",
    "platform.list_schedules",
    "platform.list_recent_runs",
    "platform.get_run",
    "platform.list_runs",
    "platform.list_capabilities",
)
# The Architect's bridge to the MCP control plane: every control tool `a.b.c` is callable as
# `control__a_b_c` (a run's tool names are discovered at run time with `platform.list_capabilities`, never
# listed in the spec). The bridge belongs to the built-in Architect only.
CONTROL_TOOL_PREFIX = "control__"
CONTROL_LIST_TOOL_ID = "platform.list_capabilities"


def control_tool_name(control_name: str) -> str:
    """The Architect's tool name for a control-plane tool: `eval.suite.create` -> `control__eval_suite_create`."""
    return CONTROL_TOOL_PREFIX + control_name.replace(".", "_")
# Tool-id prefixes only a built-in agent row may list (the API refuses the spec, the worker the run).
BUILTIN_ONLY_TOOL_PREFIXES: tuple[str, ...] = ("architect.", "platform.")


def ungated_architect_tools(tools: Iterable[str], approval_tools: Iterable[str] | None) -> list[str]:
    """The gated Architect tools in `tools` that `approval_tools` (spec.approvals.tools) does not list."""
    return [t for t in ungated_always_gated_tools(tools, approval_tools) if t in ARCHITECT_GATED_TOOL_IDS]


MAX_AGENT_TOOLS = 32
END = "end"  # a step's `next` that ends the workflow
RETURN_TOOL_ID = "workflow.return"
MAX_BRANCHES = 4
_PLACEHOLDER = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")
_VALID_PLACEHOLDER = re.compile(r"^(?:input|previous|steps\.[a-z][a-z0-9_]{0,63})$")


# Ids a model invents when it has nothing to say; a spec that carries one gets an id from its name.
GENERIC_IDS = ("definition", "id", "agent", "workflow", "<id>", "spec", "draft", "new", "untitled", "test")


def slugify(name: str, kind: Literal["agent", "workflow"] = "agent") -> str:
    """A slug (`AGENT_ID_PATTERN`) from a name: lowercase words joined by dashes, at most 48 characters.
    A name with no Latin letters or digits (CJK, say) gets `<kind>-` and four hex digits of its hash, so
    the same name always maps to the same id. (No transliteration library is a dependency, so CJK is not romanised.)"""
    fallback = f"{kind}-{hashlib.sha1(name.strip().encode()).hexdigest()[:4]}"
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:48].strip("-")
    if not slug:
        return fallback
    if not slug[0].isalpha():
        slug = f"a-{slug}".strip("-")[:48].strip("-")
    return slug if AGENT_ID_PATTERN.fullmatch(slug) else fallback


def is_generic_id(value: str | None) -> bool:
    """True when `value` is missing or one of `GENERIC_IDS`: the id should come from the name."""
    return value is None or value.strip().lower() in GENERIC_IDS


# `workflow.` tools a created workflow's tool step may use. `workflow.return` only shapes a result;
# `workflow.history` reads the calling workflow's own runs, so a created agent is never granted it.
WORKFLOW_STEP_TOOL_IDS: tuple[str, ...] = ("workflow.return", "workflow.history")
WORKFLOW_ONLY_TOOL_IDS: tuple[str, ...] = ("workflow.history",)


def is_denied_tool(tool_id: str) -> bool:
    if tool_id in WORKFLOW_STEP_TOOL_IDS:
        return False
    return any(tool_id == p or tool_id.startswith(p) for p in ARCHITECT_DENIED_TOOL_PREFIXES)


APPROVAL_NOTE = (
    "Approvals gate agent-initiated calls only: an agent's call to this tool waits for a person; a workflow tool "
    "step runs as published, with no approval each run."
)


def offer_tool(
    tool_id: str, title: str, description: str, *, side_effect: bool = False, host_only: bool = False,
    input_schema: dict[str, Any] | None = None, example: dict[str, Any] | None = None,
    result_example: Any = None, result_schema: dict[str, Any] | None = None,
) -> "AllowlistTool | None":
    """The allowlist entry for one platform tool, or None when the code denylist or `host_only` keeps it
    from created agents. `requires_approval` is the tool's side effect or its place in GATED_TOOL_IDS.
    The one rule both the worker (its registry) and the API (the tool declarations) apply, so the two
    allowlists cannot drift apart."""
    if host_only or is_denied_tool(tool_id):
        return None
    return AllowlistTool(
        id=tool_id, title=title, description=description or title,
        requires_approval=bool(side_effect) or tool_id in GATED_TOOL_IDS,
        input_schema=input_schema, example=example, result_example=result_example, result_schema=result_schema,
    )


# ------------------------------------------------------------------------------------- allowlist


class AllowlistTool(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    title: str
    description: str
    # Kept for the Studio: true when an agent that is granted the tool waits for a person on each call.
    requires_approval: bool = False
    # Where that applies: `agent_runs` an agent's own call waits for approval; `workflow_steps` is always false,
    # because a workflow tool step runs as published (the person reviewed the workflow when publishing it).
    approval: dict[str, bool] = Field(default_factory=dict)
    approval_note: str = ""
    # The tool's argument schema and one short example call (arguments), so a model can call it without guessing.
    input_schema: dict[str, Any] | None = None
    example: dict[str, Any] | None = None
    # What the tool returns (a step's output is exactly this value): an example and, where cheap, a JSON Schema.
    result_example: Any = None
    result_schema: dict[str, Any] | None = None

    @model_validator(mode="before")
    @classmethod
    def _approval(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        agent_runs = bool(data.get("requires_approval", False))
        out = dict(data)
        out["approval"] = {"agent_runs": agent_runs, "workflow_steps": False}
        out["approval_note"] = APPROVAL_NOTE if agent_runs else ""
        return out


class AllowlistMcpServer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str  # the server name (what `McpToolRef.server` holds)
    name: str
    tools: tuple[str, ...] = ()


class AllowlistKnowledgeBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    name: str


class AllowlistModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str  # "provider:name"
    provider: str
    capabilities: dict[str, Any] = Field(default_factory=dict)


class AllowlistAgent(BaseModel):
    """An Architect-made agent a created workflow may name (draft or published; publishing the workflow needs a published version)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str  # slug
    name: str
    # The row id, for compile_workflow only. Excluded from every dump: it never reaches the model.
    row_id: str | None = Field(default=None, exclude=True)


class Allowlist(BaseModel):
    """What a created agent or workflow of this tenant may use. The single source for the read tool,
    the page's pickers and the compile functions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tools: tuple[AllowlistTool, ...] = ()
    mcp_servers: tuple[AllowlistMcpServer, ...] = ()
    knowledge_bases: tuple[AllowlistKnowledgeBase, ...] = ()
    models: tuple[AllowlistModel, ...] = ()
    agents: tuple[AllowlistAgent, ...] = ()
    default_model: str | None = None


# --------------------------------------------------------------------------------- agent format


class ArchitectModelSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, gt=0, le=200_000)


class ArchitectMcpTool(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    server: str = Field(min_length=1, max_length=100)
    tool: str = Field(min_length=1, max_length=128)


def as_uuid(value: str | None) -> str | None:
    """The canonical (lowercase, hyphenated) form of `value` when it is a UUID, else None."""
    if not isinstance(value, str):
        return None
    try:
        return str(UUID(value.strip()))
    except ValueError:
        return None


def _check_id(value: str | None) -> str | None:
    """A definition's `id`: a slug, or the UUID of an existing definition (a save then updates that one). A
    placeholder (`definition`, `id`, `agent`, `workflow`, `<id>`, ...) is no id: the slug comes from the name."""
    if value is None or is_generic_id(value):
        return None
    return as_uuid(value) or _check_slug(value)


def _check_slug(value: str | None, what: str = "id") -> str | None:
    if value is not None and not AGENT_ID_PATTERN.fullmatch(value):
        raise ValueError(f"{what} must match ^[a-z][a-z0-9-]{{1,47}}$")
    return value


class ArchitectAgentSpec(BaseModel):
    """The agent format the Architect may write. Strict: unknown fields are refused."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=80)
    id: str | None = None
    description: str = Field(default="", max_length=300)
    instructions: str = Field(min_length=1, max_length=8000)
    model: str | None = Field(default=None, max_length=257)  # "provider:name"
    tools: tuple[str, ...] = Field(default=(), max_length=MAX_AGENT_TOOLS)
    mcp_tools: tuple[ArchitectMcpTool, ...] = Field(default=(), max_length=MAX_AGENT_TOOLS)
    knowledge_bases: tuple[str, ...] = Field(default=(), max_length=8)
    memory: bool = False
    model_settings: ArchitectModelSettings | None = None
    # A JSON Schema object every answer must match (structured output). A workflow step then reads a field as
    # `steps.<id>.output.<field>`.
    output_schema: dict[str, Any] | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name cannot be blank")
        return value.strip()

    @field_validator("id")
    @classmethod
    def _id(cls, value: str | None) -> str | None:
        return _check_id(value)

    @field_validator("model")
    @classmethod
    def _model(cls, value: str | None) -> str | None:
        if value is not None and (":" not in value or value.startswith(":") or value.endswith(":")):
            raise ValueError('model must look like "provider:name"')
        return value


def compile_agent(spec: ArchitectAgentSpec, allowlist: Allowlist) -> AgentSpec:
    """The full `AgentSpec` for a restricted spec. Raises ValueError("tool_not_allowed: <id>") for a
    tool (or MCP tool, as "server/tool") the allowlist did not offer or the code denylist forbids,
    and `model_not_allowed`, `model_required`, `knowledge_base_not_allowed` likewise.

    Approvals are not editable in the restricted format: `approvals.tools` lists every granted tool
    the allowlist marks `requires_approval` and every granted MCP tool."""
    offered = {t.id: t for t in allowlist.tools}
    for tool_id in spec.tools:
        if is_denied_tool(tool_id) or tool_id in WORKFLOW_ONLY_TOOL_IDS or tool_id not in offered:
            raise ValueError(f"tool_not_allowed: {tool_id}")
    servers = {s.id: s for s in allowlist.mcp_servers}
    for ref in spec.mcp_tools:
        server = servers.get(ref.server)
        if server is None or ref.tool not in server.tools:
            raise ValueError(f"tool_not_allowed: {ref.server}/{ref.tool}")
    kb_ids = {kb.id for kb in allowlist.knowledge_bases}
    for kb in spec.knowledge_bases:
        if kb not in kb_ids:
            raise ValueError(f"knowledge_base_not_allowed: {kb}")
    from eap_schemas.architect_seed import rank_model_ids  # lazy: architect_seed imports this module

    ranked = rank_model_ids([m.id for m in allowlist.models])
    # A tenant with no default model has none to give; otherwise the ranked pick, not the first listed.
    model_id = spec.model or ((ranked[0] if ranked else None) if allowlist.default_model else None)
    if model_id is None:
        raise ValueError("model_required: choose a model from the allowlist")
    if model_id not in {m.id for m in allowlist.models}:
        raise ValueError(f"model_not_allowed: {model_id}")
    provider, _, name = model_id.partition(":")

    gated = [t for t in spec.tools if offered[t].requires_approval]
    gated += [f"{ref.server}/{ref.tool}" for ref in spec.mcp_tools]
    settings = spec.model_settings
    try:
        return AgentSpec(
            name=spec.name,
            instructions=spec.instructions,
            model=ModelRef(provider=provider, name=name),
            tools=tuple(spec.tools),
            mcp_tools=tuple(McpToolRef(server=r.server, tool=r.tool) for r in spec.mcp_tools),
            knowledge_bases=tuple(KnowledgeBaseRef(id=kb) for kb in spec.knowledge_bases),
            memory=MemoryConfig() if spec.memory else None,
            model_settings=ModelSettings(
                temperature=settings.temperature if settings else None,
                max_tokens=settings.max_tokens if settings else None,
            ),
            approvals=ApprovalPolicy(tools=tuple(dict.fromkeys(gated))) if gated else None,
            output=OutputSpec(json_schema=spec.output_schema) if spec.output_schema is not None else None,
        )
    except ValidationError as exc:
        problems = "; ".join(e["msg"].removeprefix("Value error, ") for e in exc.errors())
        raise ValueError(f"agent_invalid: {problems}") from None


# ------------------------------------------------------------------------------- workflow format


def _step_id(value: str) -> str:
    if not STEP_ID_RE.fullmatch(value):
        raise ValueError("step id must match ^[a-z][a-z0-9_]{0,31}$")
    if value in RESERVED_STEP_IDS or value.endswith("_pause"):
        raise ValueError(f"step id '{value}' is reserved")
    return value


class _Step(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        return _step_id(value)


class ArchitectAgentStep(_Step):
    """Run an agent. Give `prompt` (text with `{{input}}`, `{{previous}}`, `{{steps.<id>}}` placeholders) or `input`
    (an object of literals, `{"$ref": "steps.x.output.y"}` and `{"$template": ...}` values; a single string value
    is the agent's whole prompt). `next` is a step id or "end"; absent means the following step."""

    kind: Literal["agent"] = "agent"
    agent: str
    prompt: str | None = Field(default=None, min_length=1, max_length=4000)
    input: dict[str, Any] | None = None
    next: str | None = None

    @field_validator("agent")
    @classmethod
    def _agent(cls, value: str) -> str:
        return _check_slug(value, "agent")  # type: ignore[return-value]

    @model_validator(mode="after")
    def _one_prompt(self) -> "ArchitectAgentStep":
        if (self.prompt is None) == (self.input is None):
            raise ValueError("an agent step needs exactly one of prompt and input")
        return self


class ArchitectBranch(_Step):
    """A branch of a parallel step: an agent step with a prompt (the only kind a branch may be)."""

    kind: Literal["agent"] = "agent"
    agent: str
    prompt: str = Field(min_length=1, max_length=4000)

    @field_validator("agent")
    @classmethod
    def _agent(cls, value: str) -> str:
        return _check_slug(value, "agent")  # type: ignore[return-value]


class ArchitectParallelStep(_Step):
    kind: Literal["parallel"] = "parallel"
    branches: tuple[ArchitectBranch, ...] = Field(min_length=2, max_length=MAX_BRANCHES)


class ArchitectToolStep(_Step):
    """Call a tool. A param value is a literal, a whole-value `{{...}}` placeholder, `{"$ref": "steps.x.output.y"}`
    or `{"$template": <json with "{{ steps.x.output.y }}" strings>}`. `next` is a step id or "end"; absent means the
    following step, except for `workflow.return`, which ends the workflow."""

    kind: Literal["tool"] = "tool"
    tool: str = Field(min_length=1, max_length=64)
    params: dict[str, Any] = Field(default_factory=dict)
    next: str | None = None
    on_error: Literal["fail", "continue"] = "fail"


class ArchitectBranchStep(_Step):
    """Go to the first case whose `when` holds, else to `default`. A case is `{"when": <condition>, "next": <step id>}`;
    a condition is `{"op": "eq|ne|gt|lt|contains|exists", "left": <$ref or literal>, "right": <$ref or literal>}`
    or `{"all": [...]}`, `{"any": [...]}`, `{"not": <condition>}`."""

    kind: Literal["branch"] = "branch"
    cases: tuple[dict[str, Any], ...] = Field(min_length=1, max_length=8)
    default: str = Field(min_length=1, max_length=32)


ArchitectStep = Annotated[
    Union[ArchitectAgentStep, ArchitectParallelStep, ArchitectToolStep, ArchitectBranchStep], Field(discriminator="kind")
]


class ArchitectWorkflowSpec(BaseModel):
    """The workflow format the Architect may write: 1 up to the platform's step cap (`workflows.MAX_STEPS`; a parallel step and each of its
    branches count) of kind agent, tool, branch or parallel (2 to 4 agent branches). Steps run in list order unless a step names a `next` (or a branch picks one). No loops or code
    steps. Without `input_schema` the workflow takes `{input: string}` and prompts and tool params may use
    `{{input}}`, `{{previous}}` and `{{steps.<id>}}`; with `input_schema` (an object schema) a step reads
    `input.<field>` through `$ref` / `$template`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=80)
    id: str | None = None
    description: str = Field(default="", max_length=300)
    input_description: str = Field(default="The text to work on.", max_length=300)
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    steps: tuple[ArchitectStep, ...] = Field(min_length=1, max_length=MAX_STEPS)

    @field_validator("steps", mode="before")
    @classmethod
    def _step_count(cls, value: Any) -> Any:
        """Same rule and message as the platform's WorkflowSpec: a parallel step and each branch count."""
        if isinstance(value, (list, tuple)) and value:
            total = 0
            for item in value:
                branches = item.get("branches") if isinstance(item, dict) else getattr(item, "branches", None)
                total += 1 + (len(branches) if isinstance(branches, (list, tuple)) else 0)
            if total > MAX_STEPS:
                raise ValueError(f"a workflow has at most {MAX_STEPS} steps, got {total}")
        return value

    @field_validator("id")
    @classmethod
    def _id(cls, value: str | None) -> str | None:
        return _check_id(value)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name cannot be blank")
        return value.strip()

    @model_validator(mode="after")
    def _unique_ids(self) -> "ArchitectWorkflowSpec":
        seen: set[str] = set()
        for step in self.steps:
            ids = [b.id for b in step.branches] + [step.id] if isinstance(step, ArchitectParallelStep) else [step.id]
            for sid in ids:
                if sid in seen:
                    raise ValueError(f"duplicate step id: {sid}")
                seen.add(sid)
        return self


def _ref(key: str, previous: str | None, known: set[str]) -> dict[str, str]:
    """The `$ref` of one placeholder, or ValueError("unknown_placeholder: {{key}}")."""
    if not _VALID_PLACEHOLDER.fullmatch(key):
        raise ValueError(f"unknown_placeholder: {{{{{key}}}}}")
    if key == "input":
        return {"$ref": "input.input"}
    if key == "previous":
        if previous is None:
            raise ValueError("unknown_placeholder: {{previous}} (there is no previous step here)")
        return {"$ref": f"steps.{previous}.output"}
    step_id = key.removeprefix("steps.")
    if step_id not in known:
        raise ValueError(f"unknown_placeholder: {{{{{key}}}}} (no earlier step '{step_id}')")
    return {"$ref": f"steps.{step_id}.output"}


def _prompt_input(prompt: str, previous: str | None, known: set[str]) -> dict[str, Any]:
    """A prompt as a step input. A prompt that is exactly one placeholder passes that value as it is;
    a prompt with text around its placeholders is passed as `{"instruction": <text>, <placeholder>: <value>}`
    so the agent receives the text and each value by the name the text uses."""
    tokens = list(_PLACEHOLDER.finditer(prompt))
    if "{{" in _PLACEHOLDER.sub("", prompt) or "}}" in _PLACEHOLDER.sub("", prompt):
        raise ValueError("unknown_placeholder: unbalanced braces in the prompt")
    if not tokens:
        return {"prompt": prompt}
    keys = [m.group(1) for m in tokens]
    refs = {key: _ref(key, previous, known) for key in dict.fromkeys(keys)}
    if len(tokens) == 1 and tokens[0].group(0) == prompt.strip():
        return {"prompt": refs[keys[0]]}
    return {"instruction": prompt, **refs}


def _params_input(params: dict[str, Any], previous: str | None, known: set[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in params.items():
        if isinstance(value, str) and "{{" in value:
            match = _PLACEHOLDER.fullmatch(value.strip())
            if match is None:
                raise ValueError(
                    f"placeholder_not_whole_value: param '{key}' must be exactly one placeholder "
                    "(or use {\"$template\": \"text {{ steps.x.output.y }}\"} to mix text and references)")
            out[key] = _ref(match.group(1), previous, known)
        else:
            out[key] = value
    return out


def compile_workflow(spec: ArchitectWorkflowSpec, allowlist: Allowlist) -> WorkflowSpec:
    """The full `WorkflowSpec` for a restricted spec, input schema `{input: string}`. An agent step
    must name an agent of `allowlist.agents` (with a row id), a tool step a tool of `allowlist.tools`;
    otherwise ValueError("tool_not_allowed: <id>") or ("agent_not_allowed: <id>"). A parallel step
    joins at the step after it, so it cannot be last. Pure: the same input gives the same spec."""
    tools = {t.id for t in allowlist.tools}
    agents = {a.id: a for a in allowlist.agents}

    def agent_uuid(slug: str) -> UUID:
        found = agents.get(slug)
        if found is None or found.row_id is None:
            raise ValueError(f"agent_not_allowed: {slug}")
        return UUID(found.row_id)

    top = spec.steps
    ids = {sid for st in top for sid in ([b.id for b in st.branches] + [st.id] if isinstance(st, ArchitectParallelStep) else [st.id])}
    steps: list[Any] = []
    known: set[str] = set()
    previous: str | None = None  # the previous top-level step that has an output a placeholder may use

    def target(value: str | None, following: str | None, default_end: bool = False) -> str | None:
        """The compiled `next`: the step named, None for "end", else the following step."""
        if value is None:
            return None if default_end else following
        if value == END:
            return None
        if value not in ids:
            raise ValueError(f"unknown_step: next '{value}' is not a step of this workflow")
        return value

    for i, step in enumerate(top):
        following = top[i + 1].id if i + 1 < len(top) else None
        if isinstance(step, ArchitectAgentStep):
            steps.append(AgentStep(
                id=step.id, agent_id=agent_uuid(step.agent), next=target(step.next, following),
                input=_prompt_input(step.prompt, previous, known) if step.prompt is not None else step.input,
            ))
            known.add(step.id)
            previous = step.id
        elif isinstance(step, ArchitectToolStep):
            if is_denied_tool(step.tool) or step.tool not in tools:
                raise ValueError(f"tool_not_allowed: {step.tool}")
            steps.append(ToolStep(
                id=step.id, tool_id=step.tool, next=target(step.next, following, default_end=step.tool == RETURN_TOOL_ID),
                input=_params_input(step.params, previous, known), on_error=step.on_error,
            ))
            known.add(step.id)
            previous = step.id
        elif isinstance(step, ArchitectBranchStep):
            cases = tuple(
                {**c, "next": target(c.get("next"), None)} if isinstance(c, dict) and c.get("next") is not None else c
                for c in step.cases
            )
            if step.default not in ids:
                raise ValueError(f"unknown_step: default '{step.default}' is not a step of this workflow")
            steps.append(BranchStep(id=step.id, cases=cases, default=step.default))
            previous = None
        else:
            if following is None:
                raise ValueError("parallel_needs_following_step: a parallel step joins at the step after it")
            steps.append(ParallelStep(id=step.id, branches=tuple(b.id for b in step.branches), join=following))
            for branch in step.branches:
                steps.append(AgentStep(
                    id=branch.id, agent_id=agent_uuid(branch.agent), next=None,
                    input=_prompt_input(branch.prompt, previous, known),
                ))
            known.update(b.id for b in step.branches)
            previous = None  # the step after a parallel reads {{steps.<branch>}}, not {{previous}}
    input_schema = spec.input_schema or {
        "type": "object",
        "properties": {"input": {"type": "string", "description": spec.input_description}},
        "required": ["input"],
        "additionalProperties": False,
    }
    try:
        return WorkflowSpec(
            name=spec.name,
            description=spec.description,
            input_schema=input_schema,
            output_schema=spec.output_schema,
            steps=tuple(steps),
            start=top[0].id,
        )
    except ValidationError as exc:
        problems = "; ".join(e["msg"].removeprefix("Value error, ") for e in exc.errors())
        raise ValueError(f"workflow_invalid: {problems}") from None


__all__ = [
    "AGENT_ID_PATTERN",
    "as_uuid",
    "ARCHITECT_DENIED_TOOL_PREFIXES",
    "ARCHITECT_PLATFORM_TOOL_IDS",
    "CONTROL_LIST_TOOL_ID",
    "CONTROL_TOOL_PREFIX",
    "control_tool_name",
    "BUILTIN_ONLY_TOOL_PREFIXES",
    "Allowlist",
    "AllowlistAgent",
    "AllowlistKnowledgeBase",
    "AllowlistMcpServer",
    "AllowlistModel",
    "AllowlistTool",
    "ArchitectAgentSpec",
    "ArchitectAgentStep",
    "ArchitectBranch",
    "ArchitectBranchStep",
    "ArchitectMcpTool",
    "ArchitectModelSettings",
    "ArchitectParallelStep",
    "ArchitectToolStep",
    "ArchitectWorkflowSpec",
    "ARCHITECT_GATED_TOOL_IDS",
    "ARCHITECT_TOOL_IDS",
    "GATED_TOOL_IDS",
    "RESERVED_AGENT_IDS",
    "compile_agent",
    "compile_workflow",
    "WORKFLOW_ONLY_TOOL_IDS",
    "WORKFLOW_STEP_TOOL_IDS",
    "is_denied_tool",
    "offer_tool",
    "GENERIC_IDS",
    "is_generic_id",
    "slugify",
    "ungated_architect_tools",
]
