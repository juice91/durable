"""Workflow schedule helpers: 5-field cron + IANA timezone checks, the next fire time, and input
validation against a workflow spec's input_schema (shared with the worker tick)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

from eap_api.routes.workflow_runs import MAX_WORKFLOW_INPUT_BYTES, input_schema_errors, non_finite_errors


def check_cron(cron: str) -> bool:
    """True for a valid 5-field cron (minute hour day month weekday); no seconds field."""
    return isinstance(cron, str) and len(cron.split()) == 5 and croniter.is_valid(cron)


def get_zone(name: str) -> ZoneInfo | None:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


def next_fire(cron: str, timezone_name: str, after: datetime) -> datetime:
    """The first fire time strictly after `after`, computed in `timezone_name`, returned in UTC."""
    zone = ZoneInfo(timezone_name)
    base = after.astimezone(zone) if after.tzinfo else after.replace(tzinfo=timezone.utc).astimezone(zone)
    return croniter(cron, base).get_next(datetime).astimezone(timezone.utc)


def validate_schedule_input(spec: Any, input: Any) -> list[str]:
    """Problems with `input` for the spec's input_schema, as short strings that never quote the
    value (path and failing keyword only); empty when valid. `spec` is a WorkflowSpec."""
    errors = non_finite_errors(input) or input_schema_errors(spec.input_schema, input)
    problems = [f"{'.'.join(str(p) for p in e['loc'][1:]) or 'input'}: {e['type']}" for e in errors]
    if not problems and len(json.dumps(input, sort_keys=True, ensure_ascii=False).encode("utf-8")) > MAX_WORKFLOW_INPUT_BYTES:
        problems.append(f"input: must be at most {MAX_WORKFLOW_INPUT_BYTES // 1024} KiB as JSON")
    return problems
