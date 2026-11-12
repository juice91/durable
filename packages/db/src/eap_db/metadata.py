"""The SQLAlchemy metadata Alembic compares against: every table in eap_app, from the modules that own it.

Table definitions stay where the code that uses them lives; this module only collects them.
"""

from __future__ import annotations

from sqlalchemy import MetaData


def target_metadata() -> list[MetaData]:
    from eap_api.models import Base as ApiBase  # agents, agent_versions
    import eap_api.mcp_servers.models  # noqa: F401 - mcp_servers, mcp_server_secrets (on ApiBase)
    from eap_run_store import Base as RunStoreBase  # runs, execution_events, side_effects
    from eap_worker.tickets import metadata as tickets_metadata  # fake_external_tickets, fake_external_ticket_calls

    return [ApiBase.metadata, RunStoreBase.metadata, tickets_metadata]


def schema_diff(connection) -> list:
    """Alembic autogenerate's differences between the connected schema and the models ([] = in sync).

    Same comparison options as env.py, so this equals what `alembic check` reports.
    """
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext

    ctx = MigrationContext.configure(connection, opts={"compare_type": True, "compare_server_default": True})
    return compare_metadata(ctx, target_metadata())
