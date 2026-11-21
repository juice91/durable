"""Scorers, test suites and eval runs over MCP, and the rule suggester.

Reads (`scorer.list`, `eval.suite.list`, `eval.run.get`) need any key of the tenant; mutations
(`scorer.create`, `scorer.attach`, `eval.suite.create`, `eval.case.add`, `eval.run.start`,
`scorer.suggest`) need the `manage` scope. Each body validates its arguments with the platform's own
request models and calls the service the HTTP routes call, so a refusal is Studio's refusal.
`scorer.suggest` runs the built-in Scorer suggester (a model run) and returns its validated rules; it
adds nothing: create a scorer or a suite from a suggestion with the other tools.
A key restricted to listed agents or workflows sees and changes only suites and runs of those.
"""

from __future__ import annotations

import uuid
from typing import Any

from eap_api.deps import eval_store_of, scorer_store_of
from eap_api.mcp_control.helpers import (
    DEFAULT_PAGE, guard_json, not_found, page, require_agent, require_unrestricted, require_workflow, uid,
)
from eap_api.mcp_control.routing import McpToolError, call_service, translate
from eap_api.mcp_control.server import current_ctx
from eap_api.service_deps.architect import ArchitectSuggesterPorts
from eap_api.views import (
    agent_run_started_json, case_json, eval_run_json, result_json, scorer_json, suite_json,
    workflow_run_started_json,
)
from eap_api.services.evals import EvalService
from eap_api.service_deps.architect import build_architect_service
from eap_api.service_deps.runs import build_run_service
from eap_api.services.scorer_suggestions import ScorerSuggestionService
from eap_api.services.scorers import ScorerService
from eap_observability import current_trace_context

MAX_RESULTS_PAGE = 100


def _model(cls: Any, **fields: Any) -> Any:
    try:
        return cls(**fields)
    except Exception as exc:  # noqa: BLE001 - pydantic ValidationError; translate() re-raises anything else
        raise translate(exc) from None


def _one_target(agent_id: str | None, workflow_id: str | None) -> None:
    if (agent_id is None) == (workflow_id is None):
        raise McpToolError("invalid", "give exactly one of agent_id and workflow_id")


def _evals(ctx: Any) -> Any:
    """`build` for call_service: an EvalService on this call's session."""
    return lambda db: EvalService(eval_store_of(ctx.app), db, ctx.stores[0], ctx.launcher)


def _scorers(ctx: Any) -> Any:
    return lambda db: ScorerService(scorer_store_of(ctx.app), db, ctx.stores[0], ctx.launcher)


async def _suite_target(ctx: Any, suite_id: str) -> tuple[str | None, str | None]:
    """`(agent_id, workflow_id)` of the tenant's suite, after the key's allowlists; not_found otherwise."""
    sid = uid(suite_id, "suite_id")
    try:
        agent_id, workflow_id = await call_service(ctx, _evals(ctx), lambda s: s.suite_target(ctx.tenant_id, sid))
    except McpToolError as exc:
        if exc.code == "not_found":
            raise not_found("Suite") from None
        raise
    if agent_id is not None and not ctx.agent_allowed(agent_id):
        raise not_found("Suite")
    if workflow_id is not None and not ctx.workflow_allowed(workflow_id):
        raise not_found("Suite")
    return agent_id, workflow_id


def register_tools(tool: Any) -> None:
    # ------------------------------------------------------------------------------ scorers

    @tool("scorer.list")
    async def scorer_list(limit: int = DEFAULT_PAGE, offset: int = 0) -> dict[str, Any]:
        """The tenant's reusable scorers: id, name, description, evaluator and the agent_ids each is attached
        to. Paged: limit <= 100."""
        ctx = current_ctx()
        records = await call_service(ctx, _scorers(ctx), lambda s: s.list(ctx.tenant_id))
        rows = [
            {**scorer_json(r), "agent_ids": [a for a in r.agent_ids if ctx.agent_allowed(a)]} for r in records
        ]
        return page(rows, limit, offset)

    @tool("scorer.create", manage=True)
    async def scorer_create(name: str, evaluator: dict[str, Any], description: str | None = None) -> dict[str, Any]:
        """Create a reusable scorer. `evaluator` is one of: {kind: contains, value, case_sensitive?},
        {kind: regex, pattern, negate?, field?} (RE2), {kind: schema_valid, json_schema?}, {kind: field_equals,
        path, value}, {kind: max_cost, usd}. Attach it to an agent with scorer.attach."""
        from eap_api.bodies import ScorerCreate

        ctx = current_ctx()
        require_unrestricted(ctx, "create scorers")
        guard_json(evaluator, "evaluator", max_bytes=16 * 1024)
        body = _model(ScorerCreate, name=name, description=description, evaluator=evaluator)
        record = await call_service(
            ctx, _scorers(ctx), lambda s: s.create(ctx.tenant_id, body.name, body.evaluator, body.description)
        )
        return scorer_json(record)

    @tool("scorer.attach", manage=True)
    async def scorer_attach(agent_id: str, scorer_id: str, sampling_rate: float = 1.0) -> dict[str, Any]:
        """Attach a scorer to an agent: the worker scores that share of the agent's finished runs
        (`sampling_rate` in (0, 1]). Returns the agent's attached scorers. At most 10 per agent."""
        from eap_api.bodies import Attachment

        ctx = current_ctx()
        require_agent(ctx, agent_id)
        body = _model(Attachment, sampling_rate=sampling_rate)
        aid, sid = uid(agent_id, "agent_id"), uid(scorer_id, "scorer_id")
        items = await call_service(
            ctx, _scorers(ctx), lambda s: s.attach(ctx.tenant_id, aid, sid, body.sampling_rate)
        )
        return {"items": items}

    @tool("scorer.suggest", manage=True)
    async def scorer_suggest(agent_id: str | None = None, workflow_id: str | None = None, max: int = 8,
                             include_runs: bool = True) -> dict[str, Any]:
        """AI-suggested rules for an agent or a workflow (give one): {suggestions: [{title, rationale, scope
        (scorer | evaluator | both), spec, evidence}], model, dropped}. `spec` is a valid evaluator; for scope
        scorer or both it is also a valid scorer, so pass it to scorer.create; any spec goes into
        eval.suite.create's `evaluators`. Runs a model (about a minute at most); the answer is cached for 10
        minutes. Reads the definition and up to 5 recent run outputs, redacted; adds nothing."""
        from eap_api.bodies import SuggestRequest

        ctx = current_ctx()
        _one_target(agent_id, workflow_id)
        body = _model(SuggestRequest, max=max, include_runs=include_runs)
        if agent_id is not None:
            require_agent(ctx, agent_id)
            kind, target = "agent", uid(agent_id, "agent_id")
        else:
            require_workflow(ctx, workflow_id)
            kind, target = "workflow", uid(workflow_id, "workflow_id")

        def build(db: Any) -> Any:
            architect = build_architect_service(ctx.engine, build_run_service(db, ctx.stores[0], ctx.stores[1], ctx.launcher))
            ports = ArchitectSuggesterPorts(architect, ctx.tenant_id)
            return ScorerSuggestionService(db, ctx.stores[0]), ports

        return await call_service(
            ctx, build,
            lambda built: built[0].suggest(
                ctx.tenant_id, ctx.actor.user_id, kind, target, max=body.max, include_runs=body.include_runs,
                ports=built[1],
            ),
        )

    # ------------------------------------------------------------------------------- suites

    @tool("eval.suite.list")
    async def eval_suite_list(agent_id: str | None = None, workflow_id: str | None = None, include_archived: bool = False,
                              limit: int = DEFAULT_PAGE, offset: int = 0) -> dict[str, Any]:
        """The test suites of one agent or one workflow (give one): id, name, description, evaluators,
        case_count. Paged: limit <= 100."""
        ctx = current_ctx()
        _one_target(agent_id, workflow_id)
        if agent_id is not None:
            require_agent(ctx, agent_id)
            aid = uid(agent_id, "agent_id")
            suites = await call_service(
                ctx, _evals(ctx), lambda s: s.list_suites(ctx.tenant_id, aid, include_archived=include_archived)
            )
            items = [suite_json(x) for x in suites]
        else:
            require_workflow(ctx, workflow_id)
            wid = uid(workflow_id, "workflow_id")
            name, suites = await call_service(
                ctx, _evals(ctx), lambda s: s.list_workflow_suites(ctx.tenant_id, wid, include_archived=include_archived)
            )
            items = [suite_json(x, name) for x in suites]
        return page(items, limit, offset)

    @tool("eval.suite.create", manage=True)
    async def eval_suite_create(name: str, agent_id: str | None = None, workflow_id: str | None = None,
                                evaluators: list[dict[str, Any]] | None = None, description: str | None = None,
                                suite_id: str | None = None) -> dict[str, Any]:
        """Create a test suite for an agent or a workflow (give one) with up to 10 suite evaluators (the kinds
        of scorer.suggest's specs, plus equals_expected, max_duration and llm_judge). `suite_id` makes a retry
        idempotent. Add cases with eval.case.add."""
        from eap_api.schemas import EvalSuiteCreate

        ctx = current_ctx()
        _one_target(agent_id, workflow_id)
        guard_json(evaluators or [], "evaluators", max_bytes=64 * 1024)
        body = _model(EvalSuiteCreate, name=name, description=description, evaluators=evaluators or [],
                      suite_id=uid(suite_id, "suite_id") if suite_id else None)
        if agent_id is not None:
            require_agent(ctx, agent_id)
            kind, target = "agent", uid(agent_id, "agent_id")
        else:
            require_workflow(ctx, workflow_id)
            kind, target = "workflow", uid(workflow_id, "workflow_id")
        suite, target_name, _ = await call_service(
            ctx, _evals(ctx),
            lambda s: s.create_suite(
                ctx.tenant_id, kind, target, name=body.name, description=body.description,
                evaluators=body.evaluators, suite_id=body.suite_id,
            ),
        )
        return suite_json(suite, target_name)

    @tool("eval.case.add", manage=True)
    async def eval_case_add(suite_id: str, name: str, input: str, expected_output: Any = None,
                            metadata: dict[str, Any] | None = None, evaluators: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Add a case to a suite: `input` is the text sent to the agent (for a workflow suite, JSON text of an
        object valid for the workflow's input schema), `expected_output` what equals_expected compares with,
        `evaluators` up to 5 extra evaluators for this case. 409 for a name already in the suite."""
        from eap_api.schemas import EvalCaseCreate

        ctx = current_ctx()
        await _suite_target(ctx, suite_id)
        guard_json({"input": input, "expected_output": expected_output, "metadata": metadata, "evaluators": evaluators},
                   "case", max_bytes=96 * 1024)
        body = _model(EvalCaseCreate, name=name, input=input, expected_output=expected_output,
                      metadata=metadata or {}, evaluators=evaluators or [])
        sid = uid(suite_id, "suite_id")
        case = body.model_dump(exclude={"case_id", "position"})
        record = await call_service(
            ctx, _evals(ctx),
            lambda s: s.add_case(ctx.tenant_id, sid, case, position=body.position, case_id=body.case_id),
        )
        return case_json(record)

    # ----------------------------------------------------------------------------- eval runs

    @tool("eval.run.start", manage=True)
    async def eval_run_start(suite_id: str, version: int | str | None = None, max_concurrency: int = 4,
                             run_id: str | None = None) -> dict[str, Any]:
        """Run a suite's cases against the target: a published `version` (null: the latest; for an agent also
        "draft"). `run_id` makes a retry idempotent. Returns {run_id, status, cases, warnings, ...}; read it with
        eval.run.get. Runs the agent or workflow once per case (a model call each)."""
        from eap_api.schemas import EvalRunCreate

        ctx = current_ctx()
        agent_id, workflow_id = await _suite_target(ctx, suite_id)
        body = _model(EvalRunCreate, run_id=uid(run_id, "run_id") if run_id else uuid.uuid4(), suite_id=uid(suite_id, "suite_id"),
                      version=version, max_concurrency=max_concurrency)
        trace_id, traceparent = current_trace_context()
        common = {"run_id": body.run_id, "suite_id": body.suite_id, "version": body.version,
                  "max_concurrency": body.max_concurrency, "trace_id": trace_id, "traceparent": traceparent}
        if agent_id is not None:
            aid = uid(agent_id, "agent_id")
            started = await call_service(
                ctx, _evals(ctx), lambda s: s.start_agent_eval_run(ctx.tenant_id, aid, **common)
            )
            return agent_run_started_json(started)
        wid = uid(workflow_id, "workflow_id")
        started = await call_service(
            ctx, _evals(ctx), lambda s: s.start_workflow_eval_run(ctx.tenant_id, wid, **common)
        )
        return workflow_run_started_json(started)

    @tool("eval.run.get")
    async def eval_run_get(run_id: str, results_limit: int = 0) -> dict[str, Any]:
        """An eval run: status and aggregates (cases, passed, failed, errors, pass_rate, scores_avg, cost_usd).
        With `results_limit` > 0 (<= 100) also the first per-case results as `results`."""
        ctx = current_ctx()
        rid = uid(run_id, "run_id")
        run = await call_service(ctx, _evals(ctx), lambda s: s.get_run(ctx.tenant_id, rid))
        if run is None or run.get("kind") != "eval":
            raise not_found("Run")
        agent_id, workflow_id = run.get("agent_id"), run.get("workflow_id")
        if (agent_id and not ctx.agent_allowed(str(agent_id))) or (workflow_id and not ctx.workflow_allowed(str(workflow_id))):
            raise not_found("Run")
        limit = max(0, min(int(results_limit), MAX_RESULTS_PAGE))
        workflow = not agent_id
        target = uid(str(workflow_id if workflow else agent_id), "workflow_id" if workflow else "agent_id")

        def read(s: EvalService) -> dict[str, Any]:
            found, aggregates = s.get_eval_run(ctx.tenant_id, target, rid, workflow=workflow)
            out = eval_run_json(found, aggregates)
            if limit:
                results, _ = s.list_eval_results(ctx.tenant_id, target, rid, workflow=workflow, limit=limit, after=None)
                out["results"] = [result_json(r) for r in results]
            return out

        return await call_service(ctx, _evals(ctx), read)
