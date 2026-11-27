"""The SQLAlchemy metadata Alembic compares against: every table in eap_app, from the modules that own it.

Table definitions stay where the code that uses them lives; this module only collects them.
"""

from __future__ import annotations

from sqlalchemy import ForeignKeyConstraint, MetaData


def target_metadata() -> list[MetaData]:
    from eap_api.models import Base as ApiBase  # agents, agent_versions
    import eap_api.mcp_servers.models  # noqa: F401 - mcp_servers, mcp_server_secrets (on ApiBase)
    import eap_api.workflows.models  # noqa: F401 - workflows, workflow_versions (on ApiBase)
    from eap_run_store import Base as RunStoreBase  # runs, execution_events, side_effects
    from eap_worker.tickets import metadata as tickets_metadata  # fake_external_tickets, fake_external_ticket_calls

    _add_cross_metadata_keys(ApiBase.metadata, RunStoreBase.metadata)
    return [ApiBase.metadata, RunStoreBase.metadata, tickets_metadata]


def _add_cross_metadata_keys(api: MetaData, run_store: MetaData) -> None:
    """Foreign keys from a run-store table to an API table: declared here, where both are loaded, once."""
    exports = run_store.tables["workflow_exports"]
    name = "fk_workflow_exports_workflow"
    if any(c.name == name for c in exports.constraints):
        return
    workflows = api.tables["workflows"]
    exports.append_constraint(
        ForeignKeyConstraint(["tenant_id", "workflow_id"], [workflows.c.tenant_id, workflows.c.id], name=name)
    )


def schema_diff(connection) -> list:
    """Alembic autogenerate's differences between the connected schema and the models ([] = in sync).

    Same comparison options as env.py, so this equals what `alembic check` reports.
    """
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext

    ctx = MigrationContext.configure(connection, opts={"compare_type": True, "compare_server_default": True})
    return compare_metadata(ctx, target_metadata())
