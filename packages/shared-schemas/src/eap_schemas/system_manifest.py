"""The versioned system manifest (system-aware Architect, phase B).

`docs/platform/system-manifest.yaml` describes the platform to the Architect: what it is, its components, how a
definition moves through its life and which shapes it supports. Two kinds of section:

- hand-written (`platform`, `components`, `relationships`, `lifecycle`, `patterns`): roles and rules in prose;
- generated (`generated:`, between marker comments): limits, tools, step kinds, triggers, providers, known gaps
  and context budgets, produced from code constants and the capability registry by `generate()`. Never edited by
  hand: `scripts/ci/system_manifest.py --write` regenerates, `--check` fails on any difference.

Hand-written text may not repeat a generated value (a number or an id): it writes `{{limits.workflow.max_steps}}`
and `SystemManifest.resolve()` substitutes. The hash is the sha256 of the canonical JSON of the whole file, so a
changed generated value changes it; `platform_version()` is `<project version>+<build sha>` (`+dev` locally).

No I/O except the loader, which reads one file. The worker image packages a copy (see the Dockerfile) so a worker
needs no network to read it.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from eap_schemas.capabilities import architect_step_kinds, canonical_json, collect_declared, platform_step_kinds
from eap_schemas.capability_gaps import GAPS, GapDecl, designed_in_problem, known_gaps
from eap_schemas.system_context import CONTEXT_BUDGET_TOKENS, estimate_tokens

MANIFEST_RELATIVE_PATH = "docs/platform/system-manifest.yaml"
MANIFEST_PATH_ENV = "EAP_SYSTEM_MANIFEST_PATH"  # where the worker image keeps its copy
BUILD_SHA_ENV = "EAP_BUILD_SHA"  # injected at image build; absent locally
BEGIN_MARKER = "# BEGIN GENERATED (scripts/ci/system_manifest.py)"
END_MARKER = "# END GENERATED"
SUPPORTED_MANIFEST_VERSION = 2
# A hand-written number is checked against the generated ones only from here up: smaller numbers ("two
# components", "step 3") are ordinary prose, and the limits worth guarding are all larger.
MIN_GUARDED_NUMBER = 10

_ID = r"^[a-z][a-z0-9_-]*$"
_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_.]+)\s*\}\}")
_NUMBER = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?(?![\w])")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ------------------------------------------------------------------------------------- hand-written


class Platform(_Strict):
    name: str = Field(min_length=1, max_length=40)
    purpose: str = Field(min_length=1, max_length=600)


class Component(_Strict):
    id: str = Field(pattern=_ID, max_length=40)
    role: str = Field(min_length=1, max_length=240)


class Relationship(_Strict):
    source: str = Field(pattern=_ID, max_length=40)
    target: str = Field(pattern=_ID, max_length=40)
    via: str = Field(min_length=1, max_length=160)


class Lifecycle(_Strict):
    agent: tuple[str, ...] = Field(max_length=12)
    workflow: tuple[str, ...] = Field(max_length=12)
    rules: tuple[str, ...] = Field(max_length=16)

    @model_validator(mode="after")
    def _rule_length(self) -> Lifecycle:
        for rule in self.rules:
            if not 1 <= len(rule) <= 240:
                raise ValueError("a lifecycle rule has 1 to 240 characters")
        return self


class Pattern(_Strict):
    id: str = Field(pattern=r"^pattern\.[a-z][a-z0-9_]*$", max_length=60)
    summary: str = Field(min_length=1, max_length=240)
    needs: tuple[str, ...] = Field(min_length=1, max_length=16)  # capability ids; each must resolve in the registry


# --------------------------------------------------------------------------------------- generated


class GeneratedTool(_Strict):
    id: str
    scope: str
    side_effect: bool
    requires_approval: bool
    writable_by_architect: bool


class GeneratedTrigger(_Strict):
    id: str
    status: str


class GeneratedGap(_Strict):
    id: str
    status: str
    designed_in: str | None = None
    entry_ids: tuple[str, ...] = ()
    alternatives: tuple[str, ...] = ()


class StepKinds(_Strict):
    platform: tuple[str, ...]
    architect_writable: tuple[str, ...]


class Generated(_Strict):
    limits: dict[str, dict[str, int | str]]
    tools: tuple[GeneratedTool, ...]
    architect_gated_tools: tuple[str, ...]
    denied_tool_prefixes: tuple[str, ...]
    step_kinds: StepKinds
    triggers: tuple[GeneratedTrigger, ...]
    providers: tuple[str, ...]
    known_gaps: tuple[GeneratedGap, ...]
    context_budget: dict[str, int]


class SystemManifest(_Strict):
    manifest_version: int = Field(ge=1)
    platform: Platform
    components: tuple[Component, ...] = Field(min_length=1, max_length=24)
    relationships: tuple[Relationship, ...] = Field(max_length=40)
    lifecycle: Lifecycle
    patterns: tuple[Pattern, ...] = Field(max_length=40)
    generated: Generated

    @model_validator(mode="after")
    def _relationships_name_components(self) -> SystemManifest:
        ids = {c.id for c in self.components}
        if len(ids) != len(self.components):
            raise ValueError("duplicate component id")
        for r in self.relationships:
            for end in (r.source, r.target):
                if end not in ids:
                    raise ValueError(f"relationship names an undeclared component: {end!r}")
        if len({p.id for p in self.patterns}) != len(self.patterns):
            raise ValueError("duplicate pattern id")
        return self

    # -- generated values in prose ----------------------------------------------------------------

    def resolve(self, text: str) -> str:
        """Substitute `{{limits.workflow.max_steps}}`-style placeholders with the generated values."""
        data = self.generated.model_dump(mode="json")

        def value(match: re.Match[str]) -> str:
            node: Any = data
            for part in match.group(1).split("."):
                if not isinstance(node, dict) or part not in node:
                    raise ValueError(f"unknown generated value {match.group(1)!r}")
                node = node[part]
            if isinstance(node, (dict, list)):
                raise ValueError(f"{match.group(1)!r} is not a single value")
            return str(node)

        return _PLACEHOLDER.sub(value, text)


# ---------------------------------------------------------------------------------- generation


def generate(providers: Sequence[str], gaps: tuple[GapDecl, ...] = GAPS) -> dict[str, Any]:
    """The generated block from code constants and the capability registry (read at call time, so a changed
    constant shows up as a difference). `providers` are the model provider ids (they live in the API)."""
    from eap_schemas import agent_tools as at
    from eap_schemas import architect as arch
    from eap_schemas import limits as run_limits
    from eap_schemas import tools as tool_mod
    from eap_schemas import workflows as wf

    entries = collect_declared()
    limits: dict[str, dict[str, int | str]] = {
        "workflow": {
            "max_steps": wf.MAX_STEPS,
            "max_spec_bytes": wf.MAX_SPEC_BYTES,
            "max_literal_depth": wf.MAX_LITERAL_DEPTH,
            "max_spec_depth": wf.MAX_SPEC_DEPTH,
            "max_template_placeholders": wf.MAX_TEMPLATE_PLACEHOLDERS,
            "max_loop_iterations": wf.MAX_LOOP_ITERATIONS,
            "max_foreach_concurrency": wf.MAX_FOREACH_CONCURRENCY,
            "max_loop_depth": wf.MAX_LOOP_DEPTH,
            "step_output_bytes": wf.DEFAULT_MAX_STEP_OUTPUT_BYTES,
        },
        "run": {
            "max_requests": run_limits.MAX_REQUESTS,
            "max_tool_calls": run_limits.MAX_TOOL_CALLS,
            "max_tokens": run_limits.MAX_TOKENS,
            "max_cost_usd": str(run_limits.MAX_COST_USD),
        },
        "architect": {"max_branches": arch.MAX_BRANCHES, "max_agent_tools": arch.MAX_AGENT_TOOLS},
        "agent_tools": {
            "max_per_agent": at.MAX_AGENT_TOOLS_PER_AGENT,
            "default_max_calls_per_run": at.DEFAULT_MAX_CALLS_PER_RUN,
            "max_calls_per_run": at.MAX_CALLS_PER_RUN_LIMIT,
            "max_timeout_seconds": at.MAX_TIMEOUT_SECONDS,
        },
        "http": {
            "max_response_bytes": tool_mod.HTTP_MAX_RESPONSE_BYTES_CAP,
            "max_reduced_response_bytes": tool_mod.HTTP_MAX_REDUCED_RESPONSE_BYTES_CAP,
        },
    }
    return {
        "limits": limits,
        "tools": [
            {
                "id": e.id.removeprefix("tool."), "scope": e.scope, "side_effect": e.side_effect,
                "requires_approval": e.requires_approval, "writable_by_architect": e.writable_by_architect,
            }
            for e in sorted(entries, key=lambda e: e.id) if e.kind == "tool"
        ],
        "architect_gated_tools": sorted(arch.ARCHITECT_GATED_TOOL_IDS),
        "denied_tool_prefixes": sorted(arch.ARCHITECT_DENIED_TOOL_PREFIXES),
        "step_kinds": {"platform": list(platform_step_kinds()), "architect_writable": list(architect_step_kinds())},
        "triggers": [{"id": e.id, "status": e.status} for e in sorted(entries, key=lambda e: e.id) if e.kind == "trigger"],
        "providers": sorted(set(providers)),
        "known_gaps": [
            {
                "id": g.id, "status": g.status, "designed_in": g.designed_in,
                "entry_ids": list(g.entry_ids), "alternatives": list(g.alternatives),
            }
            for g in known_gaps(gaps)
        ],
        "context_budget": dict(CONTEXT_BUDGET_TOKENS),
    }


def generated_yaml(generated: Mapping[str, Any]) -> str:
    """The block's text: the marker lines and the generated mapping indented under `generated:`."""
    body = yaml.safe_dump(dict(generated), sort_keys=True, default_flow_style=False, allow_unicode=True, width=10_000)
    indented = "".join(f"  {line}" if line.strip() else line for line in body.splitlines(keepends=True))
    return f"  {BEGIN_MARKER}\n{indented}  {END_MARKER}\n"


def replace_generated(text: str, generated: Mapping[str, Any]) -> str:
    """`text` with the region between the markers replaced by the freshly generated block."""
    begin, end = text.find(BEGIN_MARKER), text.find(END_MARKER)
    if begin < 0 or end < begin:
        raise ValueError("the manifest has no generated block markers")
    start = text.rfind("\n", 0, begin) + 1  # from the start of the BEGIN line ...
    stop = text.find("\n", end)  # ... to the end of the END line
    stop = len(text) if stop < 0 else stop + 1
    return text[:start] + generated_yaml(generated) + text[stop:]


# ----------------------------------------------------------------------------------------- loading


@dataclass(frozen=True)
class LoadedManifest:
    manifest: SystemManifest
    manifest_hash: str  # sha256 of the canonical JSON of the whole file, generated block included
    handwritten_hash: str  # the same without `generated` (what CI check 4 compares across commits)

    @property
    def manifest_version(self) -> int:
        return self.manifest.manifest_version


def _hash(data: Any) -> str:
    return hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()


def parse_manifest(text: str) -> LoadedManifest:
    raw = yaml.safe_load(text)
    if not isinstance(raw, dict):
        raise ValueError("the system manifest is not a mapping")
    manifest = SystemManifest.model_validate(raw)
    if manifest.manifest_version > SUPPORTED_MANIFEST_VERSION:
        raise ValueError(f"manifest_version {manifest.manifest_version} is newer than this code understands")
    dumped = manifest.model_dump(mode="json")
    return LoadedManifest(
        manifest=manifest,
        manifest_hash=_hash(dumped),
        handwritten_hash=_hash({k: v for k, v in dumped.items() if k not in ("generated", "manifest_version")}),
    )


def repo_manifest_path() -> Path:
    """The manifest in a source checkout (`docs/platform/...` at the repository root)."""
    return Path(__file__).resolve().parents[4] / MANIFEST_RELATIVE_PATH


def default_manifest_path(explicit: str | os.PathLike[str] | None = None) -> Path:
    """`explicit`, else `$EAP_SYSTEM_MANIFEST_PATH` (the image's packaged copy), else the checkout's file."""
    if explicit:
        return Path(explicit)
    configured = os.environ.get(MANIFEST_PATH_ENV)
    return Path(configured) if configured else repo_manifest_path()


def load_manifest(path: str | os.PathLike[str] | None = None) -> LoadedManifest:
    return parse_manifest(default_manifest_path(path).read_text(encoding="utf-8"))


# -------------------------------------------------------------------------------------- the checks


def handwritten_strings(manifest: SystemManifest) -> Iterator[tuple[str, str]]:
    """(where, text) for every hand-written free-text field."""
    yield "platform.purpose", manifest.platform.purpose
    for c in manifest.components:
        yield f"components.{c.id}.role", c.role
    for i, r in enumerate(manifest.relationships):
        yield f"relationships[{i}].via", r.via
    for i, rule in enumerate(manifest.lifecycle.rules):
        yield f"lifecycle.rules[{i}]", rule
    for p in manifest.patterns:
        yield f"patterns.{p.id}.summary", p.summary


def _scalars(node: Any) -> Iterator[Any]:
    if isinstance(node, Mapping):
        for v in node.values():
            yield from _scalars(v)
    elif isinstance(node, (list, tuple)):
        for v in node:
            yield from _scalars(v)
    else:
        yield node


def generated_numbers(generated: Generated) -> set[str]:
    """The generated numbers a hand-written string must not repeat: limits and budgets as written, as thousands,
    and as KiB or MiB when they divide evenly (`32 KiB` for 32768)."""
    numbers: set[int] = set()
    for scalar in _scalars({"limits": generated.limits, "budget": generated.context_budget}):
        text = str(scalar)
        if text.isdigit() and not isinstance(scalar, bool):
            numbers.add(int(text))
    out: set[str] = set()
    for n in numbers:
        for candidate in (n, n // 1024 if n % 1024 == 0 else None, n // 1_048_576 if n % 1_048_576 == 0 else None):
            if candidate is not None and candidate >= MIN_GUARDED_NUMBER:
                out.add(str(candidate))
    return out


def generated_ids(generated: Generated) -> set[str]:
    """Dotted ids a hand-written string must not name (tool, trigger and gap ids): `patterns[].needs` is where
    a hand-written section refers to capabilities."""
    ids = {t.id for t in generated.tools if "." in t.id}
    ids.update(t.id for t in generated.triggers)
    ids.update(g.id for g in generated.known_gaps)
    return ids


def handwritten_problems(manifest: SystemManifest) -> list[str]:
    """Hand-written strings that repeat a generated number or id (CI check 2)."""
    numbers, ids = generated_numbers(manifest.generated), generated_ids(manifest.generated)
    problems: list[str] = []
    for where, text in handwritten_strings(manifest):
        bare = _PLACEHOLDER.sub(" ", text)
        for match in _NUMBER.finditer(bare):
            if match.group(0).replace(",", "") in numbers:
                problems.append(f"{where}: repeats the generated number {match.group(0)!r}; write a {{{{limits...}}}} placeholder")
        for ident in sorted(ids):
            if re.search(rf"(?<![\w.]){re.escape(ident)}(?![\w.])", bare):
                problems.append(f"{where}: names the generated id {ident!r}; list it under a pattern's needs")
        try:
            manifest.resolve(text)
        except ValueError as exc:
            problems.append(f"{where}: {exc}")
    return problems


def registry_problems(
    manifest: SystemManifest, entry_ids: Iterable[str], gaps: tuple[GapDecl, ...], path_exists: Callable[[str], bool]
) -> list[str]:
    """`patterns[].needs` and the declared gaps must resolve (CI check 1)."""
    known = set(entry_ids)
    problems: list[str] = []
    for p in manifest.patterns:
        problems.extend(f"{p.id}: needs {need!r}, which is not in the capability registry" for need in p.needs if need not in known)
    for gap in manifest.generated.known_gaps:
        problems.extend(f"{gap.id}: entry {i!r} is not in the capability registry" for i in gap.entry_ids if i not in known)
    for decl in gaps:
        if decl.status == "designed_not_built":
            problem = designed_in_problem(decl.designed_in, path_exists)
            if problem:
                problems.append(f"{decl.id}: {problem}")
    return problems


def render_platform_layer(manifest: SystemManifest) -> str:
    """The Platform layer of the block (purpose, components, relationships, lifecycle one-liners): fixed text,
    generated numbers substituted. Phase C embeds it; CI checks it stays within `context_budget.platform`."""
    lines = [manifest.resolve(manifest.platform.purpose), "Components:"]
    lines += [f"- {c.id}: {manifest.resolve(c.role)}" for c in manifest.components]
    lines += ["Relationships:"]
    lines += [f"- {r.source} -> {r.target}: {manifest.resolve(r.via)}" for r in manifest.relationships]
    lines += [f"Agent lifecycle: {', '.join(manifest.lifecycle.agent)}", f"Workflow lifecycle: {', '.join(manifest.lifecycle.workflow)}"]
    lines += [f"- {manifest.resolve(rule)}" for rule in manifest.lifecycle.rules]
    return "\n".join(lines)


def platform_layer_over_budget(manifest: SystemManifest) -> str | None:
    used, budget = estimate_tokens(render_platform_layer(manifest)), manifest.generated.context_budget["platform"]
    return None if used <= budget else f"the Platform layer is about {used} tokens, over its budget of {budget}"


# ---------------------------------------------------------------------------------------- version


def _project_version() -> str:
    for name in ("pydanticos", "eap-schemas"):
        try:
            return metadata.version(name)
        except metadata.PackageNotFoundError:
            continue
    return "0"


def platform_version(project_version: str | None = None, build_sha: str | None = None) -> str:
    """`<project version>+<build sha>`: the sha comes from `EAP_BUILD_SHA` (an image build), `dev` locally."""
    sha = build_sha if build_sha is not None else os.environ.get(BUILD_SHA_ENV, "")
    sha = re.sub(r"[^0-9A-Za-z]", "", sha)[:40] or "dev"
    return f"{project_version or _project_version()}+{sha}"


__all__ = [
    "BEGIN_MARKER",
    "BUILD_SHA_ENV",
    "END_MARKER",
    "MANIFEST_PATH_ENV",
    "MANIFEST_RELATIVE_PATH",
    "Component",
    "Generated",
    "LoadedManifest",
    "Pattern",
    "Platform",
    "Relationship",
    "SystemManifest",
    "default_manifest_path",
    "generate",
    "generated_yaml",
    "handwritten_problems",
    "load_manifest",
    "parse_manifest",
    "platform_layer_over_budget",
    "platform_version",
    "registry_problems",
    "render_platform_layer",
    "replace_generated",
]
