"""Processors (guardrails): the catalog, the test console and the recorded outcomes.

- `catalog`: the built-in processor types (eap_schemas `PROCESSOR_DECLARATIONS`) with their config JSON schemas.
- `test`: runs deterministic processors (`eap_processors`) on a sample, in this call only: no run, no worker,
  no model, nothing stored or logged. `moderation` calls a model, so it is refused here.
- `results`: the tenant's `processor_result` events (written by the worker), newest first, keyset-paged. Only
  the outcome fields are served (type, phase, action, match counts), never any text.
"""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, get_args

from sqlalchemy import and_, or_, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from eap_api.services.errors import InvalidInput, Unavailable
from eap_api.services.run_filters import plain_refusals
from eap_processors import (
    OUTCOME_REASONS,
    InvalidProcessorConfig,
    apply_deterministic,
    compile_processors,
    new_budget,
)
from eap_run_store.store import ExecutionEventModel, RunModel
from eap_schemas.processors import (
    MAX_PROCESSORS_PER_PHASE,
    MAX_REGEX_PATTERN_LENGTH,
    MAX_REGEX_PATTERNS,
    MODERATION_CATEGORIES,
    PII_KINDS,
    PROCESSOR_DECLARATIONS,
    PROCESSOR_PHASES,
    ProcessorAction,
    ProcessorSpec,
    ProcessorType,
)

# The test console's sample cap (UTF-8 bytes).
MAX_TEST_SAMPLE_BYTES = 32 * 1024
PROCESSOR_RESULT_EVENT = "processor_result"
MODERATION_NOT_TESTABLE = (
    "moderation calls a model and cannot run in the test console; test it in the playground"
)
MAX_RESULTS_CURSOR_LENGTH = 256
RESULTS_STATEMENT_TIMEOUT_MS = 5000
RESULTS_TIMEOUT_DETAIL = "processor results query timed out; narrow the time range or add filters"
RESULTS_UNAVAILABLE_DETAIL = "processor results are temporarily unavailable; retry shortly"
QUERY_CANCELED_SQLSTATE = "57014"
BAD_CURSOR = "before is not a valid cursor; pass the previous page's next_before verbatim"
# The test console's time budget for one request (a sync route: it holds a threadpool worker). A
# chain over budget is reported blocked with reason budget_exceeded.
TEST_CONSOLE_BUDGET_SECONDS = 2.0

_PROCESSOR_TYPES: tuple[str, ...] = get_args(ProcessorType)
_ACTIONS: tuple[str, ...] = get_args(ProcessorAction)


@dataclass(frozen=True)
class ResultsCursor:
    """A position in the (ts DESC, run_id DESC, seq DESC) order, as an opaque URL-safe token."""

    ts: datetime
    run_id: str
    seq: int

    def encode(self) -> str:
        raw = json.dumps({"t": self.ts.isoformat(), "r": self.run_id, "s": self.seq}, separators=(",", ":"))
        return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")

    @classmethod
    def decode(cls, token: str) -> ResultsCursor:
        try:
            raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
            data = json.loads(raw)
            if not isinstance(data, dict) or set(data) != {"t", "r", "s"}:
                raise ValueError
            ts = datetime.fromisoformat(data["t"])
            run_id = str(uuid.UUID(data["r"]))
            seq = data["s"]
            if ts.tzinfo is None or type(seq) is not int or seq < 0:
                raise ValueError
        except (binascii.Error, ValueError, TypeError, AttributeError):
            raise InvalidInput(BAD_CURSOR) from None
        return cls(ts, run_id, seq)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


# The only match-count keys a processor outcome documents (eap_processors.ProcessorOutcome): PII
# kinds, `pattern_<i>` for a blocklist position and `chars_over_limit`. Any other stored key is
# dropped, so a result can never serve text that found its way into a key.
_COUNT_KEYS: frozenset[str] = frozenset(
    (*PII_KINDS, *(f"pattern_{i}" for i in range(MAX_REGEX_PATTERNS)), "chars_over_limit")
)


def _one_of(value: Any, allowed: Collection[str]) -> str | None:
    """`value` when it is one of the `allowed` strings, else None. Stored event data is not
    validated, so the type check comes first: a list or dict must never reach a set lookup
    (unhashable) or be served."""
    return value if isinstance(value, str) and value in allowed else None


def _counts(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {
        k: v for k, v in value.items() if _one_of(k, _COUNT_KEYS) is not None and type(v) is int and v >= 0
    }


def _result_json(event: ExecutionEventModel, run: RunModel) -> dict[str, Any]:
    data = event.data if isinstance(event.data, dict) else {}
    item: dict[str, Any] = {
        "run_id": str(event.run_id),
        "seq": event.seq,
        "ts": event.ts.isoformat(),
        "agent_id": run.agent_id,
        "agent_version": run.agent_version,
        "parent_run_id": run.parent_run_id,
        "phase": _one_of(data.get("phase"), PROCESSOR_PHASES),
        "index": data.get("index") if type(data.get("index")) is int else None,
        "processor_type": _one_of(data.get("processor_type"), _PROCESSOR_TYPES),
        "action": _one_of(data.get("action"), _ACTIONS),
        "match_counts": _counts(data.get("match_counts")),
    }
    reason = _one_of(data.get("reason"), OUTCOME_REASONS)
    if reason is not None:
        item["reason"] = reason
    categories = data.get("categories")
    if isinstance(categories, list):
        item["categories"] = [c for c in categories if _one_of(c, MODERATION_CATEGORIES) is not None]
    return item


@plain_refusals
class ProcessorService:
    """`db` is needed only by `results`."""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def catalog(self) -> dict[str, Any]:
        """The processor catalog: every built-in type with its config schema, and the limits."""
        items = [
            {
                "id": d.id,
                "title": d.title,
                "description": d.description,
                "phases": list(d.phases),
                "actions": list(d.actions),
                "deterministic": d.deterministic,
                "config_schema": d.config_json_schema(),
            }
            for d in PROCESSOR_DECLARATIONS.values()
        ]
        return {
            "items": items,
            "limits": {
                "max_processors_per_phase": MAX_PROCESSORS_PER_PHASE,
                "max_regex_patterns": MAX_REGEX_PATTERNS,
                "max_regex_pattern_length": MAX_REGEX_PATTERN_LENGTH,
                "max_test_sample_bytes": MAX_TEST_SAMPLE_BYTES,
            },
            "moderation_categories": list(MODERATION_CATEGORIES),
        }

    def test(self, processors: list[ProcessorSpec], sample: str) -> dict[str, Any]:
        """Run deterministic processors in order on `sample` (at most 32 KiB). Stops at the first block.
        Nothing is stored or logged; the sample is answered to the caller only."""
        if len(sample.encode("utf-8")) > MAX_TEST_SAMPLE_BYTES:
            raise InvalidInput(f"text is larger than {MAX_TEST_SAMPLE_BYTES} bytes (32 KiB)")
        if any(p.type == "moderation" for p in processors):
            raise InvalidInput(MODERATION_NOT_TESTABLE)
        try:
            compiled = compile_processors(processors)
        except InvalidProcessorConfig as exc:
            raise InvalidInput(str(exc)) from None
        result = apply_deterministic(compiled, sample, new_budget(TEST_CONSOLE_BUDGET_SECONDS))
        return {
            "text": result.text,
            "blocked": result.blocked,
            "results": [{"index": i, **outcome.to_json()} for i, outcome in enumerate(result.outcomes)],
        }

    def results(
        self,
        tenant_id: str,
        *,
        agent_id: uuid.UUID | None,
        processor_type: str | None,
        action: str | None,
        phase: str | None,
        run_id: uuid.UUID | None,
        start: datetime | None,
        end: datetime | None,
        limit: int,
        before: str | None,
    ) -> dict[str, Any]:
        """The tenant's processor outcomes, newest first, filtered by agent, processor type, action, phase,
        run and time range (`start` inclusive, `end` exclusive; a time without an offset is UTC).
        `next_before` is set when a full page came back. A query past the 5 s statement timeout is
        Unavailable asking to narrow the range."""
        assert self.db is not None
        db = self.db
        start, end = _aware(start), _aware(end)
        if start is not None and end is not None and start >= end:
            raise InvalidInput("from must be before to")
        cursor = ResultsCursor.decode(before) if before is not None else None

        query = (
            select(ExecutionEventModel, RunModel)
            .join(
                RunModel,
                and_(RunModel.tenant_id == ExecutionEventModel.tenant_id, RunModel.id == ExecutionEventModel.run_id),
            )
            .where(
                ExecutionEventModel.tenant_id == tenant_id,
                RunModel.tenant_id == tenant_id,
                ExecutionEventModel.type == PROCESSOR_RESULT_EVENT,
            )
        )
        if agent_id is not None:
            query = query.where(RunModel.agent_id == str(agent_id))
        if run_id is not None:
            query = query.where(ExecutionEventModel.run_id == str(run_id))
        if processor_type is not None:
            query = query.where(ExecutionEventModel.data["processor_type"].astext == processor_type)
        if action is not None:
            query = query.where(ExecutionEventModel.data["action"].astext == action)
        if phase is not None:
            query = query.where(ExecutionEventModel.data["phase"].astext == phase)
        if start is not None:
            query = query.where(ExecutionEventModel.ts >= start)
        if end is not None:
            query = query.where(ExecutionEventModel.ts < end)
        if cursor is not None:
            query = query.where(
                or_(
                    ExecutionEventModel.ts < cursor.ts,
                    and_(ExecutionEventModel.ts == cursor.ts, ExecutionEventModel.run_id < cursor.run_id),
                    and_(
                        ExecutionEventModel.ts == cursor.ts,
                        ExecutionEventModel.run_id == cursor.run_id,
                        ExecutionEventModel.seq < cursor.seq,
                    ),
                )
            )
        query = query.order_by(
            ExecutionEventModel.ts.desc(), ExecutionEventModel.run_id.desc(), ExecutionEventModel.seq.desc()
        ).limit(limit)

        try:
            db.execute(text(f"SET LOCAL statement_timeout = {RESULTS_STATEMENT_TIMEOUT_MS}"))
            rows = db.execute(query).all()
        except OperationalError as exc:
            # Nothing was written. Postgres cancels the statement at the timeout with SQLSTATE 57014
            # (query_canceled); any other operational error is a generic refusal.
            db.rollback()
            timed_out = getattr(exc.orig, "sqlstate", None) == QUERY_CANCELED_SQLSTATE
            raise Unavailable(RESULTS_TIMEOUT_DETAIL if timed_out else RESULTS_UNAVAILABLE_DETAIL) from None
        db.rollback()
        items = [_result_json(event, run) for event, run in rows]
        next_before = None
        if len(rows) == limit:
            last = rows[-1][0]
            next_before = ResultsCursor(last.ts, str(last.run_id), last.seq).encode()
        return {"items": items, "next_before": next_before}
