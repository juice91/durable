"""GET /models: the models a caller may pick for a run (the playground's model picker, P0.8).

Lists every entry of the platform allowlist (`EAP_ALLOWED_MODELS`), in its order, then the known
scripted `test:*` models the allowlist permits that it does not already name. A `provider:*` entry
is listed as a wildcard (`name` "*", `wildcard` true): Studio offers a free-text name for it, and
the run start checks the chosen name against the allowlist. A model the allowlist forbids is never
listed. The allowlist is platform-wide, so the answer is the same for every tenant; authentication
is still required.

A wildcard of a discoverable provider (`litellm`, and `anthropic` / `openai` when their server key is
set) is expanded by listing that provider's models server-side (eap_api.model_discovery, cached about
5 minutes): each discovered name the allowlist permits is listed with `source` "discovered". When
discovery fails (or the provider is not configured, or lists nothing) the wildcard item stays and
carries `discovery_error`; the response never fails. GET /providers and POST /providers/{id}/test
report each provider's status for the Studio Providers page (no key value is ever returned).
A test reuses a result from the last few seconds; a failed check is forgotten after a short
interval rather than the full success cache.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from eap_api.auth import Principal, current_principal
from eap_api.service_deps.agents import ModelServiceDep
from eap_api.services.model_catalog import SCRIPTED_TEST_MODELS, model_items  # noqa: F401 - re-exported

router = APIRouter(tags=["models"])


@router.get("/models")
def list_models(principal: Annotated[Principal, Depends(current_principal)], service: ModelServiceDep) -> JSONResponse:
    """{"items": [{"id", "provider", "name", "wildcard", "source" ('allowlist' | 'scripted' | 'discovered'), "description", "capabilities" {"image", "pdf", "source" ('gateway' | 'static' | 'assumed')} (absent on a wildcard), "discovery_error"?}]}."""
    return JSONResponse(content=service.models())


@router.get("/providers")
def list_providers(principal: Annotated[Principal, Depends(current_principal)], service: ModelServiceDep) -> JSONResponse:
    """{"items": [{"id", "label", "configured", "env_var" (a name), "base_url_host", "status"
    ('ok' | 'unreachable' | 'not_configured'), "model_count", "checked_at", "error"}]}; a success is
    cached ~5 min, a failure or a missing key only briefly."""
    return JSONResponse(content=service.providers())


@router.post("/providers/{provider_id}/test")
def test_provider(
    provider_id: str, principal: Annotated[Principal, Depends(current_principal)], service: ModelServiceDep
) -> JSONResponse:
    """One provider's status. Refreshes the cache unless this provider was checked within the last
    few seconds. 404 for an unknown provider."""
    return JSONResponse(content=service.test_provider(provider_id))
