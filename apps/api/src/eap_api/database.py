"""Database configuration and utilities."""

import os
from typing import Generator

from fastapi import Request
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from eap_api.models import Base, create_all_tables

# Database URL from environment with default
DATABASE_URL = os.environ.get("EAP_DATABASE_URL", "sqlite:///./eap-dev.db")


def get_engine(database_url: str | None = None):
    """Get the database engine."""
    url = database_url if database_url is not None else DATABASE_URL
    return create_engine(url)


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


def init_database(engine: create_engine) -> None:
    """Initialize the database by creating all tables."""
    create_all_tables(engine)
