"""Personal API keys: a signed-in person's own keys (docs/iam.md, "Personal API keys").

- POST   /iam/me/api-keys            create a key for the caller: 201 with the plaintext `key` once (`Cache-Control: no-store`);
- GET    /iam/me/api-keys            the caller's keys, revoked and expired included; never a secret or hash;
- DELETE /iam/me/api-keys/{key_id}   revoke (204, idempotent); anyone else's key is 404.

A person only: an API key is refused at authentication like on every other people route (401, before the body is read); the dev bearer authenticates but the service refuses it with a 403 saying so. Nothing here logs a key."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Path, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from eap_api.auth import CorePrincipalDep
from eap_api.schemas import ErrorDetail
from eap_api.service_deps.iam import PersonalApiKeyServiceDep
from eap_api.services.api_keys import API_KEY_NAME_MAX_CHARS

router = APIRouter(
    prefix="/iam/me/api-keys",
    tags=["iam"],
    responses={401: {"model": ErrorDetail}, 403: {"model": ErrorDetail}, 404: {"model": ErrorDetail}},
)


class PersonalApiKeyCreate(BaseModel):
    """`scopes` default to read-only (`mcp`); the owner's roles cap them (403 beyond). `expires_at` defaults to 90 days
    from now and may be at most 365 days away: a personal key always expires."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=API_KEY_NAME_MAX_CHARS)
    scopes: list[Literal["agui:run", "mcp", "manage", "otlp"]] = Field(default_factory=lambda: ["mcp"], min_length=1, max_length=8)
    expires_at: datetime | None = None


class PersonalApiKeyOut(BaseModel):
    id: str
    name: str
    prefix: str
    status: Literal["active", "revoked", "expired"]
    scopes: list[str]
    created_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    last_used_at: datetime | None


class PersonalApiKeyCreated(PersonalApiKeyOut):
    key: str


class PersonalApiKeyList(BaseModel):
    items: list[PersonalApiKeyOut]


def _out(record) -> dict:
    return dict(
        id=record.id, name=record.name, prefix=record.prefix, status=record.status(), scopes=list(record.scopes),
        created_at=record.created_at, expires_at=record.expires_at, revoked_at=record.revoked_at,
        last_used_at=record.last_used_at,
    )


@router.post("", status_code=status.HTTP_201_CREATED, response_model=PersonalApiKeyCreated,
             responses={409: {"model": ErrorDetail}, 422: {"model": ErrorDetail}})
async def create_my_api_key(body: PersonalApiKeyCreate, principal: CorePrincipalDep, service: PersonalApiKeyServiceDep) -> JSONResponse:
    """Create a key for the caller. The response is the only place the plaintext key ever appears."""
    created = await service.create(principal, name=body.name, scopes=body.scopes, expires_at=body.expires_at)
    payload = PersonalApiKeyCreated(**_out(created.record), key=created.key)
    return JSONResponse(
        status_code=status.HTTP_201_CREATED, content=payload.model_dump(mode="json"), headers={"Cache-Control": "no-store"}
    )


@router.get("", response_model=PersonalApiKeyList)
async def list_my_api_keys(principal: CorePrincipalDep, service: PersonalApiKeyServiceDep) -> PersonalApiKeyList:
    return PersonalApiKeyList(items=[PersonalApiKeyOut(**_out(r)) for r in await service.list(principal)])


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def revoke_my_api_key(
    key_id: Annotated[str, Path(max_length=64)], principal: CorePrincipalDep, service: PersonalApiKeyServiceDep
) -> Response:
    await service.revoke(principal, key_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
