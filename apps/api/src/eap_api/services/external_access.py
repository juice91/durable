"""External access management (design sections 3, 4, 5.2 and 9): external clients, their `run` keys, workflow exports,
the enable card and the tenant's A2A endpoint.

Authorisation (docs/iam.md, "External access"): `tenant.manage_external_access` (an admin) creates and changes clients,
keys and exports and enables an export; `tenant.read_external_access` (editors) reads them. Making a key is also creating
an API key. Enabling is a person's act: it echoes the `approval_hash` the card showed, which covers every field of the
export and the contract (input schema and side effects) of the version it would run, so a change between the card and the
click is refused. A change to any of them disables the export again."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from eap_run_store import ApiKeyLimitExceeded, ApiKeyRecord, ApiKeyStore
from eap_run_store.api_keys import API_KEY_ALLOWLIST_MAX_ITEMS, RUN_SCOPE
from eap_run_store.definition_errors import DefinitionError
from eap_run_store.external_access import (
    PROTOCOLS,
    RESULT_MODES,
    ExternalAccessStore,
    ExternalNameConflict,
    WorkflowContract,
    external_warnings,
    workflow_contract,
)
from eap_run_store.revisions import RevisionMismatch
from eap_schemas.workflows import WorkflowSpec
from pydantic import ValidationError
from sqlalchemy.orm import Session

from eap_api.external_settings import CLIENT_ACTIVE_CEILING, CLIENT_PER_DAY_CEILING, ExternalSettings
from eap_api.services import access as acl
from eap_api.services.access import Access, AccessBound
from eap_api.services.errors import Conflict, Forbidden, InvalidInput, NotFound
from eap_api.services.revision_conflict import stale_revision
from eap_api.services.workflow_runs import WorkflowRunService
from eap_api.workflows.models import WorkflowModel, WorkflowVersionModel

EXPORT_NAME = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
CLIENT_NAME_MAX_CHARS = 100
CLIENT_DESCRIPTION_MAX_CHARS = 500
TITLE_MAX_CHARS = 200
DESCRIPTION_MAX_CHARS = 2000
MAX_TAGS, TAG_MAX_CHARS = 20, 50
MAX_EXAMPLES, EXAMPLE_MAX_CHARS = 10, 2000
DEFAULT_MAX_RUNTIME_SECONDS = 3600
MAX_CLIENTS_PER_TENANT = 100
MAX_EXPORTS_PER_TENANT = 100
CARD_NAME_MAX_CHARS = 100
A2A_PATH = "/api/v1/external/a2a/"
A2A_CARD_PATH = "/.well-known/agent-card.json"
# What the approval covers: the public contract of the export (the version's own contract is hashed beside it).
HASH_FIELDS = (
    "name", "title", "description", "tags", "examples", "workflow_id", "workflow_version", "protocols", "result_mode",
    "client_inputs", "max_runtime_seconds", "require_idempotency_key",
)
CHANGED = "export_changed"
DISABLED = "disabled"
CLIENT_NOT_FOUND = "external client not found"
EXPORT_NOT_FOUND = "export not found"


def config_hash(values: dict[str, Any]) -> str:
    material = {k: values[k] for k in HASH_FIELDS}
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def approval_hash(config: str, contract: str) -> str:
    return hashlib.sha256(f"{config}:{contract}".encode()).hexdigest()


def _utc(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _body(payload: Any, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise InvalidInput("body must be a JSON object", code="body_invalid")
    unknown = set(payload) - allowed
    if unknown:
        raise InvalidInput(f"unknown field(s): {', '.join(sorted(unknown))}", code="body_invalid")
    return payload


def _text(value: Any, field: str, maximum: int, *, minimum: int = 0) -> str:
    if not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum:
        raise InvalidInput(f"{field} must be {minimum} to {maximum} characters", code=f"{field}_invalid")
    return value.strip()


def _strings(value: Any, field: str, maximum: int, item_maximum: int) -> list[str]:
    if (
        not isinstance(value, list) or len(value) > maximum
        or not all(isinstance(v, str) and 1 <= len(v) <= item_maximum for v in value)
    ):
        raise InvalidInput(f"{field} is a list of at most {maximum} strings of at most {item_maximum} characters",
                           code=f"{field}_invalid")
    return list(value)


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise InvalidInput(f"{field} must be true or false", code=f"{field}_invalid")
    return value


class ExternalAccessService(AccessBound):
    def __init__(
        self, store: ExternalAccessStore, keys: ApiKeyStore, db: Session, runs: WorkflowRunService,
        settings: ExternalSettings, access: Access | None = None,
    ) -> None:
        self._store = store
        self._keys = keys
        self._db = db
        self._runs = runs
        self._settings = settings
        self._access = access  # None: no caller to check (see eap_api.services.access)

    def _require_read(self) -> None:
        acl.require(self._access, "read_external_access", "tenant", None)

    def _require_manage(self) -> None:
        acl.require(self._access, "manage_external_access", "tenant", None)

    def _grants_enforced(self) -> bool:
        return self._settings.grants_requested and self._access is not None and self._access.supports_grants

    def settings(self) -> dict[str, Any]:
        """What the page needs to know about the platform: whether external access is on and why enabling may fail."""
        self._require_read()
        return {
            "enabled": self._settings.enabled, "base_url": self._settings.base_url,
            "grants_enforced": self._grants_enforced(),
            "can_manage": self._access is None or self._access.allowed("manage_external_access", "tenant", None),
            "max_runtime_seconds": self._settings.max_runtime_seconds,
        }

    # ------------------------------------------------------------------------------------------ the A2A endpoint

    def _endpoint_view(self, row: dict[str, Any] | None) -> dict[str, Any]:
        if row is None:
            return {"endpoint": None}
        url = None if not self._settings.base_url else f"{self._settings.base_url}{A2A_PATH}{row['endpoint_id']}"
        return {"endpoint": {
            **{k: row[k] for k in ("endpoint_id", "card_name", "card_description", "enabled", "created_by")},
            "url": url, "card_url": None if url is None else url + A2A_CARD_PATH,
            "created_at": _utc(row["created_at"]), "updated_at": _utc(row["updated_at"]),
        }}

    def get_a2a_endpoint(self, tenant_id: str) -> dict[str, Any]:
        self._require_read()
        return self._endpoint_view(self._store.get_endpoint(tenant_id))

    def save_a2a_endpoint(self, tenant_id: str, actor: str, payload: Any) -> dict[str, Any]:
        """Create or change the tenant's A2A endpoint: `{card_name, card_description, enabled}`, all of them. The name
        and description are what anyone with the URL reads in the public Agent Card. Enabling needs
        `EAP_EXTERNAL_BASE_URL` (the card names the public address)."""
        self._require_manage()
        payload = _body(payload, {"card_name", "card_description", "enabled"})
        name = _text(payload.get("card_name"), "card_name", CARD_NAME_MAX_CHARS, minimum=1)
        description = _text(payload.get("card_description"), "card_description", DESCRIPTION_MAX_CHARS, minimum=1)
        enabled = _boolean(payload.get("enabled"), "enabled")
        if enabled and not self._settings.base_url:
            raise Conflict("EAP_EXTERNAL_BASE_URL is not set: nothing outside could reach the endpoint",
                           code="external_base_url_unset")
        return self._endpoint_view(self._store.save_endpoint(
            tenant_id, actor, card_name=name, card_description=description, enabled=enabled
        ))

    def rotate_a2a_endpoint(self, tenant_id: str) -> dict[str, Any]:
        """A new endpoint id: the old URL stops answering at once, and every agent needs the new one."""
        self._require_manage()
        row = self._store.rotate_endpoint(tenant_id)
        if row is None:
            raise NotFound("the A2A endpoint does not exist yet", code="a2a_endpoint_not_found")
        return self._endpoint_view(row)

    # ------------------------------------------------------------------------------------------ clients and keys

    @staticmethod
    def _key_view(record: ApiKeyRecord) -> dict[str, Any]:
        return {
            "id": record.id, "name": record.name, "prefix": record.prefix, "status": record.status(),
            "scopes": list(record.scopes), "workflow_ids": list(record.workflow_ids or ()),
            "allowed_origins": None if record.allowed_origins is None else list(record.allowed_origins),
            "created_by": record.created_by, "created_at": _utc(record.created_at),
            "expires_at": _utc(record.expires_at), "revoked_at": _utc(record.revoked_at),
            "last_used_at": _utc(record.last_used_at),
        }

    def _client_view(self, row: dict[str, Any], keys: list[ApiKeyRecord]) -> dict[str, Any]:
        return {
            **{k: row[k] for k in ("id", "name", "description", "enabled", "max_active_runs", "max_runs_per_day",
                                   "revision", "created_by")},
            "created_at": _utc(row["created_at"]), "updated_at": _utc(row["updated_at"]),
            "keys": [self._key_view(k) for k in keys if k.external_client_id == row["id"]],
        }

    def _client(self, tenant_id: str, client_id: Any) -> dict[str, Any]:
        row = self._store.get_client(tenant_id, str(client_id))
        if row is None:
            raise NotFound(CLIENT_NOT_FOUND, code="external_client_not_found")
        return row

    def list_clients(self, tenant_id: str) -> list[dict[str, Any]]:
        self._require_read()
        keys = self._keys.list_run_keys(tenant_id)
        return [self._client_view(r, keys) for r in self._store.list_clients(tenant_id)]

    def get_client(self, tenant_id: str, client_id: Any) -> dict[str, Any]:
        self._require_read()
        row = self._client(tenant_id, client_id)
        return self._client_view(row, self._keys.list_run_keys(tenant_id, row["id"]))

    @staticmethod
    def _client_values(payload: dict[str, Any]) -> dict[str, Any]:
        values: dict[str, Any] = {}
        if "name" in payload:
            values["name"] = _text(payload["name"], "name", CLIENT_NAME_MAX_CHARS, minimum=1)
        if "description" in payload:
            values["description"] = _text(payload["description"], "description", CLIENT_DESCRIPTION_MAX_CHARS)
        for field, ceiling in (("max_active_runs", CLIENT_ACTIVE_CEILING), ("max_runs_per_day", CLIENT_PER_DAY_CEILING)):
            if field in payload:
                value = payload[field]
                if value is not None and (isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= ceiling):
                    raise InvalidInput(f"{field} is an integer from 0 to {ceiling}, or null for the default",
                                       code="quotas_invalid")
                values[field] = value
        return values

    def create_client(self, tenant_id: str, actor: str, payload: Any) -> dict[str, Any]:
        self._require_manage()
        payload = _body(payload, {"name", "description", "max_active_runs", "max_runs_per_day"})
        if "name" not in payload:
            raise InvalidInput("name is required", code="name_invalid")
        values = self._client_values(payload)
        if len(self._store.list_clients(tenant_id)) >= MAX_CLIENTS_PER_TENANT:
            raise Conflict(f"a tenant may have at most {MAX_CLIENTS_PER_TENANT} external clients", code="external_client_limit")
        try:
            row = self._store.create_client(tenant_id, actor, **values)
        except ExternalNameConflict:
            raise Conflict("a client with this name exists", code="external_client_name_taken") from None
        return self._client_view(row, [])

    def update_client(
        self, tenant_id: str, client_id: Any, payload: Any, expected_revision: int | None
    ) -> dict[str, Any]:
        """Rename, describe, enable or disable a client, or set its quotas. Disabling stops its keys on the next request
        and cancels nothing (`cancel_client_runs` is the explicit action)."""
        self._require_manage()
        payload = _body(payload, {"name", "description", "enabled", "max_active_runs", "max_runs_per_day"})
        row = self._client(tenant_id, client_id)
        changes = self._client_values(payload)
        if "enabled" in payload:
            changes["enabled"] = _boolean(payload["enabled"], "enabled")
        try:
            updated = self._store.update_client(tenant_id, row["id"], changes, expected_revision)
        except RevisionMismatch:
            current = self.get_client(tenant_id, client_id)
            raise stale_revision(expected_revision, current, current["revision"]) from None
        except ExternalNameConflict:
            raise Conflict("a client with this name exists", code="external_client_name_taken") from None
        if updated is None:
            raise NotFound(CLIENT_NOT_FOUND, code="external_client_not_found")
        return self._client_view(updated, self._keys.list_run_keys(tenant_id, row["id"]))

    def create_key(self, tenant_id: str, actor: str, client_id: Any, payload: Any) -> dict[str, Any]:
        """A new `run` key of the client; the answer carries the plaintext `key`, this once."""
        self._require_manage()
        acl.require(self._access, "create", "api_key", None)
        row = self._client(tenant_id, client_id)
        payload = _body(payload, {"name", "workflow_ids", "expires_at", "allowed_origins"})
        name = _text(payload.get("name"), "name", CLIENT_NAME_MAX_CHARS, minimum=1)
        workflow_ids = payload.get("workflow_ids")
        if (
            not isinstance(workflow_ids, list) or not 1 <= len(workflow_ids) <= API_KEY_ALLOWLIST_MAX_ITEMS
            or not all(isinstance(w, str) for w in workflow_ids)
        ):
            raise InvalidInput("workflow_ids names at least one workflow the key may run", code="workflow_ids_invalid")
        found = {
            str(i) for (i,) in self._db.query(WorkflowModel.id).filter(
                WorkflowModel.tenant_id == tenant_id, WorkflowModel.id.in_(workflow_ids)
            )
        }
        unknown = [w for w in dict.fromkeys(workflow_ids) if w not in found]
        if unknown:
            raise InvalidInput(f"unknown workflow ids: {', '.join(unknown)}", code="workflow_ids_invalid")
        expires_at = payload.get("expires_at")
        if expires_at is not None:
            try:
                expires_at = datetime.fromisoformat(str(expires_at))
            except ValueError:
                raise InvalidInput("expires_at is an ISO 8601 time", code="expires_at_invalid") from None
        origins = payload.get("allowed_origins")
        if origins is not None and not isinstance(origins, list):
            raise InvalidInput("allowed_origins is a list of origins", code="allowed_origins_invalid")
        try:
            created = self._keys.create(
                tenant_id, name=name, created_by=actor, scopes=[RUN_SCOPE], workflow_ids=workflow_ids,
                allowed_origins=origins, expires_at=expires_at, external_client_id=row["id"],
            )
        except ApiKeyLimitExceeded as exc:
            raise Conflict(str(exc), code="key_limit") from None
        except ValueError as exc:
            raise InvalidInput(str(exc), code="key_invalid") from None
        acl.register(self._access, "api_key", created.record.id)  # before the response: the creator owns it
        return {**self._key_view(created.record), "key": created.key}

    def revoke_key(self, tenant_id: str, client_id: Any, key_id: str) -> None:
        """Revoke one of the client's keys (idempotent). Its runs go on; the client's other keys still reach them."""
        self._require_manage()
        row = self._client(tenant_id, client_id)
        record = self._keys.get(tenant_id, key_id)
        if record is None or record.external_client_id != row["id"]:
            raise NotFound("API key not found", code="api_key_not_found")
        self._keys.revoke(tenant_id, key_id)

    def cancel_client_runs(self, tenant_id: str, actor: str, client_id: Any) -> dict[str, Any]:
        """The explicit action beside disable and revoke: cancel the client's unended runs with the durable cancel.
        `pending` lists runs whose cancel is recorded and finishes by itself."""
        self._require_manage()
        row = self._client(tenant_id, client_id)
        cancelled: list[str] = []
        pending: list[str] = []
        for run_id in self._store.active_run_ids(tenant_id, row["id"]):
            try:
                if self._runs.cancel(tenant_id, actor, uuid.UUID(run_id))["cancelled"]:
                    cancelled.append(run_id)
            except DefinitionError as exc:
                self._db.rollback()
                if exc.status_code == 503:
                    pending.append(run_id)
        return {"cancelled": cancelled, "pending": pending}

    # ------------------------------------------------------------------------------------------ exports

    def _version_row(self, tenant_id: str, workflow_id: str, version: int | None) -> WorkflowVersionModel | None:
        query = self._db.query(WorkflowVersionModel).filter(
            WorkflowVersionModel.tenant_id == tenant_id, WorkflowVersionModel.workflow_id == workflow_id
        )
        if version is not None:
            return query.filter(WorkflowVersionModel.version == version).first()
        return query.order_by(WorkflowVersionModel.version.desc()).first()

    def _contract(self, tenant_id: str, workflow_id: str, version: int | None) -> tuple[int, WorkflowContract] | None:
        """(the version an export would run now, its contract); None when it has none this platform can read."""
        found = self._version_row(tenant_id, workflow_id, version)
        if found is None:
            return None
        try:
            spec = WorkflowSpec.model_validate(found.spec)
        except ValidationError:
            return None
        return found.version, workflow_contract(self._db.connection(), tenant_id, spec)

    def _export_view(self, row: dict[str, Any], keys: list[ApiKeyRecord], clients: dict[str, dict]) -> dict[str, Any]:
        tenant_id = row["tenant_id"]
        workflow = self._db.query(WorkflowModel).filter(
            WorkflowModel.tenant_id == tenant_id, WorkflowModel.id == row["workflow_id"]
        ).first()
        resolved = self._contract(tenant_id, row["workflow_id"], row["workflow_version"])
        version, contract = resolved if resolved is not None else (None, None)
        reaching = [
            {"id": k.id, "prefix": k.prefix, "name": k.name, "status": k.status(), "client_id": k.external_client_id,
             "client_name": clients[k.external_client_id]["name"]}
            for k in keys
            if k.status() == "active" and row["workflow_id"] in (k.workflow_ids or ())
            and clients.get(k.external_client_id or "", {}).get("enabled")
        ]
        return {
            **{k: row[k] for k in ("id", "name", "title", "description", "tags", "examples", "workflow_id", "protocols",
                                   "client_inputs", "max_runtime_seconds", "require_idempotency_key", "enabled",
                                   "disabled_reason", "enabled_by", "revision", "created_by")},
            "workflow_name": None if workflow is None else workflow.name,
            "version": "latest" if row["workflow_version"] is None else row["workflow_version"],
            "resolved_version": version, "result": row["result_mode"],
            "input_schema": None if contract is None else contract.input_schema,
            "warnings": [] if contract is None else external_warnings(contract),
            "approval_hash": None if contract is None else approval_hash(row["config_hash"], contract.hash),
            "keys": reaching,
            "enabled_at": _utc(row["enabled_at"]), "created_at": _utc(row["created_at"]),
            "updated_at": _utc(row["updated_at"]),
        }

    def _view(self, tenant_id: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        keys = self._keys.list_run_keys(tenant_id)
        clients = {c["id"]: c for c in self._store.list_clients(tenant_id)}
        return [self._export_view(r, keys, clients) for r in rows]

    def _export(self, tenant_id: str, export_id: Any) -> dict[str, Any]:
        row = self._store.get_export(tenant_id, str(export_id))
        if row is None:
            raise NotFound(EXPORT_NOT_FOUND, code="export_not_found")
        return row

    def list_exports(self, tenant_id: str) -> list[dict[str, Any]]:
        self._require_read()
        return self._view(tenant_id, self._store.list_exports(tenant_id))

    def get_export(self, tenant_id: str, export_id: Any) -> dict[str, Any]:
        self._require_read()
        return self._view(tenant_id, [self._export(tenant_id, export_id)])[0]

    def _workflow(self, tenant_id: str, workflow_id: Any) -> WorkflowModel:
        found = self._db.query(WorkflowModel).filter(
            WorkflowModel.tenant_id == tenant_id, WorkflowModel.id == str(workflow_id)
        ).first()
        if found is None:
            raise NotFound("workflow not found", code="workflow_not_found")
        if found.archived:
            raise Conflict("the workflow is archived", code="workflow_archived")
        return found

    def _pinned_version(self, tenant_id: str, workflow_id: str, version: Any) -> int | None:
        """The pinned version number, or None for "latest"; the workflow must have it (any, for latest)."""
        if version != "latest" and (isinstance(version, bool) or not isinstance(version, int) or version < 1):
            raise InvalidInput('version must be a version number or "latest"', code="version_invalid")
        pinned = None if version == "latest" else version
        if self._version_row(tenant_id, workflow_id, pinned) is None:
            if pinned is None:
                raise Conflict("publish the workflow before exporting it", code="workflow_unpublished")
            raise NotFound("workflow version not found", code="version_not_found")
        return pinned

    def _export_values(self, payload: dict[str, Any]) -> dict[str, Any]:
        values: dict[str, Any] = {}
        if "name" in payload:
            if not isinstance(payload["name"], str) or not EXPORT_NAME.fullmatch(payload["name"]):
                raise InvalidInput("name must match ^[a-z][a-z0-9_]{0,47}$", code="name_invalid")
            values["name"] = payload["name"]
        if "title" in payload:
            values["title"] = _text(payload["title"], "title", TITLE_MAX_CHARS, minimum=1)
        if "description" in payload:
            values["description"] = _text(payload["description"], "description", DESCRIPTION_MAX_CHARS)
        if "tags" in payload:
            values["tags"] = _strings(payload["tags"], "tags", MAX_TAGS, TAG_MAX_CHARS)
        if "examples" in payload:
            values["examples"] = _strings(payload["examples"], "examples", MAX_EXAMPLES, EXAMPLE_MAX_CHARS)
        if "protocols" in payload:
            protocols = payload["protocols"]
            if not isinstance(protocols, list) or not protocols or not all(p in PROTOCOLS for p in protocols):
                raise InvalidInput(f"protocols lists one or both of {', '.join(PROTOCOLS)}", code="protocols_invalid")
            values["protocols"] = [p for p in PROTOCOLS if p in protocols]
        if "result" in payload:
            if payload["result"] not in RESULT_MODES:
                raise InvalidInput(f"result is one of {', '.join(RESULT_MODES)}", code="result_invalid")
            values["result_mode"] = payload["result"]
        if "client_inputs" in payload:
            values["client_inputs"] = _boolean(payload["client_inputs"], "client_inputs")
        if "max_runtime_seconds" in payload:
            value = payload["max_runtime_seconds"]
            limit = self._settings.max_runtime_seconds
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= limit:
                raise InvalidInput(f"max_runtime_seconds is from 1 to {limit}", code="max_runtime_invalid")
            values["max_runtime_seconds"] = value
        if "require_idempotency_key" in payload:
            values["require_idempotency_key"] = _boolean(payload["require_idempotency_key"], "require_idempotency_key")
        return values

    _EXPORT_FIELDS = {
        "name", "title", "description", "tags", "examples", "version", "protocols", "result", "client_inputs",
        "max_runtime_seconds", "require_idempotency_key",
    }

    def create_export(self, tenant_id: str, actor: str, payload: Any) -> dict[str, Any]:
        """A new export, disabled. `version` is a number or "latest" (the default); `require_idempotency_key` defaults
        to true when the version has a side effect."""
        self._require_manage()
        payload = _body(payload, self._EXPORT_FIELDS | {"workflow_id"})
        for field in ("name", "title", "workflow_id", "protocols"):
            if field not in payload:
                raise InvalidInput(f"{field} is required", code=f"{field}_invalid")
        values = {
            "description": "", "tags": [], "examples": [], "result_mode": "output", "client_inputs": False,
            "max_runtime_seconds": min(DEFAULT_MAX_RUNTIME_SECONDS, self._settings.max_runtime_seconds),
            **self._export_values(payload),
        }
        workflow = self._workflow(tenant_id, payload["workflow_id"])
        values["workflow_id"] = str(workflow.id)
        values["workflow_version"] = self._pinned_version(tenant_id, str(workflow.id), payload.get("version", "latest"))
        if "require_idempotency_key" not in values:
            resolved = self._contract(tenant_id, str(workflow.id), values["workflow_version"])
            values["require_idempotency_key"] = resolved is None or bool(resolved[1].side_effects)
        values["config_hash"] = config_hash(values)
        if len(self._store.list_exports(tenant_id)) >= MAX_EXPORTS_PER_TENANT:
            raise Conflict(f"a tenant may have at most {MAX_EXPORTS_PER_TENANT} exports", code="export_limit")
        self._db.rollback()  # end the reads' transaction before the store writes in its own
        try:
            row = self._store.create_export(tenant_id, actor, values)
        except ExternalNameConflict:
            raise Conflict("an export with this name exists", code="export_name_taken") from None
        return self._view(tenant_id, [row])[0]

    def update_export(
        self, tenant_id: str, export_id: Any, payload: Any, expected_revision: int | None
    ) -> dict[str, Any]:
        """Change an export. A change to anything the approval covers disables it. The workflow cannot be changed:
        make another export."""
        self._require_manage()
        payload = _body(payload, self._EXPORT_FIELDS)
        row = self._export(tenant_id, export_id)
        changes = self._export_values(payload)
        if "version" in payload:
            self._workflow(tenant_id, row["workflow_id"])
            changes["workflow_version"] = self._pinned_version(tenant_id, row["workflow_id"], payload["version"])
        merged = {**row, **changes}
        changes["config_hash"] = config_hash(merged)
        state: dict[str, Any] = {}
        if changes["config_hash"] != row["config_hash"]:
            if row["enabled"]:
                state = self._off(CHANGED)
        elif expected_revision is None or expected_revision == row["revision"]:
            return self._view(tenant_id, [row])[0]  # nothing changes
        self._db.rollback()
        try:
            updated = self._store.update_export(tenant_id, row["id"], changes, expected_revision, **state)
        except RevisionMismatch:
            current = self.get_export(tenant_id, export_id)
            raise stale_revision(expected_revision, current, current["revision"]) from None
        except ExternalNameConflict:
            raise Conflict("an export with this name exists", code="export_name_taken") from None
        if updated is None:
            raise NotFound(EXPORT_NOT_FOUND, code="export_not_found")
        return self._view(tenant_id, [updated])[0]

    @staticmethod
    def _off(reason: str) -> dict[str, Any]:
        return {"enabled": False, "disabled_reason": reason, "approved_hash": None, "approved_contract_hash": None,
                "enabled_by": None, "enabled_at": None}

    def enable_export(
        self, tenant_id: str, actor: str, export_id: Any, *, approved_hash: Any, confirm: Any
    ) -> dict[str, Any]:
        """A signed-in admin enables the export, echoing the `approval_hash` the card showed. Refused without a public
        base URL (`external_base_url_unset`) or enforced agent and workflow grants (`grants_not_enforced`)."""
        if self._access is None or self._access.principal.kind != "user":
            raise Forbidden("only a person can enable an export", code="human_required")
        self._require_manage()
        if confirm is not True:
            raise InvalidInput("confirm must be true", code="confirm_required")
        row = self._export(tenant_id, export_id)
        self._workflow(tenant_id, row["workflow_id"])
        resolved = self._contract(tenant_id, row["workflow_id"], row["workflow_version"])
        if resolved is None:
            raise Conflict("the workflow has no version this export can run", code="workflow_unpublished")
        contract = resolved[1]
        expected = approval_hash(row["config_hash"], contract.hash)
        if approved_hash != expected:
            raise Conflict("the export or its workflow changed since you read it; review it again",
                           code="approved_hash_mismatch")
        if not self._settings.base_url:
            raise Conflict("EAP_EXTERNAL_BASE_URL is not set: nothing outside could reach the export",
                           code="external_base_url_unset")
        if not self._grants_enforced():
            raise Conflict(
                "an exported workflow lends its grants to the caller: enforce agent and workflow grants first "
                "(EAP_AGENT_GRANTS_ENFORCE=1 with a policy engine that keeps them)", code="grants_not_enforced",
            )
        self._db.rollback()
        try:
            # Guarded by the revision read above: a change that lands in between is never approved unseen.
            updated = self._store.update_export(
                tenant_id, row["id"], {}, row["revision"], enabled=True, disabled_reason=None, approved_hash=expected,
                approved_contract_hash=contract.hash, enabled_by=actor, enabled_at=datetime.now(timezone.utc),
            )
        except RevisionMismatch:
            raise Conflict("the export changed since you read it; review it again", code="approved_hash_mismatch") from None
        if updated is None:
            raise NotFound(EXPORT_NOT_FOUND, code="export_not_found")
        return self._view(tenant_id, [updated])[0]

    def disable_export(self, tenant_id: str, export_id: Any) -> dict[str, Any]:
        """No new run starts from the next request on. Running runs go on. Idempotent."""
        self._require_manage()
        row = self._export(tenant_id, export_id)
        if row["enabled"]:
            row = self._store.update_export(tenant_id, row["id"], {}, None, **self._off(DISABLED)) or row
        return self._view(tenant_id, [row])[0]


__all__ = ["ExternalAccessService", "approval_hash", "config_hash"]
