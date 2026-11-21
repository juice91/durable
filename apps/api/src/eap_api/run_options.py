"""Run options on agent runs and conversation turns (milestone P0, task P0.8).

A run start (`POST /agents/{id}/runs`, `POST /conversations/{id}/runs`) may carry:

- `version`: a published version number, null for the latest one, or `"draft"`: a draft test run.
  The agent's current draft is validated as a publish would (`_check_publishable`), except that a
  deleted knowledge base is not a 422 (the run's retrieve tool answers "no longer exist", as for a
  published version), and its snapshot is stored on the run (`runs.draft_spec`, `agent_version` NULL). The hand-off passes it as
  `spec_json` with `version=0`; a retry relaunches from the snapshot, never from the live draft.
- `request_context`: values for the target spec's `context_fields`, validated with
  `eap_schemas.request_context.validate_request_context` (unknown keys, reserved identity names,
  wrong types and more than 8 KiB are 422) and stored on `runs.request_context`.
- `model`: an override of the spec's model, checked against the platform allowlist and the provider
  rules publish applies (Anthropic refuses a temperature). The effective model is always recorded
  on `runs.model_id`, with `runs.model_overridden` when it differs from the spec's model.
- `model_settings`: per-run overrides of the spec's model settings (temperature, top_p, top_k,
  penalties, seed, max_tokens, max_steps, max_retries; a set field replaces the spec's value, an
  unset one keeps it). The same range checks and provider rules as publish apply to the EFFECTIVE
  settings on the effective model (422); the overrides are stored on `runs.model_settings` (NULL
  when none), and the worker merges them over the spec's.

The worker reads all of it back from the row in `eap.start_run`; the hand-off arguments are
unchanged.

Context errors are a 422 whose `detail` is a list of `{"loc", "msg", "type"}` with `loc` starting
`["body", "request_context", ...]` (never echoing a value), the same shape the guarded routes give
body validation errors. `POST /agents/{id}/request-context/validate` answers with the same shape.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import HTTPException, status
from pydantic import ValidationError
from sqlalchemy.orm import Session

from eap_api.models import AgentModel
from eap_api.service_errors import to_http_exception
from eap_api.services import run_options as core
from eap_api.services.errors import ServiceError
from eap_api.services.run_options import (  # noqa: F401 - re-exported: callers import them from here
    DRAFT,
    DRAFT_HANDOFF_VERSION,
    RunOptions,
    RunTarget,
    version_row,
)
from eap_schemas import AgentSpec, ModelRef, ModelSettings
from eap_schemas.request_context import RequestContextError, validate_request_context

def _http(call: Any, *args: Any, **kwargs: Any) -> Any:
    """Run a framework-free core function; its refusal becomes the HTTPException the routes always raised."""
    try:
        return call(*args, **kwargs)
    except ServiceError as exc:
        raise to_http_exception(exc) from None


def resolve_version(db: Session, agent_id: uuid.UUID | str, requested: int | None) -> int:
    """The requested published version, or the latest one when `requested` is None (409 if none, 404 if unknown)."""
    return _http(core.resolve_version, db, agent_id, requested)


def resolve_target(db: Session, tenant_id: str, agent: AgentModel, requested: int | str | None) -> RunTarget:
    """The target of a new run: the draft (validated like a publish) or a published version
    (latest when `requested` is None; 409 when there is none, 404 for an unknown number)."""
    return _http(core.resolve_target, db, tenant_id, agent, requested)


def resolve_workflow_target(db: Session, tenant_id: str, workflow_id: uuid.UUID | str, requested: int | None) -> RunTarget:
    """The published workflow version an eval run executes (latest when `requested` is None; 409
    when the workflow has none, 404 for an unknown number)."""
    return _http(core.resolve_workflow_target, db, tenant_id, workflow_id, requested)


def _published_target(db: Session, agent_id: uuid.UUID | str, version: int) -> RunTarget:
    return core._published_target(db, agent_id, version)


def stored_target(db: Session, run: dict[str, Any]) -> RunTarget:
    """The target an existing run was recorded with (its draft snapshot or its version)."""
    return core.stored_target(db, run)


def check_spec_payload_size(spec_dict: dict[str, Any], input_text: str) -> None:
    """413 when the hand-off payload (spec JSON plus input) exceeds EAP_MAX_RUN_PAYLOAD_BYTES."""
    _http(core.check_spec_payload_size, spec_dict, input_text)



def context_error_detail(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Context errors as compact 422 items located under `body.request_context`."""
    return core.context_error_detail(errors)


def validate_context(spec: AgentSpec, value: dict[str, Any] | None) -> dict[str, Any]:
    """The accepted context (declared fields only) or a 422 with the field errors."""
    return _http(core.validate_context, spec, value)


def settings_json(settings: ModelSettings | None) -> dict[str, Any] | None:
    """The per-run settings as stored: only the fields that are set, None when none is."""
    return core.settings_json(settings)


def effective_model(spec: AgentSpec, override: ModelRef | None) -> tuple[str, bool]:
    """(model id `provider:name`, overridden): see the core."""
    return core.effective_model(spec, override)


def check_model_override(
    spec: AgentSpec, override: ModelRef | None, settings_override: ModelSettings | None = None
) -> None:
    """422 unless the override passes the allowlist and provider rules (see the core)."""
    _http(core.check_model_override, spec, override, settings_override)


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
    """Validate a new run's options (see the core): target, request context, model override, payload size."""
    return _http(
        core.resolve_run_options, db, tenant_id, agent, version=version, request_context=request_context,
        model=model, input_text=input_text, model_settings=model_settings,
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
    """Whether a retry (same run_id) asks for the run as recorded (see the core)."""
    return core.same_options(
        db, run, version=version, request_context=request_context, model=model, model_settings=model_settings
    )


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
