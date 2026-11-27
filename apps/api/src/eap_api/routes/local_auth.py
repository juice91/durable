"""Password sign-in of the built-in admin account (docs/admin-account.md): 404 unless the chain has such a provider.

These routes take no bearer: the password, the refresh token or the one-time setup code in the body is the credential. No cookie is set or read,
so there is nothing for another site to ride on. Bodies are small, a refused body is never echoed (the guard answers
location and message only) and every answer is `no-store`."""

from __future__ import annotations

from typing import Any

from eap_iam import SessionTokens, SignInResult
from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict

from eap_api.inbound_guard import client_ip, trusted_proxies_from_env
from eap_api.request_guard import guarded_json_route
from eap_api.service_deps.local_auth import LocalAuthServiceDep

MAX_BODY_BYTES = 4096

router = APIRouter(
    prefix="/auth/local",
    tags=["local-auth"],
    route_class=guarded_json_route(max_body_bytes=MAX_BODY_BYTES, check_storable=False),
)


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class SetupBody(_Body):
    setup_code: str
    username: str
    password: str


class SetupStatus(BaseModel):
    setup_required: bool


class CreatedAdmin(BaseModel):
    username: str


class SignInBody(_Body):
    username: str
    password: str


class ChangePasswordBody(_Body):
    username: str
    current_password: str
    new_password: str


class RefreshBody(_Body):
    refresh_token: str


class SessionView(BaseModel):
    password_change_required: bool = False
    access_token: str | None = None
    refresh_token: str | None = None
    token_type: str | None = None
    expires_in: int | None = None


def _session(tokens: SessionTokens | None) -> dict[str, Any]:
    if tokens is None:
        return {"password_change_required": True}
    return {
        "password_change_required": False, "access_token": tokens.access_token, "refresh_token": tokens.refresh_token,
        "token_type": "Bearer", "expires_in": tokens.expires_in,
    }


def _answer(response: Response, result: SignInResult | SessionTokens) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return _session(result.tokens if isinstance(result, SignInResult) else result)


def _address(request: Request) -> str:
    """The sender's address for the per-address throttle (a trusted proxy's `CF-Connecting-IP`, else the TCP peer)."""
    peer = request.client.host if request.client else None
    return client_ip(peer, request.headers, trusted_proxies_from_env())


@router.get("", response_model=SetupStatus)
def status(response: Response, service: LocalAuthServiceDep) -> dict:
    """What Studio shows first: the setup page (no admin yet) or the sign-in form."""
    response.headers["Cache-Control"] = "no-store"
    return {"setup_required": service.setup_required()}


@router.post("/setup", response_model=CreatedAdmin, status_code=201)
def set_up(body: SetupBody, request: Request, response: Response, service: LocalAuthServiceDep) -> dict:
    """Create the admin with the one-time setup code from the API's log. 404 once there is an admin."""
    response.headers["Cache-Control"] = "no-store"
    return {"username": service.set_up_admin(body.setup_code, body.username, body.password, _address(request))}


@router.post("/sign-in", response_model=SessionView, response_model_exclude_none=True)
def sign_in(body: SignInBody, request: Request, response: Response, service: LocalAuthServiceDep) -> dict:
    return _answer(response, service.sign_in(body.username, body.password, _address(request)))


@router.post("/password", response_model=SessionView, response_model_exclude_none=True)
def change_password(body: ChangePasswordBody, request: Request, response: Response, service: LocalAuthServiceDep) -> dict:
    return _answer(response, service.change_password(
        body.username, body.current_password, body.new_password, _address(request)
    ))


@router.post("/refresh", response_model=SessionView, response_model_exclude_none=True)
def refresh(body: RefreshBody, response: Response, service: LocalAuthServiceDep) -> dict:
    return _answer(response, service.refresh(body.refresh_token))


@router.post("/sign-out", status_code=204)
def sign_out(body: RefreshBody, service: LocalAuthServiceDep) -> Response:
    service.sign_out(body.refresh_token)
    return Response(status_code=204)
