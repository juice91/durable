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

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from eap_api import model_discovery
from eap_api.auth import Principal, current_principal
from eap_schemas.models_policy import ModelPolicy

router = APIRouter(tags=["models"])

# The worker's scripted test models (eap_worker.models; any other `test:` name echoes the prompt).
SCRIPTED_TEST_MODELS: tuple[tuple[str, str], ...] = (
    ("echo", "Streams a short answer echoing the prompt"),
    ("ticket", "Calls tickets__create once, then answers"),
    ("first-tool", "Calls the first offered tool once, then answers"),
    ("mcp", "Calls one MCP tool, then reports the result"),
    ("history", "Answers with the conversation history it was given"),
    ("context", "Answers with its rendered instructions"),
    ("moderation", "Moderation verdicts: flags text containing FLAG_ME"),
)


def model_items(policy: ModelPolicy) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in policy.entries:
        provider, _, name = entry.partition(":")
        if name == "*" and provider in model_discovery.PROVIDER_BY_ID:
            status = model_discovery.provider_status(provider)
            discovered = [m for m in status.models if policy.allows(provider, m.name)] if status.status == "ok" else []
            if discovered:
                for entry_found in discovered:
                    found = entry_found.name
                    if f"{provider}:{found}" in seen or f"{provider}:{found}" in policy.entries:
                        continue
                    seen.add(f"{provider}:{found}")
                    items.append({
                        "id": f"{provider}:{found}", "provider": provider, "name": found, "wildcard": False,
                        "source": "discovered", "description": None,
                        "capabilities": entry_found.capabilities.public(),
                    })
                continue
            items.append({
                "id": entry, "provider": provider, "name": name, "wildcard": True,
                "source": "allowlist", "description": None,
                "discovery_error": status.error or "the provider lists no models",
            })
            continue
        seen.add(entry)
        items.append({
            "id": entry, "provider": provider, "name": name, "wildcard": name == "*",
            "source": "allowlist", "description": None,
            **({} if name == "*" else {"capabilities": model_discovery.capabilities_for(provider, name).public()}),
        })
    listed = set(policy.entries)
    for name, description in SCRIPTED_TEST_MODELS:
        model_id = f"test:{name}"
        if model_id not in listed and policy.allows("test", name):
            items.append({
                "id": model_id, "provider": "test", "name": name, "wildcard": False,
                "source": "scripted", "description": description,
                "capabilities": model_discovery.capabilities_for("test", name).public(),
            })
    return items


@router.get("/models")
def list_models(principal: Annotated[Principal, Depends(current_principal)]) -> JSONResponse:
    """{"items": [{"id", "provider", "name", "wildcard", "source" ('allowlist' | 'scripted' | 'discovered'), "description", "capabilities" {"image", "pdf", "source" ('gateway' | 'static' | 'assumed')} (absent on a wildcard), "discovery_error"?}]}."""
    return JSONResponse(content={"items": model_items(ModelPolicy.from_env())})


@router.get("/providers")
def list_providers(principal: Annotated[Principal, Depends(current_principal)]) -> JSONResponse:
    """{"items": [{"id", "label", "configured", "env_var" (a name), "base_url_host", "status"
    ('ok' | 'unreachable' | 'not_configured'), "model_count", "checked_at", "error"}]}; a success is
    cached ~5 min, a failure or a missing key only briefly."""
    return JSONResponse(content={"items": [s.public() for s in model_discovery.all_provider_statuses()]})


@router.post("/providers/{provider_id}/test")
def test_provider(provider_id: str, principal: Annotated[Principal, Depends(current_principal)]) -> JSONResponse:
    """One provider's status. Refreshes the cache unless this provider was checked within the last
    few seconds. 404 for an unknown provider."""
    if provider_id not in model_discovery.PROVIDER_BY_ID:
        raise HTTPException(status_code=404, detail="unknown provider")
    return JSONResponse(content=model_discovery.provider_status(provider_id, refresh=True).public())
