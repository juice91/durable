"""`ExternalRunService`: what an external agent may do, whichever protocol it speaks (docs/external-access.md; design
sections 4, 5.2, 6 and 7). The surfaces (`eap_api.external_mcp`, `eap_api.external_a2a`) are thin controllers over this
service.

The rules:
- may start = client enabled and key active and scope `run` and export enabled for this protocol and the export's
  workflow in the key's allowlist. The key check goes through `services/authz.py` (`run.external`).
- may touch = the run is a claim of this client. Anything else (another client's run, a person's run, another tenant's)
  reads as not found, and the claim is checked before the run is read.
- The caller adds no authority: the input is data checked against the schema, the run acts as the key for nobody, and
  its tools and secrets come from the workflow's grants.
- Idempotency: the claim comes first, in our table, with the resolved version and the input's hash; the run id derives
  from (client, export, key). A crash anywhere is repaired by the caller's retry or the worker's sweep.
- Every start, cancel and refusal writes an audit row naming the key and the client; never the input or the key's
  idempotency value. The name the caller declares for itself (`principal.extra["agent"]`) is kept there as an
  untrusted label; nothing is decided by it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, TypeVar

from eap_iam import ACTION_RUN_EXTERNAL, Principal
from eap_run_store import ApiKeyStore, AuditEntry, AuditStore, InvalidRunCursor, RunActor, RunCursor, RunStore
from eap_run_store.api_keys import RUN_SCOPE
from eap_run_store.definition_errors import DefinitionError
from eap_run_store.external_access import (
    CONTRACT_CHANGED,
    DEADLINE_REASON,
    PROTOCOLS,
    RUN_STATES,
    ClaimLimits,
    ExternalAccessStore,
    ExternalQuotaExceeded,
    IdempotencyConflict,
    workflow_contract,
)
from eap_run_store.store import ENQUEUE_FAILED_ERROR, RUN_CANCELLED_CODE, TERMINAL_RUN_STATUSES
from eap_schemas.workflows import WorkflowSpec
from sqlalchemy.orm import Session

from eap_api.external_settings import ExternalSettings
from eap_api.services.access import Access
from eap_api.services.authz import AuthzService
from eap_api.services.errors import (
    Conflict,
    Forbidden,
    InvalidInput,
    NotFound,
    ServiceError,
    TooManyRequests,
    Unavailable,
)
from eap_api.services.workflow_inputs import input_schema_errors, non_finite_errors
from eap_api.services.workflow_runs import MAX_WORKFLOW_INPUT_BYTES, WorkflowRunService, canonical_json
from eap_api.workflows.models import WorkflowVersionModel

log = logging.getLogger("eap_api.external")

T = TypeVar("T")

IDEMPOTENCY_KEY_MAX_CHARS = 128
CONTEXT_LABEL_MAX_CHARS = 200
MAX_RESULT_BYTES = 256 * 1024
POLL_SECONDS = {"queued": 2, "running": 2, "input_required": 15}
_ERROR_CODE = re.compile(r"[a-z0-9_.]{1,64}")
# Fixed texts: nothing of the run's own error (hosts, step ids, tool or server names) leaves the platform.
MESSAGES = {
    DEADLINE_REASON: "The run did not finish before its deadline and was cancelled.",
    "outcome_unknown": "The run needs a person: an external action may have happened. Do not resubmit it.",
    "approval_rejected": "A person in the platform rejected the approval.",
    "not_started": "The run could not be started. Send the same request again.",
}
DEFAULT_MESSAGE = "The run failed."


@dataclass(frozen=True)
class Caller:
    """A resolved external caller: the principal (its scope and allowlist as the key holds them now) and the client."""

    principal: Principal
    client: dict[str, Any]

    @property
    def tenant_id(self) -> str:
        return self.principal.tenant_id

    @property
    def key_id(self) -> str:
        return self.principal.subject


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class ExternalRunService:
    def __init__(
        self,
        store: ExternalAccessStore,
        keys: ApiKeyStore,
        runs: WorkflowRunService,
        run_store: RunStore,
        authz: AuthzService,
        audit: AuditStore,
        db: Session,
        settings: ExternalSettings,
    ) -> None:
        self._store = store
        self._keys = keys
        self._runs = runs  # built with no Access: the authority is decided here
        self._run_store = run_store
        self._authz = authz
        self._audit = audit
        self._db = db
        self._settings = settings

    # ------------------------------------------------------------------------------------------ who is calling

    def _caller(self, principal: Principal, context: dict[str, Any]) -> Caller:
        if not self._settings.enabled:
            raise NotFound("not found", code="external_access_disabled")
        if principal.kind != "api_key":
            raise Forbidden("the external surface takes an API key", code="api_key_required")
        if RUN_SCOPE not in principal.scopes:
            raise Forbidden("API key lacks the run scope", code="missing_scope")
        record = self._keys.get(principal.tenant_id, principal.subject)
        if record is None or record.status() != "active":
            raise Forbidden("the API key is not active", code="key_inactive")
        if record.scopes != (RUN_SCOPE,) or record.external_client_id is None or record.owner_subject is not None:
            raise Forbidden("API key lacks the run scope", code="missing_scope")
        client = self._store.get_client(principal.tenant_id, record.external_client_id)
        if client is not None:
            context["client"] = client["name"]
        if client is None or not client["enabled"]:
            raise Forbidden("this external client is disabled", code="client_disabled")
        current = principal.model_copy(update={
            "scopes": frozenset(record.scopes), "agent_ids": record.agent_ids, "workflow_ids": record.workflow_ids,
        })
        return Caller(current, client)

    def _allowed(self, caller: Caller, workflow_id: str) -> bool:
        return Access(self._authz, caller.principal).allowed(ACTION_RUN_EXTERNAL, "workflow", workflow_id)

    def _endpoint(self, tenant_id: str, endpoint_id: str) -> None:
        """The A2A endpoint of the URL is the caller's tenant's own and is served; any other id does not exist."""
        found = self._store.get_endpoint(tenant_id)
        if found is None or not found["enabled"] or found["endpoint_id"] != endpoint_id:
            raise NotFound("not found", code="endpoint_not_found")

    def public_endpoint(self, endpoint_id: str) -> dict[str, str]:
        """What the public Agent Card of an endpoint says. No caller, no audit row (unauthenticated noise writes
        nothing); one answer for an unknown, disabled or switched-off endpoint."""
        found = None
        if self._settings.enabled and self._settings.base_url:
            found = self._store.find_endpoint(endpoint_id)
        if found is None or not found["enabled"]:
            raise NotFound("not found", code="endpoint_not_found")
        return {"name": found["card_name"], "description": found["card_description"]}

    # ------------------------------------------------------------------------------------------ audit

    def _audited(self, action: str, principal: Principal, context: dict[str, Any], call: Callable[[], T]) -> T:
        """Run `call`; a refusal writes an audit row (`denied` for a Forbidden one) naming its stable code."""
        try:
            return call()
        except ServiceError as exc:
            outcome = "denied" if isinstance(exc, Forbidden) else "failed"
            self._record(action, principal, context, outcome, reason=exc.code)
            raise

    def _record(
        self, action: str, principal: Principal, context: dict[str, Any], outcome: str, *, reason: str | None = None
    ) -> None:
        detail = {k: context[k] for k in ("client", "protocol", "export") if context.get(k)}
        if principal.extra.get("agent"):
            detail["agent"] = str(principal.extra["agent"])
        if reason:
            detail["reason"] = reason
        try:
            self._audit.record(AuditEntry(
                tenant_id=principal.tenant_id, actor_kind=principal.kind, actor_subject=principal.subject,
                action=f"external.{action}", outcome=outcome, resource_kind="external_run",
                resource_id=context.get("run_id"), run_id=context.get("run_id"), detail=detail,
            ))
        except Exception:  # noqa: BLE001 - never fail the call over its log; the row is not logged (it names people)
            log.exception("audit_log write failed for action external.%s", action)

    def admit(self, principal: Principal, endpoint_id: str | None = None) -> None:
        """Whether this caller may use the external surface at all (switch, run key, enabled client; for A2A also the
        endpoint of its tenant), asked once per request by a controller; every other method checks the caller again."""
        context: dict[str, Any] = {}

        def go() -> None:
            caller = self._caller(principal, context)
            if endpoint_id is not None:
                self._endpoint(caller.tenant_id, endpoint_id)

        self._audited("access", principal, context, go)

    # ------------------------------------------------------------------------------------------ exports

    def exports(self, principal: Principal, protocol: str) -> list[dict[str, Any]]:
        """The exports this key may start over `protocol`, with what a caller needs to start one."""
        context: dict[str, Any] = {"protocol": protocol}

        def go() -> list[dict[str, Any]]:
            caller = self._caller(principal, context)
            self._protocol(protocol)
            return self._exports(caller, protocol)

        return self._audited("exports.list", principal, context, go)

    def _exports(self, caller: Caller, protocol: str) -> list[dict[str, Any]]:
        found = []
        for export in self._store.list_exports(caller.tenant_id):
            if not export["enabled"] or protocol not in export["protocols"]:
                continue
            if not self._allowed(caller, export["workflow_id"]):
                continue
            version = self._version(caller.tenant_id, export["workflow_id"], export["workflow_version"])
            if version is None:
                continue
            found.append({
                "name": export["name"], "title": export["title"], "description": export["description"],
                "tags": export["tags"], "examples": export["examples"],
                "input_schema": version.spec.get("input_schema") or {}, "result": export["result_mode"],
                "require_idempotency_key": export["require_idempotency_key"], "version": version.version,
            })
        return found

    @staticmethod
    def _protocol(protocol: str) -> None:
        if protocol not in PROTOCOLS:
            raise InvalidInput(f"protocol must be one of {', '.join(PROTOCOLS)}", code="protocol_invalid")

    def _version(self, tenant_id: str, workflow_id: str, version: int | None) -> WorkflowVersionModel | None:
        self._db.expire_all()  # a version published since this session last looked
        query = self._db.query(WorkflowVersionModel).filter(
            WorkflowVersionModel.tenant_id == tenant_id, WorkflowVersionModel.workflow_id == workflow_id
        )
        if version is not None:
            return query.filter(WorkflowVersionModel.version == version).first()
        return query.order_by(WorkflowVersionModel.version.desc()).first()

    # ------------------------------------------------------------------------------------------ start

    def start(
        self,
        principal: Principal,
        protocol: str,
        export: str,
        input: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        deadline_seconds: int | None = None,
        context_label: str | None = None,
    ) -> dict[str, Any]:
        """Start a run of the export `export`, or answer the run `idempotency_key` already names; the run view.

        NotFound `export_not_found` for an export this caller may not start (unknown, disabled, another protocol, outside
        the key's allowlist: one answer); Conflict `idempotency_conflict` (the key with another input),
        `grants_not_enforced`, `workflow_archived`; InvalidInput `input_invalid` (loc, msg and keyword only),
        `input_too_large`, `idempotency_key_required`; TooManyRequests `quota_exceeded`; Unavailable
        `start_unavailable` (retry the same request: the same key finishes it)."""
        context: dict[str, Any] = {"protocol": protocol, "export": export}

        def go() -> dict[str, Any]:
            caller = self._caller(principal, context)
            view = self._start(
                caller, protocol, export, input, idempotency_key, deadline_seconds, context_label, context
            )
            self._record("run.start", principal, context, "success")
            return view

        return self._audited("run.start", principal, context, go)

    # ------------------------------------------------------------------------------------------ messages (A2A)

    def send(
        self,
        principal: Principal,
        protocol: str,
        *,
        message_id: str,
        data: dict[str, Any] | None = None,
        text: str | None = None,
        skill: str | None = None,
        base_skill: str | None = None,
        context_id: str | None = None,
        deadline_seconds: int | None = None,
    ) -> dict[str, Any]:
        """A new message of a conversational protocol (design 5.2): choose the export, make its input and start it, with
        `message_id` as the idempotency key; the run view with `context_id` and `updated_at`.

        The export is the one the message names (`skill`) or the one its address names (`base_skill`; the two must
        agree); else the one an earlier send of this `message_id` started; else the only one the key reaches
        (InvalidInput `skill_required` otherwise). The input is `data`, or `text` for an export whose input is one
        required string property (InvalidInput `content_type_not_supported` otherwise). Refusals as `start`, and
        Conflict `idempotency_conflict` for the same message in another context."""
        context: dict[str, Any] = {"protocol": protocol}

        def go() -> dict[str, Any]:
            caller = self._caller(principal, context)
            self._protocol(protocol)
            if skill is not None and base_skill is not None and skill != base_skill:
                raise InvalidInput(f"this address runs the skill {base_skill} only", code="skill_mismatch")
            key_hash = hashlib.sha256(message_id.encode()).hexdigest()
            name = skill or base_skill
            if name is None:
                earlier = self._store.claimed_exports(caller.tenant_id, caller.client["id"], protocol, key_hash)
                reachable = earlier if len(earlier) == 1 else [e["name"] for e in self._exports(caller, protocol)]
                if len(reachable) != 1:
                    raise InvalidInput(
                        f"name the skill to run; {self._skills(caller, protocol)}", code="skill_required"
                    )
                name = reachable[0]
            context["export"] = name
            try:
                return self._send(
                    caller, protocol, name, message_id, key_hash, data, text, context_id, deadline_seconds, context
                )
            except NotFound as exc:
                if exc.code != "export_not_found":
                    raise
                raise NotFound(
                    f"export not found; {self._skills(caller, protocol)}", code="export_not_found"
                ) from None

        return self._audited("run.start", principal, context, go)

    def _skills(self, caller: Caller, protocol: str) -> str:
        names = [e["name"] for e in self._exports(caller, protocol)]
        return f"the skills you may run: {', '.join(names)}" if names else "this key reaches no skill"

    def _send(
        self, caller: Caller, protocol: str, name: str, message_id: str, key_hash: str, data: dict[str, Any] | None,
        text: str | None, context_id: str | None, deadline_seconds: int | None, context: dict[str, Any],
    ) -> dict[str, Any]:
        tenant, client_id = caller.tenant_id, caller.client["id"]
        export = self._store.get_export_by_name(tenant, name)
        if export is None or not self._allowed(caller, export["workflow_id"]):
            raise NotFound("export not found", code="export_not_found")
        claimed = self._store.find_claim(tenant, client_id, export["id"], key_hash)
        if claimed is not None and context_id is not None:
            if context_id != (claimed["context_label"] or claimed["run_id"]):
                raise Conflict("this message id was used in another context", code="idempotency_conflict")
        if text is not None:
            if claimed is None and (not export["enabled"] or protocol not in export["protocols"]):
                raise NotFound("export not found", code="export_not_found")
            wanted = claimed["workflow_version"] if claimed is not None else export["workflow_version"]
            version = self._version(tenant, export["workflow_id"], wanted)
            if version is None:
                raise NotFound("export not found", code="export_not_found")
            field = text_property(version.spec.get("input_schema") or {})
            if field is None:
                raise InvalidInput(
                    f"{name} takes a data part (a JSON object), not text", code="content_type_not_supported"
                )
            data = {field: text}
        view = self._start(
            caller, protocol, name, data, message_id, deadline_seconds,
            context_id or str(uuid.uuid4()), context,
        )
        self._record("run.start", caller.principal, context, "success")
        return self._view(tenant, self._own_run(caller, view["run_id"]), task_fields=True)

    def follow_up(self, principal: Principal, run_id: str, context_id: str | None = None) -> None:
        """A message addressed to an existing run. Nothing is answerable from outside in this release (an approval
        never is), so it always refuses and changes nothing: NotFound `run_not_found`; InvalidInput `context_mismatch`;
        Conflict `task_terminal` for an ended run, else `answer_not_supported`."""
        context: dict[str, Any] = {}

        def go() -> None:
            caller = self._caller(principal, context)
            found = self._own_run(caller, run_id)
            context["run_id"] = found["run_id"]
            context["export"] = found["export_name"]
            if context_id is not None and context_id != (found["context_label"] or found["run_id"]):
                raise InvalidInput("the context is not this task's", code="context_mismatch")
            if found["status"] in TERMINAL_RUN_STATUSES:
                raise Conflict("the task has ended and takes no further message", code="task_terminal")
            raise Conflict(
                "a task takes no further message from outside; a person in the platform decides what it waits for",
                code="answer_not_supported",
            )

        self._audited("run.answer", principal, context, go)

    def _start(
        self, caller: Caller, protocol: str, name: str, input: dict[str, Any], key: str | None,
        deadline_seconds: int | None, context_label: str | None, context: dict[str, Any],
    ) -> dict[str, Any]:
        self._protocol(protocol)
        tenant = caller.tenant_id
        client_id = caller.client["id"]
        export = self._store.get_export_by_name(tenant, name)
        if export is None or not self._allowed(caller, export["workflow_id"]):
            raise NotFound("export not found", code="export_not_found")
        if key is not None and not (isinstance(key, str) and 1 <= len(key) <= IDEMPOTENCY_KEY_MAX_CHARS):
            raise InvalidInput(
                f"idempotency_key is 1 to {IDEMPOTENCY_KEY_MAX_CHARS} characters", code="idempotency_key_invalid"
            )
        if not isinstance(input, dict):
            raise InvalidInput("input must be a JSON object", code="input_invalid")
        errors = non_finite_errors(input)
        if errors:
            raise InvalidInput("the input is not valid", code="input_invalid", details=errors)
        input_json = canonical_json(input)
        input_hash = hashlib.sha256(input_json.encode()).hexdigest()
        key_hash = None if key is None else hashlib.sha256(key.encode()).hexdigest()

        claimed = None if key_hash is None else self._store.find_claim(tenant, client_id, export["id"], key_hash)
        if claimed is not None:
            if claimed["input_hash"] != input_hash:
                raise Conflict("this idempotency key was used with another input", code="idempotency_conflict")
            found = self._store.get_external_run(tenant, client_id, claimed["run_id"])
            assert found is not None  # the claim exists
            if not self._needs_handoff(found):
                context["run_id"] = found["run_id"]
                return self._view(tenant, found)
            if found["status"] is not None and not export["enabled"]:
                context["run_id"] = found["run_id"]  # touching a run of one's own is allowed; starting it is not
                return self._view(tenant, found)

        if not export["enabled"] or protocol not in export["protocols"]:
            raise NotFound("export not found", code="export_not_found")
        if not (self._settings.grants_requested and self._authz.supports_grants):
            raise Conflict("agent and workflow grants are not enforced on this platform", code="grants_not_enforced")
        if key is None and export["require_idempotency_key"]:
            raise InvalidInput("this export needs an idempotency_key", code="idempotency_key_required")
        if deadline_seconds is not None and (
            isinstance(deadline_seconds, bool) or not isinstance(deadline_seconds, int) or deadline_seconds < 1
        ):
            raise InvalidInput("deadline_seconds is a positive number of seconds", code="deadline_invalid")
        runtime = min(export["max_runtime_seconds"], self._settings.max_runtime_seconds)
        if deadline_seconds is not None:
            runtime = min(runtime, deadline_seconds)

        wanted = claimed["workflow_version"] if claimed is not None else export["workflow_version"]
        version = self._version(tenant, export["workflow_id"], wanted)
        if version is None:
            raise NotFound("export not found", code="export_not_found")
        spec = WorkflowSpec.model_validate(version.spec)
        if claimed is None and export["workflow_version"] is None:
            contract = workflow_contract(self._db.connection(), tenant, spec)
            self._db.rollback()  # the read's transaction
            if contract.hash != export["approved_contract_hash"]:
                self._disable(tenant, export)
                raise NotFound("export not found", code="export_not_found")
        errors = input_schema_errors(spec.input_schema, input)
        if errors:
            raise InvalidInput("the input is not valid", code="input_invalid", details=errors)
        if len(input_json.encode("utf-8")) > MAX_WORKFLOW_INPUT_BYTES:
            raise InvalidInput(
                f"input must be at most {MAX_WORKFLOW_INPUT_BYTES // 1024} KiB as JSON", code="input_too_large"
            )

        run_id = uuid.uuid4() if key is None else uuid.uuid5(uuid.UUID(client_id), f"{export['id']}:{key}")
        client = caller.client
        limits = ClaimLimits(
            client_active=self._settings.client_active if client["max_active_runs"] is None else client["max_active_runs"],
            tenant_active=self._settings.tenant_active,
            client_per_day=(
                self._settings.client_per_day if client["max_runs_per_day"] is None else client["max_runs_per_day"]
            ),
        )
        try:
            claim = self._store.claim_run(
                tenant, client_id=client_id, key_id=caller.key_id, export_id=export["id"], protocol=protocol,
                idempotency_hash=key_hash, input_hash=input_hash, run_id=str(run_id), workflow_version=version.version,
                deadline_at=datetime.now(timezone.utc) + timedelta(seconds=runtime),
                context_label=None if context_label is None else str(context_label)[:CONTEXT_LABEL_MAX_CHARS],
                limits=limits,
            )
        except IdempotencyConflict:
            raise Conflict("this idempotency key was used with another input", code="idempotency_conflict") from None
        except ExternalQuotaExceeded as exc:
            raise TooManyRequests(
                f"the {exc.quota} quota ({exc.limit}) is reached", code="quota_exceeded", retry_after=exc.retry_after
            ) from None
        run_id = uuid.UUID(claim.row["run_id"])
        context["run_id"] = str(run_id)
        try:
            self._runs.start(
                tenant, uuid.UUID(export["workflow_id"]), input, version=claim.row["workflow_version"], run_id=run_id,
                actor=RunActor(kind="api_key", subject=caller.key_id),
            )
        except DefinitionError as exc:
            # A refusal (not a crash) with no run row leaves nothing behind: the key can be used again.
            self._db.rollback()
            if self._store.release_claim(tenant, str(run_id)):
                context.pop("run_id")
            if exc.status_code == 503:
                raise Unavailable(
                    "the run could not be started; send the same request again", code="start_unavailable"
                ) from None
            raise Conflict("the workflow cannot be started now", code=exc.code or "start_refused") from None
        found = self._store.get_external_run(tenant, client_id, str(run_id))
        assert found is not None
        return self._view(tenant, found)

    @staticmethod
    def _needs_handoff(found: dict[str, Any]) -> bool:
        """The claim's run was not (certainly) handed to the worker: no run row, still queued, or the hand-off failed."""
        return found["status"] in (None, "queued") or (
            found["status"] == "failed" and found["error"] == ENQUEUE_FAILED_ERROR
        )

    def _disable(self, tenant_id: str, export: dict[str, Any]) -> None:
        self._store.update_export(
            tenant_id, export["id"], {}, None, enabled=False, disabled_reason=CONTRACT_CHANGED, approved_hash=None,
            approved_contract_hash=None, enabled_by=None, enabled_at=None,
        )

    # ------------------------------------------------------------------------------------------ one's own runs

    def _own_run(self, caller: Caller, run_id: str) -> dict[str, Any]:
        found = self._store.get_external_run(caller.tenant_id, caller.client["id"], str(run_id))
        if found is None:
            raise NotFound("run not found", code="run_not_found")
        return found

    def get(self, principal: Principal, run_id: str, *, task_fields: bool = False) -> dict[str, Any]:
        """One's own run; `task_fields` adds `context_id` and `updated_at` (A2A)."""
        context: dict[str, Any] = {}

        def go() -> dict[str, Any]:
            caller = self._caller(principal, context)
            return self._view(caller.tenant_id, self._own_run(caller, run_id), task_fields=task_fields)

        return self._audited("run.get", principal, context, go)

    def list(
        self, principal: Principal, *, state: str | None = None, limit: int = 50, cursor: str | None = None,
        context_id: str | None = None, changed_after: datetime | None = None, with_result: bool = False,
        with_total: bool = False, task_fields: bool = False,
    ) -> dict[str, Any]:
        """This client's runs, the latest status change first: `{items, next_cursor}` (results only `with_result`;
        `total`, the count before paging, only `with_total`)."""
        context: dict[str, Any] = {}

        def go() -> dict[str, Any]:
            caller = self._caller(principal, context)
            if state is not None and state not in RUN_STATES:
                raise InvalidInput(f"state must be one of {', '.join(RUN_STATES)}", code="state_invalid")
            try:
                after = None if cursor is None else RunCursor.decode(cursor)
            except InvalidRunCursor:
                raise InvalidInput("cursor is not valid", code="cursor_invalid") from None
            tenant, client_id = caller.tenant_id, caller.client["id"]
            filters: dict[str, Any] = {"state": state, "context_id": context_id, "changed_after": changed_after}
            rows, following = self._store.list_runs(tenant, client_id, limit=limit, cursor=after, **filters)
            page = {
                "items": [
                    self._view(tenant, r, with_result=with_result, task_fields=task_fields) for r in rows
                ],
                "next_cursor": None if following is None else following.encode(),
            }
            if with_total:
                page["total"] = self._store.count_runs(tenant, client_id, **filters)
            return page

        return self._audited("run.list", principal, context, go)

    def cancel(
        self, principal: Principal, run_id: str, *, require_cancelable: bool = False, task_fields: bool = False
    ) -> dict[str, Any]:
        """Cancel one's own run with the platform's durable cancel: `{accepted, run}`. `accepted` is false for a run
        that had already ended: a repeated cancel is harmless, unless `require_cancelable` asks for Conflict
        `cancel_refused` then. Unavailable `cancel_unavailable` while the run has not been created yet or the cancel is
        recorded but unfinished (it completes by itself; retrying is safe)."""
        context: dict[str, Any] = {}

        def go() -> dict[str, Any]:
            caller = self._caller(principal, context)
            found = self._own_run(caller, run_id)
            context["run_id"] = found["run_id"]
            context["export"] = found["export_name"]
            accepted = False
            if found["status"] is None:
                raise Unavailable("the run has not started yet; try again", code="cancel_unavailable")
            if found["status"] not in TERMINAL_RUN_STATUSES:
                try:
                    result = self._runs.cancel(caller.tenant_id, f"apikey:{caller.key_id}", uuid.UUID(found["run_id"]))
                    accepted = bool(result["cancelled"])
                except DefinitionError as exc:
                    self._db.rollback()
                    if exc.status_code == 503:
                        raise Unavailable(
                            "the cancel is recorded and not finished; it completes by itself", code="cancel_unavailable"
                        ) from None
                    if exc.status_code != 409:  # 409: the run ended in between
                        raise Conflict("the run cannot be cancelled", code="cancel_refused") from None
            view = self._view(caller.tenant_id, self._own_run(caller, run_id), task_fields=task_fields)
            if require_cancelable and not accepted and view["done"]:
                raise Conflict("the run has ended and cannot be cancelled", code="cancel_refused")
            self._record("run.cancel", principal, context, "success")
            return {"accepted": accepted, "run": view}

        return self._audited("run.cancel", principal, context, go)

    # ------------------------------------------------------------------------------------------ the view

    def _view(
        self, tenant_id: str, row: dict[str, Any], *, with_result: bool = True, task_fields: bool = False
    ) -> dict[str, Any]:
        """The run as an external caller sees it (design 6): its state, never step names, logs or internal errors."""
        status = row["status"]
        view: dict[str, Any] = {
            "run_id": row["run_id"], "export": row["export_name"], "version": row["workflow_version"],
            "deadline": _iso(row["deadline_at"]),
        }
        if status in (None, "queued"):
            state = "queued"
        elif status == "running":
            state = "running"
            if row["waiting_approval"]:
                state = "input_required"
                kind = self._store.waiting_kind(tenant_id, row["run_id"]) or "approval"
                # Nothing is answerable from outside in this release; an approval never is.
                view["waiting"] = {"kind": kind, "answerable": False}
        elif status in ("completed", "skipped"):
            state = "completed"
            if status == "skipped":
                view["outcome"] = "skipped"
            if with_result and row["result_mode"] == "output":
                view.update(_result(row))
        elif status == "needs_attention":
            state = "failed"
            view["error"] = _error("outcome_unknown")
            view["retry_safe"] = False
        else:  # failed
            state = "failed"
            code = row["error_code"]
            if row["error"] == ENQUEUE_FAILED_ERROR:
                view["error"], view["retry_safe"] = _error("not_started"), True
            elif code == RUN_CANCELLED_CODE and row["cancel_reason"] == DEADLINE_REASON:
                view["error"] = _error(DEADLINE_REASON)
            elif code == RUN_CANCELLED_CODE:
                state = "cancelled"
            else:
                view["error"] = _error(code if code and _ERROR_CODE.fullmatch(code) else "run_failed")
        view["state"] = state
        view["done"] = state in ("completed", "failed", "cancelled")
        view["poll_after_seconds"] = POLL_SECONDS.get(state, 0)
        if task_fields:
            view["context_id"] = row["context_label"] or row["run_id"]
            view["updated_at"] = _iso(row["changed_at"])
        return view


def text_property(input_schema: dict[str, Any]) -> str | None:
    """The property a plain text fills: the only property of an object schema, when it is a required string."""
    properties = input_schema.get("properties")
    if input_schema.get("type") != "object" or not isinstance(properties, dict) or len(properties) != 1:
        return None
    (name, schema), = properties.items()
    if input_schema.get("required") != [name] or not isinstance(schema, dict) or schema.get("type") != "string":
        return None
    return name


def _error(code: str) -> dict[str, str]:
    return {"code": code, "message": MESSAGES.get(code, DEFAULT_MESSAGE)}


def _result(row: dict[str, Any]) -> dict[str, Any]:
    """The workflow's return value (`result`), or `truncated` when it is over 256 KiB."""
    output = row["output"]
    if output is None:
        return {}
    if len(output.encode("utf-8")) > MAX_RESULT_BYTES:
        return {"truncated": True}
    if row["output_format"] == "json":
        try:
            return {"result": json.loads(output)}
        except ValueError:
            pass
    return {"result": output}
