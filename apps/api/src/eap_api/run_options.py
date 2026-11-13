"""Run options on agent runs and conversation turns (milestone P0, task P0.8).

A run start (`POST /agents/{id}/runs`, `POST /conversations/{id}/runs`) may carry:

- `version`: a published version number, null for the latest one, or `"draft"`: a draft test run.
  The agent's current draft is validated exactly as a publish would (`_check_publishable`) and its
  snapshot is stored on the run (`runs.draft_spec`, `agent_version` NULL). The hand-off passes it as
  `spec_json` with `version=0`; a retry relaunches from the snapshot, never from the live draft.
- `request_context`: values for the target spec's `context_fields`, validated with
  `eap_schemas.request_context.validate_request_context` (unknown keys, reserved identity names,
  wrong types and more than 8 KiB are 422) and stored on `runs.request_context`.
- `model`: an override of the spec's model, checked against the platform allowlist and the provider
  rules publish applies (Anthropic refuses a temperature). The effective model is always recorded
  on `runs.model_id`, with `runs.model_overridden` when it differs from the spec's model.

The worker reads all of it back from the row in `eap.start_run`; the hand-off arguments are
unchanged.

Context errors are a 422 whose `detail` is a list of `{"loc", "msg", "type"}` with `loc` starting
`["body", "request_context", ...]` (never echoing a value), the same shape the guarded routes give
body validation errors. `POST /agents/{id}/request-context/validate` answers with the same shape.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, status
from pydantic import ValidationError
from sqlalchemy import func
from sqlalchemy.orm import Session

from eap_api.models import AgentModel, AgentVersionModel
from eap_api.routes import _check_publishable
from eap_schemas import AgentSpec, ModelRef
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
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Agent has no published version")
        return latest
    if version_row(db, agent_id, requested) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Version not found")
    return requested


@dataclass(frozen=True)
class RunTarget:
    """What a run executes: a published version (`version` set) or a draft snapshot (`draft_spec`)."""

    version: int | None
    spec: AgentSpec
    spec_dict: dict[str, Any]
    draft_spec: dict[str, Any] | None

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
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Agent has no draft")
    try:
        spec = AgentSpec.model_validate(agent.draft_spec)
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {"loc": (), "msg": "invalid"}
        where = ".".join(str(p) for p in first.get("loc", ())) or "spec"
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"The draft is not a valid agent spec ({where}: {first.get('msg', 'invalid')}); fix it in the editor",
        ) from None
    _check_publishable(db, tenant_id, spec)
    snapshot = spec.model_dump(mode="json")
    return RunTarget(version=None, spec=spec, spec_dict=snapshot, draft_spec=snapshot)


def resolve_target(db: Session, tenant_id: str, agent: AgentModel, requested: int | str | None) -> RunTarget:
    """The target of a new run: the draft (validated like a publish) or a published version
    (latest when `requested` is None; 409 when there is none, 404 for an unknown number)."""
    if requested == DRAFT:
        return _draft_target(db, tenant_id, agent)
    assert requested is None or isinstance(requested, int)
    return _published_target(db, agent.id, resolve_version(db, agent.id, requested))


def stored_target(db: Session, run: dict[str, Any]) -> RunTarget:
    """The target an existing run was recorded with (its draft snapshot or its version)."""
    if run.get("draft_spec") is not None:
        snapshot = run["draft_spec"]
        return RunTarget(
            version=None, spec=AgentSpec.model_validate(snapshot), spec_dict=snapshot, draft_spec=snapshot
        )
    return _published_target(db, run["agent_id"], run["agent_version"])


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
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=context_error_detail(exc.errors)
        ) from None


def effective_model(spec: AgentSpec, override: ModelRef | None) -> tuple[str, bool]:
    """(model id `provider:name`, overridden): the override when given, else the spec's model.
    `overridden` is True only when the override differs from the spec's model."""
    model = override or spec.model
    overridden = override is not None and (override.provider, override.name) != (spec.model.provider, spec.model.name)
    return f"{model.provider}:{model.name}", overridden


def check_model_override(spec: AgentSpec, override: ModelRef | None) -> None:
    """422 unless the override passes the allowlist and the provider rules publish applies."""
    if override is None:
        return
    if not ModelPolicy.from_env().allows(override.provider, override.name):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(ModelNotAllowed(override.provider, override.name)),
        )
    if override.provider == "anthropic" and spec.model_settings.temperature is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"model '{override.provider}:{override.name}' does not accept a temperature setting; "
                "remove temperature from the agent's model settings or choose another model"
            ),
        )


def check_spec_payload_size(spec_dict: dict[str, Any], input_text: str) -> None:
    """413 when the hand-off payload (spec JSON plus input) exceeds EAP_MAX_RUN_PAYLOAD_BYTES."""
    payload_bytes = len(json.dumps(spec_dict).encode("utf-8")) + len(input_text.encode("utf-8"))
    max_payload_bytes = int(os.environ.get("EAP_MAX_RUN_PAYLOAD_BYTES", str(256 * 1024)))
    if payload_bytes > max_payload_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"Run payload exceeds the {max_payload_bytes}-byte enqueue limit",
        )


@dataclass(frozen=True)
class RunOptions:
    """A new run's validated options; `store_kwargs()` are the run-store insert arguments."""

    target: RunTarget
    request_context: dict[str, Any]
    model_id: str
    model_overridden: bool

    def store_kwargs(self) -> dict[str, Any]:
        return {
            "request_context": self.request_context,
            "draft_spec": self.target.draft_spec,
            "model_id": self.model_id,
            "model_overridden": self.model_overridden,
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
) -> RunOptions:
    """Validate a new run's options, in this order: target (404/409, 422 for an invalid draft),
    request context (422 list), model override (422), payload size (413)."""
    target = resolve_target(db, tenant_id, agent, version)
    context = validate_context(target.spec, request_context)
    check_model_override(target.spec, model)
    check_spec_payload_size(target.spec_dict, input_text)
    model_id, overridden = effective_model(target.spec, model)
    return RunOptions(target=target, request_context=context, model_id=model_id, model_overridden=overridden)


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
) -> bool:
    """Whether a retry (same run_id) asks for the run as recorded: same kind of target (a draft run
    only for `"draft"`, the same version number when one is given), the same validated context and
    the same effective model. A draft run is compared with its own snapshot, not the live draft, so
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
    if run.get("model_id") is None:  # recorded before run options existed: only "no override" matches
        return model is None
    return (model_id, overridden) == (run["model_id"], bool(run.get("model_overridden")))


def context_spec(db: Session, agent: AgentModel, requested: int | str | None) -> tuple[int | None, AgentSpec]:
    """(version, spec) whose context fields a context is checked against outside a run start: a
    published version (latest when None) or, for `"draft"`, the draft as a parsed spec (version
    None). Only the spec's shape is needed here, so the draft is not held to the publish policy."""
    if requested == DRAFT:
        if agent.draft_spec is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Agent has no draft")
        try:
            return None, AgentSpec.model_validate(agent.draft_spec)
        except ValidationError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="The draft is not a valid agent spec; fix it in the editor",
            ) from None
    assert requested is None or isinstance(requested, int)
    version = resolve_version(db, agent.id, requested)
    return version, _published_target(db, agent.id, version).spec
