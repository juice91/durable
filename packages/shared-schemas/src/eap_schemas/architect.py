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

import re
from typing import Annotated, Any, Literal, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from eap_schemas import AgentSpec, ApprovalPolicy, KnowledgeBaseRef, McpToolRef, MemoryConfig, ModelRef, ModelSettings
from eap_schemas.tools import ARCHITECT_GATED_TOOL_IDS, ungated_always_gated_tools
from eap_schemas.workflows import AgentStep, ParallelStep, ToolStep, WorkflowSpec

AGENT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,47}$")
STEP_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
RESERVED_STEP_IDS = frozenset({"run_end"})
# Slugs the Architect may never create or edit: the built-in specs.
RESERVED_AGENT_IDS = frozenset({"architect", "architect-drafter", "architect-workflow-drafter"})
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
    "architect.save_agent",
    "architect.save_workflow",
    *ARCHITECT_GATED_TOOL_IDS,
)


def ungated_architect_tools(tools: Iterable[str], approval_tools: Iterable[str] | None) -> list[str]:
    """The gated Architect tools in `tools` that `approval_tools` (spec.approvals.tools) does not list."""
    return [t for t in ungated_always_gated_tools(tools, approval_tools) if t in ARCHITECT_GATED_TOOL_IDS]


MAX_AGENT_TOOLS = 32
MAX_STEPS = 8
MAX_BRANCHES = 4
_PLACEHOLDER = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")
_VALID_PLACEHOLDER = re.compile(r"^(?:input|previous|steps\.[a-z][a-z0-9_]{0,63})$")


def slugify(name: str) -> str:
    """A slug (`AGENT_ID_PATTERN`) from a name: lowercase words joined by dashes, at most 48 characters."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:48].strip("-")
    if not slug or not slug[0].isalpha():
        slug = f"a-{slug}".strip("-")[:48].strip("-")
    return slug if AGENT_ID_PATTERN.fullmatch(slug) else "definition"


def is_denied_tool(tool_id: str) -> bool:
    return any(tool_id == p or tool_id.startswith(p) for p in ARCHITECT_DENIED_TOOL_PREFIXES)


def offer_tool(
    tool_id: str, title: str, description: str, *, side_effect: bool = False, host_only: bool = False
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
    )


# ------------------------------------------------------------------------------------- allowlist


class AllowlistTool(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    title: str
    description: str
    requires_approval: bool = False


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
    """An Architect-made agent a created workflow may run (it has a published version)."""

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

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name cannot be blank")
        return value.strip()

    @field_validator("id")
    @classmethod
    def _id(cls, value: str | None) -> str | None:
        return _check_slug(value)

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
        if is_denied_tool(tool_id) or tool_id not in offered:
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
    model_id = spec.model or allowlist.default_model
    if model_id is None:
        raise ValueError("model_required: choose a model from the allowlist")
    if model_id not in {m.id for m in allowlist.models}:
        raise ValueError(f"model_not_allowed: {model_id}")
    provider, _, name = model_id.partition(":")

    gated = [t for t in spec.tools if offered[t].requires_approval]
    gated += [f"{ref.server}/{ref.tool}" for ref in spec.mcp_tools]
    settings = spec.model_settings
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
    )


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
    kind: Literal["agent"] = "agent"
    agent: str
    prompt: str = Field(min_length=1, max_length=4000)

    @field_validator("agent")
    @classmethod
    def _agent(cls, value: str) -> str:
        return _check_slug(value, "agent")  # type: ignore[return-value]


class ArchitectBranch(ArchitectAgentStep):
    """A branch of a parallel step: an agent step (the only kind a branch may be)."""


class ArchitectParallelStep(_Step):
    kind: Literal["parallel"] = "parallel"
    branches: tuple[ArchitectBranch, ...] = Field(min_length=2, max_length=MAX_BRANCHES)


class ArchitectToolStep(_Step):
    kind: Literal["tool"] = "tool"
    tool: str = Field(min_length=1, max_length=64)
    params: dict[str, Any] = Field(default_factory=dict)


ArchitectStep = Annotated[
    Union[ArchitectAgentStep, ArchitectParallelStep, ArchitectToolStep], Field(discriminator="kind")
]


class ArchitectWorkflowSpec(BaseModel):
    """The workflow format the Architect may write: 1 to 8 steps of kind agent, parallel (2 to 4
    agent branches) or tool, run in order. No loops, conditions or code steps. Prompts and tool
    params may use `{{input}}`, `{{previous}}` and `{{steps.<id>}}`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=80)
    id: str | None = None
    description: str = Field(default="", max_length=300)
    input_description: str = Field(default="The text to work on.", max_length=300)
    steps: tuple[ArchitectStep, ...] = Field(min_length=1, max_length=MAX_STEPS)

    @field_validator("id")
    @classmethod
    def _id(cls, value: str | None) -> str | None:
        return _check_slug(value)

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
                raise ValueError(f"placeholder_not_whole_value: param '{key}' must be exactly one placeholder")
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
    steps: list[Any] = []
    known: set[str] = set()
    previous: str | None = None  # the previous top-level step that has an output a placeholder may use
    for i, step in enumerate(top):
        following = top[i + 1].id if i + 1 < len(top) else None
        if isinstance(step, ArchitectAgentStep):
            steps.append(AgentStep(
                id=step.id, agent_id=agent_uuid(step.agent), next=following,
                input=_prompt_input(step.prompt, previous, known),
            ))
            known.add(step.id)
            previous = step.id
        elif isinstance(step, ArchitectToolStep):
            if is_denied_tool(step.tool) or step.tool not in tools:
                raise ValueError(f"tool_not_allowed: {step.tool}")
            steps.append(ToolStep(
                id=step.id, tool_id=step.tool, next=following,
                input=_params_input(step.params, previous, known),
            ))
            known.add(step.id)
            previous = step.id
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
    try:
        return WorkflowSpec(
            name=spec.name,
            description=spec.description,
            input_schema={
                "type": "object",
                "properties": {"input": {"type": "string", "description": spec.input_description}},
                "required": ["input"],
                "additionalProperties": False,
            },
            steps=tuple(steps),
            start=top[0].id,
        )
    except ValidationError as exc:
        problems = "; ".join(e["msg"].removeprefix("Value error, ") for e in exc.errors())
        raise ValueError(f"workflow_invalid: {problems}") from None


__all__ = [
    "AGENT_ID_PATTERN",
    "ARCHITECT_DENIED_TOOL_PREFIXES",
    "Allowlist",
    "AllowlistAgent",
    "AllowlistKnowledgeBase",
    "AllowlistMcpServer",
    "AllowlistModel",
    "AllowlistTool",
    "ArchitectAgentSpec",
    "ArchitectAgentStep",
    "ArchitectBranch",
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
    "is_denied_tool",
    "offer_tool",
    "slugify",
    "ungated_architect_tools",
]
