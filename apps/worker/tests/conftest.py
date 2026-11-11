"""Test fixtures and configuration."""

import os
import re
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

# Read database URL from environment or use test default
DATABASE_URL = os.environ.get(
    "EAP_DATABASE_URL", 
    "postgresql+psycopg://eap_app:eap_app_password@localhost:54329/eap_app"
)


def _get_test_db_url():
    """Get the test database URL - use eap_app user with eap_app database and _test suffix."""
    return DATABASE_URL


@pytest.fixture(scope="module")
def engine():
    """Create a test database engine using the main eap_app database."""
    test_db_url = _get_test_db_url()
    eng = create_engine(test_db_url)
    
    from eap_worker.store import create_tables
    create_tables(eng)
    
    yield eng
    
    # Do NOT drop tables - tests must clean up their own data


@pytest.fixture
def test_tenant():
    """Generate a unique tenant_id for each test."""
    return f"test_tenant_{uuid4().hex[:8]}"


@pytest.fixture
def run_store(engine, test_tenant):
    """Create a RunStore for testing."""
    from eap_worker.store import RunStore
    return RunStore(engine)


@pytest.fixture
def event_log(engine):
    """Create an EventLog for testing."""
    from eap_worker.store import EventLog
    return EventLog(engine)


@pytest.fixture
def side_effect_store(engine, test_tenant):
    """Create a PostgresSideEffectStore for testing."""
    from eap_worker.store import PostgresSideEffectStore
    return PostgresSideEffectStore(engine, test_tenant)


def cleanup_run(engine, run_id):
    """Clean up a run and its events."""
    from eap_worker.store import ExecutionEventModel, RunModel
    with engine.begin() as conn:
        conn.execute(
            ExecutionEventModel.__table__.delete().where(ExecutionEventModel.run_id == str(run_id))
        )
        conn.execute(RunModel.__table__.delete().where(RunModel.id == str(run_id)))


def cleanup_side_effects(engine, tenant_id, key=None):
    """Clean up side effects."""
    from eap_worker.store import SideEffectModel
    with engine.begin() as conn:
        if key:
            conn.execute(
                SideEffectModel.__table__.delete().where(
                    SideEffectModel.key == key,
                    SideEffectModel.tenant_id == tenant_id
                )
            )
        else:
            conn.execute(
                SideEffectModel.__table__.delete().where(
                    SideEffectModel.tenant_id == tenant_id
                )
            )


def cleanup_side_effects_by_tenant(engine, tenant_id, key=None):
    """Clean up side effects by tenant."""
    from eap_worker.store import SideEffectModel
    with engine.begin() as conn:
        if key:
            conn.execute(
                SideEffectModel.__table__.delete().where(
                    SideEffectModel.key == key,
                    SideEffectModel.tenant_id == tenant_id
                )
            )
        else:
            conn.execute(
                SideEffectModel.__table__.delete().where(
                    SideEffectModel.tenant_id == tenant_id
                )
            )
