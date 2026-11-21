"""The models a caller may pick: the platform allowlist, expanded by provider discovery (fastapi-free core of
GET /models; the Architect's allowlist and seed model read the same list)."""

from __future__ import annotations

from typing import Any

from eap_api import model_discovery
from eap_schemas.models_policy import ModelPolicy

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
            discovered = [m for m in status.models if policy.allows(provider, m.name)]
            if discovered:
                for entry_found in discovered:
                    found = entry_found.name
                    if f"{provider}:{found}" in seen or f"{provider}:{found}" in policy.entries:
                        continue
                    seen.add(f"{provider}:{found}")
                    items.append({
                        "id": f"{provider}:{found}", "provider": provider, "name": found, "wildcard": False,
                        "source": entry_found.source, "description": None,
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
