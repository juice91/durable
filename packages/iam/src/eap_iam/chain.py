"""The identity chain (design 1.2): configured order, first match wins, an invalid credential never falls through."""

from __future__ import annotations

from collections.abc import Sequence

from eap_iam.protocols import IdentityProvider
from eap_iam.types import AuthError, Principal, RequestView

MISSING_AUTHORIZATION_DETAIL = "Missing Authorization header"
INVALID_AUTHORIZATION_DETAIL = "Invalid Authorization header format"
# Kept word for word: the answer every route gave when no dev bearer provider was active.
NO_USER_PROVIDER_DETAIL = "dev bearer authentication is not enabled"


class IdentityChain:
    def __init__(self, providers: Sequence[IdentityProvider]) -> None:
        self.providers: tuple[IdentityProvider, ...] = tuple(providers)

    async def authenticate(self, view: RequestView) -> Principal:
        """The principal of the request, or AuthError (the entry point turns it into its protocol's refusal)."""
        applicable = [p for p in self.providers if p.kinds & view.accepts]
        if not applicable:
            raise AuthError(NO_USER_PROVIDER_DETAIL)
        for provider in applicable:
            principal = await provider.authenticate(view)  # AuthError propagates: no fall-through
            if principal is not None:
                if principal.kind not in view.accepts:  # a provider must not hand a route a kind it refuses
                    raise AuthError(INVALID_AUTHORIZATION_DETAIL, www_authenticate=False)
                return principal
        bearer = "api_key" in view.accepts
        if view.header("authorization") is None:
            raise AuthError(MISSING_AUTHORIZATION_DETAIL, www_authenticate=bearer)
        if "user" in view.accepts and not any("user" in p.kinds for p in self.providers):
            raise AuthError(NO_USER_PROVIDER_DETAIL)
        raise AuthError(INVALID_AUTHORIZATION_DETAIL, www_authenticate=False)
