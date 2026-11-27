"""Suggested rules: the business logic behind eap_api.routes.scorer_suggestions.

One run of the built-in "Scorer suggester" agent reads the target's redacted definition and up to 5 recent
completed run outputs and answers with rules a person can add as a scorer or a suite evaluator. The answer
is validated, cached per (tenant, target, definition, max, include_runs) for 10 minutes, so a repeat click
costs no run.

Seeding the suggester agent and running it are the Architect's and the runs' business (their groups have
not been migrated to services yet), so the caller passes them in as `SuggesterPorts`.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from typing import Any, Protocol

from sqlalchemy.orm import Session

from eap_api.models import AgentModel
from eap_api.services import access as acl
from eap_api.services.access import Access, AccessBound
from eap_api.services.errors import NotFound
from eap_api.workflows.models import WorkflowModel
from eap_observability.redaction import redact
from eap_processors import Re2PatternError, compile_re2
from eap_run_store.definition_errors import DefinitionError, refuse
from eap_schemas import ModelRef
from eap_schemas.scorer_suggestions import (
    DEFAULT_SUGGESTIONS,  # noqa: F401 - re-exported for the request model
    MAX_SUGGESTIONS,
    Suggestion,
    finalize_suggestions,
    validate_suggestion,
)

AGENT_NOT_FOUND = "Agent not found"
WORKFLOW_NOT_FOUND = "Workflow not found"
CACHE_TTL_S = 600.0
CACHE_MAX_ENTRIES = 256
MAX_RUNS = 5
MAX_RUN_INPUT_CHARS = 600
MAX_RUN_OUTPUT_CHARS = 800
MAX_DEFINITION_CHARS = 8000
MAX_SCHEMA_CHARS = 2000
MAX_PROMPT_CHARS = 20000

_cache: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}
_cache_lock = threading.Lock()


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _cache_get(key: tuple[Any, ...]) -> dict[str, Any] | None:
    with _cache_lock:
        hit = _cache.get(key)
        if hit is None:
            return None
        if hit[0] < time.monotonic():
            del _cache[key]
            return None
        return hit[1]


def _cache_put(key: tuple[Any, ...], value: dict[str, Any]) -> None:
    now = time.monotonic()
    with _cache_lock:
        for stale in [k for k, (expires, _) in _cache.items() if expires < now]:
            del _cache[stale]
        while len(_cache) >= CACHE_MAX_ENTRIES:
            del _cache[next(iter(_cache))]
        _cache[key] = (now + CACHE_TTL_S, value)




class SuggesterPorts(Protocol):
    """What the caller supplies: the suggester agent and one run of it."""

    def ensure_suggester(self, tenant_id: str, user_id: str) -> tuple[str, str | None]:
        """`(the tenant's suggester agent id, its model id)`, seeding the agent when missing. Raises the
        Architect's `choose_model` DefinitionError when a row is missing and no model exists."""

    def run_drafter(self, suggester_id: str, prompt: str) -> tuple[dict[str, Any], list[str], str, str, str | None]:
        """One run of the agent and its answer: `(parsed object, assumptions, raw, run_id, model)`."""


# ------------------------------------------------------------------------------------ the prompt


def _clip(value: Any, limit: int) -> str:
    """`value` as redacted text of at most `limit` characters."""
    raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return redact(raw, max_length=limit)


def _agent_definition(name: str, spec: dict[str, Any]) -> str:
    lines = [f"Target: agent {_clip(name, 100)}", "Instructions:", _clip(spec.get("instructions") or "(none)", MAX_DEFINITION_CHARS)]
    schema = (spec.get("output") or {}).get("json_schema")
    lines.append("Output: " + (f"structured, schema {_clip(schema, MAX_SCHEMA_CHARS)}" if schema else "free text"))
    tools = spec.get("tools") or []
    if tools:
        lines.append("Tools: " + ", ".join(_clip(t, 60) for t in tools[:20]))
    return "\n".join(lines)


def _workflow_definition(name: str, spec: dict[str, Any]) -> str:
    lines = [f"Target: workflow {_clip(name, 100)}", "Description: " + _clip(spec.get("description") or "(none)", 500)]
    lines.append("Input schema: " + _clip(spec.get("input_schema") or {}, MAX_SCHEMA_CHARS))
    if spec.get("output_schema"):
        lines.append("Output schema: " + _clip(spec["output_schema"], MAX_SCHEMA_CHARS))
    lines.append("Steps:\n" + _clip(spec.get("steps") or [], MAX_DEFINITION_CHARS))
    return "\n".join(lines)


def _run_section(runs: list[dict[str, Any]]) -> str:
    if not runs:
        return "Recent runs: none"
    lines = ["Recent runs (newest first):"]
    for index, run in enumerate(runs, 1):
        lines.append(f"- run {index} input: {_clip(run.get('input') or '', MAX_RUN_INPUT_CHARS)}")
        lines.append(f"  run {index} output: {_clip(run.get('output') or '', MAX_RUN_OUTPUT_CHARS)}")
    return "\n".join(lines)


def build_prompt(definition: str, runs: list[dict[str, Any]], limit: int) -> str:
    """The suggester's input: the redacted definition, the redacted recent runs and the answer size, cut
    to MAX_PROMPT_CHARS."""
    prompt = "\n".join([
        f"Propose at most {limit} rules.", "", "--- Definition ---", definition, "", "--- Recent runs ---", _run_section(runs),
    ])
    return prompt[:MAX_PROMPT_CHARS]



# ------------------------------------------------------------------------------------- the run

_RELABEL = {"draft_failed": "suggest_failed", "draft_timeout": "suggest_timeout", "draft_invalid": "suggest_invalid"}


def _regex_ok(item: Suggestion | None) -> Suggestion | None:
    if item is not None and item.spec["kind"] == "regex":
        try:
            compile_re2(item.spec["pattern"], allow_empty_match=True)
        except Re2PatternError:
            return None
    return item


class ScorerSuggestionService(AccessBound):
    """Rule suggestions for an agent or workflow: an authoring aid that spends tokens, so `edit` on the target."""

    def __init__(self, db: Session, run_store: Any, access: Access | None = None) -> None:
        self.db = db
        self.run_store = run_store
        self._access = access  # None: no caller to check

    def _target(self, tenant_id: str, kind: str, target_id: uuid.UUID) -> tuple[str, dict[str, Any]]:
        """`(name, draft spec)` of the tenant's agent or workflow, NotFound otherwise."""
        model = AgentModel if kind == "agent" else WorkflowModel
        row = self.db.query(model).filter(model.id == str(target_id), model.tenant_id == tenant_id).first()
        if row is None:
            raise NotFound(AGENT_NOT_FOUND if kind == "agent" else WORKFLOW_NOT_FOUND)
        acl.require(self._access, "edit", kind, str(target_id))
        return row.name, row.draft_spec or {}

    def _suggester(self, tenant_id: str, user_id: str, ports: SuggesterPorts) -> tuple[str, ModelRef]:
        """The tenant's suggester agent id and the model an `llm_judge` suggestion gets."""
        try:
            agent_id, model_id = ports.ensure_suggester(tenant_id, user_id)
        except DefinitionError as exc:
            if exc.code == "choose_model":
                raise refuse(409, "No model is available for the scorer suggester.", code="no_model", **exc.extra) from None
            raise
        if model_id is None:
            raise refuse(409, "No model is available for the scorer suggester.", code="no_model")
        provider, _, name = model_id.partition(":")
        return agent_id, ModelRef(provider=provider, name=name)

    def suggest(
        self, tenant_id: str, user_id: str, kind: str, target_id: uuid.UUID, *, max: int, include_runs: bool,
        ports: SuggesterPorts,
    ) -> dict[str, Any]:
        """Suggest rules for the agent (`kind` "agent") or workflow ("workflow"): `{suggestions, model, dropped}`."""
        name, spec = self._target(tenant_id, kind, target_id)
        digest = hashlib.sha256(json.dumps(spec, sort_keys=True, default=str).encode()).hexdigest()
        key = (tenant_id, kind, str(target_id), digest, max, include_runs)
        cached = _cache_get(key)
        if cached is not None:
            return cached

        suggester_id, judge = self._suggester(tenant_id, user_id, ports)
        runs: list[dict[str, Any]] = []
        if include_runs:
            filters = {"agent_id": target_id} if kind == "agent" else {"workflow_id": target_id}
            runs = self.run_store.list_runs(tenant_id, limit=MAX_RUNS, status="completed", **filters)
        definition = _agent_definition(name, spec) if kind == "agent" else _workflow_definition(name, spec)
        try:
            parsed, _, raw, _, model = ports.run_drafter(suggester_id, build_prompt(definition, runs, max))
        except DefinitionError as exc:
            if exc.code in _RELABEL:
                detail = exc.detail.replace("drafter", "scorer suggester").replace("definition", "list of rules")
                raise type(exc)(detail, code=_RELABEL[exc.code], **exc.extra) from None
            raise
        entries = parsed.get("suggestions")
        if not isinstance(entries, list):
            raise refuse(422, "The scorer suggester's answer has no list of suggestions.", code="suggest_invalid",
                         raw=raw[:2000])
        candidates = [_regex_ok(validate_suggestion(e, judge_model=judge)) for e in entries[: MAX_SUGGESTIONS * 2]]
        kept, dropped = finalize_suggestions(candidates, limit=max, target="agent" if kind == "agent" else "workflow")
        result = {"suggestions": [s.model_dump(mode="json") for s in kept], "model": model, "dropped": dropped}
        _cache_put(key, result)
        return result
