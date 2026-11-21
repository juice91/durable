"""The Architect: seed, options, draft and page save (Task 5 of the Architect milestone).

- PUT  /architect/model      set the model of the three built-in agents and publish them.
- POST /architect            seed the tenant's three built-in agents (idempotent): 201 when it made one, else 200.
- GET  /architect            seed state plus the agents and workflows the Architect made.
- GET  /architect/options    the allowlist the page's pickers show (what a created agent may use).
- POST /architect/draft      the form's "Fill in the form": one drafter run, its structured answer
                             filtered through the allowlist.
- POST /architect/draft-workflow  the same for the workflow form (the built-in workflow drafter).
- POST /architect/agents     save (and optionally publish) an agent from the restricted format.
- POST /architect/workflows  the same for a workflow.

Ids on this surface are the Architect's slugs (`^[a-z][a-z0-9-]{1,47}$`); a row id appears only as
`agent_id` / `workflow_id` in answers, for links into the Agents and Workflows pages.

The allowlist is computed here from the API's own sources (tool declarations, model discovery, the
tenant's MCP servers and knowledge bases) with the rule the worker applies to its registry
(`eap_schemas.architect.offer_tool`), and `compile_agent` / `compile_workflow` validate every save
against it, so the page cannot grant what the options route did not offer.

Publishing on the page is the signed-in user's own decision: it calls the shared
definitions publish with `created_by` = the user and `created_via` = "architect-page".
A refused publish saves nothing (one transaction).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from eap_api.auth import Principal, current_principal
from eap_api.request_guard import GuardedJsonRoute
from eap_api.service_deps.architect import ArchitectServiceDep
from eap_api.services.architect import (  # noqa: F401 - re-exported: the app's startup reconcile and the tests read them
    DRAFT_TIMEOUT_S,
    MAX_DESCRIPTION_CHARS,
    ensure_seed,
    reconcile_all_seeds,
    seed_model,
)
from eap_schemas.architect import ArchitectAgentSpec, ArchitectWorkflowSpec

router = APIRouter(prefix="/architect", tags=["architect"], route_class=GuardedJsonRoute)

PrincipalDep = Annotated[Principal, Depends(current_principal)]


@router.post("", responses={409: {"description": "choose_model"}})
def seed(principal: PrincipalDep, service: ArchitectServiceDep) -> JSONResponse:
    """Seed the tenant's Architect and its two drafters (idempotent). 201 `{architect_agent_id,
    drafter_agent_id, workflow_drafter_agent_id, model}` when a row was created, 200 with the same shape otherwise (the tenant's edits are kept).
    409 `{detail, code: "choose_model", providers}` when no model is available."""
    body, created = service.ensure_seed(principal.tenant_id, principal.user_id)
    return JSONResponse(content=body, status_code=201 if created else 200)


class ModelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=3, max_length=200, pattern=r"^[^:\s]+:\S+$")  # "provider:name"


@router.put(
    "/model",
    responses={404: {"description": "not_seeded"}, 422: {"description": "model_not_allowed"}},
)
def set_model(body: ModelRequest, principal: PrincipalDep, service: ArchitectServiceDep) -> JSONResponse:
    """Set the model of the tenant's three built-in agents (architect, architect-drafter,
    architect-workflow-drafter): each row's draft gets the new model (nothing else in the draft changes,
    hand-edited instructions stay) and is published as the signed-in user (`created_via`
    "architect-page"), in one transaction. A row whose draft already equals its latest version after the
    change publishes nothing. 200 `{model, agent_ids: [..]}`; 404 `not_seeded`; 422 `model_not_allowed`
    when the platform allowlist or provider discovery does not list the model."""
    return JSONResponse(content=service.set_model(principal.tenant_id, principal.user_id, body.model))


@router.get("")
def state(principal: PrincipalDep, service: ArchitectServiceDep) -> JSONResponse:
    """`{seeded, choose_model, architect_agent_id, drafter_agent_id, workflow_drafter_agent_id, model,
    agents, workflows}`.
    `seeded` is true when both built-in agents exist; `choose_model` is true when they do not and
    no model is available to seed them with. `agents` and `workflows` are the Architect-made
    definitions by id: `{id (slug), agent_id | workflow_id (the row id), name, versions,
    latest_created_via (null, "architect-page" or "architect:<run_id>"), draft (the draft differs from
    the latest version, or nothing is published), archived}`. The built-in agents are not listed."""
    return JSONResponse(content=service.state(principal.tenant_id))


@router.get("/options")
def options(principal: PrincipalDep, service: ArchitectServiceDep) -> JSONResponse:
    """The allowlist for the page: `{tools: [{id, title, description, requires_approval}], mcp_servers:
    [{id, name, tools: [name]}], knowledge_bases: [{id, name}], models: [{id, provider, capabilities:
    {image, pdf, source}}], agents: [{id (slug), name}] (Architect-made, draft or published, not archived: what a
    workflow step may run), default_model}`. Tools exclude the platform and host tools; a tool marked
    `requires_approval` is gated when granted (the save writes the approvals entry). MCP tools of a server
    that does not answer are not listed."""
    return JSONResponse(content=service.build_allowlist(principal.tenant_id).model_dump(mode="json"))


class DraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1, max_length=MAX_DESCRIPTION_CHARS)
    current: ArchitectAgentSpec | None = None


@router.post("/draft", responses={409: {"description": "choose_model"}, 422: {"description": "draft_invalid"}})
def draft(body: DraftRequest, principal: PrincipalDep, service: ArchitectServiceDep) -> JSONResponse:
    """Run the drafter once (a normal agent run of the built-in `architect-drafter`, seeded on first use)
    and wait for its structured answer, at most 60 s. 200 `{spec: ArchitectAgentSpec, assumptions:
    [string], dropped: [{kind: tool | mcp_tool | knowledge_base | model | id, id}], run_id, model}`: the
    spec is cut down to what the allowlist offers (see /architect/options), `dropped` lists what was
    removed. 422 `{code: "draft_invalid", raw, problems}` when the answer is not a valid definition
    (`raw` is the text); 502 `draft_failed` when the run failed; 504 `draft_timeout`; 409 `choose_model`."""
    return JSONResponse(content=service.draft_agent(principal.tenant_id, principal.user_id, body.description, body.current))


class DraftWorkflowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1, max_length=MAX_DESCRIPTION_CHARS)
    current: ArchitectWorkflowSpec | None = None


@router.post(
    "/draft-workflow", responses={409: {"description": "choose_model"}, 422: {"description": "draft_invalid"}}
)
def draft_workflow(body: DraftWorkflowRequest, principal: PrincipalDep, service: ArchitectServiceDep) -> JSONResponse:
    """`/architect/draft` for a workflow (the built-in `architect-workflow-drafter`). 200 `{spec:
    ArchitectWorkflowSpec, assumptions: [string], dropped: [{kind: agent | tool | id, id}], run_id,
    model}`: a step that uses an agent or tool the allowlist did not offer is removed and reported (a
    parallel step keeps its remaining branches), so `spec.steps` may come back empty. 422 `draft_invalid`
    with `raw` and `problems`; 502 `draft_failed`; 504 `draft_timeout`; 409 `choose_model`. The page
    saves the result through POST /architect/workflows, which validates it again."""
    return JSONResponse(
        content=service.draft_workflow(principal.tenant_id, principal.user_id, body.description, body.current)
    )


class SaveAgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spec: ArchitectAgentSpec
    expected_version: int | None = Field(default=None, ge=1)
    publish: bool = False


class SaveWorkflowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spec: ArchitectWorkflowSpec
    expected_version: int | None = Field(default=None, ge=1)
    publish: bool = False


@router.post("/agents", responses={409: {"description": "version_mismatch"}, 422: {"description": "not allowed"}})
def save_agent(body: SaveAgentRequest, principal: PrincipalDep, service: ArchitectServiceDep) -> JSONResponse:
    """Save an agent from the restricted format as the draft of the Architect-made agent with `spec.id`
    (a new one, with an id derived from the name when `spec.id` is empty), and with `publish: true` publish
    it as the signed-in user (`created_via` "architect-page"). `expected_version` is the latest version the
    page last saw (null: none); another latest version is 409 `version_mismatch` with `latest_version`.
    200 `{id, agent_id, version (latest, null when unpublished), published, warnings}`. 422 with the code
    `tool_not_allowed`, `knowledge_base_not_allowed`, `model_not_allowed`, `model_required` or `reserved_id`;
    a refused publish (422 or 409) saves nothing."""
    return JSONResponse(content=service.save_agent(
        principal.tenant_id, principal.user_id, body.spec, expected_version=body.expected_version, publish=body.publish,
    ))


@router.post("/workflows", responses={409: {"description": "version_mismatch"}, 422: {"description": "not allowed"}})
def save_workflow(body: SaveWorkflowRequest, principal: PrincipalDep, service: ArchitectServiceDep) -> JSONResponse:
    """The same for a workflow: the restricted format compiles to a full workflow spec (agent steps may
    name any Architect-made agent, draft or published; publishing needs each to have a published version, 422
    `agent_not_published`; tool steps only offered tools). 200 `{id,
    workflow_id, version, published, warnings}`; 422 `agent_not_allowed`, `tool_not_allowed`,
    `unknown_placeholder`, `workflow_invalid`, `reserved_id`; 409 `version_mismatch`."""
    return JSONResponse(content=service.save_workflow(
        principal.tenant_id, principal.user_id, body.spec, expected_version=body.expected_version, publish=body.publish,
    ))
