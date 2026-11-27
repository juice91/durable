"""The inbound webhook pipeline after authentication (design 4.2 steps 7 to 9, 4.4 and 5).

By the time `handle` runs, the `webhook` identity provider has verified the HMAC (and the timestamp window) over the raw
bytes: the principal IS the trigger, with the grant stored in its row: start a run of exactly its workflow, from the
mapped input, nothing else. This service never reads anything a request says about a workflow, version, tenant or agent.

Order: re-read the trigger (a disable that landed since the signature check wins) -> parse the body (strict UTF-8, no NUL,
depth, storable strings, an object) -> claim the dedupe key -> validate the payload against `payload_schema` -> map it to
the run input -> per-trigger concurrency and daily limits -> `WorkflowRunService.start` with the deterministic run id
`uuid5(trigger_id, dedupe_key)` -> the claim row becomes `accepted` with the run id.

Exactly-once by construction: a retried delivery has the same dedupe key, so it finds the claim; even if every table but
`runs` were lost, the same key derives the same run id and `start`'s `run_id` retry path answers the same run again.
Every crash point ends in the same run or none: the next delivery after the 60 s lease takes the claim over.

The answers are uniform and carry no run id, tenant id or reason that helps a prober: `{"status": "accepted"|"duplicate",
"delivery": "<delivery row id>"}`."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from eap_observability.redaction import log_failure

from eap_api.services.errors import InvalidInput, NotFound, ServiceError, TooManyRequests, Unavailable
from eap_api.services.workflow_runs import WorkflowRunService
from eap_api.text_checks import decode_utf8_json_body, json_depth_exceeds, unstorable_strings
from eap_run_store import AuditEntry, AuditStore, WebhookStore
from eap_run_store.definition_errors import DefinitionError

log = logging.getLogger("eap_api.webhooks")

WEBHOOK_NAMESPACE = uuid.UUID("6f0d9a52-3c1e-4b7a-9d2e-5a8c1f3b7e41")
MAX_BODY_DEPTH = 32
CONCURRENCY_RETRY_SECONDS = 30
BUSY_RETRY_SECONDS = 2
NOT_FOUND_DETAIL = "not found"
LABEL_MAX_CHARS = 64
AUDIT_ACTION = "webhook_run_started"
_LABEL_SAFE = re.compile(r"[^A-Za-z0-9._:@ /+=-]")


@dataclass(frozen=True)
class TriggerGate:
    """What the route needs before it reads the body: the limits of an enabled trigger."""

    trigger_id: str
    rate_per_minute: int
    max_body_bytes: int


@dataclass(frozen=True)
class VerifiedDelivery:
    """What the identity provider proved about a delivery (nothing the sender merely claimed)."""

    tenant_id: str
    trigger_id: str
    dedupe_key: str
    key_kind: str
    secret_slot: int | None
    skew_seconds: int | None
    config_hash: str


def run_id_for(trigger_id: str, dedupe_key: str) -> uuid.UUID:
    """The deterministic run id of a delivery: only a signature holder can choose it, and only by replaying content that
    dedupes to the same key."""
    return uuid.uuid5(WEBHOOK_NAMESPACE, f"{trigger_id}:{dedupe_key}")


def sanitize_label(value: str | None) -> str | None:
    """An UNVERIFIED label from a header (a provider's delivery GUID or event name): short, printable, display only."""
    if not value:
        return None
    return _LABEL_SAFE.sub("?", value.strip())[:LABEL_MAX_CHARS] or None


def resolve_pointer(payload: Any, pointer: str) -> tuple[bool, Any]:
    """RFC 6901 `pointer` into `payload`: (found, value)."""
    if pointer == "":
        return True, payload
    current = payload
    for raw in pointer[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if token not in current:
                return False, None
            current = current[token]
        elif isinstance(current, list):
            if not token.isdigit() or (len(token) > 1 and token.startswith("0")) or int(token) >= len(current):
                return False, None
            current = current[int(token)]
        else:
            return False, None
    return True, current


def apply_input_map(mapping: Any, payload: Any, path: str = "input_map") -> Any:
    """The run input from `input_map`: leaves are literals, `{"$pointer": ...}` or `{"$payload": true}`. A pointer that does
    not resolve raises InvalidInput `input_map_unresolved` naming the mapping location only."""
    if isinstance(mapping, dict):
        if set(mapping) == {"$pointer"}:
            found, value = resolve_pointer(payload, mapping["$pointer"])
            if not found:
                raise InvalidInput("a pointer of the input mapping did not resolve", code="input_map_unresolved", details=[{"loc": path}])
            return value
        if set(mapping) == {"$payload"}:
            return payload
        return {key: apply_input_map(value, payload, f"{path}.{key}") for key, value in mapping.items()}
    if isinstance(mapping, list):
        return [apply_input_map(value, payload, f"{path}[{i}]") for i, value in enumerate(mapping)]
    return mapping


def payload_errors(schema: dict[str, Any], payload: Any) -> list[dict[str, Any]]:
    """`payload` against the action's `payload_schema` with an empty registry: location and keyword only, never a value."""
    import jsonschema
    from referencing import Registry

    try:
        cls = jsonschema.validators.validator_for(schema)
        errors = sorted(cls(schema, registry=Registry()).iter_errors(payload), key=lambda e: list(e.absolute_path))
        return [
            {"loc": ["payload", *[p if isinstance(p, int) else str(p)[:100] for p in err.absolute_path]],
             "msg": f"does not satisfy the payload schema's '{err.validator}' keyword", "type": f"json_schema.{err.validator}"}
            for err in errors[:20]
        ]
    except Exception:  # noqa: BLE001 - an invalid stored schema is the trigger's problem, never the sender's text
        return [{"loc": ["payload"], "msg": "the trigger's payload schema could not be applied", "type": "json_schema.invalid_schema"}]


class WebhookIngestService:
    def __init__(self, store: WebhookStore, runs: WorkflowRunService, audit: AuditStore | None = None) -> None:
        self._store = store
        self._runs = runs
        self._audit = audit

    # ------------------------------------------------------------------------------------------ before the body

    def gate(self, trigger_id: str) -> TriggerGate:
        """The limits of an enabled, approved trigger, or NotFound (the same answer for unknown, disabled, archived,
        pending and edited-after-approval triggers: the cause never reaches the sender)."""
        try:
            uuid.UUID(trigger_id)
        except ValueError:
            raise NotFound(NOT_FOUND_DETAIL, code="not_found") from None
        row = self._store.get_trigger_by_id(trigger_id)
        if row is None or row["status"] != "enabled" or row["approved_config_hash"] != row["config_hash"]:
            raise NotFound(NOT_FOUND_DETAIL, code="not_found")
        limits = row["limits"]
        return TriggerGate(row["id"], int(limits.get("rate_per_minute", 60)), int(limits.get("max_body_bytes", 262144)))

    # ------------------------------------------------------------------------------------------ the delivery

    def handle(
        self, delivery: VerifiedDelivery, raw_body: bytes, *, content_type: str | None = None,
        provider_delivery_id: str | None = None, event_label: str | None = None,
    ) -> dict[str, Any]:
        """Process one authenticated delivery. Returns `{"status", "delivery"}` (`accepted` or `duplicate`) or raises a
        ServiceError the route renders: NotFound (404), InvalidInput (415/422), TooManyRequests (429), Unavailable (503)."""
        tenant, trigger_id = delivery.tenant_id, delivery.trigger_id
        row = self._store.get_trigger(tenant, trigger_id)
        if (
            row is None or row["status"] != "enabled" or row["approved_config_hash"] != row["config_hash"]
            or row["config_hash"] != delivery.config_hash
        ):
            raise NotFound(NOT_FOUND_DETAIL, code="not_found")
        diagnostics = {
            "body_sha256": hashlib.sha256(raw_body).hexdigest(),
            "body_bytes": len(raw_body), "content_type": (content_type or "")[:100] or None,
            "timestamp_skew_seconds": delivery.skew_seconds, "secret_slot": delivery.secret_slot,
            "provider_delivery_id": sanitize_label(provider_delivery_id), "event_label": sanitize_label(event_label),
        }
        payload = self._parse(tenant, trigger_id, raw_body, diagnostics)
        run_id = run_id_for(trigger_id, delivery.dedupe_key)
        verdict, claimed = self._store.claim_delivery(
            tenant, trigger_id, delivery.dedupe_key, delivery.key_kind, run_id=str(run_id), **diagnostics
        )
        if verdict == "duplicate":
            self._redrive(row, claimed, payload)
            return {"status": "duplicate", "delivery": claimed["id"]}
        if verdict == "busy":
            raise Unavailable("a delivery with this id is being processed; retry shortly", code="delivery_busy")
        delivery_id = claimed["id"]
        try:
            return self._start(row, delivery_id, run_id, payload)
        except ServiceError:
            raise
        except Exception as exc:  # noqa: BLE001 - the claim must not stay live if this attempt dies in an unexpected way
            log_failure(log, "webhook delivery failed", exc, code="webhook_delivery_failed", level=logging.ERROR, trigger_id=trigger_id)
            self._store.finish_delivery(tenant, delivery_id, "unavailable", reason="internal_error")
            raise Unavailable("the delivery could not be processed; retry", code="delivery_unavailable") from None

    # ------------------------------------------------------------------------------------------ steps

    def _record_run(self, row: dict[str, Any], run_id: str) -> None:
        """The audit row of a run an outside system started, naming the trigger. Only accepted deliveries write one (the
        trigger's run limits bound them), so a flood cannot grow the log. Best effort: the run already started."""
        if self._audit is None:
            return
        try:
            self._audit.record(AuditEntry(
                tenant_id=row["tenant_id"], actor_kind="webhook", actor_subject=row["id"], action=AUDIT_ACTION,
                outcome="success", resource_kind="webhook-triggers", resource_id=row["id"], run_id=run_id,
                detail={"name": row["name"]},
            ))
        except Exception as exc:  # noqa: BLE001
            log_failure(log, "webhook audit write failed", exc, code="webhook_audit_failed", level=logging.ERROR, trigger_id=row["id"])

    def _parse(self, tenant: str, trigger_id: str, raw_body: bytes, diagnostics: dict[str, Any]) -> dict[str, Any]:
        """Only now parse: the bytes are authentic, but an authentic sender can still have a bug. A refusal is a
        `body_invalid` row (the signature was valid) and a fixed message."""
        text = decode_utf8_json_body(raw_body)
        problem: tuple[str, str] | None = None
        payload: Any = None
        if text is None:
            problem = ("body_not_utf8", "the body must be UTF-8 JSON without NUL bytes")
        elif json_depth_exceeds(text, MAX_BODY_DEPTH):
            problem = ("body_too_deep", f"the body nests more than {MAX_BODY_DEPTH} levels")
        else:
            try:
                payload = json.loads(text, parse_constant=_refuse_constant)
            except ValueError:
                problem = ("body_not_json", "the body is not valid JSON")
            else:
                if unstorable_strings(payload):
                    problem = ("body_unstorable", "the body holds a string the platform cannot store")
                elif not isinstance(payload, dict):
                    problem = ("body_not_object", "the body must be a JSON object")
        if problem is not None:
            self._store.record_delivery(tenant, trigger_id, "body_invalid", problem[0], **diagnostics)
            self._store.note_delivery(tenant, trigger_id, "body_invalid")
            raise InvalidInput(problem[1], code=problem[0])
        return payload

    def _redrive(self, row: dict[str, Any], claimed: dict[str, Any], payload: dict[str, Any]) -> None:
        """A duplicate of an accepted delivery whose run never got past `queued` (a crash between the run row and the
        enqueue): hand it off again. `WorkflowRunService.start` is idempotent for the same run id, input and version."""
        run_id = claimed.get("run_id")
        if not run_id or self._store.get_run_status(row["tenant_id"], run_id) != "queued":
            return
        try:
            start = row["actions"]["start"]
            self._runs.start(
                row["tenant_id"], uuid.UUID(row["workflow_id"]), apply_input_map(start["input_map"], payload),
                version=row["workflow_version"], run_id=uuid.UUID(run_id), trigger_id=row["id"],
            )
        except Exception as exc:  # noqa: BLE001 - the answer is `duplicate` either way; the next retry tries again
            log_failure(log, "webhook redrive failed", exc, code="webhook_redrive_failed", level=logging.WARNING)

    def _start(self, row: dict[str, Any], delivery_id: str, run_id: uuid.UUID, payload: dict[str, Any]) -> dict[str, Any]:
        tenant, trigger_id = row["tenant_id"], row["id"]
        start = row["actions"]["start"]

        def reject(outcome: str, reason: str) -> None:
            self._store.finish_delivery(tenant, delivery_id, outcome, reason=reason)  # leaves the live-claim index: a corrected resend is processed
            self._store.note_delivery(tenant, trigger_id, outcome)

        errors = payload_errors(start["payload_schema"], payload)
        if errors:
            reject("rejected_schema", "payload_schema")
            raise InvalidInput("the payload does not match the trigger's schema", code="payload_invalid", details=errors)
        try:
            run_input = apply_input_map(start["input_map"], payload)
        except InvalidInput as exc:
            reject("rejected_schema", "input_map_unresolved")
            raise exc
        with self._store.serialize_starts(trigger_id):
            limits = row["limits"]
            recorded = self._store.get_run_status(tenant, str(run_id)) is not None  # a retry of a run already counted
            if not recorded and self._store.count_active_runs(tenant, trigger_id) >= int(limits.get("max_concurrent_runs", 5)):
                reject("rejected_concurrency", "max_concurrent_runs")
                raise TooManyRequests("too many runs of this trigger are active", retry_after=CONCURRENCY_RETRY_SECONDS, code="concurrency_limit")
            since = datetime.now(timezone.utc) - timedelta(hours=24)
            if not recorded and self._store.count_runs_since(tenant, trigger_id, since) >= int(limits.get("max_runs_per_day", 2000)):
                reject("rejected_rate", "max_runs_per_day")
                raise TooManyRequests("the daily run limit of this trigger is reached", retry_after=CONCURRENCY_RETRY_SECONDS * 10, code="daily_limit")
            try:
                self._runs.start(
                    tenant, uuid.UUID(row["workflow_id"]), run_input, version=row["workflow_version"], run_id=run_id, trigger_id=trigger_id,
                )
            except DefinitionError as exc:
                if exc.status_code == 503:  # run recorded, hand-off failed: the sender's retry re-drives it
                    reject("unavailable", "enqueue_failed")
                    raise Unavailable("the run could not be started; retry", code="delivery_unavailable") from None
                if exc.status_code == 422:
                    reject("rejected_schema", "input_invalid")
                    raise InvalidInput("the mapped input does not fit the workflow's input schema", code="input_invalid", details=_loc_only(exc)) from None
                reject("workflow_unavailable", "workflow_unavailable")  # 404 or 409: archived, version gone, no published version
                raise NotFound(NOT_FOUND_DETAIL, code="not_found") from None
        self._store.finish_delivery(tenant, delivery_id, "accepted", run_id=str(run_id))
        self._store.note_delivery(tenant, trigger_id, "accepted")
        self._record_run(row, str(run_id))
        return {"status": "accepted", "delivery": delivery_id}


def _refuse_constant(_value: str) -> Any:
    raise ValueError("NaN and Infinity are not JSON")


def _loc_only(exc: DefinitionError) -> list[dict[str, Any]]:
    """The locations of a 422 from `start` (`{loc, msg, type}` items), never the rejected values."""
    detail = exc.detail
    return [{"loc": e.get("loc"), "type": e.get("type")} for e in detail[:20] if isinstance(e, dict)] if isinstance(detail, list) else []
