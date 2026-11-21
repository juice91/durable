"""Provider of the credential lookup service (the MCP control endpoint authenticates with it)."""

from __future__ import annotations

from typing import Any

from eap_api.deps import api_key_store_of, database_engine_of
from eap_api.services.auth_lookup import AuthLookupService


def auth_lookup_service_of(app: Any) -> AuthLookupService:
    """The service for `app`, built from its API key store and database engine."""
    return AuthLookupService(api_key_store_of(app), database_engine_of(app))
