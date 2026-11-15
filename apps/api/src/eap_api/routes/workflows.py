"""Workflow definitions API: drafts and immutable published versions, mirroring /agents.

Publish re-validates the draft and checks what the spec points at: every agent step's agent must
belong to this tenant and have a published version (the pinned one, if pinned); every tool step's
tool must exist in the platform catalog and be allowed by EAP_ALLOWED_TOOLS.

Structured agent output (AC.9): an agent step whose agent version has `output.json_schema` outputs
that object, so `steps.<id>.output.<path>` reads its fields. Publish checks every such field `$ref`
(in input templates and branch conditions; a whole-output ref is not checked) against the schema,
following it as the runtime validates it (output_ref_problem):
- pinned step, a path that cannot exist (into a scalar, a named field of an array, or a field an
  `additionalProperties: false` object does not list) -> 422;
- pinned step, a field not listed by an object that allows other fields (`additionalProperties`
  absent or true) -> warning `unlisted_output_field`;
- pinned step whose version outputs text -> warning `text_agent_output_ref`;
- unpinned step (`version: null`) -> ALWAYS a warning `unpinned_agent_output_ref`, saying whether
  the latest version matches today: the version is resolved when a run reaches the step, so a
  later agent version can rename the field (at run time the branch's step_completed then lists the
  path in `unresolved_refs`).
Publish answers 201 with the version and `warnings` ([{code, path: "steps[<index>]", message}]). `GET /workflows/agent-output-schema` serves an
agent version's output schema and its field paths to API clients and editors.

Workflow loops (LP.5): every check above walks loop bodies at every depth, and a body step's
warning `path` is `steps[<i>].body.steps[<j>]`.
- Agent and tool steps inside bodies are checked like top-level ones (reference_problems).
- Every `llm_judge` evaluator of an `evaluate` step needs an allowlisted model (EAP_ALLOWED_MODELS).
- Environment caps (loop_limit_problems): each loop's `max_items`/`max_iterations` <=
  EAP_WORKFLOW_MAX_LOOP_ITERATIONS (100); `static_execution_bound` <= EAP_WORKFLOW_MAX_STEP_EXECUTIONS
  (2000, `workflow_too_many_executions`); `run_concurrency` <= EAP_WORKFLOW_MAX_RUN_CONCURRENCY (8,
  `workflow_concurrency_exceeded`). Checked at publish and run start; a draft save accepts them.
  A malformed value fails app startup (check_workflow_configuration).
- `loop.<L>.item.<path>` is checked like a field ref: the foreach's `items` ref is followed to the
  agent step output it lists, and the path is checked under that array's `items` schema.
- A pinned body agent whose version has an approval policy gives the warning
  `loop_agent_approvals_denied` (D8a): inside a loop its gated tool calls are denied.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from eap_api.auth import Principal, current_principal
from eap_api.database import get_session
from eap_api.models import AgentModel, AgentVersionModel
from eap_api.request_guard import guarded_json_route, schema_checks_storable
from eap_api.routes.tools import _allowed_tool_ids
from eap_api.schemas import ErrorDetail
from eap_api.workflows.models import WorkflowModel, WorkflowVersionModel
from eap_api.workflows.schemas import WorkflowCreate, WorkflowUpdate
from eap_schemas.models_policy import ModelPolicy
from eap_schemas.tools import TOOL_DECLARATIONS
from eap_schemas.workflow_scope import LOOP_ERROR_MESSAGES
from eap_schemas.workflows import (
    LOOP_STEP_TYPES,
    MAX_FOREACH_CONCURRENCY,
    MAX_LOOP_DEPTH,
    MAX_LOOP_ITERATIONS,
    MAX_SPEC_DEPTH,
    REF_KEY,
    REF_PATTERN,
    AgentStep,
    BranchStep,
    Condition,
    EvaluateStep,
    ForeachStep,
    Step,
    ToolStep,
    WorkflowSpec,
    canonical_workflow_hash,
    iter_refs,
    loop_exit_refs,
    run_concurrency,
    static_execution_bound,
    walk_steps,
)

# A request body is {"spec": <spec>}: one level above the spec's own nesting cap.
MAX_BODY_DEPTH = MAX_SPEC_DEPTH + 1
# The spec is capped at 64 KiB as JSON; the body adds the {"spec": ...} wrapper and whitespace.
MAX_BODY_BYTES = 128 * 1024

# The shared JSON write-route guards (eap_api.request_guard): more than MAX_BODY_BYTES -> 413; not
# strict UTF-8 (or any NUL byte) -> 415; nested deeper than MAX_BODY_DEPTH -> 422; compact 422s;
# unstorable strings (U+0000, lone surrogates) -> 422. Create and patch skip that last check
# (@schema_checks_storable): WorkflowSpec rejects them itself, with messages naming the step.
BoundedJsonRoute = guarded_json_route(
    max_body_bytes=MAX_BODY_BYTES,
    max_depth=MAX_BODY_DEPTH,
    depth_detail=f"workflow spec is nested more than {MAX_SPEC_DEPTH} levels deep",
)


router = APIRouter(prefix="/workflows", tags=["workflows"], route_class=BoundedJsonRoute)

SessionDep = Annotated[Session, Depends(get_session)]
PrincipalDep = Annotated[Principal, Depends(current_principal)]


def _get_workflow(db: Session, workflow_id: uuid.UUID, tenant_id: str) -> WorkflowModel:
    workflow = (
        db.query(WorkflowModel)
        .filter(WorkflowModel.id == str(workflow_id), WorkflowModel.tenant_id == tenant_id)
        .first()
    )
    if workflow is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found")
    return workflow


def _latest_version(db: Session, workflow: WorkflowModel) -> int | None:
    return (
        db.query(func.max(WorkflowVersionModel.version))
        .filter(
            WorkflowVersionModel.workflow_id == workflow.id,
            WorkflowVersionModel.tenant_id == workflow.tenant_id,
        )
        .scalar()
    )


def _workflow_response(workflow: WorkflowModel, latest_version: int | None) -> dict:
    return {
        "id": str(workflow.id),
        "name": workflow.name,
        "draft_spec": workflow.draft_spec,
        "latest_version": latest_version,
    }


def _version_response(version: WorkflowVersionModel) -> dict:
    return {
        "workflow_id": str(version.workflow_id),
        "version": version.version,
        "spec": version.spec,
        "spec_hash": version.spec_hash,
        "created_at": version.created_at.isoformat(),
        "created_by": version.created_by,
    }


LOOP_RUNTIME_PENDING = "is not available yet: the workflow loops runtime has not landed (LP.3/LP.7)"


def runtime_pending_problems(spec: WorkflowSpec) -> list[str]:
    """Spec features the schema accepts but the worker cannot run yet (workflow loops milestone,
    temporary gate until LP.3/LP.7 ship the runtime): foreach, loop and evaluate steps, all/any/not
    conditions and ``limits``. Refused at publish and at run start, so a run never reaches the
    worker with them. Drafts may still be saved with them."""
    problems: list[str] = []
    for _, step in walk_steps(spec.steps):
        if step.type in ("foreach", "loop", "evaluate"):
            problems.append(f"step '{step.id}': the {step.type} step {LOOP_RUNTIME_PENDING}")
        elif isinstance(step, BranchStep) and any(not isinstance(c.when, Condition) for c in step.cases):
            problems.append(f"step '{step.id}': an all/any/not condition {LOOP_RUNTIME_PENDING}")
    if spec.limits is not None and not spec.limits.is_empty():
        problems.append(f"limits {LOOP_RUNTIME_PENDING}")
    return problems


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


def reference_problems(db: Session, spec: WorkflowSpec, tenant_id: str, *, lock: bool = False) -> list[str]:
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
            (
                db.query(AgentModel.id)
                .filter(AgentModel.id.in_(agent_ids), AgentModel.tenant_id == tenant_id)
                .order_by(AgentModel.id)
                .with_for_update(read=True)
                .all()
            )
    problems: list[str] = runtime_pending_problems(spec)
    problems += loop_limit_problems(spec)
    allowed_tools = _allowed_tool_ids()
    for _, step in walk_steps(spec.steps):
        if isinstance(step, AgentStep):
            agent_id = str(step.agent_id)
            agent = (
                db.query(AgentModel)
                .filter(AgentModel.id == agent_id, AgentModel.tenant_id == tenant_id)
                .first()
            )
            if agent is None or agent.archived:
                problems.append(f"step '{step.id}': agent '{agent_id}' was not found")
                continue
            versions = db.query(AgentVersionModel.version).filter(
                AgentVersionModel.agent_id == agent_id, AgentVersionModel.tenant_id == tenant_id
            )
            if step.version is None:
                if versions.first() is None:
                    problems.append(f"step '{step.id}': agent '{agent_id}' has no published version")
            elif versions.filter(AgentVersionModel.version == step.version).first() is None:
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


def _agent_version(db: Session, tenant_id: str, agent_id: str, version: int | None) -> AgentVersionModel | None:
    """The agent's pinned version, or its latest one for `version=None` (None when there is none)."""
    query = db.query(AgentVersionModel).filter(
        AgentVersionModel.agent_id == agent_id, AgentVersionModel.tenant_id == tenant_id
    )
    if version is not None:
        return query.filter(AgentVersionModel.version == version).first()
    return query.order_by(AgentVersionModel.version.desc()).first()


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
    db: Session, spec: WorkflowSpec, tenant_id: str
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
            row = _agent_version(db, tenant_id, str(step.agent_id), step.version)
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


def loop_agent_approval_warnings(db: Session, spec: WorkflowSpec, tenant_id: str) -> list[dict[str, str]]:
    """D8a: a pinned agent step inside a loop body whose version has an approval policy runs with
    tool approvals denied, so publish warns (not 422: an unpinned agent can gain a policy after
    publish, so a publish-time refusal could not be complete). Unresolved versions are left to
    reference_problems."""
    warnings: list[dict[str, str]] = []
    for where, loops, step in _walk_with_paths(spec.steps):
        if not loops or not isinstance(step, AgentStep) or step.version is None:
            continue
        row = _agent_version(db, tenant_id, str(step.agent_id), step.version)
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


@router.get(
    "/agent-output-schema",
    response_class=JSONResponse,
    responses={404: {"model": ErrorDetail}},
)
def get_agent_output_schema(
    principal: PrincipalDep,
    db: SessionDep,
    agent_id: Annotated[uuid.UUID, Query()],
    version: Annotated[int | None, Query(ge=1)] = None,
) -> JSONResponse:
    """The output schema an agent step would read (AC.9): the pinned `version`, else the latest
    published one. `json_schema` is null for a text agent (its step output is a string), and
    `fields` lists the paths `steps.<id>.output.<path>` can read."""
    agent = (
        db.query(AgentModel)
        .filter(AgentModel.id == str(agent_id), AgentModel.tenant_id == principal.tenant_id)
        .first()
    )
    if agent is None or agent.archived:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    row = _agent_version(db, principal.tenant_id, str(agent_id), version)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Version not found")
    schema = output_schema_of(row.spec)
    return JSONResponse(content={
        "agent_id": str(agent_id),
        "version": row.version,
        "pinned": version is not None,
        "structured": schema is not None,
        "json_schema": schema,
        "fields": output_fields(schema) if schema is not None else [],
    })


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_class=JSONResponse,
    responses={422: {"model": ErrorDetail}},
)
@schema_checks_storable
def create_workflow(request: WorkflowCreate, principal: PrincipalDep, db: SessionDep) -> JSONResponse:
    """Create a workflow with an initial draft."""
    workflow = WorkflowModel(
        tenant_id=principal.tenant_id,
        name=request.spec.name,
        draft_spec=request.spec.model_dump(mode="json"),
    )
    db.add(workflow)
    db.commit()
    db.refresh(workflow)
    return JSONResponse(content=_workflow_response(workflow, None), status_code=201)


@router.get("", response_class=JSONResponse)
def list_workflows(principal: PrincipalDep, db: SessionDep) -> JSONResponse:
    """List this tenant's workflows (not archived), oldest first."""
    workflows = (
        db.query(WorkflowModel)
        .filter(WorkflowModel.tenant_id == principal.tenant_id, WorkflowModel.archived.is_(False))
        .order_by(WorkflowModel.created_at, WorkflowModel.id)
        .all()
    )
    return JSONResponse(content=[_workflow_response(w, _latest_version(db, w)) for w in workflows])


@router.get("/{workflow_id}", response_class=JSONResponse, responses={404: {"model": ErrorDetail}})
def get_workflow(workflow_id: uuid.UUID, principal: PrincipalDep, db: SessionDep) -> JSONResponse:
    workflow = _get_workflow(db, workflow_id, principal.tenant_id)
    return JSONResponse(content=_workflow_response(workflow, _latest_version(db, workflow)))


@router.patch(
    "/{workflow_id}",
    response_class=JSONResponse,
    responses={404: {"model": ErrorDetail}, 422: {"model": ErrorDetail}},
)
@schema_checks_storable
def patch_workflow(
    workflow_id: uuid.UUID, request: WorkflowUpdate, principal: PrincipalDep, db: SessionDep
) -> JSONResponse:
    """Replace the draft. Published versions never change."""
    workflow = _get_workflow(db, workflow_id, principal.tenant_id)
    workflow.name = request.spec.name
    workflow.draft_spec = request.spec.model_dump(mode="json")
    db.commit()
    db.refresh(workflow)
    return JSONResponse(content=_workflow_response(workflow, _latest_version(db, workflow)))


@router.post(
    "/{workflow_id}/versions",
    status_code=status.HTTP_201_CREATED,
    response_class=JSONResponse,
    responses={404: {"model": ErrorDetail}, 409: {"model": ErrorDetail}, 422: {"model": ErrorDetail}},
)
def publish_workflow_version(workflow_id: uuid.UUID, principal: PrincipalDep, db: SessionDep) -> JSONResponse:
    """Publish the draft as the next immutable version (409 when it equals the latest one): 201 with
    the version and `warnings` (AC.9: refs into unpinned or text agents' output; LP.5: pinned loop
    body agents with an approval policy)."""
    workflow = _get_workflow(db, workflow_id, principal.tenant_id)
    if workflow.draft_spec is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Workflow has no draft spec")
    try:
        spec = WorkflowSpec.model_validate(workflow.draft_spec)
    except ValidationError as exc:
        messages = "; ".join(err["msg"].removeprefix("Value error, ") for err in exc.errors())
        raise HTTPException(status_code=422, detail=f"draft spec is invalid: {messages}") from None
    problems = reference_problems(db, spec, principal.tenant_id, lock=True)
    ref_problems, warnings = output_ref_checks(db, spec, principal.tenant_id)
    problems += ref_problems
    warnings += loop_agent_approval_warnings(db, spec, principal.tenant_id)
    if problems:
        raise HTTPException(status_code=422, detail="; ".join(problems))

    spec_hash = canonical_workflow_hash(spec)
    latest = _latest_version(db, workflow)
    if latest is not None:
        latest_row = (
            db.query(WorkflowVersionModel)
            .filter(WorkflowVersionModel.workflow_id == workflow.id, WorkflowVersionModel.version == latest)
            .first()
        )
        if latest_row is not None and latest_row.spec_hash == spec_hash:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"no changes since version {latest}")

    version = WorkflowVersionModel(
        workflow_id=workflow.id,
        tenant_id=principal.tenant_id,
        version=(latest or 0) + 1,
        spec=spec.model_dump(mode="json"),
        spec_hash=spec_hash,
        created_by=principal.user_id,
    )
    db.add(version)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="concurrent publish, retry") from None
    db.refresh(version)
    return JSONResponse(content={**_version_response(version), "warnings": warnings}, status_code=201)


@router.get("/{workflow_id}/versions", response_class=JSONResponse, responses={404: {"model": ErrorDetail}})
def list_workflow_versions(workflow_id: uuid.UUID, principal: PrincipalDep, db: SessionDep) -> JSONResponse:
    """Published versions, newest first."""
    workflow = _get_workflow(db, workflow_id, principal.tenant_id)
    versions = (
        db.query(WorkflowVersionModel)
        .filter(
            WorkflowVersionModel.workflow_id == workflow.id,
            WorkflowVersionModel.tenant_id == principal.tenant_id,
        )
        .order_by(WorkflowVersionModel.version.desc())
        .all()
    )
    return JSONResponse(content=[_version_response(v) for v in versions])


@router.get(
    "/{workflow_id}/versions/{version_num}",
    response_class=JSONResponse,
    responses={404: {"model": ErrorDetail}},
)
def get_workflow_version(
    workflow_id: uuid.UUID, version_num: int, principal: PrincipalDep, db: SessionDep
) -> JSONResponse:
    workflow = _get_workflow(db, workflow_id, principal.tenant_id)
    version = (
        db.query(WorkflowVersionModel)
        .filter(
            WorkflowVersionModel.workflow_id == workflow.id,
            WorkflowVersionModel.tenant_id == principal.tenant_id,
            WorkflowVersionModel.version == version_num,
        )
        .first()
    )
    if version is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Version not found")
    return JSONResponse(content=_version_response(version))
