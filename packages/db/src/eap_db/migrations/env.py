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


def _configure(connection) -> None:
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
        with engine.connect() as connection:
            _configure(connection)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
