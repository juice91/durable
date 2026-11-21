"""AI explanation of a published workflow: `GET` and `POST /workflows/{id}/explanation`.

One run of the built-in "workflow-explainer" agent (seeded on first use like the scorer suggester, with the
Architect's ranked default model) reads one published version's spec and answers in plain language:
`{summary, inputs: [{name, meaning}], outcomes: [{status, meaning}], steps: [{id, note}]}`. The answer is
validated, cut to its caps and stored per (tenant, workflow, version, locale) in `workflow_explanations`; a
version is immutable, so a stored explanation never goes stale. It describes the published version and
changes nothing; a draft has no explanation.

- `GET ?version=<n>&locale=zh-TW|en`: the stored explanation (version default: latest). 404
  `explanation_missing` when none is stored yet, 404 `version_not_found`.
- `POST {version?, locale?, refresh?}`: runs the helper (up to 90 s) and stores the answer; with a stored
  explanation and `refresh` false it answers with that one and runs nothing.

200 `{workflow_id, version, locale, model, created_at, summary, inputs, outcomes, steps}`. Errors: 404, 409
`no_model`, 422 (body; `explanation_invalid` when the answer is not shaped as asked), 502
`explanation_failed`, 504 `explanation_timeout`.

What the helper sees is bounded and redacted: the version's spec, for each tool step the tool's title and
description, for each agent step the agent's name and the first 300 graphemes of its instructions. Never
secrets' values, memory, knowledge-base content, run data or other tenants.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from eap_api.auth import Principal, current_principal
from eap_api.request_guard import GuardedJsonRoute
from eap_api.schemas import ErrorDetail
from eap_api.service_deps.architect import ArchitectServiceDep
from eap_api.service_deps.workflow_explanations import WorkflowExplanationServiceDep
from eap_api.services.architect import ArchitectService
from eap_api.services.workflow_explanations import (  # noqa: F401 - re-exported: the API's users and tests import them from here
    LOCALE_NAMES,
    DefinitionError,
    refuse,
    MAX_PROMPT_CHARS,
    MAX_TOOL_TEXT,
    WORKFLOW_NOT_FOUND,
)
from eap_schemas.workflow_explanations import Locale

router = APIRouter(
    tags=["workflow-explanations"],
    responses={404: {"model": ErrorDetail}, 409: {"model": ErrorDetail}, 422: {"model": ErrorDetail}},
    route_class=GuardedJsonRoute,
)
PrincipalDep = Annotated[Principal, Depends(current_principal)]

EXPLAIN_TIMEOUT_S = 90.0


class ExplainRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int | None = None
    locale: Locale = "zh-TW"
    refresh: bool = False


class ExplainerPorts:
    """The explainer agent and one run of it, through the Architect service's seeding and drafter run."""

    def __init__(self, architect: ArchitectService, tenant_id: str) -> None:
        self.architect, self.tenant_id = architect, tenant_id

    def ensure_explainer(self, tenant_id: str, user_id: str) -> str:
        """The tenant's explainer agent id (seeded when missing)."""
        return self.architect.ensure_explainer(tenant_id, user_id)

    def run_drafter(self, explainer_id: str, prompt: str) -> tuple[dict[str, Any], list[str], str, str, str | None]:
        return self.architect.run_drafter(self.tenant_id, explainer_id, prompt, "explanation", EXPLAIN_TIMEOUT_S)


@router.get("/workflows/{workflow_id}/explanation")
def get_explanation(
    workflow_id: uuid.UUID, principal: PrincipalDep, service: WorkflowExplanationServiceDep,
    version: Annotated[int | None, Query(ge=1)] = None, locale: Locale = "zh-TW",
) -> JSONResponse:
    """The stored explanation of a published version (default: the latest), 404 `explanation_missing` when none."""
    return JSONResponse(content=service.get(principal.tenant_id, workflow_id, version, locale))


@router.post("/workflows/{workflow_id}/explanation", responses={409: {"description": "no_model"}})
def explain(
    workflow_id: uuid.UUID, principal: PrincipalDep, service: WorkflowExplanationServiceDep,
    architect: ArchitectServiceDep, body: ExplainRequest | None = None,
) -> JSONResponse:
    """Generate (or, with a stored one and `refresh` false, return) the explanation of a published version."""
    body = body or ExplainRequest()
    return JSONResponse(content=service.explain(
        principal.tenant_id, principal.user_id, workflow_id, version=body.version, locale=body.locale,
        refresh=body.refresh, ports=ExplainerPorts(architect, principal.tenant_id),
    ))
