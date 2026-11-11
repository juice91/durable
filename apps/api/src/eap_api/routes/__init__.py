"""Route definitions for the control-plane API."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
import uuid

from eap_api.auth import Principal, current_principal
from eap_api.database import get_session
from eap_api.models import AgentModel, AgentVersionModel
from eap_api.schemas import AgentCreate, AgentUpdate, ErrorDetail
from eap_schemas import AgentSpec, canonical_spec_hash

router = APIRouter()

SessionDep = Annotated[Session, Depends(get_session)]


def _agent_to_response(agent: AgentModel, latest_version: int | None) -> dict:
    """Convert AgentModel to response dict."""
    return {
        "id": str(agent.id),
        "name": agent.name,
        "draft_spec": agent.draft_spec,
        "latest_version": latest_version,
    }


def _version_to_response(version: AgentVersionModel) -> dict:
    """Convert AgentVersionModel to response dict."""
    return {
        "agent_id": str(version.agent_id),
        "version": version.version,
        "spec": version.spec,
        "spec_hash": version.spec_hash,
        "created_at": version.created_at.isoformat(),
        "created_by": version.created_by,
    }


@router.post(
    "/agents",
    status_code=status.HTTP_201_CREATED,
    response_class=JSONResponse,
    tags=["agents"],
    responses={422: {"model": ErrorDetail}},
)
def create_agent(
    request: AgentCreate,
    principal: Annotated[Principal, Depends(current_principal)],
    db: SessionDep,
) -> dict:
    """Create a new agent."""
    spec_dict = request.spec.model_dump(mode="json")

    agent = AgentModel(
        tenant_id=principal.tenant_id,
        name=request.spec.name,
        draft_spec=spec_dict,
    )
    db.add(agent)
    db.commit()
    db.refresh(agent)

    return JSONResponse(
        content={
            "id": str(agent.id),
            "name": agent.name,
            "draft_spec": agent.draft_spec,
            "latest_version": None,
        },
        status_code=201,
    )


@router.get(
    "/agents",
    response_class=JSONResponse,
    tags=["agents"],
)
def list_agents(
    principal: Annotated[Principal, Depends(current_principal)],
    db: SessionDep,
) -> list[dict]:
    """List agents for the current tenant (non-archived)."""
    agents = (
        db.query(AgentModel)
        .filter(AgentModel.tenant_id == principal.tenant_id, AgentModel.archived == False)
        .all()
    )

    results = []
    for agent in agents:
        latest = (
            db.query(func.max(AgentVersionModel.version))
            .filter(AgentVersionModel.agent_id == agent.id)
            .scalar()
        )
        results.append(_agent_to_response(agent, latest))

    return JSONResponse(content=results)


@router.get(
    "/agents/{agent_id}",
    response_class=JSONResponse,
    tags=["agents"],
    responses={404: {"model": ErrorDetail}},
)
def get_agent(
    agent_id: uuid.UUID,
    principal: Annotated[Principal, Depends(current_principal)],
    db: SessionDep,
) -> dict:
    """Get a specific agent by ID."""
    agent = (
        db.query(AgentModel)
        .filter(
            AgentModel.id == str(agent_id),
            AgentModel.tenant_id == principal.tenant_id,
        )
        .first()
    )
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found",
        )

    latest = (
        db.query(func.max(AgentVersionModel.version))
        .filter(AgentVersionModel.agent_id == str(agent_id))
        .scalar()
    )

    return JSONResponse(content=_agent_to_response(agent, latest))


@router.patch(
    "/agents/{agent_id}",
    response_class=JSONResponse,
    tags=["agents"],
    responses={404: {"model": ErrorDetail}, 422: {"model": ErrorDetail}},
)
def patch_agent(
    agent_id: uuid.UUID,
    request: AgentUpdate,
    principal: Annotated[Principal, Depends(current_principal)],
    db: SessionDep,
) -> dict:
    """Update an agent's draft spec."""
    agent = (
        db.query(AgentModel)
        .filter(
            AgentModel.id == str(agent_id),
            AgentModel.tenant_id == principal.tenant_id,
        )
        .first()
    )
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found",
        )

    spec_dict = request.spec.model_dump(mode="json")

    agent.name = request.spec.name
    agent.draft_spec = spec_dict
    db.commit()
    db.refresh(agent)

    latest = (
        db.query(func.max(AgentVersionModel.version))
        .filter(AgentVersionModel.agent_id == str(agent_id))
        .scalar()
    )

    return JSONResponse(content=_agent_to_response(agent, latest))


@router.post(
    "/agents/{agent_id}/versions",
    status_code=status.HTTP_201_CREATED,
    response_class=JSONResponse,
    tags=["agents", "versions"],
    responses={404: {"model": ErrorDetail}, 409: {"model": ErrorDetail}, 422: {"model": ErrorDetail}},
)
def publish_version(
    agent_id: uuid.UUID,
    principal: Annotated[Principal, Depends(current_principal)],
    db: SessionDep,
) -> dict:
    """Publish the agent's draft as a new version."""
    agent = (
        db.query(AgentModel)
        .filter(
            AgentModel.id == str(agent_id),
            AgentModel.tenant_id == principal.tenant_id,
        )
        .first()
    )
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found",
        )

    if agent.draft_spec is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Agent has no draft spec",
        )

    latest = (
        db.query(func.max(AgentVersionModel.version))
        .filter(AgentVersionModel.agent_id == str(agent_id))
        .scalar()
    )

    spec = AgentSpec.model_validate(agent.draft_spec)
    draft_hash = canonical_spec_hash(spec)

    if latest is not None:
        latest_version = (
            db.query(AgentVersionModel)
            .filter(
                AgentVersionModel.agent_id == str(agent_id),
                AgentVersionModel.version == latest,
            )
            .first()
        )
        if latest_version and latest_version.spec_hash == draft_hash:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"no changes since version {latest}",
            )

    new_version_num = (latest or 0) + 1
    version = AgentVersionModel(
        agent_id=str(agent_id),
        tenant_id=principal.tenant_id,
        version=new_version_num,
        spec=spec.model_dump(mode="json"),
        spec_hash=draft_hash,
        created_by=principal.user_id,
    )
    db.add(version)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="concurrent publish, retry",
        )
    db.refresh(version)

    return JSONResponse(
        content={
            "agent_id": str(version.agent_id),
            "version": version.version,
            "spec": version.spec,
            "spec_hash": version.spec_hash,
            "created_at": version.created_at.isoformat(),
            "created_by": version.created_by,
        },
        status_code=201,
    )


@router.get(
    "/agents/{agent_id}/versions",
    response_class=JSONResponse,
    tags=["agents", "versions"],
    responses={404: {"model": ErrorDetail}},
)
def list_versions(
    agent_id: uuid.UUID,
    principal: Annotated[Principal, Depends(current_principal)],
    db: SessionDep,
) -> list[dict]:
    """List versions for an agent (newest first)."""
    agent = (
        db.query(AgentModel)
        .filter(
            AgentModel.id == str(agent_id),
            AgentModel.tenant_id == principal.tenant_id,
        )
        .first()
    )
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found",
        )

    versions = (
        db.query(AgentVersionModel)
        .filter(AgentVersionModel.agent_id == str(agent_id))
        .order_by(AgentVersionModel.version.desc())
        .all()
    )

    return JSONResponse(content=[_version_to_response(v) for v in versions])


@router.get(
    "/agents/{agent_id}/versions/{version_num}",
    response_class=JSONResponse,
    tags=["agents", "versions"],
    responses={404: {"model": ErrorDetail}},
)
def get_version(
    agent_id: uuid.UUID,
    version_num: int,
    principal: Annotated[Principal, Depends(current_principal)],
    db: SessionDep,
) -> dict:
    """Get a specific version by number."""
    agent = (
        db.query(AgentModel)
        .filter(
            AgentModel.id == str(agent_id),
            AgentModel.tenant_id == principal.tenant_id,
        )
        .first()
    )
    if agent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found",
        )

    version = (
        db.query(AgentVersionModel)
        .filter(
            AgentVersionModel.agent_id == str(agent_id),
            AgentVersionModel.version == version_num,
        )
        .first()
    )
    if version is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Version not found",
        )

    return JSONResponse(content=_version_to_response(version))
