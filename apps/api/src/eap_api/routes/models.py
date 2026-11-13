"""GET /models: the models a caller may pick for a run (the playground's model picker, P0.8).

Lists every entry of the platform allowlist (`EAP_ALLOWED_MODELS`), in its order, then the known
scripted `test:*` models the allowlist permits that it does not already name. A `provider:*` entry
is listed as a wildcard (`name` "*", `wildcard` true): Studio offers a free-text name for it, and
the run start checks the chosen name against the allowlist. A model the allowlist forbids is never
listed. The allowlist is platform-wide, so the answer is the same for every tenant; authentication
is still required.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

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
    for entry in policy.entries:
        provider, _, name = entry.partition(":")
        items.append({
            "id": entry, "provider": provider, "name": name, "wildcard": name == "*",
            "source": "allowlist", "description": None,
        })
    listed = set(policy.entries)
    for name, description in SCRIPTED_TEST_MODELS:
        model_id = f"test:{name}"
        if model_id not in listed and policy.allows("test", name):
            items.append({
                "id": model_id, "provider": "test", "name": name, "wildcard": False,
                "source": "scripted", "description": description,
            })
    return items


@router.get("/models")
def list_models(principal: Annotated[Principal, Depends(current_principal)]) -> JSONResponse:
    """{"items": [{"id", "provider", "name", "wildcard", "source" ('allowlist' | 'scripted'), "description"}]}."""
    return JSONResponse(content={"items": model_items(ModelPolicy.from_env())})
