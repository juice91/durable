"""Providers of the evals services (test suites, cases, eval runs, scorers, suggested rules)."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from eap_api.deps import EvalStoreDep, RunLauncherDep, ScorerStoreDep, SessionDep, StoresDep
from eap_api.services.evals import EvalService
from eap_api.services.scorer_suggestions import ScorerSuggestionService
from eap_api.services.scorers import ScorerService


def get_eval_service(db: SessionDep, store: EvalStoreDep, stores: StoresDep, launcher: RunLauncherDep) -> EvalService:
    return EvalService(store, db, stores[0], launcher)


EvalServiceDep = Annotated[EvalService, Depends(get_eval_service)]


def get_scorer_service(
    db: SessionDep, store: ScorerStoreDep, stores: StoresDep, launcher: RunLauncherDep
) -> ScorerService:
    return ScorerService(store, db, stores[0], launcher)


ScorerServiceDep = Annotated[ScorerService, Depends(get_scorer_service)]


def get_scorer_suggestion_service(db: SessionDep, stores: StoresDep) -> ScorerSuggestionService:
    return ScorerSuggestionService(db, stores[0])


ScorerSuggestionServiceDep = Annotated[ScorerSuggestionService, Depends(get_scorer_suggestion_service)]
