"""Parsing of the run list filters shared by the run, span and trace listings (framework-free).

Every refusal is an `InvalidInput` (422)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from eap_api.services.errors import InvalidInput

MAX_STATUS_FILTERS = 10
RUN_LIST_TIMEOUT_DETAIL = "Run list query timed out; narrow the time range or the filters"
FROM_AFTER_TO = "from must be before to"
BAD_CURSOR = "before is not a valid cursor; pass the previous page's next_before verbatim"


def status_filter(values: list[str] | None) -> list[str] | None:
    """`?status=a&status=b,c` as a de-duplicated list, None when absent (422 past MAX_STATUS_FILTERS)."""
    if not values:
        return None
    statuses = list(dict.fromkeys(s.strip() for v in values for s in v.split(",") if s.strip()))
    if len(statuses) > MAX_STATUS_FILTERS or any(len(s) > 32 for s in statuses):
        raise InvalidInput(f"at most {MAX_STATUS_FILTERS} status values of at most 32 characters are allowed")
    return statuses or None


def list_param(values: list[str] | None, name: str, *, split: bool) -> list[str] | None:
    """A repeated query parameter, de-duplicated, at most 10 entries of at most 200 characters (422)."""
    if not values:
        return None
    items = [s.strip() for v in values for s in (v.split(",") if split else [v]) if s.strip()]
    items = list(dict.fromkeys(items))
    if len(items) > 10 or any(len(i) > 200 for i in items):
        raise InvalidInput(f"at most 10 {name} values of at most 200 characters are allowed")
    return items or None


def metadata_filter(metadata: list[str] | None) -> dict[str, str] | None:
    """`?metadata=key=value` pairs as a dict (422 for a malformed pair); None when absent."""
    found: dict[str, str] = {}
    for pair in list_param(metadata, "metadata", split=False) or []:
        key, sep, value = pair.partition("=")
        if not sep or not key.strip() or len(key) > 64 or len(value) > 256:
            raise InvalidInput("metadata must be key=value pairs (key up to 64 and value up to 256 characters)")
        found[key.strip()] = value
    return found or None


def to_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def time_window(created_from: datetime | None, created_to: datetime | None) -> tuple[datetime | None, datetime | None]:
    """`[from, to)` in UTC, either bound optional; 422 when `from` is not before `to`."""
    start = to_utc(created_from) if created_from is not None else None
    end = to_utc(created_to) if created_to is not None else None
    if start is not None and end is not None and start >= end:
        raise InvalidInput(FROM_AFTER_TO)
    return start, end


def text_query(q: str | None) -> str | None:
    return q.strip() if q and q.strip() else None


def plain_refusals(cls: type) -> type:
    """Class decorator: every public method's `ServiceError` answers the body these routes always had,
    `{"detail": <message>}` without a `code` (a refusal that already carries a `detail_object` keeps it)."""
    import functools

    from eap_api.services.errors import ServiceError

    def wrap(fn: Any) -> Any:
        @functools.wraps(fn)
        def call(*args: Any, **kwargs: Any) -> Any:
            try:
                return fn(*args, **kwargs)
            except ServiceError as exc:
                if exc.detail_object is None:
                    exc.detail_object = exc.message
                raise

        return call

    for name, member in list(vars(cls).items()):
        if not name.startswith("_") and callable(member):
            setattr(cls, name, wrap(member))
    return cls
