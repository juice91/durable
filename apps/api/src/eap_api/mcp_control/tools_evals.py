"""Scorers, test suites and eval runs over MCP, and the rule suggester.

Reads (`scorer.list`, `eval.suite.list`, `eval.run.get`) need any key of the tenant; mutations
(`scorer.create`, `scorer.attach`, `eval.suite.create`, `eval.case.add`, `eval.run.start`,
`scorer.suggest`) need the `manage` scope. Each body validates its arguments with the platform's own
request models and calls the route function the HTTP API serves, so a refusal is Studio's refusal.
`scorer.suggest` runs the built-in Scorer suggester (a model run) and returns its validated rules; it
adds nothing: create a scorer or a suite from a suggestion with the other tools.
A key restricted to listed agents or workflows sees and changes only suites and runs of those.
"""

from __future__ import annotations

import uuid
from typing import Any

from starlette.concurrency import run_in_threadpool

from eap_api.mcp_control.helpers import (
    DEFAULT_PAGE, guard_json, not_found, page, require_agent, require_unrestricted, require_workflow, uid,
)
from eap_api.mcp_control.routing import DB, McpToolError, call, translate
from eap_api.mcp_control.server import current_ctx

MAX_RESULTS_PAGE = 100


def _model(cls: Any, **fields: Any) -> Any:
    try:
        return cls(**fields)
    except Exception as exc:  # noqa: BLE001 - pydantic ValidationError; translate() re-raises anything else
        raise translate(exc) from None


def _one_target(agent_id: str | None, workflow_id: str | None) -> None:
    if (agent_id is None) == (workflow_id is None):
        raise McpToolError("invalid", "give exactly one of agent_id and workflow_id")


async def _suite_target(ctx: Any, suite_id: str) -> tuple[str | None, str | None]:
    """`(agent_id, workflow_id)` of the tenant's suite, after the key's allowlists; not_found otherwise."""
    from eap_api.routes.evals import get_eval_store
    from eap_run_store.evals import EvalSuiteNotFound

    store = get_eval_store(ctx.request)
    try:
        suite = await run_in_threadpool(store.get_suite, ctx.tenant_id, uid(suite_id, "suite_id"))
    except EvalSuiteNotFound:
        raise not_found("Suite") from None
    if suite.agent_id is not None and not ctx.agent_allowed(suite.agent_id):
        raise not_found("Suite")
    if suite.workflow_id is not None and not ctx.workflow_allowed(suite.workflow_id):
        raise not_found("Suite")
    return suite.agent_id, suite.workflow_id


def register_tools(tool: Any) -> None:
    # ------------------------------------------------------------------------------ scorers

    @tool("scorer.list")
    async def scorer_list(limit: int = DEFAULT_PAGE, offset: int = 0) -> dict[str, Any]:
        """The tenant's reusable scorers: id, name, description, evaluator and the agent_ids each is attached
        to. Paged: limit <= 100."""
        from eap_api.routes.scorers import get_scorer_store, list_scorers

        ctx = current_ctx()
        out = await call(ctx, list_scorers, principal=ctx.actor, store=get_scorer_store(ctx.request))
        rows = [{**s, "agent_ids": [a for a in s["agent_ids"] if ctx.agent_allowed(a)]} for s in out["items"]]
        return page(rows, limit, offset)

    @tool("scorer.create", manage=True)
    async def scorer_create(name: str, evaluator: dict[str, Any], description: str | None = None) -> dict[str, Any]:
        """Create a reusable scorer. `evaluator` is one of: {kind: contains, value, case_sensitive?},
        {kind: regex, pattern, negate?, field?} (RE2), {kind: schema_valid, json_schema?}, {kind: field_equals,
        path, value}, {kind: max_cost, usd}. Attach it to an agent with scorer.attach."""
        from eap_api.routes.scorers import ScorerCreate, create_scorer, get_scorer_store

        ctx = current_ctx()
        require_unrestricted(ctx, "create scorers")
        guard_json(evaluator, "evaluator", max_bytes=16 * 1024)
        body = _model(ScorerCreate, name=name, description=description, evaluator=evaluator)
        return await call(ctx, create_scorer, body=body, principal=ctx.actor, store=get_scorer_store(ctx.request))

    @tool("scorer.attach", manage=True)
    async def scorer_attach(agent_id: str, scorer_id: str, sampling_rate: float = 1.0) -> dict[str, Any]:
        """Attach a scorer to an agent: the worker scores that share of the agent's finished runs
        (`sampling_rate` in (0, 1]). Returns the agent's attached scorers. At most 10 per agent."""
        from eap_api.routes.scorers import Attachment, attach_scorer, get_scorer_store

        ctx = current_ctx()
        require_agent(ctx, agent_id)
        body = _model(Attachment, sampling_rate=sampling_rate)
        return await call(ctx, attach_scorer, agent_id=uid(agent_id, "agent_id"), scorer_id=uid(scorer_id, "scorer_id"),
                          body=body, principal=ctx.actor, db=DB, store=get_scorer_store(ctx.request))

    @tool("scorer.suggest", manage=True)
    async def scorer_suggest(agent_id: str | None = None, workflow_id: str | None = None, max: int = 8,
                             include_runs: bool = True) -> dict[str, Any]:
        """AI-suggested rules for an agent or a workflow (give one): {suggestions: [{title, rationale, scope
        (scorer | evaluator | both), spec, evidence}], model, dropped}. `spec` is a valid evaluator; for scope
        scorer or both it is also a valid scorer, so pass it to scorer.create; any spec goes into
        eval.suite.create's `evaluators`. Runs a model (about a minute at most); the answer is cached for 10
        minutes. Reads the definition and up to 5 recent run outputs, redacted; adds nothing."""
        from eap_api.routes.scorer_suggestions import SuggestRequest, suggest_for_agent, suggest_for_workflow

        ctx = current_ctx()
        _one_target(agent_id, workflow_id)
        body = _model(SuggestRequest, max=max, include_runs=include_runs)
        common = {"request": ctx.request, "principal": ctx.actor, "db": DB, "stores": ctx.stores,
                  "launcher": ctx.launcher, "body": body}
        if agent_id is not None:
            require_agent(ctx, agent_id)
            return await call(ctx, suggest_for_agent, agent_id=uid(agent_id, "agent_id"), **common)
        require_workflow(ctx, workflow_id)
        return await call(ctx, suggest_for_workflow, workflow_id=uid(workflow_id, "workflow_id"), **common)

    # ------------------------------------------------------------------------------- suites

    @tool("eval.suite.list")
    async def eval_suite_list(agent_id: str | None = None, workflow_id: str | None = None, include_archived: bool = False,
                              limit: int = DEFAULT_PAGE, offset: int = 0) -> dict[str, Any]:
        """The test suites of one agent or one workflow (give one): id, name, description, evaluators,
        case_count. Paged: limit <= 100."""
        from eap_api.routes.evals import get_eval_store, list_suites, list_workflow_suites

        ctx = current_ctx()
        _one_target(agent_id, workflow_id)
        store = get_eval_store(ctx.request)
        if agent_id is not None:
            require_agent(ctx, agent_id)
            out = await call(ctx, list_suites, agent_id=uid(agent_id, "agent_id"), principal=ctx.actor, db=DB,
                             store=store, include_archived=include_archived)
        else:
            require_workflow(ctx, workflow_id)
            out = await call(ctx, list_workflow_suites, workflow_id=uid(workflow_id, "workflow_id"), principal=ctx.actor,
                             db=DB, store=store, include_archived=include_archived)
        return page(out["items"], limit, offset)

    @tool("eval.suite.create", manage=True)
    async def eval_suite_create(name: str, agent_id: str | None = None, workflow_id: str | None = None,
                                evaluators: list[dict[str, Any]] | None = None, description: str | None = None,
                                suite_id: str | None = None) -> dict[str, Any]:
        """Create a test suite for an agent or a workflow (give one) with up to 10 suite evaluators (the kinds
        of scorer.suggest's specs, plus equals_expected, max_duration and llm_judge). `suite_id` makes a retry
        idempotent. Add cases with eval.case.add."""
        from eap_api.routes.evals import create_suite, create_workflow_suite, get_eval_store
        from eap_api.schemas import EvalSuiteCreate

        ctx = current_ctx()
        _one_target(agent_id, workflow_id)
        guard_json(evaluators or [], "evaluators", max_bytes=64 * 1024)
        body = _model(EvalSuiteCreate, name=name, description=description, evaluators=evaluators or [],
                      suite_id=uid(suite_id, "suite_id") if suite_id else None)
        store = get_eval_store(ctx.request)
        if agent_id is not None:
            require_agent(ctx, agent_id)
            return await call(ctx, create_suite, agent_id=uid(agent_id, "agent_id"), body=body, principal=ctx.actor, db=DB, store=store)
        require_workflow(ctx, workflow_id)
        return await call(ctx, create_workflow_suite, workflow_id=uid(workflow_id, "workflow_id"), body=body,
                          principal=ctx.actor, db=DB, store=store)

    @tool("eval.case.add", manage=True)
    async def eval_case_add(suite_id: str, name: str, input: str, expected_output: Any = None,
                            metadata: dict[str, Any] | None = None, evaluators: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Add a case to a suite: `input` is the text sent to the agent (for a workflow suite, JSON text of an
        object valid for the workflow's input schema), `expected_output` what equals_expected compares with,
        `evaluators` up to 5 extra evaluators for this case. 409 for a name already in the suite."""
        from eap_api.routes.evals import add_case, get_eval_store
        from eap_api.schemas import EvalCaseCreate

        ctx = current_ctx()
        await _suite_target(ctx, suite_id)
        guard_json({"input": input, "expected_output": expected_output, "metadata": metadata, "evaluators": evaluators},
                   "case", max_bytes=96 * 1024)
        body = _model(EvalCaseCreate, name=name, input=input, expected_output=expected_output,
                      metadata=metadata or {}, evaluators=evaluators or [])
        return await call(ctx, add_case, suite_id=uid(suite_id, "suite_id"), body=body, principal=ctx.actor, db=DB,
                          store=get_eval_store(ctx.request))

    # ----------------------------------------------------------------------------- eval runs

    @tool("eval.run.start", manage=True)
    async def eval_run_start(suite_id: str, version: int | str | None = None, max_concurrency: int = 4,
                             run_id: str | None = None) -> dict[str, Any]:
        """Run a suite's cases against the target: a published `version` (null: the latest; for an agent also
        "draft"). `run_id` makes a retry idempotent. Returns {run_id, status, cases, warnings, ...}; read it with
        eval.run.get. Runs the agent or workflow once per case (a model call each)."""
        from eap_api.routes.evals import get_eval_store, start_eval_run, start_workflow_eval_run
        from eap_api.schemas import EvalRunCreate

        ctx = current_ctx()
        agent_id, workflow_id = await _suite_target(ctx, suite_id)
        body = _model(EvalRunCreate, run_id=uid(run_id, "run_id") if run_id else uuid.uuid4(), suite_id=uid(suite_id, "suite_id"),
                      version=version, max_concurrency=max_concurrency)
        common = {"body": body, "principal": ctx.actor, "db": DB, "stores": ctx.stores,
                  "store": get_eval_store(ctx.request), "launcher": ctx.launcher}
        if agent_id is not None:
            return await call(ctx, start_eval_run, agent_id=uid(agent_id, "agent_id"), **common)
        return await call(ctx, start_workflow_eval_run, workflow_id=uid(workflow_id, "workflow_id"), **common)

    @tool("eval.run.get")
    async def eval_run_get(run_id: str, results_limit: int = 0) -> dict[str, Any]:
        """An eval run: status and aggregates (cases, passed, failed, errors, pass_rate, scores_avg, cost_usd).
        With `results_limit` > 0 (<= 100) also the first per-case results as `results`."""
        from eap_api.routes.evals import (
            get_eval_run, get_eval_store, list_eval_results, list_workflow_eval_results, get_workflow_eval_run,
        )

        ctx = current_ctx()
        run_store, _ = ctx.stores
        run = await run_in_threadpool(run_store.get_run, ctx.tenant_id, uid(run_id, "run_id"))
        if run is None or run.get("kind") != "eval":
            raise not_found("Run")
        agent_id, workflow_id = run.get("agent_id"), run.get("workflow_id")
        if (agent_id and not ctx.agent_allowed(str(agent_id))) or (workflow_id and not ctx.workflow_allowed(str(workflow_id))):
            raise not_found("Run")
        store = get_eval_store(ctx.request)
        rid = uid(run_id, "run_id")
        limit = max(0, min(int(results_limit), MAX_RESULTS_PAGE))
        if agent_id:
            out = await call(ctx, get_eval_run, agent_id=uid(str(agent_id), "agent_id"), run_id=rid, principal=ctx.actor,
                             stores=ctx.stores, store=store)
            if limit:
                out["results"] = (await call(ctx, list_eval_results, agent_id=uid(str(agent_id), "agent_id"), run_id=rid,
                                             principal=ctx.actor, store=store, limit=limit, after=None))["items"]
            return out
        out = await call(ctx, get_workflow_eval_run, workflow_id=uid(str(workflow_id), "workflow_id"), run_id=rid,
                         principal=ctx.actor, stores=ctx.stores, store=store)
        if limit:
            out["results"] = (await call(ctx, list_workflow_eval_results, workflow_id=uid(str(workflow_id), "workflow_id"),
                                         run_id=rid, principal=ctx.actor, store=store, limit=limit, after=None))["items"]
        return out
