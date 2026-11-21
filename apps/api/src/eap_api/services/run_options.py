"""What a run executes, resolved independently of the transport (framework-free core of eap_api.run_options).

`resolve_target` and friends raise `ServiceError` subclasses; eap_api.run_options keeps the old function
names as wrappers that answer the same HTTP refusals.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import func
from sqlalchemy.orm import Session

from eap_api.models import AgentModel, AgentVersionModel
from eap_api.services.errors import Conflict, InvalidInput, NotFound, PayloadTooLarge
from eap_run_store.agent_checks import check_publishable, validate_stored_draft
from eap_schemas import AgentSpec, ModelRef, ModelSettings
from eap_schemas.model_settings_support import merge_model_settings, unsupported_settings
from eap_schemas.models_policy import ModelNotAllowed, ModelPolicy
from eap_schemas.request_context import RequestContextError, validate_request_context

DRAFT = "draft"
# The hand-off `version` argument of a draft test run (published versions start at 1).
DRAFT_HANDOFF_VERSION = 0


def version_row(db: Session, agent_id: uuid.UUID | str, version: int) -> AgentVersionModel | None:
    return (
        db.query(AgentVersionModel)
        .filter(AgentVersionModel.agent_id == str(agent_id), AgentVersionModel.version == version)
        .first()
    )


def resolve_version(db: Session, agent_id: uuid.UUID | str, requested: int | None) -> int:
    """The requested published version, or the latest one when `requested` is None (409 if none, 404 if unknown)."""
    if requested is None:
        latest = db.query(func.max(AgentVersionModel.version)).filter(AgentVersionModel.agent_id == str(agent_id)).scalar()
        if latest is None:
            raise Conflict("Agent has no published version")
        return latest
    if version_row(db, agent_id, requested) is None:
        raise NotFound("Version not found")
    return requested


@dataclass(frozen=True)
class RunTarget:
    """What a run executes: a published version (`version` set) or a draft snapshot (`draft_spec`).
    `kind` is "agent" (`spec` an AgentSpec) or "workflow" (`spec` a WorkflowSpec, never a draft,
    `workflow_version_id` the row the worker starts)."""

    version: int | None
    spec: Any
    spec_dict: dict[str, Any]
    draft_spec: dict[str, Any] | None
    kind: Literal["agent", "workflow"] = "agent"
    workflow_version_id: str | None = None

    @property
    def is_draft(self) -> bool:
        return self.draft_spec is not None

    @property
    def handoff_version(self) -> int:
        return DRAFT_HANDOFF_VERSION if self.version is None else self.version


def _published_target(db: Session, agent_id: uuid.UUID | str, version: int) -> RunTarget:
    row = version_row(db, agent_id, version)
    assert row is not None  # versions are immutable and never deleted
    return RunTarget(version=version, spec=AgentSpec.model_validate(row.spec), spec_dict=row.spec, draft_spec=None)


def _draft_target(db: Session, tenant_id: str, agent: AgentModel) -> RunTarget:
    """The agent's current draft, validated exactly as a publish would, as a snapshot."""
    if agent.draft_spec is None:
        raise Conflict("Agent has no draft")
    spec = validate_stored_draft(agent.draft_spec)
    # Like a publish, except that a deleted knowledge base is the retrieve tool's error at run time
    # (as for a published version), not a 422 here.
    check_publishable(db.connection(), tenant_id, spec, draft_run=True)
    snapshot = spec.model_dump(mode="json")
    return RunTarget(version=None, spec=spec, spec_dict=snapshot, draft_spec=snapshot)


def resolve_target(db: Session, tenant_id: str, agent: AgentModel, requested: int | str | None) -> RunTarget:
    """The target of a new run: the draft (validated like a publish) or a published version
    (latest when `requested` is None; 409 when there is none, 404 for an unknown number)."""
    if requested == DRAFT:
        return _draft_target(db, tenant_id, agent)
    assert requested is None or isinstance(requested, int)
    return _published_target(db, agent.id, resolve_version(db, agent.id, requested))


def resolve_workflow_target(db: Session, tenant_id: str, workflow_id: uuid.UUID | str, requested: int | None) -> RunTarget:
    """The published workflow version an eval run executes (latest when `requested` is None; 409
    when the workflow has none, 404 for an unknown number)."""
    from eap_api.workflows.models import WorkflowVersionModel
    from eap_schemas.workflows import WorkflowSpec

    query = db.query(WorkflowVersionModel).filter(
        WorkflowVersionModel.workflow_id == str(workflow_id), WorkflowVersionModel.tenant_id == tenant_id
    )
    if requested is None:
        row = query.order_by(WorkflowVersionModel.version.desc()).first()
        if row is None:
            raise Conflict("Workflow has no published version")
    else:
        row = query.filter(WorkflowVersionModel.version == requested).first()
        if row is None:
            raise NotFound("Version not found")
    return RunTarget(
        version=row.version,
        spec=WorkflowSpec.model_validate(row.spec),
        spec_dict=row.spec,
        draft_spec=None,
        kind="workflow",
        workflow_version_id=str(row.id),
    )


def stored_target(db: Session, run: dict[str, Any]) -> RunTarget:
    """The target an existing run was recorded with (its draft snapshot or its version)."""
    if run.get("draft_spec") is not None:
        snapshot = run["draft_spec"]
        return RunTarget(
            version=None, spec=AgentSpec.model_validate(snapshot), spec_dict=snapshot, draft_spec=snapshot
        )
    return _published_target(db, run["agent_id"], run["agent_version"])




def check_spec_payload_size(spec_dict: dict[str, Any], input_text: str) -> None:
    """413 when the hand-off payload (spec JSON plus input) exceeds EAP_MAX_RUN_PAYLOAD_BYTES."""
    payload_bytes = len(json.dumps(spec_dict).encode("utf-8")) + len(input_text.encode("utf-8"))
    max_payload_bytes = int(os.environ.get("EAP_MAX_RUN_PAYLOAD_BYTES", str(256 * 1024)))
    if payload_bytes > max_payload_bytes:
        raise PayloadTooLarge(f"Run payload exceeds the {max_payload_bytes}-byte enqueue limit")


def context_error_detail(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Context errors as compact 422 items located under `body.request_context`."""
    return [
        {"loc": ["body", "request_context", *err.get("loc", [])], "msg": err["msg"], "type": err["type"]}
        for err in errors
    ]


def validate_context(spec: AgentSpec, value: dict[str, Any] | None) -> dict[str, Any]:
    """The accepted context (declared fields only) or a 422 with the field errors."""
    try:
        return validate_request_context(spec.context_fields, value)
    except RequestContextError as exc:
        detail = context_error_detail(exc.errors)
        raise InvalidInput("request_context is invalid", detail_object=detail) from None


def settings_json(settings: ModelSettings | None) -> dict[str, Any] | None:
    """The per-run settings as stored: only the fields that are set, None when none is."""
    values = settings.set_values() if settings is not None else {}
    return values or None


def effective_model(spec: AgentSpec, override: ModelRef | None) -> tuple[str, bool]:
    """(model id `provider:name`, overridden): the override when given, else the spec's model.
    `overridden` is True only when the override differs from the spec's model."""
    model = override or spec.model
    overridden = override is not None and (override.provider, override.name) != (spec.model.provider, spec.model.name)
    return f"{model.provider}:{model.name}", overridden


def _refuse_setting(model: ModelRef, name: str, where: str) -> InvalidInput:
    return InvalidInput(
        f"model '{model.provider}:{model.name}' does not accept a {name} setting; "
        f"remove {name} from {where} or choose another model",
        detail_object=(
            f"model '{model.provider}:{model.name}' does not accept a {name} setting; "
            f"remove {name} from {where} or choose another model"
        ),
    )


def check_model_override(
    spec: AgentSpec, override: ModelRef | None, settings_override: ModelSettings | None = None
) -> None:
    """422 unless the override passes the allowlist and the provider rules publish applies, and
    unless the EFFECTIVE model settings (the spec's with the per-run overrides on top) are accepted
    by the effective model. Without a model override the spec's model and its fallbacks (which
    answer with the same settings) are checked for the settings the run adds; the spec's own were
    checked at publish."""
    if override is not None and not ModelPolicy.from_env().allows(override.provider, override.name):
        text = str(ModelNotAllowed(override.provider, override.name))
        raise InvalidInput(text, detail_object=text)
    effective = merge_model_settings(spec.model_settings, settings_override)
    if override is not None:
        for name in unsupported_settings(override.provider, effective):
            raise _refuse_setting(override, name, "the agent's model settings")
        return
    if settings_override is None:
        return
    for model in (spec.model, *spec.fallback_models):
        for name in unsupported_settings(model.provider, settings_override):
            raise _refuse_setting(model, name, "the run's model settings")


@dataclass(frozen=True)
class RunOptions:
    """A new run's validated options; `store_kwargs()` are the run-store insert arguments."""

    target: RunTarget
    request_context: dict[str, Any]
    model_id: str
    model_overridden: bool
    model_settings: dict[str, Any] | None = None

    def store_kwargs(self) -> dict[str, Any]:
        return {
            "request_context": self.request_context,
            "draft_spec": self.target.draft_spec,
            "model_id": self.model_id,
            "model_overridden": self.model_overridden,
            "model_settings": self.model_settings,
        }


def resolve_run_options(
    db: Session,
    tenant_id: str,
    agent: AgentModel,
    *,
    version: int | str | None,
    request_context: dict[str, Any] | None,
    model: ModelRef | None,
    input_text: str,
    model_settings: ModelSettings | None = None,
) -> RunOptions:
    """Validate a new run's options, in this order: target (404/409, 422 for an invalid draft),
    request context (422 list), model override and settings (422), payload size (413)."""
    target = resolve_target(db, tenant_id, agent, version)
    context = validate_context(target.spec, request_context)
    check_model_override(target.spec, model, model_settings)
    check_spec_payload_size(target.spec_dict, input_text)
    model_id, overridden = effective_model(target.spec, model)
    return RunOptions(
        target=target,
        request_context=context,
        model_id=model_id,
        model_overridden=overridden,
        model_settings=settings_json(model_settings),
    )


def _comparable(field_type: str, value: Any) -> Any:
    """A context value in a form that survives a JSONB round trip: Postgres prints a large number
    without an exponent (1e300 comes back as the integer 10**300), so number fields compare as
    float and integer fields as int."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    if field_type == "number":
        return float(value)
    if field_type == "integer":
        return int(value)
    return value


def _same_context(spec: AgentSpec, requested: dict[str, Any], stored: dict[str, Any]) -> bool:
    """Whether a retry's validated context equals the stored one, field by field (see _comparable)."""
    if set(requested) != set(stored):
        return False
    types = {f.name: f.type for f in spec.context_fields}
    return all(
        _comparable(types.get(name, ""), requested[name]) == _comparable(types.get(name, ""), stored[name])
        for name in requested
    )


def same_options(
    db: Session,
    run: dict[str, Any],
    *,
    version: int | str | None,
    request_context: dict[str, Any] | None,
    model: ModelRef | None,
    model_settings: ModelSettings | None = None,
) -> bool:
    """Whether a retry (same run_id) asks for the run as recorded: same kind of target (a draft run
    only for `"draft"`, the same version number when one is given), the same validated context and
    the same effective model and per-run model settings. A draft run is compared with its own snapshot, not the live draft, so
    an edit of the draft after the first attempt does not turn a retry into a different run."""
    if run.get("agent_id") is None:
        return False
    target = stored_target(db, run)
    if version == DRAFT:
        if not target.is_draft:
            return False
    elif target.is_draft:
        return False
    elif version is not None and run["agent_version"] != version:
        return False
    try:
        context = validate_request_context(target.spec.context_fields, request_context)
    except RequestContextError:
        return False
    if not _same_context(target.spec, context, run.get("request_context") or {}):
        return False
    model_id, overridden = effective_model(target.spec, model)
    if settings_json(model_settings) != (run.get("model_settings") or None):
        return False
    if run.get("model_id") is None:  # recorded before run options existed: only "no override" matches
        return model is None
    return (model_id, overridden) == (run["model_id"], bool(run.get("model_overridden")))
