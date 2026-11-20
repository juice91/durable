"""Optimistic concurrency for the definition-class resources: ONE implementation of the `revision` token.

Agents, workflows, schedules, eval suites and cases, connections, kb sources, scorers and MCP servers
carry `revision INTEGER NOT NULL DEFAULT 1` (migration 0033). Every write that changes such a row adds 1
in the same UPDATE, guarded by the caller's expectation:

    UPDATE <table> SET ..., revision = revision + 1
     WHERE id = :id AND tenant_id = :t AND (:expected IS NULL OR revision = :expected)

When that matches no row but the row exists, the write was stale: `RevisionMismatch` carries the current
revision, and the caller (the API route, an Architect tool, an MCP tool) decides what to do with it.
`expected=None` means "no precondition": the write still bumps, so a blind writer invalidates readers.

Three entry points cover every store style:

- `bump_revision(conn, table, ...)`: Core UPDATE of the revision alone, for a row another statement
  edits in the same transaction (the ORM stores, definitions.py);
- `guard_sql(expected)` and `REVISION_SET`: fragments the raw-SQL stores splice into their own UPDATE,
  then `explain_empty_update` when that UPDATE returned no row;
- `check_expected(current, expected)`: the in-transaction comparison for code that already holds the
  row lock (the guard that fails fast before any validation work is repeated).

`revision` is the concurrency token; `updated_at` keeps its meaning (when the row last changed).
System writes that are not edits of the definition (a schedule's `mark_fired`, a kb source's checkpoint,
a connection's last check) do not bump it, so a runtime tick never makes an open editor stale.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Table, and_, select, text, update
from sqlalchemy.engine import Connection

REVISION_SET = "revision = revision + 1"


class RevisionMismatch(Exception):
    """The row's revision is not the one the writer read; nothing was written."""

    def __init__(self, current_revision: int, expected: int | None = None):
        super().__init__(f"revision is {current_revision}, expected {expected}")
        self.current_revision = current_revision
        self.expected = expected


def guard_sql(expected: int | None, params: dict[str, Any], column: str = "revision") -> str:
    """The WHERE fragment of a raw UPDATE (` AND (CAST(:expected AS integer) IS NULL OR revision = :expected)`);
    sets `expected` in `params`."""
    params["expected"] = expected
    return f" AND (CAST(:expected AS integer) IS NULL OR {column} = :expected)"


def check_expected(current: int, expected: int | None) -> None:
    """RevisionMismatch unless there is no expectation or it equals `current`."""
    if expected is not None and current != expected:
        raise RevisionMismatch(current, expected)


def explain_empty_update(conn: Connection, table: str, tenant_id: str, row_id: str, expected: int | None,
                         id_column: str = "id") -> None:
    """Call when a guarded UPDATE matched no row: raises RevisionMismatch when the row exists (the guard
    refused), returns when it does not (the caller reports not-found)."""
    current = conn.execute(
        text(f"SELECT revision FROM {table} WHERE tenant_id = :t AND {id_column} = :id"),  # table is a literal of ours
        {"t": tenant_id, "id": row_id},
    ).scalar_one_or_none()
    if current is not None and expected is not None:
        raise RevisionMismatch(int(current), expected)


def bump_revision(
    conn: Connection, table: Table, tenant_id: str, row_id: str, expected: int | None = None, *,
    id_column: str = "id",
) -> int | None:
    """Add 1 to the row's revision when `expected` is None or equals it; returns the new revision.

    None when the tenant has no such row. RevisionMismatch when the row exists with another revision.
    Runs in the caller's transaction (it also takes the row lock, so a later statement of the same
    transaction cannot interleave with another writer)."""
    id_col, rev, tenant = table.c[id_column], table.c.revision, table.c.tenant_id
    conditions = [id_col == row_id, tenant == tenant_id]
    if expected is not None:
        conditions.append(rev == expected)
    new = conn.execute(update(table).where(and_(*conditions)).values(revision=rev + 1).returning(rev)).scalar_one_or_none()
    if new is not None:
        return int(new)
    current = conn.execute(select(rev).where(id_col == row_id, tenant == tenant_id)).scalar_one_or_none()
    if current is None:
        return None
    raise RevisionMismatch(int(current), expected)


__all__ = [
    "REVISION_SET", "RevisionMismatch", "bump_revision", "check_expected", "explain_empty_update", "guard_sql",
]


def bump_row(session: Any, row: Any, expected: int | None = None) -> int:
    """`bump_revision` for an ORM row (its class has `tenant_id`, `id` and `revision`): the guarded UPDATE
    runs now in the session's transaction and the row's `revision` attribute is set to the new value.
    RevisionMismatch when stale; LookupError when the row vanished underneath (deleted concurrently)."""
    from sqlalchemy.orm.attributes import set_committed_value

    new = bump_revision(session.connection(), type(row).__table__, row.tenant_id, str(row.id), expected)
    if new is None:
        raise LookupError(f"{type(row).__tablename__} {row.id} is gone")
    set_committed_value(row, "revision", new)
    return new


__all__.append("bump_row")
