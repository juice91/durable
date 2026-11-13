"""Alembic environment for the eap_app database.

The database comes from a connection passed in `config.attributes["connection"]` (tests and
programmatic use) or else from EAP_DATABASE_URL. Migrations run in the connection's current schema
(search_path), so a test session can migrate its own throwaway schema.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from eap_db.metadata import target_metadata

config = context.config
if config.config_file_name is not None:  # the CLI (alembic.ini); programmatic configs keep the caller's logging
    fileConfig(config.config_file_name, disable_existing_loggers=False)


class SearchPathNotSingleSchema(RuntimeError):
    def __init__(self, schemas: list[str]) -> None:
        super().__init__(schemas)
        self.schemas = schemas

    def __str__(self) -> str:
        return (
            f"migrations must run with a search path of exactly one schema, got {self.schemas}: with more, "
            "unqualified names (alembic_version, DROP ... IF EXISTS, DELETE) can resolve to another "
            "schema's objects. Reference extension objects as public.vector instead of adding public."
        )


def _require_single_schema(connection) -> None:
    # The test plugin puts `public` after the session schema so that pgvector resolves at runtime;
    # such a URL must never migrate (eap_db.testing). A plain URL has just one existing schema.
    schemas = list(connection.exec_driver_sql("SELECT current_schemas(false)").scalar_one())
    if len(schemas) != 1:
        raise SearchPathNotSingleSchema(schemas)


def _configure(connection) -> None:
    _require_single_schema(connection)
    context.configure(
        connection=connection,
        target_metadata=target_metadata(),
        compare_type=True,
        compare_server_default=True,
    )


def _database_url() -> str:
    url = os.environ.get("EAP_DATABASE_URL")
    if not url:
        raise RuntimeError(
            "EAP_DATABASE_URL is not set; point it at the eap_app database, e.g. "
            "postgresql+psycopg://eap_app:eap_app_password@localhost:54329/eap_app"
        )
    return url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata(),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _configure(connection)
        with context.begin_transaction():
            context.run_migrations()
        return

    engine = create_engine(_database_url(), poolclass=pool.NullPool)
    try:
        # begin(), not connect(): _configure's search-path query autobegins the connection's
        # transaction, so Alembic's begin_transaction() joins it as an external one and never
        # commits; this block commits it (a failed migration rolls everything back).
        with engine.begin() as connection:
            _configure(connection)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
