"""The dev bearer, accepted only from a loopback TCP peer (design 1.4)."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

from eap_iam import AuthError, PluginContext, Principal, RequestView

TENANT_USER_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
PROVIDER = "loopback-dev"
DEV_AUTH_DISABLED_DETAIL = "dev bearer authentication is not enabled"
NOT_LOOPBACK_DETAIL = "dev bearer is accepted only from a loopback peer"
INVALID_FORMAT = "Invalid Authorization header format"


class DevLoopbackProvider:
    name = "devloopback"
    kinds = frozenset({"user"})

    def __init__(self, env: Mapping[str, str]) -> None:
        self._extra_peers = {p.strip() for p in env.get("EAP_IAM_DEVLOOPBACK_EXTRA_PEERS", "").split(",") if p.strip()}

    def _from_loopback(self, request: RequestView) -> bool:
        if request.is_loopback:
            return True
        return request.peer_host in self._extra_peers and not request.has_proxy_headers

    async def authenticate(self, request: RequestView) -> Principal | None:
        authorization = request.header("authorization")
        # The bearer works in EAP_AUTH_MODE=dev only, read per request as it always was.
        if os.environ.get("EAP_AUTH_MODE") != "dev":
            if authorization is None and "api_key" in request.accepts:
                return None  # a key route answers "missing" itself
            raise AuthError(DEV_AUTH_DISABLED_DETAIL)
        if authorization is None or not authorization.startswith("Bearer dev:"):
            return None
        parts = authorization.split(":")  # "Bearer dev:<tenant>:<user>"
        bearer = "api_key" in request.accepts
        if len(parts) != 3 or parts[0] != "Bearer dev":
            raise AuthError(INVALID_FORMAT, www_authenticate=False)
        tenant_id, user_id = parts[1], parts[2]
        if not tenant_id.strip():
            raise AuthError("Tenant ID cannot be empty", www_authenticate=False)
        if not user_id.strip():
            raise AuthError("User ID cannot be empty", www_authenticate=False)
        if not TENANT_USER_PATTERN.match(tenant_id):
            raise AuthError("Invalid tenant ID format", www_authenticate=False)
        if not TENANT_USER_PATTERN.match(user_id):
            raise AuthError("Invalid user ID format", www_authenticate=False)
        if not self._from_loopback(request):
            raise AuthError(NOT_LOOPBACK_DETAIL, www_authenticate=bearer)
        return Principal(tenant_id=tenant_id.strip(), kind="user", subject=user_id.strip(), provider=PROVIDER)


def create(ctx: PluginContext) -> DevLoopbackProvider:
    return DevLoopbackProvider(ctx.env)
