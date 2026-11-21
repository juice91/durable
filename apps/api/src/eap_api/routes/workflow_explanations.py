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

import json
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from eap_api.deps import SessionDep, get_database_engine
from eap_api.auth import Principal, current_principal
from eap_api.deps import get_run_launcher
from eap_api.launcher import RunLauncher
from eap_api.models import AgentModel, AgentVersionModel
from eap_api.request_guard import GuardedJsonRoute
from eap_api.routes.architect import _run_drafter, _seed_one, _spec_model, seed_model
from eap_api.routes.runs import StoresDep
from eap_api.schemas import ErrorDetail
from eap_api.workflows.models import WorkflowExplanationModel, WorkflowModel, WorkflowVersionModel
from eap_observability import redact
from eap_run_store.definition_errors import DefinitionError, refuse
from eap_schemas.tools import TOOL_DECLARATIONS
from eap_schemas.workflow_explanations import (
    EXPLAINER_NAME,
    EXPLAINER_SLUG,
    INSTRUCTION_EXCERPT,
    Locale,
    clip_graphemes,
    explainer_agent_spec,
    finalize_explanation,
)

router = APIRouter(
    tags=["workflow-explanations"],
    responses={404: {"model": ErrorDetail}, 409: {"model": ErrorDetail}, 422: {"model": ErrorDetail}},
    route_class=GuardedJsonRoute,
)
PrincipalDep = Annotated[Principal, Depends(current_principal)]

WORKFLOW_NOT_FOUND = "Workflow not found"
EXPLAIN_TIMEOUT_S = 90.0
MAX_PROMPT_CHARS = 90000
MAX_TOOL_TEXT = 240
LOCALE_NAMES = {"zh-TW": "zh-TW (Traditional Chinese, 繁體中文; formal, clear wording)", "en": "en (plain English)"}


class ExplainRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int | None = None
    locale: Locale = "zh-TW"
    refresh: bool = False


# ------------------------------------------------------------------------------------ the prompt


def _step_nodes(steps: Any) -> list[dict[str, Any]]:
    """Every step dict at every depth, in spec order."""
    out: list[dict[str, Any]] = []
    for step in steps if isinstance(steps, list) else []:
        if isinstance(step, dict):
            out.append(step)
            body = step.get("body")
            if isinstance(body, dict):
                out.extend(_step_nodes(body.get("steps")))
    return out


def step_ids(spec: dict[str, Any]) -> set[str]:
    return {str(s["id"]) for s in _step_nodes(spec.get("steps")) if "id" in s}


def _one_line(value: str, limit: int) -> str:
    return clip_graphemes(" ".join(value.split()), limit)


def build_prompt(db: Session, tenant_id: str, spec: dict[str, Any], locale: str) -> str:
    """The explainer's input: the language, the redacted spec, the tools' titles and one-line descriptions,
    the agents' names and instruction excerpts."""
    nodes = _step_nodes(spec.get("steps"))
    tool_lines: list[str] = []
    for tool_id in dict.fromkeys(str(n["tool_id"]) for n in nodes if n.get("type") == "tool" and n.get("tool_id")):
        declaration = TOOL_DECLARATIONS.get(tool_id)
        if declaration is None:
            tool_lines.append(f"- {tool_id}: (not in the platform catalogue)")
        else:
            tool_lines.append(f"- {tool_id}: {declaration.title}. {_one_line(declaration.description, MAX_TOOL_TEXT)}")
    agent_lines: list[str] = []
    for agent_id in dict.fromkeys(str(n["agent_id"]) for n in nodes if n.get("type") == "agent" and n.get("agent_id")):
        agent = db.query(AgentModel).filter(AgentModel.id == agent_id, AgentModel.tenant_id == tenant_id).first()
        if agent is None:
            agent_lines.append(f"- {agent_id}: (agent not found)")
            continue
        pinned = next((n.get("version") for n in nodes if str(n.get("agent_id")) == agent_id and n.get("version")), None)
        query = db.query(AgentVersionModel).filter(AgentVersionModel.agent_id == agent_id)
        version = (
            query.filter(AgentVersionModel.version == pinned).first()
            if pinned else query.order_by(AgentVersionModel.version.desc()).first()
        )
        instructions = ((version.spec if version else agent.draft_spec) or {}).get("instructions") or ""
        excerpt = json.dumps(clip_graphemes(instructions, INSTRUCTION_EXCERPT), ensure_ascii=False)
        agent_lines.append(f"- {agent_id}: name {json.dumps(agent.name, ensure_ascii=False)}, instructions start {excerpt}")
    spec_json = json.dumps(spec, ensure_ascii=False, sort_keys=True, default=str)
    prompt = "\n".join([
        f"Write in: {LOCALE_NAMES[locale]}", "", "--- Workflow spec (JSON) ---", spec_json, "",
        "--- Tools the steps call ---", *(tool_lines or ["(none)"]), "",
        "--- Agents the steps run ---", *(agent_lines or ["(none)"]),
    ])
    return redact(prompt, max_length=MAX_PROMPT_CHARS)


# ------------------------------------------------------------------------------------- the run


def _explainer(engine: Engine, principal: Principal) -> str:
    """The tenant's explainer agent id (seeded when missing)."""
    try:
        with engine.begin() as conn:
            row, _ = _seed_one(conn, principal.tenant_id, principal.user_id, EXPLAINER_SLUG, EXPLAINER_NAME,
                               explainer_agent_spec, [])
    except DefinitionError as exc:
        if exc.code == "choose_model":
            raise refuse(409, "No model is available for the workflow explainer.", code="no_model", **exc.extra) from None
        raise
    if _spec_model(row.draft_spec) is None and seed_model() is None:
        raise refuse(409, "No model is available for the workflow explainer.", code="no_model")
    return str(row.id)


_RELABEL = {"draft_failed": "explanation_failed", "draft_timeout": "explanation_timeout", "draft_invalid": "explanation_invalid"}


def _version(db: Session, workflow_id: uuid.UUID, tenant_id: str, number: int | None) -> WorkflowVersionModel:
    if db.query(WorkflowModel.id).filter(WorkflowModel.id == str(workflow_id), WorkflowModel.tenant_id == tenant_id).first() is None:
        raise refuse(404, WORKFLOW_NOT_FOUND)
    query = db.query(WorkflowVersionModel).filter(
        WorkflowVersionModel.workflow_id == str(workflow_id), WorkflowVersionModel.tenant_id == tenant_id
    )
    row = query.filter(WorkflowVersionModel.version == number).first() if number else query.order_by(
        WorkflowVersionModel.version.desc()).first()
    if row is None:
        raise refuse(404, "That version is not published." if number else "The workflow has no published version.",
                     code="version_not_found")
    return row


def _stored(db: Session, tenant_id: str, workflow_id: uuid.UUID, version: int, locale: str) -> WorkflowExplanationModel | None:
    return (
        db.query(WorkflowExplanationModel)
        .filter(
            WorkflowExplanationModel.tenant_id == tenant_id, WorkflowExplanationModel.workflow_id == str(workflow_id),
            WorkflowExplanationModel.version == version, WorkflowExplanationModel.locale == locale,
        )
        .first()
    )


def _present(row: WorkflowExplanationModel) -> dict[str, Any]:
    created: datetime = row.created_at
    return {
        "workflow_id": row.workflow_id, "version": row.version, "locale": row.locale, "model": row.model,
        "created_at": created.isoformat(), **row.body,
    }


@router.get("/workflows/{workflow_id}/explanation")
def get_explanation(
    workflow_id: uuid.UUID, principal: PrincipalDep, db: SessionDep, version: Annotated[int | None, Query(ge=1)] = None,
    locale: Locale = "zh-TW",
) -> JSONResponse:
    """The stored explanation of a published version (default: the latest), 404 `explanation_missing` when none."""
    row = _version(db, workflow_id, principal.tenant_id, version)
    stored = _stored(db, principal.tenant_id, workflow_id, row.version, locale)
    if stored is None:
        raise refuse(404, "No explanation has been generated for this version yet.", code="explanation_missing")
    return JSONResponse(content=_present(stored))


@router.post("/workflows/{workflow_id}/explanation", responses={409: {"description": "no_model"}})
def explain(
    workflow_id: uuid.UUID, request: Request, principal: PrincipalDep, db: SessionDep, stores: StoresDep,
    launcher: Annotated[RunLauncher, Depends(get_run_launcher)], body: ExplainRequest | None = None,
) -> JSONResponse:
    """Generate (or, with a stored one and `refresh` false, return) the explanation of a published version."""
    body = body or ExplainRequest()
    if body.version is not None and body.version < 1:
        raise refuse(422, "version must be 1 or more.", code="invalid_version")
    row = _version(db, workflow_id, principal.tenant_id, body.version)
    if not body.refresh:
        stored = _stored(db, principal.tenant_id, workflow_id, row.version, body.locale)
        if stored is not None:
            return JSONResponse(content=_present(stored))
    spec: dict[str, Any] = row.spec
    engine: Engine = get_database_engine(request)
    explainer_id = _explainer(engine, principal)
    try:
        parsed, _, raw, _, model = _run_drafter(
            explainer_id, build_prompt(db, principal.tenant_id, spec, body.locale), principal, db, stores, launcher,
            "explanation", EXPLAIN_TIMEOUT_S,
        )
    except DefinitionError as exc:
        if exc.code in _RELABEL:
            detail = exc.detail.replace("drafter", "workflow explainer").replace("definition", "explanation")
            raise type(exc)(detail, code=_RELABEL[exc.code], **exc.extra) from None
        raise
    try:
        answer, dropped = finalize_explanation(parsed, step_ids(spec))
    except ValidationError as exc:
        raise refuse(422, "The workflow explainer's answer is not shaped as asked.", code="explanation_invalid",
                     raw=raw[:2000], problems=[f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()][:10]) from None
    values = {
        "id": str(uuid.uuid4()), "tenant_id": principal.tenant_id, "workflow_id": str(workflow_id), "version": row.version,
        "locale": body.locale, "body": answer, "model": model,
    }
    statement = insert(WorkflowExplanationModel).values(**values).on_conflict_do_update(
        constraint="uq_workflow_explanations_version_locale",
        set_={"body": answer, "model": model, "created_at": datetime.now(timezone.utc)},
    )
    db.execute(statement)
    db.commit()
    stored = _stored(db, principal.tenant_id, workflow_id, row.version, body.locale)
    assert stored is not None
    db.refresh(stored)
    return JSONResponse(content={**_present(stored), "dropped": dropped})

