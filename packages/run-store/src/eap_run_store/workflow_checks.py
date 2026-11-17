"""Workflow publish-time validation, shared by the API's publish route and the Architect's gated
tools (one implementation; see eap_run_store.definitions).

Moved from eap_api.routes.workflows with the module docstring's rules intact (what a spec may point
at, refs into structured agent output, loop caps, publish warnings). Functions that read the
database take a SQLAlchemy `Connection`.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import Any

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.engine import Connection

from eap_run_store.agent_checks import WARNING_COST_CAP_UNPRICED, cost_cap_zero_reason
from eap_schemas import AgentSpec
from eap_schemas.models_policy import ModelPolicy
from eap_schemas.tools import TOOL_DECLARATIONS
from eap_schemas.workflow_scope import LOOP_ERROR_MESSAGES
from eap_schemas.workflows import (
    LOOP_STEP_TYPES,
    MAX_FOREACH_CONCURRENCY,
    MAX_LOOP_DEPTH,
    MAX_LOOP_ITERATIONS,
    REF_KEY,
    REF_PATTERN,
    AgentStep,
    EvaluateStep,
    ForeachStep,
    Step,
    ToolStep,
    WorkflowSpec,
    iter_refs,
    loop_exit_refs,
    run_concurrency,
    static_execution_bound,
    walk_steps,
)


def allowed_tool_ids() -> frozenset[str] | None:
    """The optional platform tool allowlist (EAP_ALLOWED_TOOLS), as the worker reads it."""
    configured = os.environ.get("EAP_ALLOWED_TOOLS")
    if configured is None:
        return None
    return frozenset(tool_id.strip() for tool_id in configured.split(",") if tool_id.strip())


# Environment caps of loop specs (LP.5, D7a, D10): (variable, default). Read at every publish and
# run start. Unset or blank means the default; any other value must be ASCII digits within the
# variable's bounds, else ValueError naming the variable. create_app calls
# check_workflow_configuration, so a malformed value fails startup rather than the first publish.
LOOP_ITERATIONS_ENV = ("EAP_WORKFLOW_MAX_LOOP_ITERATIONS", 100)
STEP_EXECUTIONS_ENV = ("EAP_WORKFLOW_MAX_STEP_EXECUTIONS", 2000)
RUN_CONCURRENCY_ENV = ("EAP_WORKFLOW_MAX_RUN_CONCURRENCY", 8)
# Inclusive bounds: the schema's loop ceiling; a generous execution ceiling; the largest product of
# foreach concurrency the schema allows (8 per foreach, nested 2 deep).
CAP_BOUNDS: dict[str, tuple[int, int]] = {
    LOOP_ITERATIONS_ENV[0]: (1, MAX_LOOP_ITERATIONS),
    STEP_EXECUTIONS_ENV[0]: (1, 1_000_000),
    RUN_CONCURRENCY_ENV[0]: (1, MAX_FOREACH_CONCURRENCY**MAX_LOOP_DEPTH),
}


def _env_cap(setting: tuple[str, int]) -> int:
    name, default = setting
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    low, high = CAP_BOUNDS[name]
    # str.isdigit alone accepts non-ASCII digits (e.g. "٣"); int() would accept signs, "_" and spaces.
    if not (raw.isascii() and raw.isdigit()) or not low <= int(raw) <= high:
        raise ValueError(f"{name} must be a whole number from {low} to {high}, got {raw!r}")
    return int(raw)


def check_workflow_configuration() -> None:
    """Fail startup on a malformed EAP_WORKFLOW_MAX_LOOP_ITERATIONS, EAP_WORKFLOW_MAX_STEP_EXECUTIONS
    or EAP_WORKFLOW_MAX_RUN_CONCURRENCY (ValueError), not at the first publish or run start."""
    for setting in (LOOP_ITERATIONS_ENV, STEP_EXECUTIONS_ENV, RUN_CONCURRENCY_ENV):
        _env_cap(setting)


def loop_limit_problems(spec: WorkflowSpec) -> list[str]:
    """The platform's loop caps (LP.5): each loop's bound, the static execution bound and the run
    concurrency, one message per problem. Empty for a spec without loops."""
    loops = [step for _, step in walk_steps(spec.steps) if isinstance(step, LOOP_STEP_TYPES)]
    if not loops:
        return []
    problems: list[str] = []
    iterations_cap = _env_cap(LOOP_ITERATIONS_ENV)
    for step in loops:
        field, bound = (
            ("max_items", step.max_items) if isinstance(step, ForeachStep) else ("max_iterations", step.max_iterations)
        )
        if bound > iterations_cap:
            problems.append(
                f"step '{step.id}': {field} {bound} is above this platform's loop limit of {iterations_cap} "
                f"({LOOP_ITERATIONS_ENV[0]})"
            )
    executions, executions_cap = static_execution_bound(spec), _env_cap(STEP_EXECUTIONS_ENV)
    if executions > executions_cap:
        problems.append(
            f"workflow_too_many_executions: {LOOP_ERROR_MESSAGES['workflow_too_many_executions']} "
            f"(up to {executions} step executions, the limit is {executions_cap}, {STEP_EXECUTIONS_ENV[0]})"
        )
    concurrency, concurrency_cap = run_concurrency(spec), _env_cap(RUN_CONCURRENCY_ENV)
    if concurrency > concurrency_cap:
        problems.append(
            f"workflow_concurrency_exceeded: {LOOP_ERROR_MESSAGES['workflow_concurrency_exceeded']} "
            f"(up to {concurrency} iterations at once, the limit is {concurrency_cap}, {RUN_CONCURRENCY_ENV[0]})"
        )
    return problems


def judge_model_problems(spec: WorkflowSpec) -> list[str]:
    """Every `llm_judge` evaluator of an evaluate step (at any depth) names an allowlisted model
    (EAP_ALLOWED_MODELS), as an eval suite's judge must; the worker re-checks at call time."""
    policy: ModelPolicy | None = None
    problems: list[str] = []
    for _, step in walk_steps(spec.steps):
        if not isinstance(step, EvaluateStep):
            continue
        for i, evaluator in enumerate(step.evaluators):
            if evaluator.kind != "llm_judge":
                continue
            policy = policy or ModelPolicy.from_env()
            if not policy.allows(evaluator.model.provider, evaluator.model.name):
                problems.append(
                    f"step '{step.id}': evaluators.{i} (llm_judge): model "
                    f"'{evaluator.model.provider}:{evaluator.model.name}' is not allowed on this platform "
                    "(not in the EAP_ALLOWED_MODELS allowlist)"
                )
    return problems


def reference_problems(conn: Connection, spec: WorkflowSpec, tenant_id: str, *, lock: bool = False) -> list[str]:
    """What a spec points at that this tenant cannot run, one message per problem, naming the step:
    agent and tool steps at every depth (loop bodies included), judge models, and the loop caps.

    Another tenant's agent reads exactly like a missing one, so nothing about it leaks. With `lock`
    (publish), the referenced agent rows (at every depth) are first locked FOR SHARE until the
    transaction ends, in id order (no deadlock between two publishes): an archive (FOR NO KEY
    UPDATE) of one of them waits for the publish, whose step then shows in the archive's
    referencing workflows, or this check sees `archived`."""
    if lock:
        agent_ids = sorted({str(step.agent_id) for _, step in walk_steps(spec.steps) if isinstance(step, AgentStep)})
        if agent_ids:
            conn.execute(
                text("SELECT id FROM agents WHERE id = ANY(:ids) AND tenant_id = :t ORDER BY id FOR SHARE"),
                {"ids": agent_ids, "t": tenant_id},
            ).all()
    problems: list[str] = loop_limit_problems(spec)
    allowed_tools = allowed_tool_ids()
    for _, step in walk_steps(spec.steps):
        if isinstance(step, AgentStep):
            agent_id = str(step.agent_id)
            agent = conn.execute(
                text("SELECT archived FROM agents WHERE id = :id AND tenant_id = :t"), {"id": agent_id, "t": tenant_id}
            ).first()
            if agent is None or agent.archived:
                problems.append(f"step '{step.id}': agent '{agent_id}' was not found")
                continue
            if step.version is None:
                has_version = conn.execute(
                    text("SELECT 1 FROM agent_versions WHERE agent_id = :id AND tenant_id = :t LIMIT 1"),
                    {"id": agent_id, "t": tenant_id},
                ).first()
                if has_version is None:
                    problems.append(f"step '{step.id}': agent '{agent_id}' has no published version")
            elif conn.execute(
                text("SELECT 1 FROM agent_versions WHERE agent_id = :id AND tenant_id = :t AND version = :v"),
                {"id": agent_id, "t": tenant_id, "v": step.version},
            ).first() is None:
                problems.append(f"step '{step.id}': agent '{agent_id}' has no published version {step.version}")
        elif isinstance(step, ToolStep):
            if step.tool_id not in TOOL_DECLARATIONS:
                problems.append(f"step '{step.id}': tool '{step.tool_id}' does not exist")
            elif allowed_tools is not None and step.tool_id not in allowed_tools:
                problems.append(
                    f"step '{step.id}': tool '{step.tool_id}' is not allowed on this platform "
                    "(not in the EAP_ALLOWED_TOOLS allowlist)"
                )
    problems += judge_model_problems(spec)
    return problems


# Publish warnings for refs into agent output (AC.9), {code, path, message} like the agent publish
# warnings; `path` is `steps[<index>]`, the message names the step and the ref.
WARNING_UNPINNED_OUTPUT_REF = "unpinned_agent_output_ref"
WARNING_TEXT_OUTPUT_REF = "text_agent_output_ref"
WARNING_UNLISTED_OUTPUT_FIELD = "unlisted_output_field"


def _schema_types(node: dict[str, Any]) -> set[str] | None:
    """The node's `type` as a set, None when it declares none (anything goes)."""
    declared = node.get("type")
    if declared is None:
        return None
    return {declared} if isinstance(declared, str) else {t for t in declared if isinstance(t, str)}


@dataclass(frozen=True)
class RefProblem:
    """Why a path cannot (blocking) or may not (not blocking) exist in a schema's object."""

    blocking: bool
    message: str


def output_ref_problem(schema: dict[str, Any], segments: list[str]) -> RefProblem | None:
    """Why `segments` (the path after `steps.<id>.output`) may not exist in an object of `schema`,
    or None when the schema lists it. Walks `properties` along the path; a numeric segment steps
    into an array's `items`. It follows the schema as the runtime validates it (Draft 2020-12, where
    a missing `additionalProperties` means true):
    - blocking: the path cannot exist (into a scalar, a named field of an array, or a field not in
      `properties` of an object with `additionalProperties: false`);
    - not blocking: the field is not listed, but the object allows other keys, so the agent may
      still return it (`additionalProperties` absent or true);
    - None: listed all the way, or below a sub-schema that constrains nothing (`true`, or an object
      without `properties`), where any path may exist."""
    node: Any = schema
    walked: list[str] = []
    for seg in segments:
        if not isinstance(node, dict):
            return None  # `true`/missing sub-schema: unconstrained
        types = _schema_types(node)
        if types is not None:
            types = types - {"null"} or {"null"}  # a nullable value is checked as its other type
        where = ".".join(walked) or "the output"
        props = node.get("properties")
        if isinstance(props, dict) and seg in props:
            node = props[seg]
        elif seg.isdigit() and (types is None or "array" in types) and ("items" in node or types == {"array"}):
            node = node.get("items", True)
        elif types is not None and not types & {"object", "array"}:
            kind = "/".join(sorted(types))
            return RefProblem(True, f"'{seg}' does not exist: {where} is a {kind}")
        elif types == {"array"}:
            return RefProblem(True, f"'{seg}' does not exist: {where} is an array (use a numeric index)")
        elif not isinstance(props, dict) and node.get("additionalProperties") is not False:
            return None  # an object that names no fields: anything may be there
        else:
            known = ", ".join(sorted(props)) if isinstance(props, dict) and props else "none"
            if node.get("additionalProperties") is False:
                return RefProblem(True, f"'{seg}' is not a field of {where} (fields: {known})")
            return RefProblem(False, (
                f"'{seg}' is not a listed field of {where} (fields: {known}); the schema allows other "
                "fields, so it resolves only if the agent returns it"
            ))
        walked.append(seg)
    return None


def output_schema_of(spec: dict[str, Any] | None) -> dict[str, Any] | None:
    """A stored agent version spec's structured output JSON Schema, None for a text agent."""
    output = (spec or {}).get("output")
    if isinstance(output, dict) and isinstance(output.get("json_schema"), dict):
        return output["json_schema"]
    return None


def output_fields(schema: dict[str, Any]) -> list[dict[str, Any]]:
    """Every field path of `schema` that a `$ref` can read (`steps.<id>.output.<path>`), depth first;
    an array's items appear under the index `0`. Each entry is {path, type, description?}."""
    fields: list[dict[str, Any]] = []

    def visit(node: Any, prefix: str) -> None:
        if not isinstance(node, dict):
            return
        props = node.get("properties")
        if isinstance(props, dict):
            for name, sub in props.items():
                path = f"{prefix}.{name}" if prefix else name
                entry: dict[str, Any] = {"path": path, "type": sub.get("type") if isinstance(sub, dict) else None}
                if isinstance(sub, dict) and isinstance(sub.get("description"), str):
                    entry["description"] = sub["description"]
                fields.append(entry)
                visit(sub, path)
        items = node.get("items")
        if isinstance(items, dict) and prefix:
            path = f"{prefix}.0"
            fields.append({"path": path, "type": items.get("type")})
            visit(items, path)

    visit(schema, "")
    return fields


def _agent_version(conn: Connection, tenant_id: str, agent_id: str, version: int | None) -> SimpleNamespace | None:
    """The agent's pinned version, or its latest one for `version=None` (None when there is none):
    an object with `.version` and `.spec`."""
    sql = "SELECT version, spec FROM agent_versions WHERE agent_id = :id AND tenant_id = :t"
    params: dict[str, Any] = {"id": agent_id, "t": tenant_id}
    if version is not None:
        sql += " AND version = :v"
        params["v"] = version
    else:
        sql += " ORDER BY version DESC"
    row = conn.execute(text(sql + " LIMIT 1"), params).first()
    return None if row is None else SimpleNamespace(version=row.version, spec=row.spec)


WARNING_LOOP_AGENT_APPROVALS = "loop_agent_approvals_denied"


def _walk_with_paths(
    steps: tuple[Step, ...], prefix: str = "steps"
) -> Iterator[tuple[str, tuple[str, ...], Step]]:
    """Every step at every depth, depth first in spec order: (warning path, enclosing loop ids, step).
    A top-level step's path is `steps[<i>]` (AC.9's shape); a body step's is
    `steps[<i>].body.steps[<j>]`."""

    def walk(steps: tuple[Step, ...], prefix: str, loops: tuple[str, ...]):
        for i, step in enumerate(steps):
            where = f"{prefix}[{i}]"
            yield where, loops, step
            if isinstance(step, LOOP_STEP_TYPES):
                yield from walk(step.body.steps, f"{where}.body.steps", (*loops, step.id))

    return walk(steps, prefix, ())


def _ref_segments(path: str) -> tuple[str | None, str | None, str | None, list[str]]:
    """(step id, loop id, loop root, segments after the root) of a valid ref path."""
    match = REF_PATTERN.fullmatch(path)
    if match is None:
        return None, None, None, []
    rest = [seg for seg in (match.group("rest") or "").split(".") if seg]
    return match.group("step"), match.group("loop"), match.group("loop_root"), rest


def output_ref_checks(
    conn: Connection, spec: WorkflowSpec, tenant_id: str
) -> tuple[list[str], list[dict[str, str]]]:
    """(problems, warnings) for `$ref`s into agent step outputs (see the module docstring), at every
    depth. A `loop.<L>.item.<path>` ref of a foreach whose `items` lists an agent step's output (or
    an enclosing foreach's item) is checked as `<items path>.0.<path>` in that agent's schema. Steps
    whose agent or version does not resolve are left to reference_problems."""
    walked = list(_walk_with_paths(spec.steps))
    agent_steps = {s.id: s for _, _, s in walked if isinstance(s, AgentStep)}
    foreach_steps = {s.id: s for _, _, s in walked if isinstance(s, ForeachStep)}
    schemas: dict[str, tuple[bool, dict[str, Any] | None] | None] = {}

    def target(step_id: str) -> tuple[bool, dict[str, Any] | None] | None:
        """(pinned, output schema) of agent step `step_id`; None when its version does not resolve."""
        if step_id not in schemas:
            step = agent_steps[step_id]
            row = _agent_version(conn, tenant_id, str(step.agent_id), step.version)
            schemas[step_id] = None if row is None else (step.version is not None, output_schema_of(row.spec))
        return schemas[step_id]

    def resolve(path: str, hops: int = 0) -> tuple[str, list[str], str, list[tuple[int, str, str]]] | None:
        """(agent step id, segments in its output, via note, item steps) a field ref reads, None when
        it does not read an agent step's output. Follows `loop.<L>.item` through the foreach's
        `items` ref, which adds the segment `0` (an array's items); each item step is (index of
        that `0` in segments, foreach id, items ref), outermost first."""
        step_id, loop_id, loop_root, rest = _ref_segments(path)
        if step_id is not None:
            return (step_id, rest, "", []) if step_id in agent_steps else None
        if loop_root != "item" or loop_id not in foreach_steps or hops > len(foreach_steps):
            return None
        items_ref = foreach_steps[loop_id].items[REF_KEY]
        base = resolve(items_ref, hops + 1)
        if base is None:
            return None
        agent, prefix, _, item_steps = base
        return (
            agent,
            [*prefix, "0", *rest],
            f" (an item of foreach '{loop_id}' over '{items_ref}')",
            [*item_steps, (len(prefix), loop_id, items_ref)],
        )

    def items_problem(schema: dict[str, Any], segments: list[str], item_steps: list[tuple[int, str, str]]):
        """(skip, problem) for the foreach lists a `loop.<L>.item` ref goes through: skip when an
        `items` path itself cannot exist (its own ref reports that); a blocking problem, worded
        without the internal `0` segment, when the listed value cannot be a list."""
        for at, loop_id, items_ref in item_steps:
            if (outer := output_ref_problem(schema, segments[:at])) is not None and outer.blocking:
                return True, None
            inner = output_ref_problem(schema, segments[: at + 1])
            if inner is not None and inner.blocking:
                return False, RefProblem(True, f"the items of foreach '{loop_id}' ('{items_ref}') are not a list")
        return False, None

    problems: list[str] = []
    warnings: list[dict[str, str]] = []
    for where, _, step in walked:
        for path in dict.fromkeys(iter_refs(step) + loop_exit_refs(step)):
            if not _ref_segments(path)[3]:
                continue  # the whole output (or the whole item)
            resolved = resolve(path)
            if resolved is None:
                continue
            ref_step, segments, via, item_steps = resolved
            found = target(ref_step)
            if found is None:
                continue
            pinned, schema = found
            if schema is None:
                version_note = "" if pinned else " (not pinned: its latest version)"
                warnings.append({
                    "code": WARNING_TEXT_OUTPUT_REF if pinned else WARNING_UNPINNED_OUTPUT_REF,
                    "path": where,
                    "message": (
                        f"step '{step.id}': $ref '{path}'{via} reads a field, but agent step '{ref_step}'"
                        f"{version_note} outputs text (no structured output), so it does not resolve"
                    ),
                })
                continue
            skip, problem = items_problem(schema, segments, item_steps)
            if skip:
                continue
            problem = problem or output_ref_problem(schema, segments)
            if not pinned:
                # Always: the version is resolved when a run reaches the step, so a later agent
                # version can rename the field (the branch then logs `unresolved_refs`).
                detail = (
                    f"it does not match the latest version's output schema: {problem.message}"
                    if problem is not None else "it matches the latest version's output schema today"
                )
                warnings.append({
                    "code": WARNING_UNPINNED_OUTPUT_REF,
                    "path": where,
                    "message": (
                        f"step '{step.id}': $ref '{path}'{via} reads the output of agent step '{ref_step}', "
                        f"which is not pinned to a version; {detail}. Pin the version to check it at publish"
                    ),
                })
            elif problem is None:
                continue
            elif problem.blocking:
                problems.append(
                    f"step '{step.id}': $ref '{path}'{via} does not match the output schema of agent step "
                    f"'{ref_step}': {problem.message}"
                )
            else:
                warnings.append({
                    "code": WARNING_UNLISTED_OUTPUT_FIELD,
                    "path": where,
                    "message": f"step '{step.id}': $ref '{path}'{via} into agent step '{ref_step}': {problem.message}",
                })
    return problems, warnings


def loop_agent_approval_warnings(conn: Connection, spec: WorkflowSpec, tenant_id: str) -> list[dict[str, str]]:
    """D8a: a pinned agent step inside a loop body whose version has an approval policy runs with
    tool approvals denied, so publish warns (not 422: an unpinned agent can gain a policy after
    publish, so a publish-time refusal could not be complete). Unresolved versions are left to
    reference_problems."""
    warnings: list[dict[str, str]] = []
    for where, loops, step in _walk_with_paths(spec.steps):
        if not loops or not isinstance(step, AgentStep) or step.version is None:
            continue
        row = _agent_version(conn, tenant_id, str(step.agent_id), step.version)
        approvals = (row.spec or {}).get("approvals") if row is not None else None
        if not isinstance(approvals, dict) or not approvals.get("tools"):
            continue
        tools = ", ".join(f"'{t}'" for t in approvals["tools"])
        warnings.append({
            "code": WARNING_LOOP_AGENT_APPROVALS,
            "path": where,
            "message": (
                f"step '{step.id}': agent '{step.agent_id}' version {step.version} requires approval for "
                f"{tools}, but tool approvals are not available inside loops (loop '{loops[-1]}'), so "
                "those tool calls are denied and the model reads the denial"
            ),
        })
    return warnings


def judge_cost_cap_warnings(spec: WorkflowSpec, today: date | None = None) -> list[dict[str, str]]:
    """With `limits.max_cost_usd`, one `cost_cap_unpriced_model` warning per `llm_judge` evaluator
    (at any depth) whose model's requests count as $0 toward the cap (no price, a $0 price, or no
    usable price file): the run tree's cost then misses those judge calls, so the cap may not stop
    the run. The lookup and the reason are the agent publish warning's (cost_cap_zero_reason).
    `path` is the step's path plus `.evaluators[<k>]`. A warning, never a 422."""
    if spec.limits is None or spec.limits.max_cost_usd is None:
        return []
    day = today or datetime.now(timezone.utc).date()
    warnings: list[dict[str, str]] = []
    for where, _, step in _walk_with_paths(spec.steps):
        if not isinstance(step, EvaluateStep):
            continue
        for k, evaluator in enumerate(step.evaluators):
            if evaluator.kind != "llm_judge":
                continue
            reason = cost_cap_zero_reason(evaluator.model.provider, evaluator.model.name, day)
            if reason is None:
                continue
            warnings.append({
                "code": WARNING_COST_CAP_UNPRICED,
                "path": f"{where}.evaluators[{k}]",
                "message": (
                    f"step '{step.id}': evaluators.{k} (llm_judge): {reason} toward limits.max_cost_usd, "
                    "so the cost limit may not stop the run"
                ),
            })
    return warnings


def agent_step_cost_cap_warnings(conn: Connection, spec: WorkflowSpec, tenant_id: str,
                                 today: date | None = None) -> list[dict[str, str]]:
    """With `limits.max_cost_usd`, one `cost_cap_unpriced_model` warning per model of an agent
    step's agent (primary, then fallbacks; the pinned version, else the latest one, at any depth)
    whose requests count as $0: a child agent run's cost counts in the run tree's cost, so a $0
    model escapes the workflow's cap the same way an unpriced judge does. The reason is the agent
    publish warning's (cost_cap_zero_reason). `path` is the step's path. Unresolved agents and
    unreadable agent specs are left to reference_problems. A warning, never a 422."""
    if spec.limits is None or spec.limits.max_cost_usd is None:
        return []
    day = today or datetime.now(timezone.utc).date()
    warnings: list[dict[str, str]] = []
    for where, _, step in _walk_with_paths(spec.steps):
        if not isinstance(step, AgentStep):
            continue
        row = _agent_version(conn, tenant_id, str(step.agent_id), step.version)
        if row is None:
            continue
        try:
            agent = AgentSpec.model_validate(row.spec or {})
        except ValidationError:
            continue
        chain = [("model", agent.model)] + [(f"fallback_models[{i}]", m) for i, m in enumerate(agent.fallback_models)]
        for chain_path, model in chain:
            reason = cost_cap_zero_reason(model.provider, model.name, day)
            if reason is None:
                continue
            warnings.append({
                "code": WARNING_COST_CAP_UNPRICED,
                "path": where,
                "message": (
                    f"step '{step.id}': agent '{step.agent_id}' version {row.version} {chain_path}: {reason} "
                    "toward limits.max_cost_usd, so the cost limit may not stop the run"
                ),
            })
    return warnings

