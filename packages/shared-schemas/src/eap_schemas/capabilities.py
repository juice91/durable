"""The capability registry's shapes and its code-derived inventory (system-aware Architect, phase A).

One entry shape for everything the platform can do: tools, workflow step kinds, triggers, agent tools,
features, the options of a tool (a `text.check` rule, an `http.request` reducer), MCP control tools and,
added at run time by probes, models and the tenant's MCP servers and their tools. The inventory is built from
the declaration modules by `collect_declared()` (pure: no I/O, no settings), so the API service, the worker and
a CI check all see the same list. A probe (eap_api.services.capability_probes) may only change an entry's
`status` and add tenant entries; it can never add a built-in one, so a tool without a declaration here is a test
failure, not a silent absence.

`status` has exactly three values: `available`, `designed_not_built` (a design exists, the code does not: see
`eap_schemas.capability_gaps`) and `disabled` (built, but off right now: a setting, a provider that is down).
`writable_by_architect` is separate: `step.foreach` is `available` and not writable, so the Architect says "a
person builds this in the editor", not "unsupported".

Ids are `<kind prefix>.<name>`: `tool.http.request`, `step.foreach`, `trigger.schedule`, `feature.kb.retrieve`,
`tool_option.text.check.verbatim_exempt`, `control.agent.list`, `mcp_tool.<server>/<tool>`, `model.<provider>`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from functools import cache
from typing import Annotated, Any, Literal, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field

CapabilityKind = Literal[
    "tool", "tool_option", "mcp_server", "mcp_tool", "step", "trigger", "agent_tool", "model", "feature", "control_tool"
]
CapabilityStatus = Literal["available", "designed_not_built", "disabled"]
# `platform_only`: a platform tool no created agent or workflow is ever granted (kb.source.*, settings.*, workflow.run.*,
# the hidden knowledge.search): it exists for tool runs and the Architect's own plumbing.
CapabilityScope = Literal["agent", "workflow", "workflow_only", "architect_only", "control", "platform_only"]

KINDS: tuple[str, ...] = get_args(CapabilityKind)
STATUSES: tuple[str, ...] = get_args(CapabilityStatus)
# What a probe may add (the tenant's and the deployment's dynamic entries); never a built-in kind.
DYNAMIC_KINDS: frozenset[str] = frozenset({"mcp_server", "mcp_tool", "model"})
MAX_DESCRIPTION_CHARS = 300


class CapabilityEntry(BaseModel):
    """One thing the platform can (or cannot yet) do."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    kind: CapabilityKind
    title: str
    description: str = Field(max_length=MAX_DESCRIPTION_CHARS)
    status: CapabilityStatus = "available"
    status_reason: str | None = None  # why `disabled` (a setting is off, a provider is down, the server does not answer)
    input_schema: dict[str, Any] | None = None
    result_schema: dict[str, Any] | None = None
    requires_approval: bool = False
    side_effect: bool = False
    limits: dict[str, int | str] = Field(default_factory=dict)  # from code constants
    scope: CapabilityScope = "agent"
    writable_by_architect: bool = True  # False: it exists, but the Architect's authoring format cannot express it
    docs_ref: str | None = None  # a path under docs/, never a URL
    designed_in: str | None = None  # for designed_not_built: a docs path or `pr:<number>` (see capability_gaps)
    needs: tuple[str, ...] = ()  # other capability ids it depends on
    permission: str | None = None  # the IAM action the principal needs to use it; None until IAM P2

    def compact(self) -> dict[str, Any]:
        """The listing form: no schemas (`architect.get_capability` returns those)."""
        return self.model_dump(mode="json", exclude={"input_schema", "result_schema"}, exclude_defaults=False)


class ControlToolDecl(BaseModel):
    """One MCP control tool as the control plane's registry declares it (eap_api.control_catalog records them)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str  # `agent.list`
    description: str = ""
    manage: bool = False
    human_only: bool = False  # never offered to the Architect's own run
    internal: bool = False  # called by the platform's own code as the Architect's run, never offered to its model


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def entries_hash(entries: Iterable[CapabilityEntry]) -> str:
    """sha256 over the canonical JSON of the entries sorted by id: the registry's content address."""
    rows = [e.model_dump(mode="json") for e in sorted(entries, key=lambda e: e.id)]
    return hashlib.sha256(canonical_json(rows).encode("utf-8")).hexdigest()


def _short(text: str, limit: int = MAX_DESCRIPTION_CHARS) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _first_sentence(text: str) -> str:
    flat = " ".join((text or "").split()).replace("``", "`")
    for end in (". ", "; "):
        if end in flat:
            flat = flat.split(end, 1)[0]
    return flat.rstrip(".")


# ------------------------------------------------------------------------------------------------ tools

# docs/tools/<file>.md for the tools that have a page (a static map, so collect_declared does no I/O; a test checks
# every file exists).
TOOL_DOCS: dict[str, str] = {
    "atproto.richtext": "docs/tools/atproto-richtext.md",
    "clock.now": "docs/tools/clock.md",
    "clock.sleep": "docs/tools/clock.md",
    "feed.parse": "docs/tools/feed-parse.md",
    "html.extract": "docs/tools/html-extract.md",
    "http.request": "docs/tools/http-request.md",
    "json.query": "docs/tools/json-query.md",
    "text.check": "docs/tools/text-check.md",
    "workflow.history": "docs/tools/workflow-history.md",
}

# One line each for the Architect's own tools (they are worker code with no declaration in eap_schemas.tools).
ARCHITECT_TOOL_DESCRIPTIONS: dict[str, str] = {
    "architect.list_approved_tools": "What a created agent or workflow may use: grantable tools, MCP servers, knowledge bases, models, agents.",
    "architect.list_capabilities": "List what the platform can do now (the capability registry): kind, status, approval, limits; plus the known gaps.",
    "architect.get_capability": "One capability with its schemas, limits, needs and docs reference.",
    "architect.list_created_agents": "The agents the Architect made, with versions and draft state.",
    "architect.list_agent_versions": "The versions of an agent the Architect made.",
    "architect.list_created_workflows": "The workflows the Architect made, with versions and draft state.",
    "architect.list_workflow_versions": "The versions of a workflow the Architect made.",
    "architect.list_workflow_step_kinds": "The step kinds the Architect may write, the placeholders and the limits.",
    "architect.save_agent": "Save an agent definition as a draft; nothing goes live.",
    "architect.save_workflow": "Save a workflow definition as a draft; nothing goes live.",
    "architect.list_eval_suites": "The test suites of an agent or workflow.",
    "architect.get_eval_run": "One eval run with its results.",
    "architect.publish_agent": "Publish an agent draft as a new version (a person approves).",
    "architect.restore_agent_version": "Restore an earlier agent version as the draft (a person approves).",
    "architect.archive_agent": "Archive an agent (a person approves).",
    "architect.publish_workflow": "Publish a workflow draft as a new version (a person approves).",
    "architect.restore_workflow_version": "Restore an earlier workflow version as the draft (a person approves).",
    "architect.archive_workflow": "Archive a workflow (a person approves).",
    "architect.set_workflow_schedule": "Create, pause, resume or delete a workflow schedule (a person approves).",
    "architect.create_eval_suite": "Create an eval suite (a person approves).",
    "architect.add_eval_case": "Add a case to an eval suite (a person approves).",
    "architect.attach_scorer": "Attach a scorer to an eval suite (a person approves).",
    "architect.start_eval_run": "Run an eval suite against a published version (a person approves).",
    "platform.list_knowledge_bases": "The tenant's knowledge bases (read only).",
    "platform.get_knowledge_base": "One knowledge base of the tenant (read only).",
    "platform.list_source_syncs": "Recent source syncs of a knowledge base (read only).",
    "platform.list_workflows": "The tenant's workflows (read only).",
    "platform.get_workflow": "One workflow of the tenant (read only).",
    "platform.list_agents": "The tenant's agents (read only).",
    "platform.list_schedules": "The tenant's workflow schedules (read only).",
    "platform.list_recent_runs": "Recent runs of the tenant (read only).",
    "platform.get_run": "One run of the tenant, bounded (read only).",
    "platform.list_runs": "Runs of the tenant, filtered and bounded (read only).",
    "platform.list_capabilities": "The MCP control plane's tools the Architect may call as `control__*`.",
}
APPROVED_TOOL_SCOPES = ("agent", "workflow", "workflow_only")


def _tool_scope(tool_id: str) -> CapabilityScope:
    from eap_schemas.architect import WORKFLOW_ONLY_TOOL_IDS, is_denied_tool

    if tool_id in WORKFLOW_ONLY_TOOL_IDS:
        return "workflow_only"
    if tool_id.startswith(("architect.", "platform.")):
        return "architect_only"
    if tool_id == "workflow.return":
        return "workflow"
    if is_denied_tool(tool_id):
        return "platform_only"
    return "agent"


def _tool_entries() -> list[CapabilityEntry]:
    from eap_schemas.architect import (
        ARCHITECT_PLATFORM_TOOL_IDS,
        ARCHITECT_TOOL_IDS,
        GATED_TOOL_IDS,
        is_denied_tool,
    )
    from eap_schemas.tools import (
        ALWAYS_GATED_TOOL_IDS,
        HIDDEN_TOOL_DECLARATIONS,
        HTTP_MAX_REDUCED_RESPONSE_BYTES_CAP,
        HTTP_MAX_RESPONSE_BYTES_CAP,
        TOOL_DECLARATIONS,
    )

    out: list[CapabilityEntry] = []
    for tool_id, decl in {**TOOL_DECLARATIONS, **HIDDEN_TOOL_DECLARATIONS}.items():
        limits: dict[str, int | str] = {}
        if tool_id == "http.request":
            limits = {"max_response_bytes": HTTP_MAX_RESPONSE_BYTES_CAP, "max_reduced_response_bytes": HTTP_MAX_REDUCED_RESPONSE_BYTES_CAP}
        out.append(CapabilityEntry(
            id=f"tool.{tool_id}", kind="tool", title=decl.title, description=_short(decl.description),
            input_schema=decl.input_json_schema(), result_schema=decl.result_schema,
            requires_approval=bool(decl.side_effect) or tool_id in GATED_TOOL_IDS or tool_id in ALWAYS_GATED_TOOL_IDS,
            side_effect=bool(decl.side_effect), limits=limits, scope=_tool_scope(tool_id),
            writable_by_architect=not is_denied_tool(tool_id) and tool_id not in HIDDEN_TOOL_DECLARATIONS,
            docs_ref=TOOL_DOCS.get(tool_id),
        ))
    gated = ALWAYS_GATED_TOOL_IDS
    for tool_id in (*ARCHITECT_TOOL_IDS, *ARCHITECT_PLATFORM_TOOL_IDS):
        out.append(CapabilityEntry(
            id=f"tool.{tool_id}", kind="tool", title=tool_id.split(".", 1)[1].replace("_", " ").capitalize(),
            description=_short(ARCHITECT_TOOL_DESCRIPTIONS.get(tool_id, tool_id)),
            requires_approval=tool_id in gated, side_effect=tool_id in gated,
            scope="architect_only", writable_by_architect=False, docs_ref="docs/architect.md",
        ))
    return out


# ------------------------------------------------------------------------------------------ tool options

TEXT_CHECK_RULE_DESCRIPTIONS: dict[str, str] = {
    "min_graphemes": "Fail when the text is shorter than this many graphemes.",
    "max_graphemes": "Fail when the text is longer than this many graphemes.",
    "forbid_urls": "Fail when the text contains a URL.",
    "forbid_hashtags": "Fail when the text contains a hashtag.",
    "forbid_emoji": "Fail when the text contains an emoji.",
    "forbid_simplified_chinese": "Fail when the text contains simplified Chinese characters.",
    "forbid_terms": "Fail when the text contains any of these terms (case sensitive).",
    "rumour_terms": "Terms that mark a claim as unconfirmed; they need an attribution term nearby.",
    "attribution_terms": "Terms that count as an attribution for a rumour term.",
    "forbid_terms_ci": "Fail when the text contains any of these terms, ignoring case.",
    "min_han_ratio": "Fail when the share of Han characters is below this ratio.",
    "max_verbatim_run": "Fail when the text copies the source verbatim for more than this many characters in a row.",
    "verbatim_exempt": "What max_verbatim_run does not count as copying: quoted text, numbers, listed terms.",
}
HTTP_REDUCERS: dict[str, str] = {
    "response_query": "Reduce a JSON response to the result of a JMESPath expression (as json.query).",
    "extract_html": "Reduce an HTML response to its article text or its links (as html.extract).",
    "parse_feed": "Reduce an RSS or Atom response to normalised feed items (as feed.parse).",
}


def _option_entries() -> list[CapabilityEntry]:
    from eap_schemas.tools import TextCheckRules

    out: list[CapabilityEntry] = []
    for name in TextCheckRules.model_fields:
        out.append(CapabilityEntry(
            id=f"tool_option.text.check.{name}", kind="tool_option", title=f"text.check rule {name}",
            description=TEXT_CHECK_RULE_DESCRIPTIONS.get(name, f"A rule of text.check: {name}."),
            needs=("tool.text.check",), docs_ref=TOOL_DOCS["text.check"],
        ))
    for name, text in HTTP_REDUCERS.items():
        out.append(CapabilityEntry(
            id=f"tool_option.http.request.{name}", kind="tool_option", title=f"http.request reducer {name}",
            description=text, needs=("tool.http.request",), docs_ref=TOOL_DOCS["http.request"],
        ))
    return out


# ------------------------------------------------------------------------------------------------ steps

def _union_members(annotation: Any) -> tuple[Any, ...]:
    """The classes of an `Annotated[Union[...], Field(discriminator=...)]` step type."""
    inner = get_args(annotation)[0] if get_origin(annotation) is Annotated else annotation
    return get_args(inner)


def platform_step_kinds() -> tuple[str, ...]:
    """The runtime's step kinds, read off the `WorkflowSpec` step union (its discriminator is `type`)."""
    from eap_schemas.workflows import Step

    return tuple(cls.model_fields["type"].default for cls in _union_members(Step))


def architect_step_kinds() -> tuple[str, ...]:
    """The subset the Architect's authoring format writes (its discriminator is `kind`)."""
    from eap_schemas.architect import ArchitectStep

    return tuple(cls.model_fields["kind"].default for cls in _union_members(ArchitectStep))


def _step_entries() -> list[CapabilityEntry]:
    from eap_schemas import workflows as wf

    writable = set(architect_step_kinds())
    limits: dict[str, dict[str, int | str]] = {
        "foreach": {"max_concurrency": wf.MAX_FOREACH_CONCURRENCY, "max_iterations": wf.MAX_LOOP_ITERATIONS},
        "loop": {"max_iterations": wf.MAX_LOOP_ITERATIONS, "max_depth": wf.MAX_LOOP_DEPTH},
        "evaluate": {"max_evaluators": wf.MAX_EVALUATE_EVALUATORS},
        "branch": {"max_operands": wf.MAX_CONDITION_OPERANDS, "max_condition_depth": wf.MAX_CONDITION_DEPTH},
    }
    from eap_schemas.architect import MAX_BRANCHES

    limits["parallel"] = {"architect_max_branches": MAX_BRANCHES}
    out: list[CapabilityEntry] = []
    for cls in _union_members(wf.Step):
        kind = cls.model_fields["type"].default
        out.append(CapabilityEntry(
            id=f"step.{kind}", kind="step", title=f"{kind} step",
            description=_short(_first_sentence(cls.__doc__ or kind)),
            scope="workflow", limits={"max_steps": wf.MAX_STEPS, **limits.get(kind, {})},
            writable_by_architect=kind in writable, docs_ref="docs/architect.md" if kind in writable else None,
        ))
    return out


# -------------------------------------------------------------------------------- triggers and features

def _trigger_and_feature_entries() -> list[CapabilityEntry]:
    from eap_schemas import agent_tools as at
    from eap_schemas.limits import MAX_REQUESTS, MAX_TOKENS, MAX_TOOL_CALLS

    return [
        CapabilityEntry(
            id="trigger.schedule", kind="trigger", title="Schedule",
            description="Start a published workflow on a cron schedule in a timezone; each fire runs the workflow's latest published version.",
            requires_approval=True, side_effect=True, scope="workflow", docs_ref="docs/architect.md",
        ),
        CapabilityEntry(
            id="trigger.api_run", kind="trigger", title="Start a run on request",
            description="A person, an API or MCP client or an AG-UI client starts a run of a published agent or workflow.",
            scope="workflow", docs_ref="docs/mcp-control.md",
        ),
        CapabilityEntry(
            id="feature.agent_tools", kind="agent_tool", title="Agents as tools",
            description="An agent may call other published agents of its tenant as tools; each call is a child run with its own events and approvals.",
            limits={
                "max_per_agent": at.MAX_AGENT_TOOLS_PER_AGENT, "default_max_calls_per_run": at.DEFAULT_MAX_CALLS_PER_RUN,
                "max_calls_per_run": at.MAX_CALLS_PER_RUN_LIMIT, "max_timeout_seconds": at.MAX_TIMEOUT_SECONDS,
            },
            writable_by_architect=False,
        ),
        CapabilityEntry(
            id="feature.kb.retrieve", kind="feature", title="Knowledge base retrieval",
            description="An agent searches the tenant's knowledge bases (hybrid retrieval) through its `knowledge_bases` field.",
            docs_ref="docs/architect.md",
        ),
        CapabilityEntry(
            id="feature.kb.source_sync", kind="feature", title="Knowledge base source sync",
            description="A knowledge base ingests a connected source (for example a GitHub repository) and re-syncs it incrementally.",
            docs_ref="docs/kb-sources.md", writable_by_architect=False,
        ),
        CapabilityEntry(
            id="feature.memory", kind="feature", title="Working memory",
            description="An agent keeps working memory across the turns of a conversation.", writable_by_architect=False,
        ),
        CapabilityEntry(
            id="feature.approvals", kind="feature", title="Approvals",
            description="A person approves a gated tool call before it runs; the always-gated tools cannot run without it.",
            docs_ref="docs/architect.md",
        ),
        CapabilityEntry(
            id="feature.output_schema", kind="feature", title="Structured output",
            description="An agent answers with an object that matches a JSON Schema (`output_schema`).",
            docs_ref="docs/architect.md",
        ),
        CapabilityEntry(
            id="feature.run_limits", kind="feature", title="Run limits",
            description="Every run has request, tool-call, token and cost ceilings.",
            limits={"max_requests": MAX_REQUESTS, "max_tool_calls": MAX_TOOL_CALLS, "max_tokens": MAX_TOKENS},
            writable_by_architect=False,
        ),
        CapabilityEntry(
            id="feature.evals", kind="feature", title="Evals",
            description="Test suites, cases and scorers for agents and workflows, run against a published version.",
            docs_ref="docs/evals.md",
        ),
    ]


# ------------------------------------------------------------------------------------------ control tools

def control_entries(tools: Sequence[ControlToolDecl]) -> list[CapabilityEntry]:
    """`control.<name>` entries for the MCP control plane's tools (a `manage` tool asks a person)."""
    out: list[CapabilityEntry] = []
    for t in sorted(tools, key=lambda d: d.name):
        out.append(CapabilityEntry(
            id=f"control.{t.name}", kind="control_tool", title=t.name,
            description=_short(_first_sentence(t.description) or t.name), requires_approval=t.manage,
            side_effect=t.manage, scope="control", writable_by_architect=False, docs_ref="docs/mcp-control.md",
        ))
    return out


# ----------------------------------------------------------------------------------------------- collect

@cache
def _base() -> tuple[CapabilityEntry, ...]:
    from eap_schemas.capability_gaps import gap_entries

    entries = [*_tool_entries(), *_option_entries(), *_step_entries(), *_trigger_and_feature_entries(), *gap_entries()]
    ids = [e.id for e in entries]
    assert len(ids) == len(set(ids)), "duplicate capability ids: " + ", ".join(sorted({i for i in ids if ids.count(i) > 1}))
    return tuple(sorted(entries, key=lambda e: e.id))


def collect_declared(control_tools: Sequence[ControlToolDecl] = ()) -> tuple[CapabilityEntry, ...]:
    """Every built-in entry, derived from the declaration modules (plus the control tools passed in, which only
    the API knows). Pure and deterministic; sorted by id."""
    if not control_tools:
        return _base()
    return tuple(sorted((*_base(), *control_entries(control_tools)), key=lambda e: e.id))


def approved_tool_ids(entries: Iterable[CapabilityEntry] | None = None) -> frozenset[str]:
    """The ids of the tools a created agent or workflow may be granted according to the registry: tool entries
    that are `available` and writable by the Architect. (`list_approved_tools` is a subset of this.)"""
    source = collect_declared() if entries is None else entries
    return frozenset(
        e.id.removeprefix("tool.") for e in source
        if e.kind == "tool" and e.status == "available" and e.writable_by_architect and e.scope in APPROVED_TOOL_SCOPES
    )


def declared_tool_status(tool_id: str) -> str | None:
    """The declared status of `tool.<tool_id>`, or None when the registry has no entry for it."""
    entry = _by_id().get(f"tool.{tool_id}")
    return None if entry is None else entry.status


@cache
def _by_id() -> dict[str, CapabilityEntry]:
    return {e.id: e for e in _base()}


__all__ = [
    "DYNAMIC_KINDS",
    "KINDS",
    "STATUSES",
    "CapabilityEntry",
    "CapabilityKind",
    "CapabilityScope",
    "CapabilityStatus",
    "ControlToolDecl",
    "approved_tool_ids",
    "architect_step_kinds",
    "canonical_json",
    "collect_declared",
    "control_entries",
    "declared_tool_status",
    "entries_hash",
    "platform_step_kinds",
]
