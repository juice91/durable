"""Reusable scorers and scoring on demand: the business logic behind eap_api.routes.scorers.

Scorers are tenant-scoped named evaluators; an agent has at most 10 attached (the store enforces it).
Scoring a finished run asks the worker (`launch_score`) to append one `score` event per scorer.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from eap_api.models import AgentModel
from eap_api.ports import RunLauncher
from eap_api.services.errors import Conflict, InvalidInput, NotFound, Unavailable
from eap_processors import Re2PatternError, compile_re2
from eap_run_store.scorers import (  # noqa: F401 - the limits are re-exported for the routes' request models
    SCORER_DESCRIPTION_MAX_CHARS,
    SCORER_NAME_MAX_CHARS,
    ScorerLimitExceeded,
    ScorerNameConflict,
    ScorerNotFound,
    ScorerStore,
)
from eap_schemas.scorers import ScorerInvalid, validate_scorer_evaluator

SCORER_NOT_FOUND = "Scorer not found"
AGENT_NOT_FOUND = "Agent not found"
NAME_IN_USE = "a scorer with this name already exists"
LIMIT_REACHED = "the agent already has the maximum number of scorers"
REGEX_INVALID = "pattern is not valid RE2 syntax or is too complex"
RUN_NOT_FOUND = "Run not found"
RUN_NOT_FINISHED = "run_not_finished"
ALREADY_SCORED = "already_scored"
MAX_SCORE_SCORERS = 8
SCORE_START_FAILED = "scoring could not be started; retry"


def validate_evaluator(evaluator: Any) -> dict[str, Any]:
    """The validated evaluator, or InvalidInput naming the path with a fixed message."""
    try:
        spec = validate_scorer_evaluator(evaluator)
    except ScorerInvalid as exc:
        raise InvalidInput(f"{exc.path}: {exc.reason}") from None
    if spec["kind"] == "regex":
        try:
            compile_re2(spec["pattern"], allow_empty_match=True)
        except Re2PatternError:
            raise InvalidInput(f"evaluator.pattern: {REGEX_INVALID}") from None
    return spec


@dataclass(frozen=True)
class ScoreOutcome:
    """The scorers queued for a run and the requested ones that already had a score event."""

    run_id: uuid.UUID
    scorer_ids: list[str]
    already_scored: list[str]


class ScorerService:
    """Scorers of one tenant at a time (every method takes the tenant id)."""

    def __init__(self, store: ScorerStore, db: Session, run_store: Any, launcher: RunLauncher) -> None:
        self.store = store
        self.db = db
        self.run_store = run_store
        self.launcher = launcher

    def _agent(self, tenant_id: str, agent_id: uuid.UUID) -> None:
        found = (
            self.db.query(AgentModel.id)
            .filter(AgentModel.id == str(agent_id), AgentModel.tenant_id == tenant_id)
            .first()
        )
        if found is None:
            raise NotFound(AGENT_NOT_FOUND)

    # ------------------------------------------------------------------------ scorers

    def create(self, tenant_id: str, name: str, evaluator: Any, description: str | None) -> Any:
        spec = validate_evaluator(evaluator)
        try:
            return self.store.create(tenant_id, name, spec, description)
        except ScorerNameConflict:
            raise Conflict(NAME_IN_USE) from None

    def list(self, tenant_id: str) -> list[Any]:
        return self.store.list(tenant_id)

    def get(self, tenant_id: str, scorer_id: uuid.UUID) -> Any:
        try:
            return self.store.get(tenant_id, scorer_id)
        except ScorerNotFound:
            raise NotFound(SCORER_NOT_FOUND) from None

    def update(
        self, tenant_id: str, scorer_id: uuid.UUID, *, name: str | None, description: str | None,
        set_description: bool, evaluator: Any, expected_revision: int | None,
    ) -> Any:
        """Change a scorer; `evaluator` None keeps it. A stale `expected_revision` raises the store's
        RevisionMismatch."""
        spec = validate_evaluator(evaluator) if evaluator is not None else None
        try:
            return self.store.update(
                tenant_id, scorer_id, name=name, description=description, set_description=set_description,
                evaluator=spec, expected_revision=expected_revision,
            )
        except ScorerNotFound:
            raise NotFound(SCORER_NOT_FOUND) from None
        except ScorerNameConflict:
            raise Conflict(NAME_IN_USE) from None

    def delete(self, tenant_id: str, scorer_id: uuid.UUID) -> None:
        try:
            self.store.delete(tenant_id, scorer_id)
        except ScorerNotFound:
            raise NotFound(SCORER_NOT_FOUND) from None

    # ------------------------------------------------------------------------ attachments

    def attached_to_agent(self, tenant_id: str, agent_id: uuid.UUID) -> list[Any]:
        self._agent(tenant_id, agent_id)
        return self.store.attached_to_agent(tenant_id, agent_id)

    def attach(
        self, tenant_id: str, agent_id: uuid.UUID, scorer_id: uuid.UUID, sampling_rate: Decimal
    ) -> list[Any]:
        """Attach a scorer to the agent (at most 10 per agent: Conflict); the agent's attached scorers."""
        self._agent(tenant_id, agent_id)
        try:
            self.store.attach(tenant_id, agent_id, scorer_id, sampling_rate)
        except ScorerNotFound:
            raise NotFound(SCORER_NOT_FOUND) from None
        except ScorerLimitExceeded:
            raise Conflict(LIMIT_REACHED) from None
        return self.store.attached_to_agent(tenant_id, agent_id)

    def detach(self, tenant_id: str, agent_id: uuid.UUID, scorer_id: uuid.UUID) -> None:
        self._agent(tenant_id, agent_id)
        self.store.detach(tenant_id, agent_id, scorer_id)

    # ------------------------------------------------------------------------ score on demand

    def _already_scored(self, tenant_id: str, run_id: uuid.UUID, scorer_ids: list[str]) -> set[str]:
        """The scorer ids that already have a `score:{id}` event on the run."""
        keys = {f"score:{s}": s for s in scorer_ids}
        rows = self.db.execute(
            text("SELECT event_key FROM execution_events WHERE tenant_id = :t AND run_id = :r AND event_key = ANY(:k)"),
            {"t": tenant_id, "r": str(run_id), "k": list(keys)},
        )
        return {keys[r[0]] for r in rows}

    def score_run(self, tenant_id: str, run_id: uuid.UUID, scorer_ids: list[uuid.UUID]) -> ScoreOutcome:
        """Score a finished run with the scorers now: enqueue the worker's scoring unless every requested
        scorer already has its event (first write wins; then `scorer_ids` of the outcome is empty).
        NotFound for another tenant's run or scorer; Conflict `run_not_finished` unless the run is
        completed or skipped (a failed or cancelled run has no answer to score); Unavailable when the
        enqueue failed (nothing was recorded; the caller may retry)."""
        run = self.run_store.get_run(tenant_id, run_id)
        if run is None:
            raise NotFound(RUN_NOT_FOUND)
        if run["status"] not in ("completed", "skipped"):
            raise Conflict(RUN_NOT_FINISHED)
        requested = list(dict.fromkeys(str(s) for s in scorer_ids))
        for scorer_id in requested:
            try:
                self.store.get(tenant_id, scorer_id)
            except ScorerNotFound:
                raise NotFound(SCORER_NOT_FOUND) from None
        done = self._already_scored(tenant_id, run_id, requested)
        already = [s for s in requested if s in done]
        queued = [s for s in requested if s not in done]
        if not queued:
            return ScoreOutcome(run_id, [], already)
        try:
            self.launcher.launch_score(run_id=run_id, tenant_id=tenant_id, scorer_ids=queued)
        except Exception:  # noqa: BLE001 - nothing was recorded; the caller may retry
            raise Unavailable(SCORE_START_FAILED) from None
        return ScoreOutcome(run_id, queued, already)
