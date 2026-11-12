"""Database configuration and utilities."""

import os
from typing import Generator

from fastapi import Request
from sqlalchemy import create_engine
from sqlalchemy.orm import Session


def database_url_from_env() -> str:
    """EAP_DATABASE_URL: the eap_app Postgres database (required; there is no SQLite fallback)."""
    url = os.environ.get("EAP_DATABASE_URL")
    if not url:
        raise RuntimeError(
            "EAP_DATABASE_URL is not set; the API needs the eap_app Postgres database, e.g. "
            "EAP_DATABASE_URL=postgresql+psycopg://eap_app:eap_app_password@localhost:54329/eap_app"
        )
    return url


def get_engine(database_url: str | None = None):
    """Get the database engine (Postgres only: JSONB columns, runs and LISTEN/NOTIFY need it)."""
    url = database_url if database_url is not None else database_url_from_env()
    engine = create_engine(url)
    if engine.dialect.name != "postgresql":
        engine.dispose()
        raise RuntimeError(f"EAP_DATABASE_URL must point at Postgres, got a {engine.dialect.name!r} URL")
    return engine


def get_db(engine: create_engine) -> Generator[Session, None, None]:
    """Dependency for FastAPI routes to get a database session."""
    session = Session(engine)
    try:
        yield session
    finally:
        session.close()


def get_session(request: Request) -> Generator[Session, None, None]:
    """Create a new database session using the engine from app state."""
    engine = request.app.state.database_engine
    session = Session(engine)
    try:
        yield session
    finally:
        session.close()

