"""Tenant API keys for third-party AG-UI frontends: create (the plaintext is returned once), list, revoke."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from eap_api.models import AgentModel
from eap_api.services import access as acl
from eap_api.services.access import Access, AccessBound
from eap_api.workflows.models import WorkflowModel
from eap_run_store import ApiKeyLimitExceeded, ApiKeyRecord, ApiKeyStore  # noqa: F401  (ApiKeyRecord: re-exported for the route)
from eap_run_store.api_keys import (  # noqa: F401  (re-exported: the route's body models use the limits)
    API_KEY_ALLOWLIST_ID_MAX_CHARS,
    API_KEY_ALLOWLIST_MAX_ITEMS,
    API_KEY_NAME_MAX_CHARS,
    API_KEY_ORIGIN_MAX_CHARS,
    API_KEY_ORIGINS_MAX_ITEMS,
)
from eap_run_store.definition_errors import DefinitionConflict, DefinitionInvalid, DefinitionNotFound

API_KEY_NOT_FOUND_DETAIL = "API key not found"


class ApiKeyService(AccessBound):
    """Tenant API keys (design 2.2: admin creates, lists and revokes; a key's owner may read and revoke it)."""

    def __init__(self, store: ApiKeyStore, db: Session, access: Access | None = None) -> None:
        self._store = store
        self._db = db
        self._access = access  # None: no caller to check

    def _unknown_ids(self, model, tenant_id: str, ids: list[str] | None) -> list[str]:
        if not ids:
            return []
        found = set(self._db.execute(select(model.id).where(model.tenant_id == tenant_id, model.id.in_(ids))).scalars())
        return [i for i in dict.fromkeys(ids) if i not in found]

    def create(
        self,
        tenant_id: str,
        created_by: str,
        *,
        name: str,
        scopes: list[str],
        agent_ids: list[str] | None,
        workflow_ids: list[str] | None,
        allowed_origins: list[str] | None,
        expires_at: datetime | None,
    ):
        """Create a key (the result carries `.record` and the plaintext `.key`). Unknown agent or workflow ids of
        this tenant, a bad origin or a past `expires_at` are refused (422); past the tenant's active-key cap, 409."""
        acl.require(self._access, "create", "api_key", None)
        unknown_agents = self._unknown_ids(AgentModel, tenant_id, agent_ids)
        if unknown_agents:
            raise DefinitionInvalid(f"unknown agent ids: {', '.join(unknown_agents)}")
        unknown_workflows = self._unknown_ids(WorkflowModel, tenant_id, workflow_ids)
        if unknown_workflows:
            raise DefinitionInvalid(f"unknown workflow ids: {', '.join(unknown_workflows)}")
        try:
            created = self._store.create(
                tenant_id, name=name, created_by=created_by, scopes=scopes, agent_ids=agent_ids,
                workflow_ids=workflow_ids, allowed_origins=allowed_origins, expires_at=expires_at,
            )
            if self._access is not None:
                self._access.register("api_key", created.record.id)  # before the response: the creator owns it
            return created
        except ApiKeyLimitExceeded as exc:
            raise DefinitionConflict(str(exc)) from None
        except ValueError as exc:
            raise DefinitionInvalid(str(exc)) from None

    def list(self, tenant_id: str) -> list[ApiKeyRecord]:
        records = self._store.list(tenant_id)
        if self._access is None:
            return records
        allowed = set(acl.keep(acl.visible(self._access, "api_key"), [r.id for r in records]))
        return [r for r in records if r.id in allowed]

    def revoke(self, tenant_id: str, key_id: str) -> None:
        """Revoke a key (idempotent); another tenant's or an unknown key is 404."""
        acl.require(self._access, "revoke", "api_key", key_id)
        if self._store.revoke(tenant_id, key_id) is None:
            raise DefinitionNotFound(API_KEY_NOT_FOUND_DETAIL)
