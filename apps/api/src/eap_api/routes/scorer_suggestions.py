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

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from eap_api.auth import Principal, current_principal
from eap_api.deps import EngineDep, RunLauncherDep, StoresDep
from eap_api.deps import SessionDep
from eap_api.request_guard import GuardedJsonRoute
from eap_api.routes.architect import _run_drafter, _seed_one, _spec_model, seed_model
from eap_api.schemas import ErrorDetail
from eap_api.service_deps.evals import ScorerSuggestionServiceDep
from eap_api.services.scorer_suggestions import (  # noqa: F401 - re-exported: tests clear and read the cache and the prompt cap
    DEFAULT_SUGGESTIONS,
    MAX_PROMPT_CHARS,
    MAX_SUGGESTIONS,
    _cache,
    build_prompt,
    clear_cache,
)
from eap_schemas.scorer_suggestions import SUGGESTER_NAME, SUGGESTER_SLUG, suggester_agent_spec

router = APIRouter(
    tags=["scorer-suggestions"],
    responses={404: {"model": ErrorDetail}, 409: {"model": ErrorDetail}, 422: {"model": ErrorDetail}},
    route_class=GuardedJsonRoute,
)
PrincipalDep = Annotated[Principal, Depends(current_principal)]


class SuggestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max: int = Field(default=DEFAULT_SUGGESTIONS, ge=1, le=MAX_SUGGESTIONS)
    include_runs: bool = True


class SuggesterPorts:
    """The suggester agent and one run of it, through the Architect's seeding and the runs route."""

    def __init__(self, engine: Any, principal: Principal, db: Any, stores: Any, launcher: Any) -> None:
        self.engine, self.principal, self.db, self.stores, self.launcher = engine, principal, db, stores, launcher

    def ensure_suggester(self, tenant_id: str, user_id: str) -> tuple[str, str | None]:
        chosen: list[str] = []
        # TODO(layering): call RunService / ArchitectService once those groups land
        with self.engine.begin() as conn:
            row, _ = _seed_one(conn, tenant_id, user_id, SUGGESTER_SLUG, SUGGESTER_NAME, suggester_agent_spec, chosen)
        return str(row.id), _spec_model(row.draft_spec) or seed_model()

    def run_drafter(self, suggester_id: str, prompt: str) -> tuple[dict[str, Any], list[str], str, str, str | None]:
        # TODO(layering): call RunService / ArchitectService once those groups land
        return _run_drafter(
            suggester_id, prompt, self.principal, self.db, self.stores, self.launcher, "scorer suggestions"
        )


@router.post("/agents/{agent_id}/scorer-suggestions", responses={409: {"description": "no_model"}})
def suggest_for_agent(
    agent_id: uuid.UUID, principal: PrincipalDep, service: ScorerSuggestionServiceDep, engine: EngineDep,
    db: SessionDep, stores: StoresDep, launcher: RunLauncherDep,
    body: Annotated[SuggestRequest | None, Body()] = None,
) -> JSONResponse:
    """Suggest scorers and suite evaluators for an agent (see the module docstring)."""
    body = body or SuggestRequest()
    return JSONResponse(content=service.suggest(
        principal.tenant_id, principal.user_id, "agent", agent_id, max=body.max, include_runs=body.include_runs,
        ports=SuggesterPorts(engine, principal, db, stores, launcher),
    ))


@router.post("/workflows/{workflow_id}/scorer-suggestions", responses={409: {"description": "no_model"}})
def suggest_for_workflow(
    workflow_id: uuid.UUID, principal: PrincipalDep, service: ScorerSuggestionServiceDep, engine: EngineDep,
    db: SessionDep, stores: StoresDep, launcher: RunLauncherDep,
    body: Annotated[SuggestRequest | None, Body()] = None,
) -> JSONResponse:
    """Suggest suite evaluators for a workflow (every suggestion is `evaluator` scope)."""
    body = body or SuggestRequest()
    return JSONResponse(content=service.suggest(
        principal.tenant_id, principal.user_id, "workflow", workflow_id, max=body.max,
        include_runs=body.include_runs, ports=SuggesterPorts(engine, principal, db, stores, launcher),
    ))
