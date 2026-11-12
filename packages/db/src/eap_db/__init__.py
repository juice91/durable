"""Alembic owns the eap_app schema; this module is what the API and worker need at startup.

Only Alembic and SQLAlchemy are imported here. The model modules (eap_api, eap_run_store,
eap_worker) are imported by `eap_db.metadata`, which only the migration environment and tests use,
so importing `eap_db` from an app never pulls another app's code in.
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Connection, Engine

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
UPGRADE_COMMAND = "uv run alembic -c packages/db/alembic.ini upgrade head"
NOT_AT_HEAD_MESSAGE = f"database schema is not at the latest migration; run: {UPGRADE_COMMAND}"


class SchemaNotAtHead(RuntimeError):
    """The eap_app database is not at the latest Alembic revision (or was never migrated)."""


def alembic_config(connection: Connection | None = None) -> Config:
    """An Alembic Config pointing at the packaged migrations.

    With `connection`, env.py migrates through it (tests, programmatic upgrades); without it,
    env.py reads EAP_DATABASE_URL, exactly like `alembic -c packages/db/alembic.ini`.
    """
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    if connection is not None:
        cfg.attributes["connection"] = connection
    return cfg


def head_revisions() -> set[str]:
    return set(ScriptDirectory.from_config(alembic_config()).get_heads())


def current_revisions(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        return set(MigrationContext.configure(conn).get_current_heads())


def require_schema_at_head(engine: Engine) -> None:
    """Fail fast unless the database is exactly at the latest migration.

    Raises SchemaNotAtHead naming the command that fixes it. Connection errors propagate as-is.
    """
    current = current_revisions(engine)
    heads = head_revisions()
    if current != heads:
        found = ", ".join(sorted(current)) or "none (not under Alembic)"
        raise SchemaNotAtHead(f"{NOT_AT_HEAD_MESSAGE} (database revision: {found}; latest: {', '.join(sorted(heads))})")


__all__ = [
    "MIGRATIONS_DIR",
    "NOT_AT_HEAD_MESSAGE",
    "SchemaNotAtHead",
    "UPGRADE_COMMAND",
    "alembic_config",
    "current_revisions",
    "head_revisions",
    "require_schema_at_head",
]
