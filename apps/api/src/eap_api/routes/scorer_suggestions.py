"""Suggested rules: `POST /agents/{id}/scorer-suggestions` and `POST /workflows/{id}/scorer-suggestions`.

One run of the built-in "Scorer suggester" agent (seeded on first use like the Architect's drafters,
with the Architect's ranked default model) reads the target's definition and up to 5 recent completed
run outputs, and answers with rules a person can add as a scorer or a suite evaluator. Body (optional):
`{"max": 1..12 (default 8), "include_runs": bool (default true)}`.

200 `{suggestions: [{title, rationale, scope: "scorer" | "evaluator" | "both", spec, evidence}], model,
dropped}`. `spec` is a valid `eap_schemas.evals` evaluator (and, for scope scorer or both, a valid
`eap_schemas.scorers` one; a regex also compiles as RE2). `dropped` counts the answer's entries that were
invalid, repeated or over the 2-judge cap. A workflow has no scorers: every suggestion is `evaluator` scope.

What the suggester sees is bounded and redacted (`eap_observability.redact`: credentials, keys, tokens,
user:password URLs): the target's instructions or steps and output schema, and at most 5 run inputs and
outputs cut to a few hundred characters. It never sees secrets, memory, knowledge-base content,
attachments, other agents or other tenants. The answer is cached per (tenant, target, definition,
max, include_runs) for 10 minutes, so a repeat click costs no run.

Errors: 404, 409 `no_model` (no model is allowed), 422 (body; `suggest_invalid` when the answer is not
shaped as asked), 502 `suggest_failed`, 504 `suggest_timeout`.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from eap_api.auth import Principal, current_principal
from eap_api.database import get_session
from eap_api.launcher import RunLauncher, get_run_launcher
from eap_api.models import AgentModel
from eap_api.request_guard import GuardedJsonRoute
from eap_api.routes.architect import _run_drafter, _seed_one, _spec_model, seed_model
from eap_api.routes.runs import StoresDep
from eap_api.schemas import ErrorDetail
from eap_api.workflows.models import WorkflowModel
from eap_observability import redact
from eap_processors import Re2PatternError, compile_re2
from eap_run_store.definition_errors import DefinitionError, refuse
from eap_schemas import ModelRef
from eap_schemas.scorer_suggestions import (
    DEFAULT_SUGGESTIONS,
    MAX_SUGGESTIONS,
    SUGGESTER_NAME,
    SUGGESTER_SLUG,
    Suggestion,
    finalize_suggestions,
    suggester_agent_spec,
    validate_suggestion,
)

router = APIRouter(
    tags=["scorer-suggestions"],
    responses={404: {"model": ErrorDetail}, 409: {"model": ErrorDetail}, 422: {"model": ErrorDetail}},
    route_class=GuardedJsonRoute,
)
PrincipalDep = Annotated[Principal, Depends(current_principal)]
SessionDep = Annotated[Session, Depends(get_session)]

AGENT_NOT_FOUND = "Agent not found"
WORKFLOW_NOT_FOUND = "Workflow not found"
CACHE_TTL_S = 600.0
CACHE_MAX_ENTRIES = 256
MAX_RUNS = 5
MAX_RUN_INPUT_CHARS = 600
MAX_RUN_OUTPUT_CHARS = 800
MAX_DEFINITION_CHARS = 8000
MAX_SCHEMA_CHARS = 2000
MAX_PROMPT_CHARS = 20000

_cache: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}
_cache_lock = threading.Lock()


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _cache_get(key: tuple[Any, ...]) -> dict[str, Any] | None:
    with _cache_lock:
        hit = _cache.get(key)
        if hit is None:
            return None
        if hit[0] < time.monotonic():
            del _cache[key]
            return None
        return hit[1]


def _cache_put(key: tuple[Any, ...], value: dict[str, Any]) -> None:
    now = time.monotonic()
    with _cache_lock:
        for stale in [k for k, (expires, _) in _cache.items() if expires < now]:
            del _cache[stale]
        while len(_cache) >= CACHE_MAX_ENTRIES:
            del _cache[next(iter(_cache))]
        _cache[key] = (now + CACHE_TTL_S, value)


class SuggestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max: int = Field(default=DEFAULT_SUGGESTIONS, ge=1, le=MAX_SUGGESTIONS)
    include_runs: bool = True


# ------------------------------------------------------------------------------------ the prompt


def _clip(value: Any, limit: int) -> str:
    """`value` as redacted text of at most `limit` characters."""
    raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return redact(raw, max_length=limit)


def _agent_definition(name: str, spec: dict[str, Any]) -> str:
    lines = [f"Target: agent {_clip(name, 100)}", "Instructions:", _clip(spec.get("instructions") or "(none)", MAX_DEFINITION_CHARS)]
    schema = (spec.get("output") or {}).get("json_schema")
    lines.append("Output: " + (f"structured, schema {_clip(schema, MAX_SCHEMA_CHARS)}" if schema else "free text"))
    tools = spec.get("tools") or []
    if tools:
        lines.append("Tools: " + ", ".join(_clip(t, 60) for t in tools[:20]))
    return "\n".join(lines)


def _workflow_definition(name: str, spec: dict[str, Any]) -> str:
    lines = [f"Target: workflow {_clip(name, 100)}", "Description: " + _clip(spec.get("description") or "(none)", 500)]
    lines.append("Input schema: " + _clip(spec.get("input_schema") or {}, MAX_SCHEMA_CHARS))
    if spec.get("output_schema"):
        lines.append("Output schema: " + _clip(spec["output_schema"], MAX_SCHEMA_CHARS))
    lines.append("Steps:\n" + _clip(spec.get("steps") or [], MAX_DEFINITION_CHARS))
    return "\n".join(lines)


def _run_section(runs: list[dict[str, Any]]) -> str:
    if not runs:
        return "Recent runs: none"
    lines = ["Recent runs (newest first):"]
    for index, run in enumerate(runs, 1):
        lines.append(f"- run {index} input: {_clip(run.get('input') or '', MAX_RUN_INPUT_CHARS)}")
        lines.append(f"  run {index} output: {_clip(run.get('output') or '', MAX_RUN_OUTPUT_CHARS)}")
    return "\n".join(lines)


def build_prompt(definition: str, runs: list[dict[str, Any]], limit: int) -> str:
    """The suggester's input: the redacted definition, the redacted recent runs and the answer size, cut
    to MAX_PROMPT_CHARS."""
    prompt = "\n".join([
        f"Propose at most {limit} rules.", "", "--- Definition ---", definition, "", "--- Recent runs ---", _run_section(runs),
    ])
    return prompt[:MAX_PROMPT_CHARS]


# ------------------------------------------------------------------------------------- the run


def _suggester(engine: Engine, principal: Principal) -> tuple[str, ModelRef]:
    """The tenant's suggester agent id (seeded when missing) and the model an `llm_judge` suggestion gets."""
    chosen: list[str] = []
    try:
        with engine.begin() as conn:
            row, _ = _seed_one(conn, principal.tenant_id, principal.user_id, SUGGESTER_SLUG, SUGGESTER_NAME,
                               suggester_agent_spec, chosen)
    except DefinitionError as exc:
        if exc.code == "choose_model":
            raise refuse(409, "No model is available for the scorer suggester.", code="no_model", **exc.extra) from None
        raise
    model_id = _spec_model(row.draft_spec) or seed_model()
    if model_id is None:
        raise refuse(409, "No model is available for the scorer suggester.", code="no_model")
    provider, _, name = model_id.partition(":")
    return str(row.id), ModelRef(provider=provider, name=name)


_RELABEL = {"draft_failed": "suggest_failed", "draft_timeout": "suggest_timeout", "draft_invalid": "suggest_invalid"}


def _regex_ok(item: Suggestion | None) -> Suggestion | None:
    if item is not None and item.spec["kind"] == "regex":
        try:
            compile_re2(item.spec["pattern"], allow_empty_match=True)
        except Re2PatternError:
            return None
    return item


def _suggest(
    *, kind: str, target_id: uuid.UUID, name: str, spec: dict[str, Any], body: SuggestRequest, principal: Principal,
    request: Request, db: Session, stores: Any, launcher: RunLauncher,
) -> JSONResponse:
    run_store, _ = stores
    digest = hashlib.sha256(json.dumps(spec, sort_keys=True, default=str).encode()).hexdigest()
    key = (principal.tenant_id, kind, str(target_id), digest, body.max, body.include_runs)
    cached = _cache_get(key)
    if cached is not None:
        return JSONResponse(content=cached)

    engine: Engine = request.app.state.database_engine
    suggester_id, judge = _suggester(engine, principal)
    runs: list[dict[str, Any]] = []
    if body.include_runs:
        filters = {"agent_id": target_id} if kind == "agent" else {"workflow_id": target_id}
        runs = run_store.list_runs(principal.tenant_id, limit=MAX_RUNS, status="completed", **filters)
    definition = _agent_definition(name, spec) if kind == "agent" else _workflow_definition(name, spec)
    try:
        parsed, _, raw, _, model = _run_drafter(
            suggester_id, build_prompt(definition, runs, body.max), principal, db, stores, launcher, "scorer suggestions"
        )
    except DefinitionError as exc:
        if exc.code in _RELABEL:
            detail = exc.detail.replace("drafter", "scorer suggester").replace("definition", "list of rules")
            raise type(exc)(detail, code=_RELABEL[exc.code], **exc.extra) from None
        raise
    entries = parsed.get("suggestions")
    if not isinstance(entries, list):
        raise refuse(422, "The scorer suggester's answer has no list of suggestions.", code="suggest_invalid",
                     raw=raw[:2000])
    candidates = [_regex_ok(validate_suggestion(e, judge_model=judge)) for e in entries[: MAX_SUGGESTIONS * 2]]
    kept, dropped = finalize_suggestions(candidates, limit=body.max, target="agent" if kind == "agent" else "workflow")
    result = {"suggestions": [s.model_dump(mode="json") for s in kept], "model": model, "dropped": dropped}
    _cache_put(key, result)
    return JSONResponse(content=result)


# ------------------------------------------------------------------------------------- routes


@router.post("/agents/{agent_id}/scorer-suggestions", responses={409: {"description": "no_model"}})
def suggest_for_agent(
    agent_id: uuid.UUID, request: Request, principal: PrincipalDep, db: SessionDep, stores: StoresDep,
    launcher: Annotated[RunLauncher, Depends(get_run_launcher)], body: Annotated[SuggestRequest | None, Body()] = None,
) -> JSONResponse:
    """Suggest scorers and suite evaluators for an agent (see the module docstring)."""
    agent = (
        db.query(AgentModel)
        .filter(AgentModel.id == str(agent_id), AgentModel.tenant_id == principal.tenant_id)
        .first()
    )
    if agent is None:
        raise refuse(404, AGENT_NOT_FOUND)
    return _suggest(
        kind="agent", target_id=agent_id, name=agent.name, spec=agent.draft_spec or {}, body=body or SuggestRequest(),
        principal=principal, request=request, db=db, stores=stores, launcher=launcher,
    )


@router.post("/workflows/{workflow_id}/scorer-suggestions", responses={409: {"description": "no_model"}})
def suggest_for_workflow(
    workflow_id: uuid.UUID, request: Request, principal: PrincipalDep, db: SessionDep, stores: StoresDep,
    launcher: Annotated[RunLauncher, Depends(get_run_launcher)], body: Annotated[SuggestRequest | None, Body()] = None,
) -> JSONResponse:
    """Suggest suite evaluators for a workflow (every suggestion is `evaluator` scope)."""
    workflow = (
        db.query(WorkflowModel)
        .filter(WorkflowModel.id == str(workflow_id), WorkflowModel.tenant_id == principal.tenant_id)
        .first()
    )
    if workflow is None:
        raise refuse(404, WORKFLOW_NOT_FOUND)
    return _suggest(
        kind="workflow", target_id=workflow_id, name=workflow.name, spec=workflow.draft_spec or {},
        body=body or SuggestRequest(), principal=principal, request=request, db=db, stores=stores, launcher=launcher,
    )
