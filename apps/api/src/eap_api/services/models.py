"""The models a caller may pick for a run, and the status of the model providers.

Lists every entry of the platform allowlist (`EAP_ALLOWED_MODELS`), in its order, then the known scripted
`test:*` models the allowlist permits that it does not already name. A `provider:*` entry is listed as a
wildcard; a wildcard of a discoverable provider is expanded by listing that provider's models server-side
(eap_api.model_discovery, cached). The allowlist is platform-wide: the answer is the same for every tenant.
"""

from __future__ import annotations

from typing import Any

from eap_api import model_discovery
from eap_api.services.errors import NotFound
from eap_api.services.model_catalog import SCRIPTED_TEST_MODELS, model_items  # noqa: F401 - re-exported
from eap_api.services.run_filters import plain_refusals
from eap_schemas.models_policy import ModelPolicy


@plain_refusals
class ModelService:
    def models(self) -> dict[str, Any]:
        return {"items": model_items(ModelPolicy.from_env())}

    def providers(self) -> dict[str, Any]:
        return {"items": [s.public() for s in model_discovery.all_provider_statuses()]}

    def test_provider(self, provider_id: str) -> dict[str, Any]:
        """One provider's status, refreshing the cache unless it was checked within the last few seconds."""
        if provider_id not in model_discovery.PROVIDER_BY_ID:
            raise NotFound("unknown provider")
        return model_discovery.provider_status(provider_id, refresh=True).public()
