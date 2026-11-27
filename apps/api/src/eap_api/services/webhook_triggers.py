"""Inbound webhook triggers: lifecycle, configuration and the one-time secret (design sections 2, 3 and 4.6).

A trigger is a configuration row plus a machine principal: it is created `disabled`, a **human** enables it (the card:
`config_hash` is echoed, so the approval binds to exactly the grant the person saw), and from then on its public URL
accepts signed deliveries that START a run of exactly its workflow. Any later change to a field that is part of the
grant returns it to `disabled` and clears the approval. Secrets are `secret:<name>` references to tenant secrets (1 or
2 active); no read returns a value, and the only place a generated value is ever shown is the response of the create
call, once (REST/Studio only: MCP creates take existing references).

Every lifecycle step writes a `webhook_trigger_events` row (never a secret, a signature or a payload). The platform's
`audit_log` records the mutating REST call as well (`AuditMiddleware`).

Authorisation (docs/iam.md, "Inbound webhooks"): a trigger is decided by the rights on its workflow (`read` to see it and
its deliveries, `edit` to create, change, rotate, disable or archive it). Its secrets follow the rules of a `secret:`
reference: binding one to an approved grant (enable, or adding one to a live trigger) needs `grant_use` on it (an admin),
and generating one is creating a secret (admin)."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets as pysecrets
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from typing import Any

from eap_inbound import CustomConfig, standard_webhooks_key
from eap_schemas.secrets import SECRET_NAME_PATTERN, is_secret_ref
from eap_schemas.workflows import WorkflowSpec
from pydantic import ValidationError
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from eap_api.services import access as acl
from eap_api.services.access import Access, AccessBound
from eap_api.services.errors import Conflict, Forbidden, InvalidInput, NotFound, Unavailable
from eap_api.services.revision_conflict import stale_revision
from eap_api.workflows.models import WorkflowModel, WorkflowVersionModel
from eap_run_store import TriggerNameConflict, WebhookStore
from eap_run_store.external_access import workflow_effects
from eap_run_store.revisions import RevisionMismatch
from eap_run_store.secrets import SecretStore

NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
DESCRIPTION_MAX_CHARS = 500
MAX_TRIGGERS_PER_TENANT = 100
MAX_PAYLOAD_SCHEMA_BYTES = 16 * 1024
MAX_INPUT_MAP_BYTES = 16 * 1024
PRESETS = ("github", "standard_webhooks", "custom")
DEFAULT_LIMITS = {"rate_per_minute": 60, "max_concurrent_runs": 5, "max_runs_per_day": 2000, "max_body_bytes": 262144}
LIMIT_MAXIMA = {"rate_per_minute": 600, "max_concurrent_runs": 50, "max_runs_per_day": 100000, "max_body_bytes": 1048576}
TOLERANCE_RANGE = (60, 900)
DEFAULT_TOLERANCE = 300
# Fields whose change is a change of the grant: the trigger goes back to `disabled` and must be approved again.
GRANT_FIELDS = ("workflow_id", "workflow_version", "preset", "preset_config", "actions", "limits", "tolerance_seconds")
_FORBIDDEN_SCHEMA_KEYS = frozenset({"$ref", "$dynamicRef", "$recursiveRef", "pattern", "patternProperties"})
_POINTER = re.compile(r"^(?:/(?:[^/~]|~[01])*)*$")

NOT_FOUND = "webhook trigger not found"
DELIVERY_NOT_FOUND = "delivery not found"
APPROVAL_REQUIRED = "grant_approval_required"
EVENT_KINDS = (
    "created", "updated", "enable_requested", "enabled", "enable_rejected", "disabled", "secret_added", "secret_retired",
    "archived",
)


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def config_hash(values: dict[str, Any]) -> str:
    """sha256 of the canonical grant-relevant configuration: workflow, version, preset and its config, the secret
    REFERENCES, actions, limits and tolerance. This is what a person approves."""
    material = {k: values[k] for k in GRANT_FIELDS}
    material["secret_refs"] = [s["ref"] for s in values["secrets"]]
    return hashlib.sha256(canonical(material).encode()).hexdigest()


def _utc(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _schema_problem(schema: Any) -> str | None:
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return "payload_schema must be a JSON Schema object with type 'object'"
    if len(canonical(schema).encode()) > MAX_PAYLOAD_SCHEMA_BYTES:
        return f"payload_schema must be at most {MAX_PAYLOAD_SCHEMA_BYTES // 1024} KiB as JSON"
    stack: list[Any] = [schema]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if _FORBIDDEN_SCHEMA_KEYS & set(item):
                return "payload_schema must not use $ref, pattern or patternProperties"
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    import jsonschema

    try:
        jsonschema.validators.validator_for(schema).check_schema(schema)
    except jsonschema.SchemaError:
        return "payload_schema is not a valid JSON Schema"
    return None


def _map_problem(value: Any, path: str = "input_map", depth: int = 0) -> str | None:
    """`input_map` leaves are a literal, `{"$pointer": "<RFC 6901>"}` or `{"$payload": true}`: data, never code."""
    if depth > 8:
        return f"{path} is nested too deeply"
    if isinstance(value, dict):
        keys = set(value)
        if keys == {"$pointer"}:
            pointer = value["$pointer"]
            return None if isinstance(pointer, str) and _POINTER.fullmatch(pointer) and len(pointer) <= 200 else f"{path}: $pointer must be an RFC 6901 pointer"
        if keys == {"$payload"}:
            return None if value["$payload"] is True else f"{path}: $payload must be true"
        if any(str(k).startswith("$") for k in keys):
            return f"{path}: only $pointer and $payload are allowed as mapping operators"
        for key, child in value.items():
            problem = _map_problem(child, f"{path}.{key}", depth + 1)
            if problem:
                return problem
    elif isinstance(value, list):
        for index, child in enumerate(value):
            problem = _map_problem(child, f"{path}[{index}]", depth + 1)
            if problem:
                return problem
    return None


def normalize_actions(actions: Any) -> dict[str, Any]:
    """The `actions` of a trigger. P1 has one action, `start` (a `wake` action needs the `wait_event` step, a later
    release): `{start: {payload_schema, input_map, run_name}}`. InvalidInput names the field, never a value."""
    if not isinstance(actions, dict) or set(actions) - {"start", "wake"} or not actions:
        raise InvalidInput("actions must be an object with a `start` action", code="actions_invalid")
    if "wake" in actions:
        raise InvalidInput("the `wake` action is not available yet (it needs the wait_event step)", code="wake_not_available")
    start = actions["start"]
    if not isinstance(start, dict) or set(start) - {"payload_schema", "input_map", "run_name"}:
        raise InvalidInput("actions.start takes payload_schema, input_map and run_name", code="actions_invalid")
    problem = _schema_problem(start.get("payload_schema"))
    if problem:
        raise InvalidInput(problem, code="payload_schema_invalid")
    input_map = start.get("input_map", {})
    if not isinstance(input_map, dict):
        raise InvalidInput("input_map must be an object", code="input_map_invalid")
    if len(canonical(input_map).encode()) > MAX_INPUT_MAP_BYTES:
        raise InvalidInput("input_map is too large", code="input_map_invalid")
    problem = _map_problem(input_map)
    if problem:
        raise InvalidInput(problem, code="input_map_invalid")
    run_name = start.get("run_name")
    if run_name is not None and not (isinstance(run_name, str) and 1 <= len(run_name) <= 200):
        raise InvalidInput("run_name must be a string of 1 to 200 characters", code="actions_invalid")
    return {"start": {"payload_schema": start["payload_schema"], "input_map": input_map, "run_name": run_name}}


def normalize_limits(limits: Any) -> dict[str, int]:
    if limits is None:
        limits = {}
    if not isinstance(limits, dict) or set(limits) - set(DEFAULT_LIMITS):
        raise InvalidInput(f"limits takes {', '.join(DEFAULT_LIMITS)}", code="limits_invalid")
    out = dict(DEFAULT_LIMITS)
    for key, value in limits.items():
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= LIMIT_MAXIMA[key]:
            raise InvalidInput(f"limits.{key} must be an integer from 1 to {LIMIT_MAXIMA[key]}", code="limits_invalid")
        out[key] = value
    return out


def normalize_preset(preset: Any, preset_config: Any) -> tuple[str, dict[str, Any]]:
    if preset not in PRESETS:
        raise InvalidInput(f"preset must be one of {', '.join(PRESETS)}", code="preset_invalid")
    if preset != "custom":
        if preset_config not in (None, {}):
            raise InvalidInput(f"the {preset} preset takes no preset_config", code="preset_config_invalid")
        return preset, {}
    try:
        config = CustomConfig.model_validate(preset_config if preset_config is not None else {})
    except ValidationError as exc:
        # Messages name the field; they never contain a secret (there is none in this config).
        first = exc.errors(include_input=False, include_url=False)[0]
        message = str(first.get("msg", "")).removeprefix("Value error, ")
        code = "replay_protection_required" if "replay_protection_required" in message else "preset_config_invalid"
        raise InvalidInput(f"preset_config: {message}", code=code) from None
    return preset, config.model_dump(mode="json", exclude_none=True)


class WebhookTriggerService(AccessBound):
    def __init__(
        self,
        store: WebhookStore,
        db: Session,
        engine: Engine,
        keyed_store: Callable[[Engine], SecretStore],
        base_url: str | None,
        access: Access | None = None,
    ) -> None:
        self._access = access  # None: no caller to check (see eap_api.services.access)
        self._store = store
        self._db = db
        self._engine = engine
        self._keyed_store = keyed_store
        self._base_url = base_url

    # ------------------------------------------------------------------------------------------ reads

    def _row(self, tenant_id: str, trigger_id: Any, action: str = "read") -> dict[str, Any]:
        """The tenant's trigger the caller may `action` (decided by its workflow). A trigger of a workflow the caller may
        not read answers exactly like one that does not exist."""
        row = self._store.get_trigger(tenant_id, str(trigger_id))
        if row is None:
            raise NotFound(NOT_FOUND, code="webhook_trigger_not_found")
        try:
            acl.require(self._access, action, "workflow", row["workflow_id"])
        except NotFound:
            raise NotFound(NOT_FOUND, code="webhook_trigger_not_found") from None
        return row

    def _readable(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """`rows` narrowed to the triggers of workflows the caller may read (one engine call)."""
        allowed = set(acl.keep(acl.visible(self._access, "workflow"), [r["workflow_id"] for r in rows]))
        return [r for r in rows if r["workflow_id"] in allowed]

    def _require_secret_approval(self, refs: Sequence[str]) -> None:
        """Only whoever may grant a secret (an admin) approves a trigger that verifies with it."""
        if self._access is None:
            return
        names = [is_secret_ref(ref) for ref in refs]
        refused = [n for n in names if n is not None and not self._access.allowed("grant_use", "secret", n)]
        if refused:
            listed = ", ".join(f"secret `{n}`" for n in refused)
            raise Forbidden(
                f"This trigger verifies its deliveries with {listed}. Only an admin may approve that.", code=APPROVAL_REQUIRED
            )

    def public_url(self, trigger_id: str) -> str | None:
        return None if not self._base_url else f"{self._base_url}/api/v1/hooks/{trigger_id}"

    def view(self, row: dict[str, Any], *, with_warnings: bool = False) -> dict[str, Any]:
        """The trigger as the API shows it: configuration, secret REFERENCE names, status; never a value."""
        out = {
            "id": row["id"], "name": row["name"], "description": row["description"], "workflow_id": row["workflow_id"],
            "workflow_version": row["workflow_version"], "version": "latest" if row["workflow_version"] is None else row["workflow_version"],
            "preset": row["preset"], "preset_config": row["preset_config"],
            "secrets": [
                {"ref": s["ref"], "added_at": s.get("added_at"), "last_matched_at": s.get("last_matched_at")} for s in row["secrets"]
            ],
            "tolerance_seconds": row["tolerance_seconds"], "actions": row["actions"], "limits": row["limits"],
            "status": row["status"], "config_hash": row["config_hash"],
            "approved": row["approved_config_hash"] is not None and row["approved_config_hash"] == row["config_hash"],
            "enable_requested_by": row["enable_requested_by"], "enable_requested_at": _utc(row["enable_requested_at"]),
            "enabled_by": row["enabled_by"], "enabled_at": _utc(row["enabled_at"]),
            "last_delivery_at": _utc(row["last_delivery_at"]), "last_outcome": row["last_outcome"],
            "revision": row["revision"], "created_by": row["created_by"], "created_at": _utc(row["created_at"]),
            "updated_at": _utc(row["updated_at"]), "archived_at": _utc(row["archived_at"]),
            "url": self.public_url(row["id"]),
        }
        if with_warnings:
            out["warnings"] = self.warnings(row)
        return out

    def list(self, tenant_id: str, *, include_archived: bool = False, workflow_id: str | None = None) -> list[dict[str, Any]]:
        rows = self._store.list_triggers(tenant_id, include_archived=include_archived, workflow_id=workflow_id)
        return [self.view(r) for r in self._readable(rows)]

    def get(self, tenant_id: str, trigger_id: Any) -> dict[str, Any]:
        return self.view(self._row(tenant_id, trigger_id), with_warnings=True)

    def events(self, tenant_id: str, trigger_id: Any, limit: int = 100) -> list[dict[str, Any]]:
        self._row(tenant_id, trigger_id)
        return [{**e, "at": _utc(e["at"])} for e in self._store.list_events(tenant_id, str(trigger_id), min(limit, 200))]

    def deliveries(
        self, tenant_id: str, *, trigger_id: Any = None, outcome: str | None = None, since: datetime | None = None, limit: int = 50,
    ) -> list[dict[str, Any]]:
        only: list[str] | None = None
        if trigger_id is not None:
            self._row(tenant_id, trigger_id)
        elif self._access is not None:
            every = self._store.list_triggers(tenant_id, include_archived=True)
            readable = self._readable(every)
            only = None if len(readable) == len(every) else [r["id"] for r in readable]
        rows = self._store.list_deliveries(
            tenant_id, trigger_id=None if trigger_id is None else str(trigger_id), trigger_ids=only, outcome=outcome,
            since=since, limit=limit,
        )
        return [{**r, "received_at": _utc(r["received_at"])} for r in rows]

    def delivery(self, tenant_id: str, delivery_id: str) -> dict[str, Any]:
        row = self._store.get_delivery(tenant_id, delivery_id)
        if row is None:
            raise NotFound(DELIVERY_NOT_FOUND, code="webhook_delivery_not_found")
        try:
            self._row(tenant_id, row["trigger_id"])
        except NotFound:
            raise NotFound(DELIVERY_NOT_FOUND, code="webhook_delivery_not_found") from None
        return {**row, "received_at": _utc(row["received_at"])}

    # ------------------------------------------------------------------------------------------ warnings

    def warnings(self, row: dict[str, Any]) -> list[dict[str, str]]:
        """Publish-style warnings shown on the enable card (they never block): see design 3, layer 4."""
        out: list[dict[str, str]] = []
        if row["workflow_version"] is None:
            out.append({"code": "webhook_version_latest", "message": (
                "this trigger follows the latest published version, so any later publish changes what an outside system can start"
            )})
        spec = self._spec(row["tenant_id"], row["workflow_id"], row["workflow_version"])
        if spec is None:
            return out
        effects = workflow_effects(self._db.connection(), row["tenant_id"], spec)
        if effects.side_effects and not effects.has_gate:
            out.append({"code": "webhook_side_effects_without_approval", "message": (
                "this workflow can be started by an outside system and has side effects without a person approving them; "
                "text from the webhook can steer an agent step (prompt injection)"
            )})
        if effects.input_into_prompt:
            out.append({"code": "webhook_payload_into_agent_prompt", "message": (
                "webhook input reaches an agent step's prompt: untrusted text can steer a model"
            )})
        return out

    def _spec(self, tenant_id: str, workflow_id: str, version: int | None) -> WorkflowSpec | None:
        query = self._db.query(WorkflowVersionModel).filter(
            WorkflowVersionModel.tenant_id == tenant_id, WorkflowVersionModel.workflow_id == workflow_id
        )
        query = query.filter(WorkflowVersionModel.version == version) if version is not None else query.order_by(WorkflowVersionModel.version.desc())
        found = query.first()
        if found is None:
            return None
        try:
            return WorkflowSpec.model_validate(found.spec)
        except ValidationError:
            return None

    # ------------------------------------------------------------------------------------------ create

    def _workflow(self, tenant_id: str, workflow_id: Any) -> WorkflowModel:
        found = self._db.query(WorkflowModel).filter(
            WorkflowModel.tenant_id == tenant_id, WorkflowModel.id == str(workflow_id)
        ).first()
        if found is None:
            raise NotFound("workflow not found", code="workflow_not_found")
        if found.archived:
            raise Conflict("the workflow is archived", code="workflow_archived")
        return found

    def _version(self, tenant_id: str, workflow_id: str, version: Any, *, stored: bool = False) -> int | None:
        """The pinned version number, or None for "latest". A request must say which (`null` is not an answer); a
        stored row's None means latest."""
        if version == "latest" or (version is None and stored):
            return None
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise InvalidInput("version must be a version number or \"latest\"", code="version_invalid")
        found = self._db.query(WorkflowVersionModel.version).filter(
            WorkflowVersionModel.tenant_id == tenant_id, WorkflowVersionModel.workflow_id == workflow_id,
            WorkflowVersionModel.version == version,
        ).first()
        if found is None:
            raise NotFound("workflow version not found", code="version_not_found")
        return version

    def _check_references(self, tenant_id: str, refs: list[str], preset: str) -> None:
        """Every reference names an existing secret of this tenant; a `standard_webhooks` one must decode."""
        names = {r["name"] for r in SecretStore(self._engine, None).names(tenant_id)}
        for ref in refs:
            name = is_secret_ref(ref)
            if name is None:
                raise InvalidInput("secrets must be references of the form secret:<name>, never a value", code="secret_ref_invalid")
            if name not in names:
                raise InvalidInput(f"secret {name!r} does not exist in this tenant", code="secret_not_found")
        if preset == "standard_webhooks":
            store = self._keyed_store(self._engine)
            for ref in refs:
                value = store.resolve(tenant_id, ref[len("secret:"):])
                if value is None or standard_webhooks_key(value) is None:
                    raise InvalidInput("a standard_webhooks secret must be whsec_<base64>", code="secret_invalid")

    @staticmethod
    def _generated_value(preset: str) -> str:
        raw = pysecrets.token_bytes(32)
        return "whsec_" + base64.b64encode(raw).decode() if preset == "standard_webhooks" else pysecrets.token_urlsafe(32)

    def _generate_secret(self, tenant_id: str, actor: str, trigger_name: str, preset: str) -> tuple[str, str]:
        existing = {r["name"] for r in SecretStore(self._engine, None).names(tenant_id)}
        base = re.sub(r"[^a-z0-9_.-]", "-", trigger_name.lower())[:40]
        name = f"webhook-{base}"
        while name in existing or not re.fullmatch(SECRET_NAME_PATTERN, name):
            name = f"webhook-{base}-{pysecrets.token_hex(3)}"
        value = self._generated_value(preset)
        self._keyed_store(self._engine).put(tenant_id, name, value, actor)
        acl.register(self._access, "secret", name)  # before the response, like a secret created in Settings
        return f"secret:{name}", value

    def create(self, tenant_id: str, actor: str, payload: Any, *, allow_generate: bool = False) -> dict[str, Any]:
        """Create a `disabled` trigger. `secrets` are existing `secret:<name>` references; with `generate: true`
        (REST/Studio only: `allow_generate`) the platform makes a 256-bit value, stores it as a tenant secret and the
        returned dict carries it once under `generated_secret`. The workflow is named, never inferred; `version` is a
        number or "latest" (required)."""
        if not isinstance(payload, dict):
            raise InvalidInput("body must be a JSON object", code="body_invalid")
        allowed = {"name", "description", "workflow_id", "version", "preset", "preset_config", "secrets", "generate",
                   "tolerance_seconds", "actions", "limits"}
        if set(payload) - allowed:
            raise InvalidInput(f"unknown field(s): {', '.join(sorted(set(payload) - allowed))}", code="body_invalid")
        name = payload.get("name")
        if not isinstance(name, str) or not NAME_PATTERN.fullmatch(name):
            raise InvalidInput("name must match ^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$", code="name_invalid")
        description = payload.get("description", "")
        if not isinstance(description, str) or len(description) > DESCRIPTION_MAX_CHARS:
            raise InvalidInput(f"description must be at most {DESCRIPTION_MAX_CHARS} characters", code="description_invalid")
        if "version" not in payload:
            raise InvalidInput("version is required: a version number or \"latest\"", code="version_invalid")
        try:
            acl.require(self._access, "edit", "workflow", str(payload.get("workflow_id")))
        except NotFound:
            raise NotFound("workflow not found", code="workflow_not_found") from None
        workflow = self._workflow(tenant_id, payload.get("workflow_id"))
        version = self._version(tenant_id, workflow.id, payload["version"])
        preset, preset_config = normalize_preset(payload.get("preset"), payload.get("preset_config"))
        tolerance = payload.get("tolerance_seconds", DEFAULT_TOLERANCE)
        if isinstance(tolerance, bool) or not isinstance(tolerance, int) or not TOLERANCE_RANGE[0] <= tolerance <= TOLERANCE_RANGE[1]:
            raise InvalidInput(f"tolerance_seconds must be {TOLERANCE_RANGE[0]} to {TOLERANCE_RANGE[1]}", code="tolerance_invalid")
        actions = normalize_actions(payload.get("actions"))
        limits = normalize_limits(payload.get("limits"))
        generate = payload.get("generate") is True
        refs = payload.get("secrets") or []
        if generate and not allow_generate:
            raise Forbidden("generating a secret is available from Studio only; create the secret and pass its reference", code="generate_not_allowed")
        if generate and refs:
            raise InvalidInput("pass either secrets or generate, not both", code="secrets_invalid")
        if not generate:
            if not isinstance(refs, list) or not 1 <= len(refs) <= 2 or not all(isinstance(r, str) for r in refs) or len(set(refs)) != len(refs):
                raise InvalidInput("secrets must list one or two distinct secret references", code="secrets_invalid")
            self._check_references(tenant_id, refs, preset)
        if len(self._store.list_triggers(tenant_id, include_archived=False)) >= MAX_TRIGGERS_PER_TENANT:
            raise Conflict(f"a tenant may have at most {MAX_TRIGGERS_PER_TENANT} webhook triggers", code="webhook_trigger_limit")
        if any(r["name"] == name for r in self._store.list_triggers(tenant_id, include_archived=True)):
            raise Conflict("a trigger with this name exists", code="webhook_trigger_name_taken")  # before a secret is generated
        generated: str | None = None
        if generate:
            acl.require(self._access, "create", "secret", None)  # generating is creating a secret (admin)
            ref, generated = self._generate_secret(tenant_id, actor, name, preset)
            refs = [ref]
        now = datetime.now(timezone.utc).isoformat()
        values = {
            "name": name, "description": description, "workflow_id": str(workflow.id), "workflow_version": version,
            "preset": preset, "preset_config": preset_config,
            "secrets": [{"ref": ref, "added_at": now, "last_matched_at": None} for ref in refs],
            "tolerance_seconds": tolerance, "actions": actions, "limits": limits,
        }
        values["config_hash"] = config_hash(values)
        try:
            row = self._store.create_trigger(tenant_id, actor, values)
        except TriggerNameConflict:
            raise Conflict("a trigger with this name exists", code="webhook_trigger_name_taken") from None
        self._store.add_event(tenant_id, row["id"], actor, "created", {"workflow_id": row["workflow_id"], "preset": preset})
        out = self.view(row, with_warnings=True)
        if generated is not None:
            out["generated_secret"] = generated  # shown once; never stored in a row, an event or a log
        return out

    # ------------------------------------------------------------------------------------------ update

    def update(self, tenant_id: str, actor: str, trigger_id: Any, payload: Any, expected_revision: int | None) -> dict[str, Any]:
        """Change a trigger. A change to anything in the grant returns it to `disabled` and clears the approval."""
        if not isinstance(payload, dict) or not payload:
            raise InvalidInput("body must be a non-empty JSON object", code="body_invalid")
        allowed = {"name", "description", "version", "preset_config", "tolerance_seconds", "actions", "limits"}
        if set(payload) - allowed:
            raise InvalidInput(
                f"cannot change: {', '.join(sorted(set(payload) - allowed))} (create a new trigger to change workflow, preset or secrets)",
                code="body_invalid",
            )
        row = self._row(tenant_id, trigger_id, "edit")
        if row["status"] == "archived":
            raise Conflict("the trigger is archived", code="webhook_trigger_archived")
        if expected_revision is not None and row["revision"] != expected_revision:
            raise stale_revision(expected_revision, self.view(row), row["revision"])
        changes: dict[str, Any] = {}
        if "name" in payload:
            if not isinstance(payload["name"], str) or not NAME_PATTERN.fullmatch(payload["name"]):
                raise InvalidInput("name must match ^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$", code="name_invalid")
            changes["name"] = payload["name"]
        if "description" in payload:
            if not isinstance(payload["description"], str) or len(payload["description"]) > DESCRIPTION_MAX_CHARS:
                raise InvalidInput(f"description must be at most {DESCRIPTION_MAX_CHARS} characters", code="description_invalid")
            changes["description"] = payload["description"]
        if "version" in payload:
            changes["workflow_version"] = self._version(tenant_id, row["workflow_id"], payload["version"])
        if "preset_config" in payload:
            _preset, changes["preset_config"] = normalize_preset(row["preset"], payload["preset_config"])
        if "tolerance_seconds" in payload:
            value = payload["tolerance_seconds"]
            if isinstance(value, bool) or not isinstance(value, int) or not TOLERANCE_RANGE[0] <= value <= TOLERANCE_RANGE[1]:
                raise InvalidInput(f"tolerance_seconds must be {TOLERANCE_RANGE[0]} to {TOLERANCE_RANGE[1]}", code="tolerance_invalid")
            changes["tolerance_seconds"] = value
        if "actions" in payload:
            changes["actions"] = normalize_actions(payload["actions"])
        if "limits" in payload:
            changes["limits"] = normalize_limits(payload["limits"])
        merged = {**row, **changes}
        new_hash = config_hash(merged)
        changes["config_hash"] = new_hash
        state: dict[str, Any] = {}
        grant_changed = new_hash != row["config_hash"]
        if grant_changed and row["status"] != "disabled":
            state.update(status="disabled", approved_config_hash=None, enable_requested_by=None, enable_requested_at=None,
                         enabled_by=None, enabled_at=None)
        try:
            updated = self._store.update_trigger(tenant_id, row["id"], changes, expected_revision, **state)
        except RevisionMismatch:
            current = self._row(tenant_id, trigger_id)
            raise stale_revision(expected_revision, self.view(current), current["revision"]) from None
        except TriggerNameConflict:
            raise Conflict("a trigger with this name exists", code="webhook_trigger_name_taken") from None
        if updated is None:
            raise NotFound(NOT_FOUND, code="webhook_trigger_not_found")
        fields = sorted(k for k in changes if k != "config_hash")
        self._store.add_event(tenant_id, row["id"], actor, "updated", {"fields": fields, "returned_to_disabled": bool(state)})
        if state:
            self._store.add_event(tenant_id, row["id"], actor, "disabled", {"reason": "grant_changed"})
        return self.view(updated, with_warnings=True)

    # ------------------------------------------------------------------------------------------ lifecycle

    def _require_base_url(self) -> None:
        if not self._base_url:
            raise Unavailable("EAP_WEBHOOK_BASE_URL is not set: nothing could reach the trigger", code="webhook_base_url_unset")

    def _check_ready(self, tenant_id: str, row: dict[str, Any]) -> None:
        """Before enabling: the workflow still exists, the pinned version exists, every secret reference resolves."""
        self._workflow(tenant_id, row["workflow_id"])
        self._version(tenant_id, row["workflow_id"], row["workflow_version"], stored=True)
        refs = [s["ref"] for s in row["secrets"]]
        self._check_references(tenant_id, refs, row["preset"])

    def _transition(self, tenant_id: str, row: dict[str, Any], state: dict[str, Any], expected: int | None = None) -> dict[str, Any]:
        try:
            updated = self._store.update_trigger(tenant_id, row["id"], {}, expected, **state)
        except RevisionMismatch:
            current = self._row(tenant_id, row["id"])
            raise stale_revision(expected, self.view(current), current["revision"]) from None
        if updated is None:
            raise NotFound(NOT_FOUND, code="webhook_trigger_not_found")
        return updated

    def request_enable(self, tenant_id: str, actor: str, trigger_id: Any) -> dict[str, Any]:
        """A non-human principal (or a person who wants a second look) asks for the trigger to be enabled: it moves to
        `pending_enable` and shows as a card in Studio. It does not open the door."""
        row = self._row(tenant_id, trigger_id, "edit")
        if row["status"] not in ("disabled", "pending_enable"):
            raise Conflict(f"a {row['status']} trigger cannot be enabled", code="webhook_trigger_state")
        self._require_base_url()
        self._check_ready(tenant_id, row)
        updated = self._transition(tenant_id, row, {
            "status": "pending_enable", "enable_requested_by": actor, "enable_requested_at": datetime.now(timezone.utc),
        })
        self._store.add_event(tenant_id, row["id"], actor, "enable_requested", {"config_hash": row["config_hash"]})
        return self.view(updated, with_warnings=True)

    def enable(self, tenant_id: str, actor: str, trigger_id: Any, *, config_hash_seen: Any, confirm: Any, by_human: bool) -> dict[str, Any]:
        """A HUMAN session enables the trigger, echoing the `config_hash` the card showed: the approval binds to exactly
        that grant, so a change between the card and the click is refused (409)."""
        if not by_human:
            raise Forbidden("only a person can enable a webhook trigger", code="human_required")
        if confirm is not True:
            raise InvalidInput("confirm must be true", code="confirm_required")
        row = self._row(tenant_id, trigger_id, "edit")
        if row["status"] not in ("disabled", "pending_enable"):
            raise Conflict(f"a {row['status']} trigger cannot be enabled", code="webhook_trigger_state")
        if config_hash_seen != row["config_hash"]:
            raise Conflict("the trigger changed since you read it; review it again", code="config_hash_mismatch")
        self._require_base_url()
        self._check_ready(tenant_id, row)
        self._require_secret_approval([s["ref"] for s in row["secrets"]])
        # Guarded by the revision read above: a change that lands in between is never approved unseen.
        updated = self._transition(tenant_id, row, {
            "status": "enabled", "approved_config_hash": row["config_hash"], "enabled_by": actor,
            "enabled_at": datetime.now(timezone.utc),
        }, row["revision"])
        self._store.add_event(tenant_id, row["id"], actor, "enabled", {"config_hash": row["config_hash"]})
        return self.view(updated, with_warnings=True)

    def decide(self, tenant_id: str, actor: str, trigger_id: Any, *, decision: Any, config_hash_seen: Any, by_human: bool) -> dict[str, Any]:
        """The card's decision (human only): approve (= enable) or reject a `pending_enable` trigger."""
        if decision not in ("approve", "reject"):
            raise InvalidInput("decision must be approve or reject", code="decision_invalid")
        row = self._row(tenant_id, trigger_id, "edit")
        if row["status"] != "pending_enable":
            raise Conflict("the trigger has no pending enable request", code="webhook_trigger_state")
        if decision == "approve":
            return self.enable(tenant_id, actor, trigger_id, config_hash_seen=config_hash_seen, confirm=True, by_human=by_human)
        if not by_human:
            raise Forbidden("only a person can decide a webhook trigger", code="human_required")
        updated = self._transition(tenant_id, row, {"status": "disabled", "enable_requested_by": None, "enable_requested_at": None})
        self._store.add_event(tenant_id, row["id"], actor, "enable_rejected", {})
        return self.view(updated, with_warnings=True)

    def disable(self, tenant_id: str, actor: str, trigger_id: Any) -> dict[str, Any]:
        """Stop the URL (404 within one request). Idempotent for a disabled trigger."""
        row = self._row(tenant_id, trigger_id, "edit")
        if row["status"] == "archived":
            raise Conflict("the trigger is archived", code="webhook_trigger_archived")
        if row["status"] == "disabled":
            return self.view(row, with_warnings=True)
        updated = self._transition(tenant_id, row, {
            "status": "disabled", "approved_config_hash": None, "enable_requested_by": None, "enable_requested_at": None,
            "enabled_by": None, "enabled_at": None,
        })
        self._store.add_event(tenant_id, row["id"], actor, "disabled", {"reason": "requested"})
        return self.view(updated, with_warnings=True)

    def archive(self, tenant_id: str, actor: str, trigger_id: Any) -> dict[str, Any]:
        row = self._row(tenant_id, trigger_id, "edit")
        if row["status"] == "archived":
            return self.view(row)
        updated = self._transition(tenant_id, row, {
            "status": "archived", "approved_config_hash": None, "enabled_by": None, "enabled_at": None,
            "archived_at": datetime.now(timezone.utc),
        })
        self._store.add_event(tenant_id, row["id"], actor, "archived", {})
        return self.view(updated)

    def rotate(
        self, tenant_id: str, actor: str, trigger_id: Any, payload: Any, *, allow_generate: bool = False, by_human: bool = False
    ) -> dict[str, Any]:
        """Rotation without downtime: `add` a second active reference, update the provider, then `retire` the old one.
        Neither changes the grant, so no new approval; but a reference added to an approved trigger accepts deliveries at
        once, so adding it takes what enabling takes (a person who may grant the secret). the audit rows name the secret NAMES, never a value. With
        `generate: true` (Studio only) the new secret is generated and shown once."""
        if not isinstance(payload, dict) or len(set(payload) & {"add", "retire", "generate"}) != 1 or set(payload) - {"add", "retire", "generate"}:
            raise InvalidInput("give exactly one of add, retire or generate", code="rotate_invalid")
        row = self._row(tenant_id, trigger_id, "edit")
        if row["status"] == "archived":
            raise Conflict("the trigger is archived", code="webhook_trigger_archived")
        approved = row["approved_config_hash"] == row["config_hash"]
        if approved and ("add" in payload or "generate" in payload) and not by_human:
            raise Forbidden("only a person can add a secret to an enabled webhook trigger", code="human_required")
        secrets = [dict(s) for s in row["secrets"]]
        generated: str | None = None
        if "retire" in payload:
            ref = payload["retire"]
            if len(secrets) < 2:
                raise Conflict("the last secret cannot be retired; add a new one first", code="last_secret")
            keep = [s for s in secrets if s["ref"] != ref]
            if len(keep) == len(secrets):
                raise NotFound("that reference is not active on this trigger", code="secret_ref_unknown")
            secrets, kind, detail = keep, "secret_retired", {"secret": ref}
        else:
            if len(secrets) >= 2:
                raise Conflict("a trigger has at most two active secrets; retire one first", code="secret_limit")
            if "generate" in payload:
                if payload["generate"] is not True:
                    raise InvalidInput("generate must be true", code="rotate_invalid")
                if not allow_generate:
                    raise Forbidden("generating a secret is available from Studio only", code="generate_not_allowed")
                acl.require(self._access, "create", "secret", None)
                ref, generated = self._generate_secret(tenant_id, actor, row["name"], row["preset"])
            else:
                ref = payload["add"]
                if ref in [s["ref"] for s in secrets]:
                    raise Conflict("that reference is already active", code="secret_ref_duplicate")
                self._check_references(tenant_id, [ref], row["preset"])
                if approved:
                    self._require_secret_approval([ref])
            secrets = [*secrets, {"ref": ref, "added_at": datetime.now(timezone.utc).isoformat(), "last_matched_at": None}]
            kind, detail = "secret_added", {"secret": ref}
        merged = {**row, "secrets": secrets}
        new_hash = config_hash(merged)
        # Rotating within the same grant does not need a new approval; the stored approval follows the hash only when it was approved.
        state: dict[str, Any] = {}
        if approved:
            state["approved_config_hash"] = new_hash
        try:
            updated = self._store.update_trigger(
                tenant_id, row["id"], {"secrets": secrets, "config_hash": new_hash}, row["revision"], **state
            )
        except RevisionMismatch:  # changed since it was read (disabled, say): never carry an approval over it
            raise Conflict("the trigger changed; retry", code="revision_mismatch") from None
        if updated is None:
            raise NotFound(NOT_FOUND, code="webhook_trigger_not_found")
        self._store.add_event(tenant_id, row["id"], actor, kind, detail)
        out = self.view(updated, with_warnings=True)
        if generated is not None:
            out["generated_secret"] = generated
        return out
