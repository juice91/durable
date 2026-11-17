"""Agent publish-time policy and warnings, shared by the API's publish, restore and draft-run routes
and by the Architect's gated tools (one implementation; see eap_run_store.definitions).

Moved verbatim from eap_api.routes: the checks raise `DefinitionError` subclasses, which the API
renders as the same status codes and bodies it always did (a handler in eap_api.app), and which a
tool turns into a model-visible error. They take a SQLAlchemy `Connection` (a Session's
`connection()` or an engine's), never a Session.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any

from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.engine import Connection

from eap_pricing import (
    OVERRIDE_ONLY_PROVIDERS,
    PRICE_UNPRICED,
    PRICE_ZERO,
    InvalidPriceFile,
    load_overrides,
    price_known,
)
from eap_processors import processor_errors
from eap_run_store.definition_errors import refuse
from eap_run_store.knowledge import KnowledgeStore
from eap_schemas import AgentSpec, canonical_spec_hash, reserved_tool_name_conflicts
from eap_schemas.embedding_policy import EmbeddingModelNotAllowed, EmbeddingModelPolicy
from eap_schemas.model_settings_support import unsupported_settings
from eap_schemas.models_policy import ModelPolicy
from eap_schemas.processors import processor_models
from eap_schemas.request_context import placeholder_errors
from eap_schemas.tools import HIDDEN_TOOL_DECLARATIONS

log = logging.getLogger("eap_api")


def _check_mcp_servers(conn: Connection, tenant_id: str, spec: AgentSpec) -> None:
    """Every MCP tool must name an enabled MCP server of this tenant (422 naming the first that
    does not). Another tenant's server is reported exactly like an unknown one. Whether the tool
    exists on the server is checked by the worker at run time, since the server can change."""
    names = list(dict.fromkeys(ref.server for ref in spec.mcp_tools))
    if not names:
        return
    enabled = {
        r.name: r.enabled
        for r in conn.execute(
            text("SELECT name, enabled FROM mcp_servers WHERE tenant_id = :t AND name = ANY(:names)"),
            {"t": tenant_id, "names": names},
        )
    }
    for name in names:
        if name not in enabled:
            raise refuse(status_code=422, detail=f"MCP server '{name}' is not registered in this tenant")
        if not enabled[name]:
            raise refuse(status_code=422, detail=f"MCP server '{name}' is disabled")


def check_publishable(conn: Connection, tenant_id: str, spec: AgentSpec, *, draft_run: bool = False) -> None:
    """Publish-time policy, applied by publish and by restore (a restore is re-validated under
    today's policy, not the one in force when the source version was published), and by a draft
    test run (`draft_run`, run_options._draft_target). 422 on the first failure.

    A draft run skips only the knowledge base existence check: a draft whose knowledge bases were
    deleted (DELETE /knowledge-bases/{id} lists draft references and needs force=true) runs like a
    published version that attaches one, and its retrieve tool answers "The attached knowledge bases
    no longer exist." The worker resolves knowledge bases within the run's tenant only, so an id of
    another tenant still finds nothing."""
    _check_memory_features(spec)

    policy = ModelPolicy.from_env()
    # Model governance: only allowlisted models (EAP_ALLOWED_MODELS) can be published. The worker
    # re-checks before every model call, since the policy can change after publish.
    if not policy.allows(spec.model.provider, spec.model.name):
        raise refuse(
            status_code=422,
            detail=(
                f"model '{spec.model.provider}:{spec.model.name}' is not allowed on this platform "
                "(not in the EAP_ALLOWED_MODELS allowlist)"
            ),
        )

    # Current Anthropic models (Claude Opus 5.5 and later) reject sampling parameters with a 400, and
    # a provider can lack a setting altogether (Anthropic: penalties and seed; OpenAI: top_k), so a
    # spec that sets one would fail on every run: refuse it at publish instead.
    for name in unsupported_settings(spec.model.provider, spec.model_settings):
        raise refuse(
            status_code=422,
            detail=(
                f"model '{spec.model.provider}:{spec.model.name}' does not accept a {name} "
                f"setting; remove {name} from the agent's model settings"
            ),
        )

    _check_fallback_models(policy, spec)

    # Processors that call a model (moderation) are governed by the same allowlist.
    for ref in processor_models(spec.input_processors + spec.output_processors):
        if not policy.allows(ref.provider, ref.name):
            raise refuse(
                status_code=422,
                detail=(
                    f"processor model '{ref.provider}:{ref.name}' is not allowed on this platform "
                    "(not in the EAP_ALLOWED_MODELS allowlist)"
                ),
            )

    # Memory's model calls (the observational memory observer, RM.13) are governed by the same chat
    # allowlist; the worker re-checks it at call time and records a memory_observe_failed event.
    if spec.memory is not None:
        for ref in spec.memory.chat_models():
            if not policy.allows(ref.provider, ref.name):
                raise refuse(
                    status_code=422,
                    detail=(
                        f"observer model '{ref.provider}:{ref.name}' is not allowed on this platform "
                        "(not in the EAP_ALLOWED_MODELS allowlist)"
                    ),
                )

    # Regex blocklist patterns must compile with RE2 (no backreferences or lookarounds, bounded
    # size, not matching empty text); eap_schemas only bounds their count and length.
    processor_problems = processor_errors(
        spec.input_processors, spec.step_processors, spec.stream_processors, spec.output_processors
    )
    if processor_problems:
        raise refuse(status_code=422, detail=processor_problems[0])

    # {{context.<name>}} placeholders must be well formed and name a declared context field.
    errors = placeholder_errors(spec.instructions, spec.context_fields)
    if errors:
        raise refuse(status_code=422, detail=f"instructions: {errors[0]}")

    _check_mcp_servers(conn, tenant_id, spec)
    _check_platform_tools(spec)
    if not draft_run:
        _check_knowledge_bases(conn, tenant_id, spec)


def _check_fallback_models(policy: ModelPolicy, spec: AgentSpec) -> None:
    """Every fallback model passes the chat allowlist (EAP_ALLOWED_MODELS), and the Anthropic
    temperature rule covers the whole chain: a fallback answers with the spec's model settings.
    AgentSpec already checks that fallbacks are distinct and none is the primary model. The worker
    re-checks the allowlist at run start and in the resolver. 422 naming `fallback_models[i]`."""
    for i, ref in enumerate(spec.fallback_models):
        if not policy.allows(ref.provider, ref.name):
            raise refuse(
                status_code=422,
                detail=(
                    f"fallback_models[{i}]: model '{ref.provider}:{ref.name}' is not allowed on this platform "
                    "(not in the EAP_ALLOWED_MODELS allowlist)"
                ),
            )
        for name in unsupported_settings(ref.provider, spec.model_settings):
            raise refuse(
                status_code=422,
                detail=(
                    f"fallback_models[{i}]: model '{ref.provider}:{ref.name}' does not accept a {name} "
                    f"setting; remove model_settings.{name} or this fallback model"
                ),
            )


# Publish warnings (agent capabilities milestone, AC.7): advice in the publish and restore
# responses, never a refusal. Each is {code, path, message}.
WARNING_COST_CAP_UNPRICED = "cost_cap_unpriced_model"
WARNING_REDACTION_FIXED_VALUES = "redaction_may_break_output_schema"


# _price_status: what the worker's price lookup (eap_pricing) says about one model.
PRICE_UNUSABLE = "unusable"  # the override file is unreadable, invalid, or named by the environment but missing


def _price_status(model_id: str, today: date) -> str:
    """How the worker would price a request of `model_id` today: PRICE_UNUSABLE when the override
    file (EAP_MODEL_PRICES_FILE) cannot be used, so the worker prices nothing at all; otherwise
    eap_pricing.price_known's "priced", "zero" or "unpriced". The same lookup as the worker's
    price() (eap_pricing.price_request: override file, then genai-prices for direct providers), so
    the publish warning and the recorded cost can never disagree."""
    overrides = load_overrides()
    if isinstance(overrides, InvalidPriceFile):
        return PRICE_UNUSABLE
    return price_known(model_id, today, overrides)


def cost_cap_zero_reason(provider: str, name: str, day: date) -> str | None:
    """Why requests of `provider:name` count as $0 toward a cost cap on `day` (no usable price
    file, a $0 price, or no price at all), None when the worker prices them above $0. The shared
    reason of the agent and workflow publish warnings `cost_cap_unpriced_model`."""
    model_id = f"{provider}:{name}"
    status_ = _price_status(model_id, day)
    if status_ == PRICE_UNUSABLE:
        return (
            "the model price override file is unreadable, invalid or missing, so no model has a price; "
            "its requests count as $0"
        )
    if status_ == PRICE_ZERO:
        return f"'{model_id}' is priced at $0, so its requests count as $0"
    if status_ == PRICE_UNPRICED:
        where = (
            "the model price override file"
            if provider in OVERRIDE_ONLY_PROVIDERS
            else "the model price override file or genai-prices"
        )
        return f"'{model_id}' has no price in {where}; its requests count as $0"
    return None


def cost_cap_warnings(spec: AgentSpec, today: date | None = None) -> list[dict[str, str]]:
    """With `limits.max_cost_usd`, one warning per model of the chain (primary, then fallbacks)
    whose requests will count as $0 toward the cap: recorded without a price, or priced at $0 (the
    repository's default price file prices `test:*` and `litellm:*` at 0).

    The lookup is the worker's (`_price_status`): a model with neither an override entry nor a
    genai-prices price (genai-prices is never consulted for `test` and `litellm`) is unpriced, a
    genai-priced model is not warned about, and an unusable override file prices nothing, so then
    every model of the chain is warned about."""
    if spec.limits is None or spec.limits.max_cost_usd is None:
        return []
    day = today or datetime.now(timezone.utc).date()
    chain = [("model", spec.model)] + [(f"fallback_models[{i}]", ref) for i, ref in enumerate(spec.fallback_models)]
    warnings: list[dict[str, str]] = []
    for path, ref in chain:
        reason = cost_cap_zero_reason(ref.provider, ref.name, day)
        if reason is None:
            continue
        warnings.append({"code": WARNING_COST_CAP_UNPRICED, "path": path, "message": f"{path}: {reason} toward limits.max_cost_usd, so the cost limit may not stop the run"})
    return warnings


def _fixed_string_paths(schema: Any, path: str = "") -> list[str]:
    """JSON paths (into the output object, `items[]` for array items) of every schema node that
    fixes a string value through `enum` or `const`."""
    if not isinstance(schema, dict):
        return []
    found: list[str] = []
    values = list(schema.get("enum") or []) + ([schema["const"]] if "const" in schema else [])
    if any(isinstance(v, str) for v in values):
        found.append(path or "$")
    for name, sub in (schema.get("properties") or {}).items():
        found += _fixed_string_paths(sub, f"{path}.{name}" if path else name)
    if isinstance(schema.get("items"), dict):
        found += _fixed_string_paths(schema["items"], f"{path}[]" if path else "[]")
    return found


def _redaction_warnings(spec: AgentSpec) -> list[dict[str, str]]:
    """With structured output, an output processor that redacts (`pii_redact` or `regex_blocklist`
    with action `redact`) rewrites string fields; a redacted `enum` or `const` string no longer
    validates, and the run then ends `output_blocked` (schema_invalid_after_processing)."""
    if spec.output is None:
        return []
    fields = _fixed_string_paths(spec.output.json_schema)
    if not fields:
        return []
    warnings: list[dict[str, str]] = []
    for i, processor in enumerate(spec.output_processors):
        if processor.type not in ("pii_redact", "regex_blocklist") or processor.config.action != "redact":
            continue
        path = f"output_processors[{i}]"
        warnings.append({"code": WARNING_REDACTION_FIXED_VALUES, "path": path, "message": (
                f"{path}: {processor.type} redacts output text, and the output schema fixes string values "
                f"({', '.join(fields[:5])}); a redacted value fails the schema and the run ends output_blocked"
            )})
    return warnings


def publish_warnings(spec: AgentSpec) -> list[dict[str, str]]:
    """Publish-time advice for a spec that passed _check_publishable (publish and restore)."""
    return cost_cap_warnings(spec) + _redaction_warnings(spec)



def _check_memory_features(spec: AgentSpec) -> None:
    """Memory's embedding models (semantic recall, RM.14) must pass the embedding allowlist
    (EAP_ALLOWED_EMBEDDING_MODELS) with known dimensions: 422 naming the first that does not, 503
    when the allowlist itself is misconfigured. The worker re-checks before every embedding call
    and records a failed `embedding` event instead of failing the turn."""
    memory = spec.memory
    if memory is None:
        return
    models = memory.embedding_models()
    if not models:
        return
    try:
        policy = EmbeddingModelPolicy.from_env()
    except ValueError as exc:  # checked at startup; the environment changed under the process
        # Fixed text and the exception type only (no environment value), as eap_observability.log_failure logs.
        log.error("embedding model allowlist invalid code=embedding_policy_invalid error_type=%s", type(exc).__name__)
        raise refuse(
            status_code=503,
            detail="the embedding model allowlist (EAP_ALLOWED_EMBEDDING_MODELS) is misconfigured",
        ) from None
    for model_id in models:
        try:
            policy.resolve(model_id)
        except EmbeddingModelNotAllowed as exc:
            raise refuse(status_code=422, detail=f"memory.semantic_recall: {exc}") from None


def _check_platform_tools(spec: AgentSpec) -> None:
    """No built-in or MCP tool may take a reserved platform tool name (`retrieve`,
    `update_working_memory`), checked for every spec (AgentSpec itself checks only specs that use
    knowledge or memory, so stored specs keep validating). Hidden platform tools (knowledge.search)
    are for tool runs only and never part of an agent."""
    conflicts = reserved_tool_name_conflicts(spec)
    if conflicts:
        raise refuse(
            status_code=422, detail=f"tool name '{conflicts[0]}' is reserved for a platform tool"
        )
    for tool_id in spec.tools:
        if tool_id in HIDDEN_TOOL_DECLARATIONS:
            raise refuse(
                status_code=422, detail=f"tool '{tool_id}' is a platform tool and cannot be added to an agent"
            )


def _check_knowledge_bases(conn: Connection, tenant_id: str, spec: AgentSpec) -> None:
    """Every attached knowledge base must exist in the principal's tenant (422 naming the first that
    does not). Another tenant's knowledge base is reported exactly like an unknown one. A knowledge
    base deleted after publish (or from a draft, whose test runs skip this check) gives the run's
    retrieve tool a model-visible error instead."""
    ids = [ref.id for ref in spec.knowledge_bases]
    if not ids:
        return
    existing = KnowledgeStore(conn.engine).existing_knowledge_base_ids(tenant_id, ids)
    for kb_id in ids:
        if kb_id not in existing:
            raise refuse(status_code=422, detail=f"knowledge base '{kb_id}' does not exist in this tenant")


def draft_hash(draft_spec: dict | None) -> str | None:
    """Canonical hash of a stored draft, or None when it no longer validates."""
    if draft_spec is None:
        return None
    try:
        return canonical_spec_hash(AgentSpec.model_validate(draft_spec))
    except ValidationError:
        return None


def validate_stored_draft(draft_spec: dict) -> AgentSpec:
    """The agent's stored draft as an AgentSpec, or 422 when it no longer validates (e.g. a row
    written under older rules). Shared by publish and draft runs (run_options._draft_target) so the
    two answers cannot drift. The detail names the first error's location and Pydantic message; such
    a message may quote a value from the draft itself, which only the draft's own tenant sees."""
    try:
        return AgentSpec.model_validate(draft_spec)
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {"loc": (), "msg": "invalid"}
        where = ".".join(str(p) for p in first.get("loc", ())) or "spec"
        raise refuse(
            status_code=422,
            detail=f"The draft is not a valid agent spec ({where}: {first.get('msg', 'invalid')}); fix it in the editor",
        ) from None
